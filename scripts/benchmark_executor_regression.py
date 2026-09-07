#!/usr/bin/env python3
"""Fixed-manifest repeated executor measurements, preserving every outcome.

Each job starts a fresh child process/controller. Categories overlap; percentiles
use per-run execution phase wall times, never sums of runtime category timers.
--check covers recorded run evidence and pairwise regressions. Exit zero does
not certify collision equivalence or external cancellation/protocol tests;
those hard acceptance gates remain explicit external validation requirements.
"""
from __future__ import annotations

import argparse
import ast
import itertools
import json
import math
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from executor_system.parallel_runner import (
    run_generated_executable, effective_timeout_seconds,
    DEFAULT_STARTUP_GRACE_SECONDS, DEFAULT_FINALIZATION_GRACE_SECONDS,
    DEFAULT_TERMINATION_GRACE_SECONDS,
)
from executor_system.run_results import atomic_write_json
from executor_system.runtime_metrics import content_hash, file_hash, runtime_metadata

PROTOCOL_FIELDS = ('metrics_schema_version', 'evaluation_version', 'scheduler_version')
GROUP_FIELDS = ('movement_mode', 'execution_policy', 'reachable_refresh_mode') + PROTOCOL_FIELDS


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def execution_seconds(row):
    value = (row.get('phase_durations_seconds') or {}).get('execution')
    return value if finite_number(value) and value >= 0 else None


def navigation_rate(row):
    metrics = row.get('navigation_metrics') or {}
    requests = metrics.get('requests', 0)
    successes = metrics.get('successes', 0)
    return successes / requests if requests else 1.0


def aggregate(rows):
    valid = [r for r in rows if r.get('evaluation_status') == 'valid' and finite_number(r.get('gcr'))]
    times = [execution_seconds(r) for r in rows if execution_seconds(r) is not None]
    nav_requests = sum((r.get('navigation_metrics') or {}).get('requests', 0) for r in rows)
    nav_successes = sum((r.get('navigation_metrics') or {}).get('successes', 0) for r in rows)
    return {
        'run_count': len(rows), 'valid_evaluation_count': len(valid),
        'timeout_count': sum(bool(r.get('timed_out')) for r in rows),
        'missing_result_count': sum(bool(r.get('missing_result')) for r in rows),
        'incomplete_evaluation_count': len(rows) - len(valid),
        'process_failure_count': sum(r.get('process_status') != 'completed' for r in rows),
        'mean_gcr': statistics.mean(r['gcr'] for r in valid) if valid else None,
        'navigation_completion_rate': nav_successes / nav_requests if nav_requests else 1.0,
        'execution_timing_count': len(times),
        'execution_p50_seconds': statistics.median(times) if times else None,
        'execution_p95_seconds': percentile(times, .95),
    }


def build_report(manifest, results):
    grouped = {}
    for row in results:
        key = tuple(row.get(field) for field in GROUP_FIELDS)
        grouped.setdefault(key, []).append(row)
    groups = [dict(zip(GROUP_FIELDS, key), aggregate=aggregate(rows))
              for key, rows in sorted(grouped.items(), key=lambda item: repr(item[0]))]
    return {
        'benchmark_schema_version': 1, 'case_count': len(manifest.get('cases', [])),
        'results': list(results), 'groups': groups,
        'raw_counts': {
            'results': len(results),
            'timeouts': sum(bool(r.get('timed_out')) for r in results),
            'missing_results': sum(bool(r.get('missing_result')) for r in results),
            'incomplete_evaluations': sum(r.get('evaluation_status') != 'valid' for r in results),
        },
        'timing_semantics': 'Category timers overlap. P50 is the median and P95 uses nearest rank over raw execution phase durations.',
        'external_validation': {'collision_equivalence': 'pending external deterministic/real review',
                                'cancellation_result_protocol_tests': 'run separately; not inferred from averages'},
    }


