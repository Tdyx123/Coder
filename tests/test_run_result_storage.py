import json
import math
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system import parallel_runner
from executor_system.run_results import RunResultStore, atomic_write_json, normalize_output, task_key_for_executable, validate_result
from tests.test_parallel_runner import write_fake_generated_script


def complete_result(run_id, task_key, attempt=1, **changes):
    result = dict(run_id=run_id, task_key=task_key, attempt=attempt,
                  metrics_schema_version=2, evaluation_version='fixed_goals_v2',
                  execution_policy='legacy', process_status='completed', execution_status='completed',
                  evaluation_status='valid', task_success=True, sr=1, gcr=1.0, tc=1.0, ru=1.0,
                  original_goal_count=1, satisfied_goal_count=1, returncode=0,
                  action_counts=dict(planned=0, started=0, succeeded=0, failed=0,
                                     skipped=0, cancelled=0, unexecuted=0, attempts=0),
                  raw_action_sr=None, ignored_failure_count=0)
    result.update(changes)
    return result


class RunResultStorageTest(unittest.TestCase):
    def test_timeout_bytes_are_json_serializable(self):
        result = {'stderr': normalize_output(b'timeout\xff')}
        self.assertIsInstance(json.dumps(result, ensure_ascii=False), str)
        self.assertIn('timeout', result['stderr'])

    def test_atomic_write_failure_preserves_previous_json_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'result.json'
            atomic_write_json(target, {'old': True})
            with mock.patch('executor_system.run_results.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    atomic_write_json(target, {'new': True})
            self.assertEqual(json.loads(target.read_text()), {'old': True})
            self.assertEqual(list(Path(temp).iterdir()), [target])

    def test_atomic_write_fsync_failure_preserves_previous_json(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'result.json'
            atomic_write_json(target, {'old': True})
            with mock.patch('executor_system.run_results.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    atomic_write_json(target, {'new': True})
            self.assertEqual(json.loads(target.read_text()), {'old': True})
            self.assertEqual(list(Path(temp).iterdir()), [target])

    def test_atomic_write_rejects_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'result.json'
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    atomic_write_json(target, {'metric': value})
            self.assertFalse(target.exists())

    def test_summary_reservations_are_unique_and_immediately_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            with ThreadPoolExecutor(max_workers=8) as pool:
                paths = list(pool.map(lambda _: parallel_runner.summary_output_path(Path(temp)), range(16)))
            self.assertEqual(len(set(paths)), 16)
            for path in paths:
                self.assertEqual(json.loads(path.read_text())['run_status'], 'in_progress')

    def test_layout_and_second_attempt_keep_first_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = uuid.uuid4().hex
            store = RunResultStore(root, run_id)
            first = task_key_for_executable(root / 'a' / 'plan.py')
            second = task_key_for_executable(root / 'b' / 'plan.py')
            path = store.record_attempt(first, 1, complete_result(run_id, first))
            second_path = store.record_attempt(second, 1, complete_result(run_id, second))
            store.record_attempt(first, 2, complete_result(run_id, first, 2, returncode=1,
                process_status='failed', execution_status='failed', evaluation_status='incomplete',
                task_success=None, sr=None, gcr=None, tc=None, ru=None, satisfied_goal_count=None))
            self.assertEqual(path, root / 'runs' / run_id / first / 'attempt_1' / 'result.json')
            self.assertNotEqual(path, second_path)
            self.assertTrue(json.loads(path.read_text())['task_success'])
            summary = store.rebuild_summary()
            self.assertEqual(summary['total_results'], 2)
            self.assertEqual(next(r for r in summary['results'] if r['task_key'] == first)['attempt'], 2)

    def test_rebuild_marks_incomplete_attempt_without_overwriting_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = uuid.uuid4().hex
            key = task_key_for_executable(root / 'plan.py')
            store = RunResultStore(root, run_id)
            store.record_attempt(key, 1, complete_result(run_id, key))
            directory = root / 'runs' / run_id / key / 'attempt_2'
            directory.mkdir(parents=True)
            log = directory / 'stdout.log'
            log.write_bytes(b'crash evidence\xff')
            (directory / 'child_metrics.json').write_text(json.dumps(complete_result(run_id, key, 2)))
            summary = store.rebuild_summary()
            self.assertEqual(summary['results'][0]['attempt'], 1)
            self.assertEqual(summary['interrupted_attempts'][0]['status'], 'interrupted')
            self.assertEqual(log.read_bytes(), b'crash evidence\xff')
            self.assertEqual(parallel_runner.main(['--rebuild-summary', str(store.run_dir)]), 0)
            self.assertEqual(log.read_bytes(), b'crash evidence\xff')

    def test_validation_rejects_nonfinite_inconsistent_and_malformed_v2(self):
        run_id = uuid.uuid4().hex
        key = task_key_for_executable(Path('plan.py'))
        for changes in ({'gcr': math.nan}, {'task_success': False}, {'sr': 'yes'},
                        {'evaluation_status': 'valid', 'sr': None}, {'execution_status': 'nonsense'},
                        {'metrics_schema_version': 3}, {'metrics_schema_version': 2.0},
                        {'satisfied_goal_count': 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(complete_result(run_id, key, **changes), returncode=0, expected_identity={})

    def test_unversioned_cannot_claim_fixed_v2(self):
        result = validate_result({'sr': 1, 'evaluation_version': 'fixed_goals_v2'}, returncode=0, expected_identity={})
        self.assertEqual(result['evaluation_version'], 'legacy_v1')

    def test_cli_stores_logs_at_attempt_and_prunes_only_successful_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script)
            output = root / 'output'
            self.assertEqual(parallel_runner.main([str(script), '--output-dir', str(output)]), 0)
            summary = json.loads(next(output.glob('*.json')).read_text())
            result = summary['results'][0]
            attempt = output / 'runs' / summary['run_id'] / result['task_key'] / 'attempt_1'
            self.assertTrue((attempt / 'result.json').is_file())
            self.assertTrue((attempt / 'stderr.log').is_file())
            self.assertFalse((attempt / 'stdout.log').exists())
            self.assertNotIn('stdout_path', result)
            self.assertEqual(summary['run_status'], 'completed')

    def test_disk_write_failure_makes_cli_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script)
            with mock.patch.object(RunResultStore, 'record_attempt', side_effect=OSError('disk full')):
                try:
                    code = parallel_runner.main([str(script), '--output-dir', str(root / 'output')])
                except KeyboardInterrupt:
                    self.fail('disk failure was hidden by KeyboardInterrupt')
                self.assertEqual(code, 1)
            summary = json.loads(next((root / 'output').glob('*.json')).read_text())
            self.assertEqual(summary['run_status'], 'failed')
            self.assertIn('disk full', summary['storage_error'])

    def test_completed_attempt_is_persisted_before_other_future_and_survives_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / 'first.py', root / 'second.py'
            first.touch()
            second.touch()
            output = root / 'output'
            first_recorded = threading.Event()
            original_record = RunResultStore.record_attempt
            def record(store, *args):
                value = original_record(store, *args)
                first_recorded.set()
                return value
            def run(path, **kwargs):
                if path == second:
                    self.assertTrue(first_recorded.wait(3))
                return complete_result(kwargs['run_id'], kwargs['task_key'], kwargs['attempt'])
            def interrupt(futures):
                self.assertTrue(first_recorded.wait(3))
                snapshot = json.loads(next(output.glob('*.json')).read_text())
                self.assertEqual(snapshot['run_status'], 'in_progress')
                self.assertGreaterEqual(snapshot['total_results'], 1)
                raise KeyboardInterrupt()
                yield
            with mock.patch.object(RunResultStore, 'record_attempt', new=record), mock.patch.object(
                parallel_runner, 'run_generated_executable', side_effect=run), mock.patch.object(
                parallel_runner, 'as_completed', side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    parallel_runner.main([str(first), str(second), '--max-workers', '2', '--output-dir', str(output)])
            summary = json.loads(next(output.glob('*.json')).read_text())
            self.assertEqual(summary['run_status'], 'interrupted')
            self.assertGreaterEqual(summary['total_results'], 1)
            store = RunResultStore(output, summary['run_id'])
            self.assertGreaterEqual(store.rebuild_summary()['total_results'], 1)

    def test_rebuild_rejects_malformed_completed_attempt_and_keeps_earlier_one(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RunResultStore(Path(temp), uuid.uuid4().hex)
            key = task_key_for_executable(Path(temp) / 'plan.py')
            store.record_attempt(key, 1, complete_result(store.run_id, key))
            for attempt, changes in enumerate(({'task_success': False}, {'gcr': 0.5},
                    {'tc': 0}, {'attempt': True}, {'run_id': 'foreign'}), start=2):
                directory = store.start_attempt(key, attempt)
                value = complete_result(store.run_id, key, attempt, storage_status='completed')
                value.update(changes)
                atomic_write_json(directory / 'result.json', value)
            summary = store.rebuild_summary()
            self.assertEqual(summary['results'][0]['attempt'], 1)
            self.assertEqual(len(summary['interrupted_attempts']), 5)

    def test_failure_and_timeout_logs_are_retained_for_every_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            script.write_text("import sys, time\nprint('diagnostic stdout', flush=True)\n"
                              "print('diagnostic stderr', file=sys.stderr, flush=True)\ntime.sleep(10)\n")
            output = root / 'output'
            self.assertEqual(parallel_runner.main([str(script), '--output-dir', str(output),
                '--timeout-seconds', '0.08', '--startup-grace-seconds', '0.01',
                '--finalization-grace-seconds', '0.01', '--termination-grace-seconds', '0.05']), 1)
            attempts = list(output.glob('runs/*/*/attempt_*'))
            self.assertEqual(len(attempts), 3)
            for attempt in attempts:
                result = json.loads((attempt / 'result.json').read_text())
                self.assertEqual(result['process_status'], 'timeout')
                self.assertIn('diagnostic stdout', (attempt / 'stdout.log').read_text())
                self.assertIn('diagnostic stderr', (attempt / 'stderr.log').read_text())
                self.assertIn('diagnostic stdout', result['stdout'])
            script.write_text("import sys\nprint('failed stdout')\nprint('failed stderr', file=sys.stderr)\nsys.exit(7)\n")
            self.assertEqual(parallel_runner.main([str(script), '--output-dir', str(output)]), 1)
            failure = next(p for p in output.glob('runs/*/*/attempt_1/result.json')
                           if json.loads(p.read_text())['returncode'] == 7)
            self.assertEqual((failure.parent / 'stdout.log').read_text(), 'failed stdout\n')
            self.assertEqual((failure.parent / 'stderr.log').read_text(), 'failed stderr\n')

    def test_save_all_stdout_retains_success_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script)
            output = root / 'output'
            self.assertEqual(parallel_runner.main([str(script), '--output-dir', str(output), '--save-all-stdout']), 0)
            result = json.loads(next(output.glob('runs/*/*/attempt_1/result.json')).read_text())
            self.assertTrue(Path(result['stdout_path']).is_file())

    def test_store_normalizes_byte_logs_and_syncs_them_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RunResultStore(root, uuid.uuid4().hex)
            key = task_key_for_executable(root / 'plan.py')
            path = store.record_attempt(key, 1, complete_result(store.run_id, key, stdout=b'output\xff', stderr=b'error\xff'))
            result = json.loads(path.read_text())
            self.assertIn('output', result['stdout'])
            self.assertEqual((path.parent / 'stdout.log').read_text(), 'output\ufffd')
            self.assertEqual((path.parent / 'stderr.log').read_text(), 'error\ufffd')

    def test_summary_write_failure_leaves_completed_attempt_recoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RunResultStore(root, uuid.uuid4().hex)
            store.summary_path = parallel_runner.summary_output_path(root)
            key = task_key_for_executable(root / 'plan.py')
            with mock.patch.object(store, 'write_summary', side_effect=OSError('summary disk failure')):
                with self.assertRaises(OSError):
                    store.record_attempt(key, 1, complete_result(store.run_id, key))
            self.assertEqual(store.rebuild_summary()['total_results'], 1)
            self.assertEqual(json.loads(store.summary_path.read_text())['run_status'], 'in_progress')

    def test_legacy_inconsistent_task_success_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_result({'evaluation_status': 'valid', 'task_success': True, 'sr': 0},
                            returncode=0, expected_identity={})

    def test_storage_error_during_interrupt_is_reported_as_run_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            script.touch()
            failure_reached = threading.Event()
            def record(*args):
                failure_reached.set()
                raise OSError('interrupted disk full')
            def run(path, **kwargs):
                return complete_result(kwargs['run_id'], kwargs['task_key'], kwargs['attempt'])
            def interrupt(futures):
                self.assertTrue(failure_reached.wait(3))
                raise KeyboardInterrupt()
                yield
            with mock.patch.object(RunResultStore, 'record_attempt', side_effect=record), mock.patch.object(
                parallel_runner, 'run_generated_executable', side_effect=run), mock.patch.object(
                parallel_runner, 'as_completed', side_effect=interrupt):
                try:
                    code = parallel_runner.main([str(script), '--output-dir', str(root / 'output')])
                except KeyboardInterrupt:
                    self.fail('disk failure was hidden by KeyboardInterrupt')
                self.assertEqual(code, 1)
            summary = json.loads(next((root / 'output').glob('*.json')).read_text())
            self.assertEqual(summary['run_status'], 'failed')
            self.assertIn('interrupted disk full', summary['storage_error'])

    def test_completed_v2_requires_coherent_action_counts(self):
        run_id, key = uuid.uuid4().hex, task_key_for_executable(Path('plan.py'))
        counts = dict(planned=1, started=1, succeeded=0, failed=1, skipped=0, cancelled=0, unexecuted=0, attempts=1)
        for changes in ({'action_counts': None}, {'action_counts': {}},
                        {'action_counts': counts, 'raw_action_sr': 1.0},
                        {'action_counts': counts, 'raw_action_sr': 0.0, 'execution_status': 'completed'},
                        {'action_counts': {**counts, 'failed': -1}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(complete_result(run_id, key, **changes), returncode=0, expected_identity={})


if __name__ == '__main__':
    unittest.main()
