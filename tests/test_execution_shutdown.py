"""Cancellation tests use real threads and release every blocked fake controller."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.execution_control import (
    ExecutionControl, ExecutionCancelled, ExecutionShutdownTimeout, PlanExecutionTimeout,
)
from executor_system.action_plan import Action, StagePlan, TaskPlan, TaskRunner
from executor_system.parallel_runner import run_action_plan_tolerant, TolerantExecutor
from executor_system.executor import Executor
from executor_system.runtime import ThorRuntime
from tests.test_parallel_runner import FakeRuntime, FakeEvent


def plan():
    return TaskPlan('shutdown', [StagePlan('stage', {'robot1': [Action('Done'), Action('Done')]})])


class ExecutionShutdownTests(unittest.TestCase):
    def test_cancellation_is_monotonic(self):
        control = ExecutionControl()
        control.cancel('worker failed')
        control.cancel('later shutdown')
        self.assertTrue(control.cancelled)
        self.assertEqual(control.reason, 'worker failed')
        self.assertTrue(control.wait(0))
        with self.assertRaises(ExecutionCancelled):
            control.check()

    def test_deadline_cancels_all_scopes(self):
        runtime = object.__new__(ThorRuntime)
        with self.assertRaises(PlanExecutionTimeout):
            with runtime.action_deadline_scope(time.monotonic() - 1, PlanExecutionTimeout):
                runtime.check_navigation_deadline()
        with self.assertRaises(PlanExecutionTimeout):
            runtime.check_navigation_deadline()
        self.assertTrue(runtime.execution_control.cancelled)

    def test_ordinary_entry_budget_and_explicit_unbounded_library_call(self):
        from executor_system import task_plan
        from executor_system.movement import MovementConfig
        for mode, budget in (('step', 120), ('teleport', 30)):
            runtime = FakeRuntime()
            runtime.movement_config = MovementConfig.resolve(mode)
            with patch.object(task_plan, 'get_runtime', return_value=runtime):
                started = time.monotonic()
                task_plan.run_action_plan(plan())
                self.assertGreaterEqual(runtime.execution_control.deadline, started + budget)
                self.assertLessEqual(runtime.execution_control.deadline, time.monotonic() + budget)
                task_plan.run_action_plan(plan(), timeout_seconds=None)
                self.assertIsNone(runtime.execution_control.deadline)

    def test_interrupt_inside_action_is_never_an_ordinary_action_failure(self):
        for runner, executor in ((run_action_plan_tolerant, TolerantExecutor),
                                 (lambda r, p, **kw: TaskRunner(r).execute(p, **kw), Executor)):
            runtime = FakeRuntime()
            calls = []
            error = SystemExit('interrupt action')
            def fail_action(*args, **kwargs):
                calls.append(threading.current_thread())
                raise error
            try:
                with patch.object(executor, 'execute_action', fail_action):
                    with self.assertRaises(SystemExit) as caught:
                        runner(runtime, plan(), timeout_seconds=None)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(calls), 1)
            finally:
                for thread in calls:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())


    def test_uncaught_worker_errors_propagate_including_interrupts(self):
        for runner, executor in ((run_action_plan_tolerant, TolerantExecutor),
                                 (lambda r, p, **kw: TaskRunner(r).execute(p, **kw), Executor)):
            for error_type in (RuntimeError, KeyboardInterrupt, SystemExit):
                with self.subTest(runner=executor.__name__, error=error_type.__name__):
                    runtime = FakeRuntime()
                    original = error_type('worker exploded')
                    with patch.object(executor, 'execute', side_effect=original):
                        with self.assertRaises(error_type) as caught:
                            runner(runtime, plan(), timeout_seconds=None)
                    self.assertIs(caught.exception, original)
                    self.assertEqual(runtime.worker_errors[0]['exception_type'], error_type.__name__)
                    self.assertIn('worker exploded', runtime.worker_errors[0]['traceback'])
                    self.assertTrue(runtime.execution_control.cancelled)

    def test_step_waiter_checks_cancel_again_inside_controller_lock(self):
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        calls, errors, threads = [], [], []
        runtime = object.__new__(ThorRuntime)
        runtime.execution_control = ExecutionControl()
        lock = threading.Lock()
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == 'queued':
                    queued.set()
                lock.acquire()
            def __exit__(self, *args):
                lock.release()
        runtime.controller_lock = ObservedLock()
        def step(payload):
            calls.append(payload['action'])
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test release missing')
            return FakeEvent()
        runtime.controller = SimpleNamespace(step=step)
        def worker(action):
            try:
                runtime._step_direct({'action': action}, save_frame=False)
            except BaseException as exc:
                errors.append(exc)
        try:
            threads.append(threading.Thread(target=worker, args=('first',), daemon=True))
            threads[0].start()
            self.assertTrue(entered.wait(1))
            threads.append(threading.Thread(target=worker, args=('second',), name='queued', daemon=True))
            threads[1].start()
            self.assertTrue(queued.wait(1))
            runtime.execution_control.cancel('stop')
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ['first'])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ExecutionCancelled)

    def test_unquiescent_worker_freezes_report_and_runtime(self):
        entered, release = threading.Event(), threading.Event()
        runtime = FakeRuntime()
        calls, worker_threads = [], []
        def execute(executor, action, **kwargs):
            worker_threads.append(threading.current_thread())
            calls.append(action.action_type)
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test release missing')
            return FakeEvent()
        try:
            with patch.object(TolerantExecutor, 'execute_action', execute), patch(
                'executor_system.execution_control.SHUTDOWN_TIMEOUT_SECONDS', 0.03
            ):
                with self.assertRaises(ExecutionShutdownTimeout):
                    run_action_plan_tolerant(runtime, plan(), timeout_seconds=0.03)
            self.assertTrue(entered.is_set())
            self.assertFalse(runtime.execution_quiescent)
            self.assertFalse(runtime.reusable)
            frozen = dict(runtime.execution_report)
            with self.assertRaisesRegex(RuntimeError, 'frozen'):
                runtime.action_ledger.record_attempt()
            with self.assertRaises(ExecutionShutdownTimeout):
                run_action_plan_tolerant(runtime, plan(), timeout_seconds=None)
        finally:
            release.set()
            for thread in worker_threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ['Done'])
        self.assertEqual(runtime.execution_report, frozen)

    def test_controller_exception_is_not_retried(self):
        runtime = object.__new__(ThorRuntime)
        runtime.execution_control = ExecutionControl()
        runtime.controller_lock = threading.Lock()
        runtime.payload_allows_step_retry = lambda payload: True
        calls = []
        def fail(payload):
            calls.append(payload)
            raise RuntimeError('controller pipe broke')
        runtime.controller = SimpleNamespace(step=fail)
        with self.assertRaisesRegex(RuntimeError, 'controller pipe broke'):
            runtime.step({'action': 'MoveAhead'}, save_frame=False)
        self.assertEqual(len(calls), 1)
        self.assertTrue(runtime.execution_control.cancelled)

    def test_main_thread_interrupt_cancels_and_joins_worker(self):
        entered = threading.Event()
        runtime = FakeRuntime()
        threads = []
        original_join = threading.Thread.join
        injected = []
        def execute(executor, action, **kwargs):
            threads.append(threading.current_thread())
            entered.set()
            executor.control.wait(2)
            return FakeEvent()
        def join(thread, timeout=None):
            if thread.name.startswith('stage-') and not injected:
                self.assertTrue(entered.wait(1))
                injected.append(True)
                raise KeyboardInterrupt('main interrupted')
            return original_join(thread, timeout)
        try:
            with patch.object(TolerantExecutor, 'execute_action', execute), patch.object(
                threading.Thread, 'join', join
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, 'main interrupted'):
                    run_action_plan_tolerant(runtime, plan(), timeout_seconds=None)
            self.assertTrue(runtime.execution_quiescent)
            self.assertTrue(runtime.execution_control.cancelled)
        finally:
            if hasattr(runtime, 'execution_control'):
                runtime.execution_control.cancel('test cleanup')
            for thread in threads:
                original_join(thread, 2)
                self.assertFalse(thread.is_alive())

    def test_unquiescent_runtime_refuses_evaluation_and_stop(self):
        runtime = object.__new__(ThorRuntime)
        runtime.execution_quiescent = False
        runtime.reusable = False
        with self.assertRaises(ExecutionShutdownTimeout):
            runtime.evaluate([])
        with self.assertRaises(ExecutionShutdownTimeout):
            runtime.stop()

    def test_generated_unquiescent_result_skips_done_evaluation_and_stop(self):
        from executor_system import generated_plan_runtime as generated
        from executor_system.movement import MovementConfig
        runtime = FakeRuntime()
        runtime.movement_config = MovementConfig.resolve('step')
        runtime.navigation_metrics = SimpleNamespace(to_dict=lambda: {})
        runtime.register_object_id_bindings = lambda bindings: None
        forbidden = []
        runtime.stop = lambda: forbidden.append('stop')
        runtime.step = lambda *args, **kwargs: forbidden.append('step')
        runtime.evaluate = lambda *args: forbidden.append('evaluate')
        entered, release = threading.Event(), threading.Event()
        threads = []
        def execute(executor, action, **kwargs):
            threads.append(threading.current_thread())
            entered.set()
            release.wait(2)
            return FakeEvent()
        bundle = SimpleNamespace(gcr=[], noop_subtasks=[], task_plan=plan(),
                                 object_mapping_warnings=[], object_id_bindings=[])
        try:
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'result.json'
                args = SimpleNamespace(metrics_output=str(destination), movement_mode='step', timeout_seconds=0.03)
                with patch.object(generated, '_runtime_inputs', return_value=({}, '1', [], [], bundle)), patch.object(
                    generated, 'ThorRuntime', return_value=runtime
                ), patch.object(TolerantExecutor, 'execute_action', execute), patch(
                    'executor_system.execution_control.SHUTDOWN_TIMEOUT_SECONDS', 0.03
                ):
                    self.assertEqual(generated.run_runner_mode(args, {}, 'unused', 0, __file__), 124)
                result = json.loads(destination.read_text())
                self.assertTrue(entered.is_set())
                self.assertEqual(result['evaluation_status'], 'incomplete')
                for key in ('gcr', 'tc', 'sr', 'ru', 'task_success', 'satisfied_goal_count'):
                    self.assertIsNone(result[key])
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(forbidden, [])


    def test_standalone_cleanup_failure_is_saved_before_exit(self):
        from executor_system import generated_plan_runtime as generated
        runtime = FakeRuntime()
        runtime.stop = lambda: (_ for _ in ()).throw(RuntimeError('close failed'))
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'plan.py'
            with self.assertRaisesRegex(RuntimeError, 'close failed'):
                generated.close_standalone_runtime(runtime, time.monotonic(), str(script), 0)
            result = json.loads((script.parent / 'parallel_run_result.json').read_text())
        self.assertEqual(result['cleanup_errors'][0]['message'], 'close failed')
        self.assertEqual(result['evaluation_status'], 'incomplete')


    def test_generated_cleanup_error_does_not_prevent_result_or_mask_worker_error(self):
        from executor_system import generated_plan_runtime as generated
        runtime = FakeRuntime()
        from executor_system.movement import MovementConfig
        runtime.movement_config = MovementConfig.resolve('step')
        runtime.navigation_metrics = SimpleNamespace(to_dict=lambda: {})
        runtime.register_object_id_bindings = lambda bindings: None
        runtime.stop = lambda: (_ for _ in ()).throw(RuntimeError('stop broke'))
        bundle = SimpleNamespace(gcr=[], noop_subtasks=[], task_plan=plan(),
                                 object_mapping_warnings=[], object_id_bindings=[])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'result.json'
            args = SimpleNamespace(metrics_output=str(destination), movement_mode='step', timeout_seconds=1)
            with patch.object(generated, '_runtime_inputs', return_value=({}, '1', [], [], bundle)), patch.object(
                generated, 'ThorRuntime', return_value=runtime
            ), patch.object(TolerantExecutor, 'execute', side_effect=RuntimeError('worker broke')):
                self.assertEqual(generated.run_runner_mode(args, {}, 'unused', 0, __file__), 1)
            result = json.loads(destination.read_text())
        self.assertEqual(result['error'], 'worker broke')
        self.assertIsNone(result['action_sr'])
        self.assertEqual(result['cleanup_errors'][0]['message'], 'stop broke')
        self.assertEqual(result['worker_errors'][0]['message'], 'worker broke')
        self.assertIsNone(result['satisfied_goal_count'])


if __name__ == '__main__':
    unittest.main()