def acceptance_failures(report):
    failures = []
    def reject(code, **details):
        failures.append(dict(code=code, **details))
    if report.get('missing_cases'):
        reject('missing_fixed_cases', cases=report['missing_cases'])
    if report.get('expected_run_count') is not None and len(report['results']) != report['expected_run_count']:
        reject('missing_runs', expected=report['expected_run_count'], actual=len(report['results']))
    rows = report.get('results', [])
    if not rows:
        reject('no_results')
    pairs = {}
    for row in rows:
        identity = {key: row.get(key) for key in ('case', 'movement_mode', 'execution_policy', 'repetition')}
        key = tuple(identity.values())
        refresh = row.get('reachable_refresh_mode')
        pair = pairs.setdefault(key, {})
        if refresh in pair:
            reject('duplicate_run', **identity, reachable_refresh_mode=refresh)
        pair[refresh] = row
        if tuple(row.get(f) for f in PROTOCOL_FIELDS) != (2, 'fixed_goals_v2', 2):
            reject('protocol_version', **identity, actual={f: row.get(f) for f in PROTOCOL_FIELDS})
        if row.get('missing_result'):
            reject('missing_result', **identity)
        if row.get('reproducibility_mismatch'):
            reject('reproducibility_mismatch', **identity, details=row['reproducibility_mismatch'])
        metrics = row.get('runtime_metrics') or {}
        counters = metrics.get('counters') or {}
        for counter in ('collisions', 'lease_leaks', 'wait_deadlocks'):
            if counters.get(counter, 0) or row.get(counter, 0):
                reject(counter, **identity)
        if row.get('movement_mode') == 'step' and ((row.get('navigation_metrics') or {}).get('action_counts') or {}).get('Teleport', 0):
            reject('step_navigation_teleports', **identity)
    has_event = any(r.get('reachable_refresh_mode') == 'event' for r in rows)
    for key, pair in pairs.items():
        identity = dict(zip(('case', 'movement_mode', 'execution_policy', 'repetition'), key))
        if has_event and set(pair) != {'full', 'event'}:
            reject('missing_comparison_pair', **identity)
            continue
        if not has_event:
            row = pair.get('full', {})
            if row.get('timed_out'):
                reject('baseline_timeout', **identity)
            continue
        full, event = pair['full'], pair['event']
        regression = []
        if full.get('evaluation_status') == 'valid' and event.get('evaluation_status') != 'valid': regression.append('evaluation')
        if not full.get('timed_out') and event.get('timed_out'): regression.append('timeout')
        if full.get('task_success') is True and event.get('task_success') is not True: regression.append('task_success')
        if full.get('process_status') == 'completed' and event.get('process_status') != 'completed': regression.append('process_status')
        if full.get('execution_status') == 'completed' and event.get('execution_status') != 'completed': regression.append('execution_status')
        if finite_number(full.get('gcr')) and finite_number(event.get('gcr')) and event['gcr'] < full['gcr']: regression.append('gcr')
        if navigation_rate(event) < navigation_rate(full): regression.append('navigation')
        if regression: reject('paired_regression', **identity, fields=regression)
        full_actions = {a['action_key']: a for a in full.get('actions', []) if a.get('action_key')}
        event_actions = {a['action_key']: a for a in event.get('actions', []) if a.get('action_key')}
        for action_key, action in full_actions.items():
            changed = event_actions.get(action_key, {})
            if action.get('status') == 'succeeded' and changed.get('status') != 'succeeded':
                reject('paired_action_regression', **identity, action_key=action_key,
                       full=action, event=changed)

        if tuple(full.get(f) for f in PROTOCOL_FIELDS) != tuple(event.get(f) for f in PROTOCOL_FIELDS):
            reject('comparison_protocol_mismatch', **identity)
        if (full.get('reproducibility') or {}).get('code_sha') != (event.get('reproducibility') or {}).get('code_sha'):
            reject('comparison_code_mismatch', **identity)
    # Only compare groups with identical movement, policy and all three versions.
    groups = {tuple(g.get(f) for f in GROUP_FIELDS): g['aggregate'] for g in report.get('groups', [])}
    for key, event in groups.items():
        if key[2] != 'event': continue
        full_key = key[:2] + ('full',) + key[3:]
        full = groups.get(full_key)
        if full is None: continue
        identity = dict(zip(GROUP_FIELDS, key))
        for field, lower_is_better in (('valid_evaluation_count', False), ('timeout_count', True),
                                       ('mean_gcr', False), ('navigation_completion_rate', False)):
            a, b = event[field], full[field]
            if a is None or b is None or (a > b if lower_is_better else a < b):
                reject('aggregate_' + field, **identity, full=b, event=a)
        if event['execution_timing_count'] != event['run_count'] or full['execution_timing_count'] != full['run_count']:
            reject('missing_execution_timing', **identity)
        for metric in ('execution_p50', 'execution_p95'):
            a, b = event[metric + '_seconds'], full[metric + '_seconds']
            if a is None or b is None or a > b * 1.05 + 1e-12:
                reject(metric, **identity, full=b, event=a, maximum_ratio=1.05)
    return failures


