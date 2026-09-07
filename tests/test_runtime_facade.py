"""Compatibility and observable boundaries of the runtime services; no simulator."""
import json
import io
from contextlib import redirect_stdout
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from executor_system.runtime import ThorRuntime
from executor_system.execution_control import ExecutionCancelled
from tests.test_world_snapshot import multi_event, snapshot_runtime


class PlanTypesFacadeTest(unittest.TestCase):
    def test_legacy_imports_are_identical_types(self):
        from executor_system import action_plan, plan_types
        for name in ('Action', 'PlannedAction', 'StagePlan', 'TaskPlan',
                     'MultiStageActionPlan', 'ActionResult', 'RobotExecutionState',
                     'ResourceRequest'):
            with self.subTest(name=name):
                self.assertIs(getattr(action_plan, name), getattr(plan_types, name))
        from executor_system.task_plan import TaskPlan
        self.assertIs(TaskPlan, plan_types.TaskPlan)

    def test_parsing_types_does_not_load_execution_or_cli_modules(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
sys.path.insert(0, 'scripts')
from executor_system.plan_types import Action, TaskPlan
value = Action.from_any({'action': 'ThrowObject', 'throwMagnitude': 7})
assert value.parameters == {'throwMagnitude': 7}
plan = TaskPlan.from_dict({'stages': [{'robot_action_queues': {'robot1': ['Pass']}}]})
assert plan.stages[0].robot_action_queues['robot1'][0].robot_id == 'robot1'
assert not any(name in sys.modules for name in (
    'executor_system.runtime', 'executor_system.action_plan',
    'executor_system.action_registry', 'executor_system.parallel_runner',
    'executor_system.generated_plan_runtime'))
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ControllerFacadeTest(unittest.TestCase):
    def setUp(self):
        from executor_system.controller_client import ControllerClient
        self.runtime = snapshot_runtime()
        self.runtime.controller_client = ControllerClient(self.runtime)

    def test_one_submission_commits_one_version_and_two_safety_checks(self):
        calls = []
        original = self.runtime.controller.step
        def submit(payload):
            calls.append(payload)
            return original(payload)
        self.runtime.controller.step = submit
        with patch.object(self.runtime.execution_control, 'check',
                          wraps=self.runtime.execution_control.check) as check:
            event = self.runtime._step_direct({'action': 'Pass'}, save_frame=False)
        self.assertIs(event, self.runtime.controller.last_event)
        self.assertEqual(calls, [{'action': 'Pass'}])
        self.assertEqual(self.runtime.state_version, 1)
        self.assertEqual(check.call_count, 2)

    def test_client_accepts_replaced_controller_and_commits_failed_event(self):
        replacement = SimpleNamespace(last_event=multi_event(success=False))
        replacement.step = lambda payload: replacement.last_event
        self.runtime.controller = replacement
        with self.assertRaisesRegex(RuntimeError, 'Pass failed'):
            self.runtime.controller_client.step({'action': 'Pass'}, save_frame=False)
        self.assertEqual(self.runtime.state_version, 1)

    def test_controller_submissions_are_mutually_exclusive(self):
        entered, release, second_started = (threading.Event() for _ in range(3))
        calls, errors = [], []
        lock = threading.RLock()
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == 'second-submission':
                    second_started.set()
                return lock.__enter__()
            def __exit__(self, *args):
                return lock.__exit__(*args)
        self.runtime.controller_lock = ObservedLock()
        original = self.runtime.controller.step
        def submit(payload):
            calls.append(payload['agentId'])
            if payload['agentId'] == 0:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test release timed out')
            return original(payload)
        self.runtime.controller.step = submit
        def run(agent):
            try:
                self.runtime._step_direct({'action': 'Pass', 'agentId': agent}, save_frame=False)
            except BaseException as exc:
                errors.append(exc)
        workers = [threading.Thread(target=run, args=(agent,),
                                    name='first-submission' if agent == 0 else 'second-submission')
                   for agent in (0, 1)]
        workers[0].start()
        try:
            self.assertTrue(entered.wait(3))
            workers[1].start()
            self.assertTrue(second_started.wait(3))
            self.assertEqual(calls, [0])
        finally:
            release.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(3)
        self.assertFalse(errors)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(calls, [0, 1])
        self.assertEqual(self.runtime.state_version, 2)

    def test_cancellation_while_waiting_for_lock_prevents_submission(self):
        checked, errors, submitted = threading.Event(), [], []
        self.runtime.controller.step = lambda payload: submitted.append(payload)
        original = self.runtime.execution_control.check
        def check():
            original()
            checked.set()
        def run():
            try:
                self.runtime._step_direct({'action': 'Pass'}, save_frame=False)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        with patch.object(self.runtime.execution_control, 'check', side_effect=check):
            with self.runtime.controller_lock:
                worker.start()
                self.assertTrue(checked.wait(3))
                self.runtime.execution_control.cancel('cancel during lock wait')
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ExecutionCancelled)
        self.assertEqual(submitted, [])
        self.assertEqual(self.runtime.state_version, 0)

    def test_repeated_stop_does_not_retry_failed_controller_shutdown(self):
        calls = []
        def stop():
            calls.append('stop')
            raise RuntimeError('shutdown failed')
        self.runtime.controller.stop = stop
        self.runtime.show_windows = False
        with self.assertRaisesRegex(RuntimeError, 'shutdown failed'):
            self.runtime.stop()
        self.runtime.stop()
        self.assertEqual(calls, ['stop'])
        self.assertIsNone(self.runtime.controller)

    def test_legacy_executor_aliases_remain_identical(self):
        from executor_system.executor import Executor
        from executor_system.central_executor import CentralStepExecutor
        from executor_system.synchronous_executor import SynchronousExecutor
        self.assertIs(CentralStepExecutor, Executor)
        self.assertIs(SynchronousExecutor, Executor)


