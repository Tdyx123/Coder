"""Fixed 48-case acceptance driver; owns only processes it starts."""
import argparse
import ast
from dataclasses import asdict
import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('code', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    code, source, output = (p.resolve() for p in (args.code, args.source, args.output))
    os.chdir(code)
    sys.path[:0] = [str(code / 'scripts'), str(code)]
    from executor_system.process_supervisor import OwnedProcessScope, run_owned_process
    from executor_system.run_results import atomic_write_json, task_key_for_executable, validate_result
    cases = json.loads((source / 'tests/fixtures/movement_benchmark_plans.json').read_text())['cases']
    if len(cases) != 12:
        raise ValueError('Fixed acceptance requires 12 manifest cases')
    if args.limit is not None and not 1 <= args.limit <= 48:
        raise ValueError('--limit must be between 1 and 48')
    prepared = []
    for case in cases:
        script = source / case['path']
        module = ast.parse(script.read_text())
        bundle = next(ast.literal_eval(node.value) for node in module.body
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BUNDLE_DATA' for t in node.targets))
        prepared.append((case, script, len(bundle['gcr'])))
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'report.json'
    if report_path.exists():
        raise RuntimeError('Use a new output directory to preserve prior evidence')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    report = {'code_sha': sha, 'code_root': str(code), 'source_root': str(source),
              'run_id': str(uuid.uuid4()), 'status': 'in_progress', 'expected_runs': 48, 'results': [],
              'group_run_ids': {f'{policy}_{mode}': str(uuid.uuid4()) for policy in ('legacy','strict') for mode in ('teleport','step')}}
    scope = OwnedProcessScope()
    atomic_write_json(report_path, report)
    try:
        for index, (case, script, goals) in enumerate(prepared):
            for policy in ('legacy', 'strict'):
                for mode, budget in (('teleport', 30), ('step', 120)):
                    if args.limit is not None and len(report['results']) >= args.limit:
                        report['status'] = 'smoke_completed'
                        return
                    attempt = output / f'{index:02d}_{policy}_{mode}'
                    attempt.mkdir()
                    metrics_path = attempt / 'metrics.json'
                    identity = {'run_id': report['group_run_ids'][f'{policy}_{mode}'], 'task_key': task_key_for_executable(script), 'attempt': 1}
                    env = os.environ.copy()
                    env.update(PYTHONPATH=os.pathsep.join((str(code/'scripts'), str(code))), PYTHONDONTWRITEBYTECODE='1',
                               LAMMAP_RUN_ID=identity['run_id'], LAMMAP_TASK_KEY=identity['task_key'], LAMMAP_ATTEMPT='1')
                    command = [sys.executable, str(script), '--runner-mode', '--metrics-output', str(metrics_path),
                               '--movement-mode', mode, '--execution-policy', policy, '--timeout-seconds', str(budget)]
                    outcome = run_owned_process(command, timeout_seconds=budget+70, termination_grace_seconds=5,
                                                stdout_path=attempt/'stdout.log', stderr_path=attempt/'stderr.log',
                                                env=env, process_scope=scope)
                    row = {'case': case, 'movement_mode': mode, 'execution_policy': policy, 'expected_goal_count': goals,
                           'source_sha256': sha256(script.read_bytes()).hexdigest(),
                           'directory': str(attempt), 'process': asdict(outcome), 'metrics_path': str(metrics_path)}
                    try:
                        metrics = json.loads(metrics_path.read_text())
                        row['metrics'] = metrics
                        checked = validate_result(metrics, returncode=124 if outcome.timed_out else outcome.returncode, expected_identity=identity)
                        if metrics.get('execution_policy') != policy or metrics.get('scheduler_version') != 2:
                            raise ValueError('Child did not execute requested scheduler2 policy')
                        row['validated_metrics'] = checked
                        row['contract_valid'] = True
                        row['fixed_denominator'] = metrics.get('original_goal_count') == goals
                    except Exception as exc:
                        row['contract_valid'] = False
                        row['validation_error'] = f'{type(exc).__name__}: {exc}'
                    report['results'].append(row)
                    atomic_write_json(report_path, report)
                    print(index, policy, mode, outcome.returncode, row.get('metrics',{}).get('execution_status'),
                          'contract='+str(row['contract_valid']), flush=True)
        report['status'] = 'completed'
        report['contract_pass'] = all(r['contract_valid'] and r.get('fixed_denominator') for r in report['results'])
    except BaseException:
        report['status'] = 'failed'
        raise
    finally:
        report['scope_cleanup'] = scope.cleanup(termination_grace_seconds=5, reason='acceptance-driver-exit')
        atomic_write_json(report_path, report)


if __name__ == '__main__':
    main()
