import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system import run_results


class RunProgressTest(unittest.TestCase):
    def test_retry_is_not_terminal_and_only_started_work_is_active(self):
        self.assertTrue(hasattr(run_results, 'RunProgress'), 'lightweight progress publisher missing')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'progress.json'
            progress = run_results.RunProgress(path, run_id='run', planned_tasks=2,
                summary_path=str(Path(temp) / 'summary.json'), movement_mode='step',
                effective_timeout_seconds=120)
            progress.started('a')
            progress.finished('a', {'timed_out': True}, retry=True)
            progress.publish()
            value = json.loads(path.read_text())
            self.assertEqual((value['running_tasks'], value['completed_tasks'], value['pending_retry_tasks']), (0, 0, 1))
            progress.started('a')
            progress.finished('a', {'process_status': 'completed', 'evaluation_status': 'valid', 'task_success': True}, retry=False)
            progress.publish()
            value = json.loads(path.read_text())
            self.assertEqual((value['completed_tasks'], value['attempts_started'], value['attempts_persisted']), (1, 2, 2))
            self.assertEqual(value['task_success_count'], 1)
            self.assertEqual(value['pending_retry_tasks'], 0)
            progress.started('b')
            progress.storage_failed('b', 'disk full')
            progress.publish()
            value = json.loads(path.read_text())
            self.assertEqual(value['completed_tasks'], 1)
            self.assertEqual(value['attempts_persisted'], 2)
            self.assertEqual(value['running_tasks'], 0)
            self.assertEqual(value['storage_error'], 'disk full')

    def test_heartbeat_and_final_phase(self):
        self.assertTrue(hasattr(run_results, 'RunProgress'), 'lightweight progress publisher missing')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'progress.json'
            progress = run_results.RunProgress(path, run_id='run', planned_tasks=1)
            progress.start()
            progress.phase('finalizing')
            self.assertEqual(json.loads(path.read_text())['phase'], 'finalizing')
            progress.phase('completed')
            progress.close()
            self.assertEqual(json.loads(path.read_text())['phase'], 'completed')

    def test_main_publishes_final_counts_and_rebuilds_only_once(self):
        from unittest import mock
        from executor_system import parallel_runner
        from tests.test_parallel_runner import write_fake_generated_script
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script)
            original = parallel_runner.RunResultStore.rebuild_summary
            calls = []
            def rebuild(store):
                calls.append(store.run_id)
                return original(store)
            path = root / 'progress.json'
            with mock.patch.object(parallel_runner.RunResultStore, 'rebuild_summary', rebuild):
                code = parallel_runner.main([str(script), '--output-dir', str(root / 'out'),
                    '--progress-file', str(path), '--movement-mode', 'step'])
            self.assertEqual(code, 0)
            value = json.loads(path.read_text())
            self.assertEqual(value['phase'], 'completed')
            self.assertEqual(value['completed_tasks'], 1)
            self.assertEqual(value['running_tasks'], 0)
            self.assertEqual(value['attempts_persisted'], 1)
            self.assertEqual(value['effective_timeout_seconds'], 120)
            self.assertEqual(len(calls), 1)
            summary = json.loads(Path(value['summary_path']).read_text())
            self.assertEqual(summary['run_id'], value['run_id'])

    def test_sigterm_reclaims_child_and_publishes_interrupted(self):
        import os
        import signal
        import subprocess
        import time
        from tests.test_parallel_runner import write_fake_generated_script, wait_for_process_exit
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script, sleep_seconds=30)
            script.write_text(script.read_text().replace('time.sleep(30)',
                "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\ntime.sleep(30)"))
            path = root / 'progress.json'
            process = subprocess.Popen([sys.executable,
                str(Path(__file__).resolve().parents[1] / 'scripts/executor_system/parallel_runner.py'),
                str(script), '--output-dir', str(root / 'out'), '--progress-file', str(path),
                '--termination-grace-seconds', '0.5'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not script.with_suffix('.pid').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(script.with_suffix('.pid').exists())
                child_pid = int(script.with_suffix('.pid').read_text())
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
                self.assertTrue(wait_for_process_exit(child_pid), 'SIGTERM left an owned child alive')
                self.assertEqual(json.loads(path.read_text())['phase'], 'interrupted')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if child_pid and not wait_for_process_exit(child_pid, .1):
                    os.killpg(child_pid, signal.SIGKILL)

    def test_summary_failure_is_failed_progress_with_recoverable_attempt(self):
        from unittest import mock
        from executor_system import parallel_runner
        from tests.test_parallel_runner import write_fake_generated_script
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'plan.py'
            write_fake_generated_script(script)
            path = root / 'progress.json'
            with mock.patch.object(parallel_runner.RunResultStore, 'write_summary', side_effect=OSError('disk full')):
                code = parallel_runner.main([str(script), '--output-dir', str(root / 'out'), '--progress-file', str(path)])
            self.assertEqual(code, 1)
            value = json.loads(path.read_text())
            self.assertEqual(value['phase'], 'failed')
            self.assertIn('disk full', value['storage_error'])
            self.assertEqual(len(list((root / 'out').glob('runs/*/*/attempt_1/result.json'))), 1)

    def test_running_count_excludes_queued_tasks(self):
        import threading
        from unittest import mock
        from executor_system import parallel_runner
        from tests.test_run_result_storage import complete_result
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = run_results.RunResultStore(root, 'run')
            store.progress = run_results.RunProgress(root / 'progress.json', run_id='run', planned_tasks=2)
            seen = []
            def run(path, **kwargs):
                store.progress.publish()
                seen.append(json.loads((root / 'progress.json').read_text()))
                return complete_result('run', kwargs['task_key'], kwargs['attempt'])
            with mock.patch.object(parallel_runner, 'run_generated_executable', side_effect=run):
                parallel_runner.run_executables_with_retries([root / 'a.py', root / 'b.py'],
                    max_workers=1, temp_metrics_dir=root, timeout_seconds=1,
                    save_all_stdout=False, run_id='run', result_store=store)
            self.assertEqual(seen[0]['running_tasks'], 1)
            self.assertEqual(seen[0]['attempts_started'], 1)
            self.assertEqual(seen[1]['completed_tasks'], 1)
            self.assertEqual(seen[1]['running_tasks'], 1)