def write_json(path, value):
    atomic_write_json(path, value)


def plan_hash(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BUNDLE_DATA' for t in node.targets):
            return content_hash(ast.literal_eval(node.value))
    return None


def run_benchmark(manifest, *, manifest_path, output_dir, repetitions=5, movement_modes=('step',),
                  execution_policies=('legacy',), reachable_refresh_modes=('full',), max_workers=1,
                  repo_root=ROOT):
    cases = manifest.get('cases', [])
    missing = [case for case in cases if not (repo_root / case['path']).is_file()]
    expected = len(cases) * repetitions * len(movement_modes) * len(execution_policies) * len(reachable_refresh_modes)
    config = dict(repetitions=repetitions, movement_modes=list(movement_modes), execution_policies=list(execution_policies),
                  reachable_refresh_modes=list(reachable_refresh_modes), max_workers=max_workers, seed=0,
                  startup_grace_seconds=DEFAULT_STARTUP_GRACE_SECONDS, finalization_grace_seconds=DEFAULT_FINALIZATION_GRACE_SECONDS,
                  termination_grace_seconds=DEFAULT_TERMINATION_GRACE_SECONDS,
                  execution_timeouts={m: effective_timeout_seconds(m, None) for m in movement_modes})
    metadata = runtime_metadata(repo_root, config=config)
    metadata['manifest_sha256'] = file_hash(manifest_path)
    metadata['manifest_path'] = str(manifest_path.resolve())
    write_json(output_dir / 'run_config.json', metadata)
    # No smaller denominator and no replacement selection if any fixed input is absent.
    if missing:
        report = build_report(manifest, [])
        report.update(missing_cases=missing, expected_run_count=expected, reproducibility=metadata)
        report['raw_counts'].update(missing_cases=len(missing), unstarted_runs=expected)
        return report
    run_id = uuid.uuid4().hex
    jobs = list(itertools.product(enumerate(cases), movement_modes, execution_policies, reachable_refresh_modes, range(1, repetitions + 1)))
    def run(job):
        (index, case), mode, policy, refresh, repetition = job
        executable = (repo_root / case['path']).resolve()
        run_dir = output_dir / 'runs' / f'case-{index + 1:02d}' / mode / policy / refresh / f'repetition-{repetition:02d}'
        run_dir.mkdir(parents=True)
        requested = dict(case=case.get('task_id', case['path']), case_index=index + 1,
                         movement_mode=mode, execution_policy=policy, reachable_refresh_mode=refresh, repetition=repetition,
                         executable_sha256=None, plan_content_sha256=None,
                         manifest_sha256=metadata['manifest_sha256'], robot_count=case.get('robot_count'), seed=0)
        try:
            requested.update(executable_sha256=file_hash(executable), plan_content_sha256=plan_hash(executable))
            write_json(run_dir / 'request.json', requested)
            result = run_generated_executable(executable, metrics_output=run_dir / 'child_metrics.json',
                      timeout_seconds=effective_timeout_seconds(mode, None), movement_mode=mode, execution_policy=policy,
                      reachable_refresh_mode=refresh, attempt=repetition, run_id=run_id, save_all_stdout=True,
                      pythonpath_prepend=[str(repo_root / 'scripts'), str(repo_root)])
        except Exception as exc:
            result = dict(status='failed', process_status='failed', execution_status='failed',
                          evaluation_status='incomplete', task_success=None, timed_out=False, error=str(exc), missing_result=True)
        write_json(run_dir / 'request.json', requested)
        result = dict(result)
        # Keep unmodified child result alongside requested dimensions in result.json.
        result['child_result'] = dict(result)
        mismatches = {}
        for field in ('movement_mode', 'execution_policy', 'reachable_refresh_mode'):
            if field in result and result[field] != requested[field]: mismatches[field] = result[field]
        child_metadata = result.get('reproducibility') or {}
        if child_metadata.get('code_sha') != metadata['code_sha']:
            mismatches['code_sha'] = child_metadata.get('code_sha')
        if child_metadata.get('code_root') != str(repo_root.resolve()):
            mismatches['code_root'] = child_metadata.get('code_root')
        if child_metadata.get('plan_content_sha256') != requested['plan_content_sha256']:
            mismatches['plan_content_sha256'] = child_metadata.get('plan_content_sha256')
        if mismatches: result['reproducibility_mismatch'] = mismatches
        if not (run_dir / 'child_metrics.json').is_file(): result['missing_result'] = True
        result.update(requested)
        write_json(run_dir / 'result.json', result)
        return result
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda r: (r['case_index'], r['movement_mode'], r['execution_policy'], r['reachable_refresh_mode'], r['repetition']))
    report = build_report(manifest, results)
    report['raw_counts'].update(missing_cases=0, unstarted_runs=expected - len(results))
    wall = time.monotonic() - started
    report.update(expected_run_count=expected, missing_cases=[], reproducibility=metadata,
                  benchmark_wall_seconds=wall, throughput_runs_per_second=len(results) / wall if wall else None)
    return report


