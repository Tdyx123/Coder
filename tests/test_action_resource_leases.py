"""Atomic action leases and binding regressions without Unity."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.resource_manager import ActionResourceManager
from executor_system.action_resources import resolve_action_resources, action_resource_scope, ResourceBindingInvalid, ResourceBindingDeferred
from executor_system.action_plan import Action
from executor_system.world_snapshot import WorldSnapshot
from executor_system.runtime import ThorRuntime
from executor_system import context


def obj(object_id, **kw):
    return dict(objectId=object_id, objectType=object_id.split('|')[0], **kw)


class ResourceRuntimeMixin:
    def runtime(self, objects):
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime.current_objects = lambda agent_id=None: objects
        runtime.physical_agent_count = 2
        runtime.robot_agent_map = {'robot1': 0, 'robot2': 1}
        runtime.agent_held_objects_for = lambda agent_id: set()
        runtime.snapshot = WorldSnapshot(0, {}, {}, {'robot1': (), 'robot2': ()}, {o['objectId']: o for o in objects})
        return runtime

class ResourceTest(ResourceRuntimeMixin, unittest.TestCase):
    def test_resource_is_released_before_next_owner_enters(self):
        manager = ActionResourceManager()
        first = manager.try_acquire('0:robot1:0', ('Microwave|1', 'Mug|1'))
        self.assertIsNotNone(first)
        self.assertIsNone(manager.try_acquire('0:robot2:0', ('Microwave|1',)))
        first.release()
        first.release()
        self.assertIsNotNone(manager.try_acquire('0:robot2:0', ('Microwave|1',)))

    def test_reverse_order_atomic_and_independent_instances(self):
        manager = ActionResourceManager()
        first = manager.try_acquire('a', ('Mug|1', 'Microwave|1'))
        self.assertIsNone(manager.try_acquire('b', ('Other|1', 'Microwave|1', 'Mug|1')))
        self.assertEqual(manager.blockers(('Other|1',)), ())
        self.assertEqual(manager.blockers(('Microwave|1',)), ('a',))
        self.assertIsNotNone(manager.try_acquire('c', ('Mug|2',)))
        first.release()
        self.assertIsNotNone(manager.try_acquire('b', ('Microwave|1', 'Mug|1')))

    def test_complete_resource_table_and_no_navigation_target_lease(self):
        objects = [obj(x) for x in ('Mug|1', 'Microwave|1', 'Toaster|1', 'Bread|1', 'Fridge|1', 'CoffeeMachine|1', 'Egg|1', 'Pan|1', 'StoveBurner|1', 'Faucet|1', 'SinkBasin|1')]
        objects.append(obj('StoveKnob|1', controlledObjects=['StoveBurner|1']))
        runtime = self.runtime(objects)
        cases = {
            'PickupObject': ('Mug|1',), 'PutObject': ('Mug|1', 'Pan|1'),
            'RunMicrowave': ('Microwave|1', 'Mug|1'), 'ColdObject': ('Fridge|1', 'Mug|1'),
            'RunCoffeeMachine': ('CoffeeMachine|1', 'Mug|1'), 'RunToaster': ('Toaster|1', 'Bread|1'),
            'PrepareEgg': ('Egg|1', 'Pan|1'),
            'CookByStoveBurner': ('StoveBurner|1', 'Pan|1', 'Egg|1'),
            'FillWater': ('Faucet|1', 'Mug|1'),
        }
        for name, args in cases.items():
            resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action(name, {'args': args}))
            expected = set(args)
            if name == 'CookByStoveBurner': expected.add('StoveKnob|1')
            if name == 'FillWater': expected.add('SinkBasin|1')
            self.assertEqual(set(resolved.keys), expected, name)
        self.assertEqual(resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('GoToObject', {'args': ('Mug',)})).keys, ())

    def test_alias_transformation_keeps_active_lease(self):
        objects = [obj('Egg|1')]
        runtime = self.runtime(objects)
        runtime.register_object_id_bindings([{'object': 'Egg1', 'object_id': 'Egg|1'}])
        original = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('BreakEgg', {'args': ('Egg1',)}))
        lease = runtime.action_resource_manager.try_acquire('a', original.keys)
        objects[:] = [obj('EggCracked|2')]
        runtime._set_object_alias_current_object('Egg1', objects[0])
        snapshot = WorldSnapshot(1, {}, {}, {}, {'EggCracked|2': objects[0]})
        changed = resolve_action_resources(runtime, snapshot, 'robot2', Action('PickupObject', {'args': ('EggCracked|2',)}))
        self.assertIsNone(runtime.action_resource_manager.try_acquire('b', changed.keys))
        lease.release()

    def test_held_by_other_is_not_an_available_resource(self):
        runtime = self.runtime([obj('Mug|1')])
        snapshot = WorldSnapshot(0, {}, {}, {'robot2': ('Mug|1',)}, {'Mug|1': obj('Mug|1')})
        with self.assertRaisesRegex(RuntimeError, 'OBJECT_HELD_BY_OTHER'):
            resolve_action_resources(runtime, snapshot, 'robot1', Action('PickupObject', {'args': ('Mug',)}))

    def test_bound_selection_and_invalidation_before_after_side_effects(self):
        objects = [obj('Mug|1'), obj('Mug|2')]
        runtime = self.runtime(objects)
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug',)}))
        with action_resource_scope(runtime, 'owner', resolved) as scope:
            objects.reverse()
            self.assertEqual(runtime.find_object('Mug')['objectId'], 'Mug|1')
            objects[:] = [obj('Mug|2')]
            with self.assertRaises(ResourceBindingDeferred): runtime.find_object('Mug')
            scope.effects_started = True
            with self.assertRaises(ResourceBindingInvalid): runtime.find_object('Mug')
            objects.append(obj('Mug|1'))

    def test_runtime_context_is_thread_local_and_nested(self):
        first, second = object(), object()
        barrier = threading.Barrier(2)
        seen = []
        def run(value):
            with context.runtime_scope(value):
                barrier.wait(1)
                seen.append(context.get_runtime() is value)
                with context.runtime_scope(object()): pass
                seen.append(context.get_runtime() is value)
        threads = [threading.Thread(target=run, args=(v,)) for v in (first, second)]
        for t in threads: t.start()
        for t in threads: t.join(2)
        self.assertEqual(seen, [True] * 4)


class SchedulerResourceTest(unittest.TestCase):
    def run_stage(self, actions, execute, timeout=1):
        from tests.snapshot_fakes import FakeRuntime
        from executor_system.action_plan import StagePlan, AI2ThorAdapter
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.execution_control import install_control
        from unittest.mock import patch
        runtime = FakeRuntime()
        runtime.objects = [obj(name) for name in ('Microwave|1', 'Mug|1', 'Mug|2')]
        stage = StagePlan('resources', actions)
        scheduler = StageScheduler(runtime, stage, control=install_control(runtime, timeout).child(), policy='legacy')
        with patch.object(AI2ThorAdapter, 'execute', execute):
            result = scheduler.run()
        self.assertEqual(runtime.action_resource_manager.holders(), {})
        return scheduler, result

    def test_shared_composite_never_interleaves_and_failure_releases(self):
        for fail in (False, True):
            order = []
            def execute(adapter, robot, action, **kw):
                order.append(robot + ':on')
                threading.Event().wait(.015)
                order.append(robot + ':off')
                if fail and robot == 'robot1': raise RuntimeError('device rejected')
            action = Action('RunMicrowave', {'args': ('Microwave|1', 'Mug|1')})
            scheduler, outcome = self.run_stage({'robot1': [action], 'robot2': [action]}, execute)
            self.assertEqual(order, ['robot1:on', 'robot1:off', 'robot2:on', 'robot2:off'])
            self.assertEqual(outcome.status, 'partial' if fail else 'completed')

    def test_distinct_instances_execute_concurrently(self):
        barrier = threading.Barrier(2)
        def execute(adapter, robot, action, **kw): barrier.wait(.5)
        _, outcome = self.run_stage({
            'robot1': [Action('PickupObject', {'args': ('Mug|1',)})],
            'robot2': [Action('PickupObject', {'args': ('Mug|2',)})]}, execute)
        self.assertEqual(outcome.status, 'completed')

    def test_conflict_skip_and_fail_stage_do_not_execute_loser(self):
        for policy in ('SKIP', 'FAIL_STAGE'):
            called = []
            def execute(adapter, robot, action, **kw): called.append(robot)
            action = Action('PickupObject', {'args': ('Mug|1',)})
            scheduler, outcome = self.run_stage({
                'robot1': [action], 'robot2': [Action('PickupObject', {'args': ('Mug|1',)}, on_conflict=policy)]}, execute)
            self.assertNotIn('robot2', called)
            if policy == 'FAIL_STAGE':
                self.assertEqual(outcome.status, 'failed')
                self.assertEqual(scheduler.executors['robot2'].state.last_action_result.failure_decision, 'fail_stage')
            else: self.assertEqual(scheduler.executors['robot2'].state.last_action_result.status, 'SKIPPED')

    def test_cancel_releases_after_worker_exits(self):
        from executor_system.execution_control import ExecutionCancelled
        def execute(adapter, robot, action, **kw):
            adapter.runtime.execution_control.cancel('test cancel')
            adapter.runtime.execution_control.check()
        from tests.snapshot_fakes import FakeRuntime
        from executor_system.action_plan import StagePlan, AI2ThorAdapter
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.execution_control import install_control
        from unittest.mock import patch
        runtime = FakeRuntime()
        runtime.objects = [obj('Mug|1')]
        scheduler = StageScheduler(runtime, StagePlan('s', {'robot1': [Action('PickupObject', {'args': ('Mug|1',)})]}), control=install_control(runtime, 1).child(), policy='legacy')
        with patch.object(AI2ThorAdapter, 'execute', execute), self.assertRaises(ExecutionCancelled): scheduler.run()
        self.assertTrue(runtime.execution_quiescent)
        self.assertEqual(runtime.action_resource_manager.holders(), {})

class HelperBindingTest(ResourceRuntimeMixin, unittest.TestCase):
    def test_fillwater_sink_query_can_use_existing_basin_fallback(self):
        runtime = self.runtime([obj('SinkBasin|1'), obj('Faucet|1'), obj('Mug|1')])
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1',
                                            Action('FillWater', {'args': ('Sink', 'Mug')}))
        self.assertEqual(set(resolved.keys), {'SinkBasin|1', 'Faucet|1', 'Mug|1'})
        self.assertEqual(resolved.bindings['@basin'], 'SinkBasin|1')

    def test_stove_and_sink_roles_stay_bound_after_selection_changes(self):
        from executor_system import actions
        objects = [obj('StoveBurner|1'), obj('StoveBurner|2'), obj('Pan|1'),
                   obj('StoveKnob|1', controlledObjects=['StoveBurner|1']),
                   obj('StoveKnob|2', controlledObjects=['StoveBurner|2']),
                   obj('Faucet|1'), obj('SinkBasin|1'), obj('SinkBasin|2'), obj('Mug|1')]
        runtime = self.runtime(objects)
        for action in (Action('HeatByStoveBurner', {'args': ('StoveBurner|1', 'Pan|1')}),
                       Action('FillWater', {'args': ('Faucet', 'Mug')})):
            resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', action)
            with context.runtime_scope(runtime), action_resource_scope(runtime, 'a', resolved):
                objects.reverse()
                if action.action_type == 'FillWater':
                    self.assertEqual(actions._find_sink_basin('robot1', 'Faucet')['objectId'], resolved.bindings['@basin'])
                    self.assertEqual(runtime.find_object('Faucet')['objectId'], resolved.bindings['@faucet'])
                else:
                    burner = actions._resolve_stove_burner('robot1', 'StoveBurner')
                    self.assertEqual(burner['objectId'], 'StoveBurner|1')
                    self.assertEqual(actions._resolve_stove_knob_for_burner('robot1', burner)['objectId'], 'StoveKnob|1')

    def test_admitted_helper_cannot_use_unleased_target_or_new_other_held_object(self):
        runtime = self.runtime([obj('Mug|1'), obj('Mug|2')])
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
        with action_resource_scope(runtime, 'a', resolved) as scope:
            with self.assertRaises(ResourceBindingDeferred):
                scope.before_step({'action': 'PickupObject', 'objectId': 'Mug|2', 'agentId': 0})
            runtime.agent_held_objects_for = lambda agent_id: {'Mug|1'} if agent_id == 1 else set()
            with self.assertRaisesRegex(RuntimeError, 'OBJECT_HELD_BY_OTHER'):
                scope.before_step({'action': 'PickupObject', 'objectId': 'Mug|1', 'agentId': 0})
            runtime.agent_held_objects_for = lambda agent_id: set()

    def test_auto_hand_container_is_leased_and_cannot_reselect(self):
        runtime = self.runtime([obj('Mug|1'), obj('Mug|2'), obj('CounterTop|1'), obj('CounterTop|2')])
        runtime.agent_held_objects_for = lambda agent_id: {'Mug|2'} if agent_id == 0 else set()
        runtime.snapshot = WorldSnapshot(0, {}, {}, {'robot1': ('Mug|2',), 'robot2': ()}, {o['objectId']: o for o in runtime.current_objects()})
        candidates = [obj('CounterTop|1'), obj('CounterTop|2')]
        runtime.compatible_receptacle_candidates = lambda *a: list(candidates)
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
        self.assertEqual(set(resolved.keys), {'Mug|1', 'Mug|2', 'CounterTop|1'})
        attempted = []
        runtime.navigate_to_object = lambda robot, dest, **kw: attempted.append(dest)
        def reject(*a): raise RuntimeError('cannot place')
        runtime.put_held_object_in_receptacle = reject
        with action_resource_scope(runtime, 'a', resolved):
            candidates.reverse()
            with self.assertRaisesRegex(RuntimeError, 'Could not place held object'):
                runtime.place_held_objects_for_pickup('robot1', 'Mug|1')
        self.assertEqual(attempted, ['CounterTop|1'])

class MovementAdmissionTest(unittest.TestCase):
    def test_direct_motion_step_uses_navigation_scope_before_controller(self):
        from contextlib import contextmanager
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime._navigation_execution_lock = threading.RLock()
        entered = []
        @contextmanager
        def navigation():
            entered.append('navigation')
            try: yield
            finally: entered.append('release')
        runtime.navigation_execution_scope = navigation
        runtime._step_with_retries = lambda *a, **kw: entered.append('controller')
        runtime.step({'action': 'Teleport', 'agentId': 0})
        self.assertEqual(entered, ['navigation', 'controller', 'release'])

    def test_pickup_backoff_keeps_navigation_scope_through_interaction(self):
        from contextlib import contextmanager
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime._navigation_execution_lock = threading.RLock()
        depth, observed = [], []
        @contextmanager
        def navigation():
            depth.append(True)
            try: yield
            finally: depth.pop()
        runtime.navigation_execution_scope = navigation
        runtime.pickup_clip_backoff_position = lambda *a: {}
        runtime.teleport_to_position_direct = lambda *a: observed.append(bool(depth))
        def step(*a, **kw):
            observed.append(bool(depth))
            return SimpleNamespace(metadata={'lastActionSuccess': True})
        runtime.step = step
        runtime.retry_pickup_after_clip_error(0, {'action': 'PickupObject'}, None)
        self.assertEqual(observed, [True, True])

class ConflictPolicyTest(unittest.TestCase):
    def test_wait_retains_request_retry_resolves_on_next_admission(self):
        from tests.snapshot_fakes import FakeRuntime
        from executor_system.action_plan import StagePlan
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.execution_control import install_control
        from executor_system.world_snapshot import SnapshotStore
        for policy in ('WAIT', 'RETRY_NEXT_TICK'):
            runtime = FakeRuntime()
            runtime.objects = [obj('Mug|1'), obj('Mug|2')]
            choice = [runtime.objects[0]]
            runtime.find_object = lambda *a, **kw: choice[0]
            scheduler = StageScheduler(runtime, StagePlan('s', {
                'robot1': [Action('PickupObject', {'args': ('Mug',)}, on_conflict=policy)]}),
                control=install_control(runtime, 1).child(), policy='legacy')
            scheduler.world.snapshot = SnapshotStore().capture(runtime, scheduler.control)
            pending = scheduler.admissions['robot1'].pending
            blocker = scheduler.resource_manager.try_acquire('other', ('Mug|1',))
            self.assertFalse(scheduler._admit_resources(pending))
            choice[0] = runtime.objects[1]
            granted = scheduler._admit_resources(pending)
            self.assertEqual(granted, policy == 'RETRY_NEXT_TICK')
            if granted:
                self.assertEqual(scheduler.resource_requests['0:robot1:0'].keys, ('Mug|2',))
            scheduler._release_resources(pending)
            blocker.release()

    def test_waiting_age_outranks_repeated_high_priority_contender(self):
        from tests.snapshot_fakes import FakeRuntime
        from executor_system.action_plan import StagePlan, AI2ThorAdapter
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.execution_control import install_control
        from unittest.mock import patch
        runtime = FakeRuntime()
        runtime.objects = [obj('Mug|1')]
        action = Action('PickupObject', {'args': ('Mug|1',)}, base_priority=15)
        low = Action('PickupObject', {'args': ('Mug|1',)})
        scheduler = StageScheduler(runtime, StagePlan('s', {'robot1': [action]*8, 'robot2': [low]}),
                                   control=install_control(runtime, 1).child(), policy='legacy')
        order = []
        with patch.object(AI2ThorAdapter, 'execute', lambda adapter, robot, action, **kw: order.append(robot)):
            outcome = scheduler.run()
        self.assertEqual(outcome.status, 'completed')
        self.assertLess(order.index('robot2'), 4)


class SideEffectBoundaryTest(ResourceRuntimeMixin, unittest.TestCase):
    def test_runtime_marks_side_effect_before_controller_and_does_not_replay(self):
        objects = [obj('Mug|1')]
        runtime = self.runtime(objects)
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
        calls = []
        def controller(*a, **kw):
            calls.append('once')
            objects.clear()
            return SimpleNamespace(metadata={'lastActionSuccess': True})
        runtime.controller_lock = threading.RLock()
        runtime.controller = SimpleNamespace(step=controller)
        runtime._commit_world_event = lambda *a: None
        runtime.assert_success = lambda *a: None
        runtime.save_frames = lambda *a: None
        with self.assertRaises(ResourceBindingInvalid) as error:
            with action_resource_scope(runtime, 'a', resolved):
                runtime.step({'action': 'PickupObject', 'objectId': 'Mug|1', 'agentId': 0})
        self.assertNotIsInstance(error.exception, ResourceBindingDeferred)
        self.assertEqual(calls, ['once'])

    def test_vanished_before_first_step_defers_without_controller_call(self):
        objects = [obj('Mug|1')]
        runtime = self.runtime(objects)
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
        calls = []
        runtime._step_with_retries = lambda *a, **kw: calls.append('unexpected')
        with self.assertRaises(ResourceBindingDeferred):
            with action_resource_scope(runtime, 'a', resolved):
                objects.clear()
                runtime.step({'action': 'PickupObject', 'objectId': 'Mug|1', 'agentId': 0})
        self.assertEqual(calls, [])

class DirectSideEffectTest(ResourceRuntimeMixin, unittest.TestCase):
    def test_direct_recovery_cannot_bypass_object_grant(self):
        runtime = self.runtime([obj('Mug|1'), obj('Cabinet|1')])
        runtime.controller_lock = threading.RLock()
        calls = []
        def step(payload):
            calls.append(payload)
            return SimpleNamespace(metadata={'lastActionSuccess': True})
        runtime.controller = SimpleNamespace(step=step)
        runtime._commit_world_event = lambda *a: None
        runtime.assert_success = lambda *a: None
        resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
        with action_resource_scope(runtime, 'a', resolved):
            with self.assertRaises(ResourceBindingDeferred):
                runtime._step_direct({'action': 'CloseObject', 'objectId': 'Cabinet|1'}, save_frame=False)
        self.assertEqual(calls, [])

class AdmittedNavigationRecoveryTest(unittest.TestCase):
    def test_admitted_goto_leases_open_blocker_and_restores_it(self):
        from tests.snapshot_fakes import FakeRuntime
        from executor_system.action_plan import StagePlan, AI2ThorAdapter
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.execution_control import install_control
        from unittest.mock import patch
        class RecoveryRuntime(ThorRuntime, FakeRuntime):
            def __init__(self):
                FakeRuntime.__init__(self)
        runtime = RecoveryRuntime()
        runtime._navigation_execution_lock = threading.RLock()
        runtime.save_frames = lambda *a: None
        drawer = obj('Drawer|1', name='Drawer_454fdaaf', openable=True, isOpen=True)
        runtime.objects = [drawer, obj('Mug|1')]
        runtime.controller.last_event.events[0].metadata['agent']['rotation']['y'] = 90
        calls = []
        def controller_step(payload):
            calls.append(payload['action'])
            event = runtime.controller.last_event
            event.metadata['lastActionSuccess'] = len(calls) != 1
            event.metadata['errorMessage'] = ('Drawer_454fdaaf is blocking Agent 0 from moving by (0.2500, 0.0000, 0.0000).' if len(calls) == 1 else '')
            if payload['action'] in ('CloseObject', 'OpenObject'):
                self.assertEqual(runtime.action_resource_manager.blockers(('Drawer|1',)), ('0:robot1:0',))
                drawer['isOpen'] = payload['action'] == 'OpenObject'
            return event
        runtime.controller.step = controller_step
        stage = StagePlan('recovery', {'robot1': [Action('GoToObject', {'args': ('Mug|1',)})]})
        scheduler = StageScheduler(runtime, stage, control=install_control(runtime, 1).child(), policy='legacy')
        def execute(adapter, robot, action, **kw):
            self.assertEqual(runtime.action_resource_manager.blockers(('Mug|1',)), ())
            self.assertTrue(runtime.move_to_adjacent_position_direct(0, {'x': .25, 'y': 0, 'z': 0}))
        with patch.object(AI2ThorAdapter, 'execute', execute):
            outcome = scheduler.run()
        self.assertEqual(outcome.status, 'completed')
        self.assertEqual(calls, ['MoveAhead', 'CloseObject', 'MoveAhead', 'OpenObject'])
        self.assertTrue(drawer['isOpen'])
        self.assertEqual(runtime.action_resource_manager.holders(), {})
