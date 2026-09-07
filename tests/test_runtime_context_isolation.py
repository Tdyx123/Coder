"""Behavioral isolation and safe compatibility recording regressions."""
import concurrent.futures
import random
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system import actions, context
from executor_system.evaluation import EvaluationContext
from executor_system.goals import record_verified_goal_state
from executor_system.task_plan import TaskPlanParser, plan_action
from tests.test_runtime_object_aliases import runtime_with_objects, FakeEvent


def WaitOneTick(robot):
    raise AssertionError('original helper must never execute')


def straight_task(robot, target='Mug', *, suffix='Cup'):
    chosen = target
    PickupObject(robot, chosen)
    PutObject(robot, chosen, suffix)


def concurrent_task(robot):
    WaitOneTick(robot)


SIDE_EFFECTS = []


def unsafe_task(robot):
    SIDE_EFFECTS.append(robot)
    WaitOneTick(robot)


def conditional_task(robot):
    if robot:
        WaitOneTick(robot)


def loop_task(robot):
    for item in ('Mug',):
        PickupObject(robot, item)


def indirect_task(robot):
    helper = WaitOneTick
    helper(robot)


def global_task(robot):
    global SIDE_EFFECTS
    SIDE_EFFECTS = []


class RuntimeContextIsolationTest(unittest.TestCase):
    def binding(self, runtime):
        self.assertTrue(hasattr(context, 'bind_runtime'), 'scoped ContextVar binding is missing')
        return context.bind_runtime(runtime)

    def test_context_binding_is_restored(self):
        first, second = object(), object()
        with self.binding(first):
            self.assertIs(context.get_runtime(), first)
            with self.binding(second):
                self.assertIs(context.get_runtime(), second)
            self.assertIs(context.get_runtime(), first)

    def test_helper_exception_restores_context(self):
        first = object()
        class FailingRuntime:
            def physical_agent_id(self, robot): return 0
            def step(self, *args, **kwargs): raise ValueError('helper failed')
        with self.binding(first):
            with self.assertRaisesRegex(ValueError, 'helper failed'):
                with self.binding(FailingRuntime()):
                    actions.WaitOneTick('robot1')
            self.assertIs(context.get_runtime(), first)

    def test_two_threads_keep_actions_and_evaluation_evidence_local(self):
        barrier = threading.Barrier(2)
        class Runtime:
            def __init__(self):
                self.calls = []
                self.evaluation_context = EvaluationContext.from_goals([
                    {'name': 'Mug', 'states': ['HOT']}])
            def physical_agent_id(self, robot): return 0
            def step(self, payload, **kwargs): self.calls.append(payload)
        first, second = Runtime(), Runtime()
        def run(runtime, state):
            with self.binding(runtime):
                barrier.wait(timeout=5)
                actions.WaitOneTick('robot1')
                record_verified_goal_state('Mug', state, 'Mug|1')
                return context.get_runtime()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(run, first, 'HOT'), pool.submit(run, second, 'COLD')]
            self.assertEqual([f.result(6) for f in futures], [first, second])
        self.assertEqual(first.calls, [{'action': 'Pass', 'agentId': 0}])
        self.assertEqual(second.calls, [{'action': 'Pass', 'agentId': 0}])
        self.assertTrue(first.evaluation_context.has_observation('Mug', 'HOT'))
        self.assertFalse(first.evaluation_context.has_observation('Mug', 'COLD'))
        self.assertTrue(second.evaluation_context.has_observation('Mug', 'COLD'))
        self.assertFalse(second.evaluation_context.has_observation('Mug', 'HOT'))

    def test_alias_transformation_and_operated_history_are_runtime_local(self):
        obj = {'objectId': 'Tomato|1', 'objectType': 'Tomato', 'name': 'Tomato_A', 'visible': True}
        first, second = runtime_with_objects([obj]), runtime_with_objects([obj])
        for runtime in (first, second):
            runtime.register_object_id_bindings([{'object': 'Tomato_1', 'object_id': 'Tomato|1', 'object_type': 'Tomato'}])
        sliced = {'objectId': 'Tomato|1|slice', 'objectType': 'TomatoSliced', 'name': 'TomatoSliced_A', 'visible': True}
        first._test_objects = [sliced]
        first.update_object_alias_after_action('SliceObject', 0, obj, event=FakeEvent([sliced]), goal_object_name='Tomato_1')
        first.record_operated_object_name(sliced)
        self.assertEqual(second.resolve_object_alias('Tomato_1'), 'Tomato|1')
        self.assertEqual(second.operated_object_names_snapshot(), set())
        self.assertTrue(first.operated_object_names_snapshot())

    def test_explicit_planned_actions_even_empty_skip_function(self):
        for explicit in ([], [plan_action('WaitOneTick')]):
            def task(robot):
                raise AssertionError('explicit plans must not execute task body')
            task.planned_actions = explicit
            self.assertEqual(TaskPlanParser().record_subtask('robot1', task), explicit)

    def test_parallel_recording_never_replaces_original_globals(self):
        barrier = threading.Barrier(2)
        original = concurrent_task.__globals__['WaitOneTick']
        class Parser(TaskPlanParser):
            def _make_action_recorder(self, name, recorded):
                recorder = super()._make_action_recorder(name, recorded)
                def record(*args, **kwargs):
                    barrier.wait(timeout=5)
                    if concurrent_task.__globals__['WaitOneTick'] is not original:
                        raise AssertionError('shared function globals were mutated')
                    recorder(*args, **kwargs)
                return record
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(Parser().record_subtask, 'robot1', concurrent_task) for _ in range(2)]
            for future in futures:
                self.assertEqual([a.action_type for a in future.result(6)], ['WaitOneTick'])
        self.assertIs(concurrent_task.__globals__['WaitOneTick'], original)

    def test_recording_preserves_defaults_keyword_defaults_and_closure(self):
        destination = 'CounterTop'
        def task(robot, item='Mug', *, target='Sink'):
            PickupObject(robot, item)
            PutObject(robot, item, target)
            GoToObject(robot, destination)
        result = TaskPlanParser().record_subtask('robot1', task)
        self.assertEqual([a.args() for a in result], [('Mug',), ('Mug', 'Sink'), ('CounterTop',)])

    def test_unsafe_recording_rejected_before_side_effects(self):
        SIDE_EFFECTS.clear()
        for function in (unsafe_task, conditional_task, loop_task, indirect_task, global_task,
                         eval('lambda robot: None')):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(RuntimeError, 'explicit.*plan|planned_actions'):
                    TaskPlanParser().record_subtask('robot1', function)
        self.assertEqual(SIDE_EFFECTS, [])