class ArtifactsFacadeTest(unittest.TestCase):
    def setUp(self):
        from executor_system.runtime_artifacts import RuntimeArtifacts
        self.runtime = snapshot_runtime()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime.output_root = ThorRuntime.resolve_output_root(Path(self.temp.name))
        self.runtime.render_image = True
        self.runtime.show_windows = False
        self.runtime.frame_counter = 0
        self.runtime.missing_frame_warning_emitted = False
        self.runtime.third_party_view_names = []
        self.runtime.top_view_enabled = False
        # Runtime's media dependency providers retain legacy patch points.
        self.runtime.artifacts = self.runtime._make_artifacts()
        self.assertIsInstance(self.runtime.artifacts, RuntimeArtifacts)

    def test_prepare_isolates_shared_container_and_preserves_other_run(self):
        other = ThorRuntime.resolve_output_root(Path(self.temp.name))
        (other / 'agent_1').mkdir()
        old_frame = other / 'agent_1' / 'img_00000.png'
        old_frame.write_bytes(b'old frame')
        self.runtime.prepare_output_dirs()
        self.assertEqual(old_frame.read_bytes(), b'old frame')
        self.assertTrue((self.runtime.output_root / 'agent_1').is_dir())
        self.assertNotEqual(self.runtime.output_root, other)

    def test_frame_files_and_counter_use_legacy_injected_media_dependency(self):
        self.runtime.prepare_output_dirs()
        def imwrite(path, frame):
            Path(path).write_bytes(frame)
            return True
        fake_cv2 = SimpleNamespace(imwrite=imwrite)
        with patch('executor_system.runtime.cv2', fake_cv2), patch(
                'executor_system.runtime.event_cv2_frame', return_value=b'frame'):
            self.runtime.save_frames(multi_event())
        self.assertEqual(self.runtime.frame_counter, 1)
        for agent in (1, 2):
            self.assertEqual((self.runtime.output_root / f'agent_{agent}' /
                              'img_00000.png').read_bytes(), b'frame')

    def test_video_uses_owned_frames_and_preserves_encoder_arguments(self):
        self.runtime.prepare_output_dirs()
        frame = self.runtime.output_root / 'agent_1' / 'img_00000.png'
        frame.write_bytes(b'frame')
        commands = []
        def encode(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b'video')
            return SimpleNamespace(returncode=0, stderr='')
        with patch('executor_system.runtime.shutil.which', return_value='/fake/ffmpeg'), patch(
                'executor_system.runtime.subprocess.run', side_effect=encode):
            self.runtime.generate_video()
        self.assertEqual(commands, [[
            'ffmpeg', '-y', '-framerate', '5', '-i',
            str(frame.parent / 'img_%05d.png'), '-pix_fmt', 'yuv420p',
            str(self.runtime.output_root / 'video_agent_1.mp4'),
        ]])
        self.assertEqual((self.runtime.output_root / 'video_agent_1.mp4').read_bytes(), b'video')

    def test_metadata_uses_legacy_flag_and_current_event(self):
        with patch('executor_system.runtime.GENERATE_METADATA', True):
            path = self.runtime.write_final_metadata()
        metadata = json.loads(path.read_text())
        self.assertEqual(metadata['agent']['position'], {'x': 1, 'y': 0, 'z': 0})

    def test_result_persists_when_metadata_write_and_stop_fail(self):
        from executor_system.generated_plan_runtime import (
            build_runner_result, record_execution_error, finalize_runner_result,
        )
        def fail_stop():
            raise RuntimeError('stop failure')
        self.runtime.controller.stop = fail_stop
        self.runtime.configure_movement('step')
        start = time.monotonic()
        result = build_runner_result('failed', start)
        destination = Path(self.temp.name) / 'result.json'
        # A directory at the metadata file path produces a real filesystem error.
        (self.runtime.output_root / 'metadata.txt').mkdir()
        with patch('executor_system.runtime.GENERATE_METADATA', True):
            try:
                self.runtime.write_final_metadata()
            except IsADirectoryError as exc:
                record_execution_error(result, exc, self.runtime)
            else:
                self.fail('metadata write unexpectedly succeeded')
        with redirect_stdout(io.StringIO()):
            finalize_runner_result(self.runtime, result, start, destination)
        persisted = json.loads(destination.read_text())
        self.assertEqual(persisted['execution_status'], 'failed')
        self.assertEqual(persisted['cleanup_errors'][0]['message'], 'stop failure')
        self.assertIsNone(self.runtime.controller)


if __name__ == '__main__':
    unittest.main()