def positive_integer(value):
    result = int(value)
    if result < 1: raise argparse.ArgumentTypeError('must be >= 1')
    return result


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'tests/fixtures/movement_benchmark_plans.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--repetitions', type=positive_integer, default=5)
    parser.add_argument('--movement-modes', nargs='+', choices=('step', 'teleport'), default=['step'])
    parser.add_argument('--execution-policies', nargs='+', choices=('legacy', 'strict'), default=['legacy'])
    parser.add_argument('--reachable-refresh-modes', nargs='+', choices=('full', 'event'), default=['full'])
    parser.add_argument('--max-workers', type=positive_integer, default=1)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    for field in ('movement_modes', 'execution_policies', 'reachable_refresh_modes'):
        if len(getattr(args, field)) != len(set(getattr(args, field))): parser.error('duplicate ' + field)
    return args


def main(argv=None):
    args = parse_arguments(argv)
    output = args.output_dir.resolve()
    # A new output directory is mandatory: never overwrite historical evidence.
    if output.exists() and any(output.iterdir()):
        print('output directory must be new or empty', file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads(args.manifest.read_text())
        if not isinstance(manifest, dict) or not isinstance(manifest.get('cases'), list) or not manifest['cases']:
            raise ValueError('manifest must contain a nonempty fixed cases list')
        if any(not isinstance(case, dict) or not isinstance(case.get('path'), str) for case in manifest['cases']):
            raise ValueError('each fixed case must contain a path')
        report = run_benchmark(manifest, manifest_path=args.manifest, output_dir=output,
                 repetitions=args.repetitions, movement_modes=args.movement_modes, execution_policies=args.execution_policies,
                 reachable_refresh_modes=args.reachable_refresh_modes, max_workers=args.max_workers)
    except (OSError, ValueError, SyntaxError) as exc:
        write_json(output / 'report.json', {'error': str(exc), 'acceptance_failures': [{'code': 'invalid_input'}]})
        print(str(exc), file=sys.stderr)
        return 2
    report['acceptance_failures'] = acceptance_failures(report)
    report['check_passed'] = not report['acceptance_failures']
    write_json(output / 'report.json', report)
    print(json.dumps({'report': str(output / 'report.json'), 'raw_counts': report['raw_counts'],
                      'missing_cases': report['missing_cases'], 'check_passed': report['check_passed']}))
    return 1 if report['missing_cases'] or (args.check and report['acceptance_failures']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