class RuntimeEntrypointIsolationTest(unittest.TestCase):
    def test_real_execution_worker_binds_its_runtime(self):
        from types import SimpleNamespace
        from executor_system.execution_control import ExecutionControl, run_workers
        from tests.snapshot_fakes import FakeRuntime
        runtime = FakeRuntime()
        seen = []
        executor = SimpleNamespace(robot_id='robot1')
        def execute():
            actions.WaitOneTick('robot1')
            seen.append(context.get_runtime())
        executor.execute = execute
        coordinator = SimpleNamespace(control=ExecutionControl(), condition=threading.Condition())
        with context.bind_runtime(object()):
            run_workers(runtime, [executor], coordinator, 'context-test')
        self.assertEqual(seen, [runtime])
        self.assertEqual(runtime.state_version, 1)

    def test_demo_runtime_facade_prefers_scoped_runtime(self):
        import demo
        first, second = object(), object()
        with patch.object(context, 'runtime', first):
            with context.bind_runtime(second):
                self.assertIs(demo.runtime, second)
            self.assertIs(demo.runtime, first)

    def test_cleanup_worker_binds_runtime_without_clearing_other_legacy_runtime(self):
        from types import SimpleNamespace
        import time
        from executor_system.generated_plan_runtime import close_standalone_runtime
        sentinel = object()
        seen = []
        runtime = SimpleNamespace(stop=lambda: seen.append(context.get_runtime()))
        with patch.object(context, 'runtime', sentinel):
            close_standalone_runtime(runtime, time.monotonic(), __file__, 0)
            self.assertIs(context.runtime, sentinel)
        self.assertEqual(seen, [runtime])

    def test_initialization_does_not_reseed_process_random(self):
        from types import SimpleNamespace
        from executor_system.runtime import ThorRuntime
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime.floor = '1'
        runtime.no_robot = runtime.physical_agent_count = 1
        runtime.top_view_enabled = False
        runtime.cloud_rendering = False
        runtime.step = lambda *args, **kwargs: SimpleNamespace(metadata={
            'actionReturn': [{'x': 0.0, 'y': 0.0, 'z': 0.0}]})
        runtime.set_initial_look_angle = lambda agent_id: None
        before = random.getstate()
        try:
            runtime.initialize_scene()
            self.assertEqual(random.getstate(), before)
            self.assertEqual(runtime.random.random(), 0.8444218515250481)
        finally:
            random.setstate(before)

    def test_bound_demo_goals_and_legacy_evidence_do_not_leak(self):
        from types import SimpleNamespace
        from executor_system.demo_state import set_ground_truth, get_ground_truth
        from executor_system.goals import goal_state_verified
        first, second = SimpleNamespace(), SimpleNamespace()
        with context.bind_runtime(first):
            set_ground_truth([{'name': 'Mug', 'states': ['HOT']}])
            record_verified_goal_state('Mug', 'HOT', 'Mug|1')
            with context.bind_runtime(second):
                set_ground_truth([{'name': 'Mug', 'states': ['COLD']}])
                self.assertFalse(goal_state_verified('Mug', 'HOT'))
                record_verified_goal_state('Mug', 'COLD', 'Mug|1')
            self.assertTrue(goal_state_verified('Mug', 'HOT'))
            self.assertFalse(goal_state_verified('Mug', 'COLD'))
            self.assertEqual(get_ground_truth(), [{'name': 'Mug', 'states': ['HOT']}])

    def test_decorated_function_cannot_hide_unsafe_wrapper(self):
        from functools import wraps
        effects = []
        def decorate(function):
            @wraps(function)
            def wrapper(robot):
                effects.append(robot)
                return function(robot)
            return wrapper
        @decorate
        def task(robot):
            WaitOneTick(robot)
        with self.assertRaisesRegex(RuntimeError, 'explicit.*plan|planned_actions'):
            TaskPlanParser().record_subtask('robot1', task)
        self.assertEqual(effects, [])

