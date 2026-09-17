import copy
import io
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import benchmark_executor_regression as benchmark
import execute_plan


def clean_metadata():
    return {'code_sha': 'a' * 40, 'code_root': str(ROOT), 'code_dirty': False}


def build_report(manifest, rows):
    report = benchmark.build_report(manifest, rows)
    report['reproducibility'] = clean_metadata()
    return report


def record(case='a', refresh='full', repetition=1, **changes):
    result = dict(case=case, movement_mode='step', execution_policy='legacy',
                  reachable_refresh_mode=refresh, repetition=repetition,
                  metrics_schema_version=2, evaluation_version='fixed_goals_v2', scheduler_version=2,
                  status='success', process_status='completed', execution_status='completed',
                  evaluation_status='valid', task_success=True, gcr=1.0, timed_out=False,
                  run_time_seconds=12, phase_durations_seconds={'execution': 10},
                  navigation_metrics={'requests': 1, 'successes': 1, 'failures': 0, 'action_counts': {}},
                  runtime_metrics={'counters': {}}, reproducibility=clean_metadata())
    result.update(changes)
    return result


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(self.stdout))
        stack.enter_context(redirect_stderr(self.stderr))

    def test_keeps_repetitions_failures_and_splits_versions(self):
        results = [record(), record(repetition=2, timed_out=True, evaluation_status='incomplete', gcr=None),
                   record(repetition=3, scheduler_version=1)]
        report = build_report({'cases': [{'task_id': 'a'}]}, results)
        self.assertEqual(report['results'], results)
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual(report['raw_counts']['timeouts'], 1)
        self.assertEqual(report['raw_counts']['incomplete_evaluations'], 1)
        self.assertTrue(benchmark.acceptance_failures(report))

    def test_pair_regression_not_hidden_by_mean(self):
        rows = [record('a'), record('b', gcr=0),
                record('a', 'event', gcr=0, task_success=False), record('b', 'event')]
        failures = benchmark.acceptance_failures(build_report({'cases': []}, rows))
        self.assertTrue(any(f['code'] == 'paired_regression' and f['case'] == 'a' and f['repetition'] == 1 for f in failures))

    def test_per_policy_performance_median_and_p95_gate(self):
        rows = [record(refresh=refresh, repetition=i, phase_durations_seconds={'execution': 10 if refresh == 'full' else 10.6})
                for refresh in ('full', 'event') for i in range(1, 6)]
        failures = benchmark.acceptance_failures(build_report({'cases': []}, rows))
        self.assertIn('execution_p50', {f['code'] for f in failures})
        self.assertIn('execution_p95', {f['code'] for f in failures})
        for row in rows:
            if row['reachable_refresh_mode'] == 'event': row['phase_durations_seconds']['execution'] = 10.5
        self.assertEqual(benchmark.acceptance_failures(build_report({'cases': []}, rows)), [])

    def test_missing_fixed_cases_nonzero_without_reselection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'cases': [{'task_id': 'missing', 'path': 'absent.py'}]}))
            output = root / 'output'
            with patch.object(benchmark, 'run_generated_executable') as runner:
                code = benchmark.main(['--manifest', str(manifest), '--output-dir', str(output), '--check'])
            self.assertNotEqual(code, 0)
            runner.assert_not_called()
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['case_count'], 1)
            self.assertEqual(report['missing_cases'][0]['task_id'], 'missing')

    def test_fake_runs_write_every_attempt_with_candidate_import_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'plan.py'
            executable.write_text('BUNDLE_DATA = {"task_plan": {}}\n')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'cases': [{'task_id': 'a', 'path': str(executable)}]}))
            calls = []
            def run(path, **kwargs):
                calls.append(kwargs)
                result = record(repetition=kwargs['attempt'])
                if kwargs['attempt'] == 2: result.update(timed_out=True, status='timeout', evaluation_status='incomplete', gcr=None)
                return result
            output = root / 'output'
            with patch.object(benchmark, 'run_generated_executable', side_effect=run):
                code = benchmark.main(['--manifest', str(manifest), '--output-dir', str(output), '--repetitions', '2', '--check'])
            self.assertNotEqual(code, 0)
            self.assertEqual([c['attempt'] for c in calls], [1, 2])
            self.assertEqual(len(list(output.glob('runs/**/result.json'))), 2)
            self.assertEqual(calls[0]['pythonpath_prepend'], [str(ROOT / 'scripts'), str(ROOT)])
            self.assertTrue(calls[0]['save_all_stdout'])
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(len(report['results']), 2)
            self.assertEqual(report['raw_counts']['timeouts'], 1)

    def test_benchmark_identity_uses_verifiable_compatibility_candidate(self):
        bundle = {'task_plan': {'task_id': 'fixture', 'stages': []}, 'gcr': []}
        with tempfile.TemporaryDirectory() as folder:
            command_dir = Path(folder)
            preferred = command_dir / 'plan_to_code' / 'executable_plan.py'
            preferred.parent.mkdir()
            preferred.write_text(
                "from executor_system.generated_plan_runtime import main as run_generated_plan\n"
                f"BUNDLE_DATA = {bundle!r}\nTASK_FILE = 'task.jsonl'\nTASK_INDEX = 0\n"
                "if __name__ == '__main__':\n    raise SystemExit(0)\n",
                encoding='utf-8',
            )
            fallback = command_dir / 'executable_plan.py'
            fallback.write_text(
                "from executor_system.generated_plan_runtime import main as run_generated_plan\n"
                f"BUNDLE_DATA = {bundle!r}\nTASK_FILE = 'task.jsonl'\nTASK_INDEX = 0\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__))\n",
                encoding='utf-8',
            )

            selected, rejected = execute_plan.find_generated_runtime(command_dir)

            self.assertEqual(selected, fallback)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(benchmark.plan_hash(selected), benchmark.content_hash(bundle))

    def test_event_without_full_and_missing_execution_timing_fail(self):
        for rows in ([record(refresh='event')], [record(), record(refresh='event', phase_durations_seconds={})]):
            self.assertTrue(benchmark.acceptance_failures(build_report({'cases': []}, rows)))

    def test_defaults(self):
        args = benchmark.parse_arguments(['--output-dir', '/tmp/unused'])
        self.assertEqual((args.repetitions, args.max_workers), (5, 1))
        self.assertEqual((args.movement_modes, args.execution_policies, args.reachable_refresh_modes), (['step'], ['legacy'], ['full']))

    def test_real_fake_subprocess_runs_candidate_code_and_preserves_raw_child(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'plan.py'
            executable.write_text('''import sys, os, json, time
from pathlib import Path
sys.path.append('/home/dwb/thor/Coder/scripts')
from executor_system import generated_plan_runtime as generated
from executor_system.runtime_metrics import runtime_metadata
from executor_system.run_results import ActionLedger
BUNDLE_DATA = {"task_plan": {}}
args = generated.parse_arguments()
result = generated.build_runner_result('success', time.monotonic())
result.update(generated.runner_identity(__file__, 0))
result.update(ActionLedger().freeze())
result['execution_quiescent'] = True
result['subgoal_results'] = [dict(subgoal_index=0, original_goal_index=0, status='satisfied')]
result['scheduler_version'] = 2
result.update(process_status='completed', execution_status='completed', evaluation_status='valid',
              gcr=1.0, tc=1.0, sr=1.0, ru=1.0, task_success=True, original_goal_count=1, atomic_goal_count=1, satisfied_goal_count=1,
              movement_mode=args.movement_mode, execution_policy=args.execution_policy,
              reachable_refresh_mode=args.reachable_refresh_mode, navigation_metrics={},
              runtime_metrics={'counters': {}}, phase_durations_seconds={'startup': 0, 'execution': 1, 'evaluation': 0, 'cleanup': 0})
result['reproducibility'] = runtime_metadata(Path(generated.__file__).resolve().parents[2], config={}, plan=BUNDLE_DATA)
Path(args.metrics_output).write_text(json.dumps(result))
''')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'cases': [{'task_id': 'a', 'path': str(executable)}]}))
            output = root / 'output'
            code = benchmark.main(['--manifest', str(manifest), '--output-dir', str(output), '--repetitions', '2', '--check'])
            report = json.loads((output / 'report.json').read_text())
            # Source edits while running tests must not be certified clean.
            expected_failure = report['reproducibility']['code_dirty'] is not False
            self.assertEqual(code, int(expected_failure), report['acceptance_failures'])
            self.assertTrue(all(f['code'] == 'invalid_code_identity' for f in report['acceptance_failures']))
            self.assertIn('check_passed', self.stdout.getvalue())
            self.assertEqual(report['raw_counts']['missing_results'], 0)
            self.assertEqual({r['reproducibility']['code_root'] for r in report['results']}, {str(ROOT)})
            self.assertEqual(len({r['child_result']['attempt'] for r in report['results']}), 2)

    def test_policy_means_cannot_mask_strict_regression(self):
        rows = [record(refresh=refresh, execution_policy=policy,
                       phase_durations_seconds={'execution': 10 if refresh == 'full' else (1 if policy == 'legacy' else 11)})
                for policy in ('legacy', 'strict') for refresh in ('full', 'event')]
        failures = benchmark.acceptance_failures(build_report({'cases': []}, rows))
        self.assertTrue(any(f['code'] == 'execution_p50' and f['execution_policy'] == 'strict' for f in failures))

    def test_safety_and_pair_missing_repetition_checks(self):
        rows = [record(repetition=1), record(repetition=2),
                record(refresh='event', navigation_metrics={'requests': 1, 'successes': 1, 'action_counts': {'Teleport': 1}},
                       runtime_metrics={'counters': {'lease_leaks': 1}})]
        codes = {f['code'] for f in benchmark.acceptance_failures(build_report({'cases': []}, rows))}
        self.assertTrue({'lease_leaks', 'step_navigation_teleports', 'missing_comparison_pair'} <= codes)

    def test_invalid_plan_still_has_one_failure_per_repetition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / 'plan.py'
            plan.write_text('this is not valid python !')
            manifest_path = root / 'manifest.json'
            manifest = {'cases': [{'task_id': 'a', 'path': str(plan)}]}
            manifest_path.write_text(json.dumps(manifest))
            with patch.object(benchmark, 'run_generated_executable') as runner:
                report = benchmark.run_benchmark(manifest, manifest_path=manifest_path, output_dir=root / 'out', repetitions=2)
            runner.assert_not_called()
            self.assertEqual(len(report['results']), 2)
            self.assertTrue(all(r['missing_result'] for r in report['results']))

    def test_partial_runs_still_expose_new_action_failure(self):
        full = record(task_success=False, execution_status='partial', actions=[
            {'action_key': '0:robot1:0', 'status': 'succeeded'},
            {'action_key': '0:robot1:1', 'status': 'failed'}])
        event = record(refresh='event', task_success=False, execution_status='partial', actions=[
            {'action_key': '0:robot1:0', 'status': 'failed'},
            {'action_key': '0:robot1:1', 'status': 'succeeded'}])
        failures = benchmark.acceptance_failures(build_report({'cases': []}, [full, event]))
        self.assertTrue(any(f['code'] == 'paired_action_regression' and f['action_key'] == '0:robot1:0' for f in failures))

    def test_missing_case_reports_unstarted_denominator(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = {'cases': [{'task_id': 'missing', 'path': 'absent.py'}]}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest))
            report = benchmark.run_benchmark(manifest, manifest_path=path, output_dir=root / 'out', repetitions=5)
            self.assertEqual(report['raw_counts']['missing_cases'], 1)
            self.assertEqual(report['raw_counts']['unstarted_runs'], 5)

    def test_output_directory_cannot_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'report.json').write_text('historical')
            code = benchmark.main(['--output-dir', str(root)])
            self.assertEqual(code, 2)
            self.assertIn('output directory must be new or empty', self.stderr.getvalue())
            self.assertEqual((root / 'report.json').read_text(), 'historical')


    def test_full_baseline_requires_parent_success_valid_evaluation_and_timing(self):
        for changes, expected in (
            ({'process_status': 'failed'}, 'baseline_process_failed'),
            ({'evaluation_status': 'incomplete'}, 'baseline_evaluation_invalid'),
            ({'evaluation_status': 'invalid'}, 'baseline_evaluation_invalid'),
            ({'gcr': None}, 'baseline_evaluation_invalid'),
            ({'phase_durations_seconds': {}}, 'baseline_execution_timing_invalid'),
        ):
            with self.subTest(changes=changes):
                failures = benchmark.acceptance_failures(build_report({'cases': []}, [record(**changes)]))
                self.assertIn(expected, {f['code'] for f in failures})
        for value in (None, -1, float('nan'), float('inf'), '10', True):
            with self.subTest(execution=value):
                failures = benchmark.acceptance_failures(build_report({'cases': []}, [record(phase_durations_seconds={'execution': value})]))
                self.assertIn('baseline_execution_timing_invalid', {f['code'] for f in failures})

    def test_valid_task_failure_is_still_baseline_evidence(self):
        for status in ('partial', 'failed'):
            row = record(execution_status=status, task_success=False, gcr=0)
            self.assertEqual(benchmark.acceptance_failures(build_report({'cases': []}, [row])), [])

    def test_paired_comparison_also_requires_a_usable_full_baseline(self):
        rows = [record(process_status='failed', evaluation_status='incomplete', gcr=None), record(refresh='event')]
        codes = {f['code'] for f in benchmark.acceptance_failures(build_report({'cases': []}, rows))}
        self.assertIn('baseline_process_failed', codes)
        self.assertIn('baseline_evaluation_invalid', codes)

    def test_parent_and_child_require_clean_known_code_identity(self):
        invalid = ({'code_dirty': True}, {'code_dirty': None}, {'code_dirty': 0},
                   {'code_dirty': 'false'}, {'code_sha': None}, {'code_sha': ''},
                   {'code_sha': 'unknown'}, {'code_root': ''}, {'code_root': None},
                   {'code_root': 'unknown'})
        for owner in ('parent', 'child'):
            for changes in invalid:
                with self.subTest(owner=owner, changes=changes):
                    report = build_report({'cases': []}, [record()])
                    metadata = report['reproducibility'] if owner == 'parent' else report['results'][0]['reproducibility']
                    metadata.update(changes)
                    failures = benchmark.acceptance_failures(report)
                    self.assertTrue(any(f['code'] == 'invalid_code_identity' and f['owner'] == owner for f in failures))
            for field in ('code_sha', 'code_root', 'code_dirty'):
                report = build_report({'cases': []}, [record()])
                metadata = report['reproducibility'] if owner == 'parent' else report['results'][0]['reproducibility']
                metadata.pop(field)
                self.assertTrue(benchmark.acceptance_failures(report))

    def test_dirty_event_and_different_parent_root_cannot_pass_same_sha(self):
        event = record(refresh='event')
        event['reproducibility']['code_dirty'] = True
        self.assertTrue(benchmark.acceptance_failures(build_report({'cases': []}, [record(), event])))
        report = build_report({'cases': []}, [record()])
        report['reproducibility']['code_root'] = '/different/checkout'
        self.assertTrue(benchmark.acceptance_failures(report))