class ExplicitServiceIsolationTest(unittest.TestCase):
    def test_pure_compatibility_helpers_need_no_runtime_binding(self):
        with patch.object(context, 'runtime', None):
            self.assertTrue(actions._object_has_liquid({'isFilledWithLiquid': True}))
            self.assertEqual(actions._stove_parent_burner_id({
                'parentReceptacles': ['StoveBurner|1']}), 'StoveBurner|1')

    def test_explicit_service_evidence_uses_injected_runtime_without_evaluation_context(self):
        from executor_system.object_interactor import ObjectInteractor
        from executor_system.goals import goal_state_verified
        obj = {'objectId': 'Mug|1', 'objectType': 'Mug', 'name': 'Mug', 'temperature': 'Hot'}
        first, second = runtime_with_objects([obj]), runtime_with_objects([obj])
        first.physical_agent_id = lambda robot: 0
        second.evaluation_context = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT']}])
        with context.bind_runtime(second):
            ObjectInteractor(first)._wait_for_object_hot('robot1', 'Mug', 'HeatByStoveBurner')
        self.assertFalse(second.evaluation_context.has_observation('Mug', 'HOT'))
        with context.bind_runtime(first):
            self.assertTrue(goal_state_verified('Mug', 'HOT'))

    def test_recorder_rejects_custom_parameter_mapping_before_user_code(self):
        effects = []
        class Parameters:
            def keys(self):
                effects.append('keys')
                return []
        parameters = Parameters()
        def task(robot):
            WaitOneTick(robot, parameters=parameters)
        with self.assertRaisesRegex(RuntimeError, 'explicit.*plan|planned_actions'):
            TaskPlanParser().record_subtask('robot1', task)
        self.assertEqual(effects, [])


if __name__ == '__main__':
    unittest.main()
