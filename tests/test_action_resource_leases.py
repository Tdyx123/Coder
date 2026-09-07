"""Atomic action leases and binding regressions without Unity."""
import sys
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.resource_manager import ActionResourceManager
from executor_system.action_resources import resolve_action_resources, action_resource_scope, ResourceBindingInvalid, ResourceBindingDeferred
from executor_system.action_plan import Action
from executor_system.world_snapshot import WorldSnapshot
from executor_system.runtime import ThorRuntime
from executor_system import context
from tests.snapshot_fakes import FakeRuntime as SnapshotFakeRuntime


def obj(object_id, **kw):
    return dict(objectId=object_id, objectType=object_id.split('|')[0], **dict({'mass': 1.0}, **kw))


class ResourceRuntimeMixin:
    def runtime(self, objects):
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime.current_objects = lambda agent_id=None: objects
        runtime.physical_agent_count = 2
        runtime.robot_agent_map = {'robot1': 0, 'robot2': 1}
        runtime.robots = SnapshotFakeRuntime().robots
        runtime.agent_held_objects_for = lambda agent_id: set()
        runtime.snapshot = WorldSnapshot(0, {'robot1': {'x': 0, 'y': 0, 'z': 0}, 'robot2': {'x': 1, 'y': 0, 'z': 0}}, {'robot1': 0, 'robot2': 0}, {'robot1': (), 'robot2': ()}, {o['objectId']: o for o in objects})
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
            runtime.snapshot = replace(runtime.snapshot, held_objects={'robot1': ('Mug|1',) if name == 'PutObject' else (), 'robot2': ()})
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
        runtime.snapshot = replace(runtime.snapshot, resource_metadata={'aliases': runtime.object_alias_bindings})
        original = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('BreakEgg', {'args': ('Egg1',)}))
        lease = runtime.action_resource_manager.try_acquire('a', original.keys)
        objects[:] = [obj('EggCracked|2')]
        runtime._commit_transformation_identities(
            SimpleNamespace(metadata={'lastActionSuccess': True, 'objects': objects}),
            {'action': 'BreakObject', 'objectId': 'Egg|1'}, {'Egg|1': obj('Egg|1')})
        runtime._set_object_alias_current_object('Egg1', objects[0])
        snapshot = WorldSnapshot(1, {'robot1': {'x': 0, 'y': 0, 'z': 0}, 'robot2': {'x': 1, 'y': 0, 'z': 0}}, {'robot1': 0, 'robot2': 0}, {}, {'EggCracked|2': objects[0]})
        changed = resolve_action_resources(runtime, snapshot, 'robot2', Action('PickupObject', {'args': ('EggCracked|2',)}))
        self.assertIsNone(runtime.action_resource_manager.try_acquire('b', changed.keys))
        lease.release()

    def test_delayed_alias_repair_cannot_merge_independent_committed_lineages(self):
        objects = [obj('Apple|1'), obj('Apple|2')]
        runtime = self.runtime(objects)
        runtime.register_object_id_bindings([{'object': 'Apple1', 'object_id': 'Apple|1'}])
        from executor_system.action_resources import manager_for
        manager = manager_for(runtime)
        first = manager.try_acquire('a', ('Apple|1',))
        second = manager.try_acquire('b', ('Apple|2',))
        manager.bind_identity('Apple|1', 'AppleSliced|1')
        manager.bind_identity('Apple|2', 'AppleSliced|2')
        # A delayed best-effort alias repair may select a different descendant;
        # it must never rewrite the already committed lease identity relation.
        runtime._set_object_alias_current_object('Apple1', obj('AppleSliced|2'))
        self.assertEqual(manager.blockers(('AppleSliced|1',)), ('a',))
        self.assertEqual(manager.blockers(('AppleSliced|2',)), ('b',))
        first.release()
        second.release()

    def test_held_by_other_is_not_an_available_resource(self):
        runtime = self.runtime([obj('Mug|1')])
        snapshot = WorldSnapshot(0, {'robot1': {'x': 0, 'y': 0, 'z': 0}, 'robot2': {'x': 1, 'y': 0, 'z': 0}}, {'robot1': 0, 'robot2': 0}, {'robot2': ('Mug|1',)}, {'Mug|1': obj('Mug|1')})
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
        runtime = self.runtime([obj('Mug|1'), obj('Mug|2'),
                                obj('CounterTop|1', position={'x': 0, 'y': 1, 'z': 0}),
                                obj('CounterTop|2', position={'x': 1, 'y': 1, 'z': 0})])
        runtime.agent_held_objects_for = lambda agent_id: {'Mug|2'} if agent_id == 0 else set()
        runtime.snapshot = WorldSnapshot(0, {'robot1': {'x': 0, 'y': 0, 'z': 0}, 'robot2': {'x': 1, 'y': 0, 'z': 0}}, {'robot1': 0, 'robot2': 0}, {'robot1': ('Mug|2',), 'robot2': ()}, {o['objectId']: o for o in runtime.current_objects()})
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
            scheduler = StageScheduler(runtime, StagePlan('s', {
                'robot1': [Action('PickupObject', {'args': ('Mug',)}, on_conflict=policy)]}),
                control=install_control(runtime, 1).child(), policy='legacy')
            scheduler.world.snapshot = SnapshotStore().capture(runtime, scheduler.control)
            pending = scheduler.admissions['robot1'].pending
            blocker = scheduler.resource_manager.try_acquire('other', ('Mug|1',))
            self.assertFalse(scheduler._admit_resources(pending))
            runtime.objects = [obj('Mug|1', visible=False), obj('Mug|2', visible=True)]
            scheduler.world.snapshot = SnapshotStore().capture(runtime, scheduler.control)
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

class AdmissionPublicationRaceTest(unittest.TestCase):
    def runtime(self, objects):
        from tests.test_world_snapshot import snapshot_runtime
        runtime = snapshot_runtime()
        for event in runtime.controller.last_event.events:
            event.metadata['objects'] = [dict(value) for value in objects]
            event.metadata['inventoryObjects'] = []
        runtime.stats_lock = threading.Lock()
        runtime.total_exec = runtime.success_exec = 0
        runtime.save_frames = lambda *a: None
        return runtime

    def test_snapshot_admission_never_waits_for_live_controller_reads(self):
        from executor_system.world_snapshot import SnapshotStore
        from executor_system.execution_control import install_control
        objects = [obj('Mug|1'), obj('Mug|2'), obj('StoveBurner|1'),
                   obj('StoveKnob|1', controlledObjects=['StoveBurner|1']),
                   obj('Faucet|1'), obj('SinkBasin|1'),
                   obj('CounterTop|1', position={'x': 0, 'y': 1, 'z': 0})]
        for action in (Action('PickupObject', {'args': ('Mug|1',)}),
                       Action('FillWater', {'args': ('Sink', 'Mug|1')}),
                       Action('HeatByStoveBurner', {'args': ('StoveBurner', 'Mug|1')})):
            with self.subTest(action=action.action_type):
                runtime = self.runtime(objects)
                runtime.controller.last_event.events[0].metadata['inventoryObjects'] = [{'objectId': 'Mug|2'}]
                control = install_control(runtime, .04)
                snapshot = SnapshotStore().capture(runtime, control)
                locked, release, finished = threading.Event(), threading.Event(), threading.Event()
                results, errors = [], []
                def hold():
                    with runtime.controller_lock:
                        locked.set()
                        release.wait(1)
                def resolve():
                    try: results.append(resolve_action_resources(runtime, snapshot, 'robot1', action))
                    except BaseException as exc: errors.append(exc)
                    finally: finished.set()
                holder = threading.Thread(target=hold)
                reader = threading.Thread(target=resolve)
                holder.start()
                self.assertTrue(locked.wait(.2))
                reader.start()
                try:
                    self.assertTrue(finished.wait(.12), 'snapshot admission blocked on live controller after deadline')
                    self.assertEqual(errors, [])
                    self.assertIn('CounterTop|1', results[0].keys)
                finally:
                    release.set()
                    holder.join(1)
                    reader.join(1)
                self.assertFalse(reader.is_alive())

    def test_transform_identity_is_published_before_delayed_high_level_alias_repair(self):
        from executor_system.world_snapshot import SnapshotStore
        from executor_system.action_resources import manager_for
        for action, source, target in (('BreakObject', 'Egg|1', 'EggCracked|2'),
                                       ('SliceObject', 'Apple|1', 'AppleSliced|2')):
            with self.subTest(action=action):
                runtime = self.runtime([obj(source)])
                token = source.split('|')[0] + '1'
                runtime.register_object_id_bindings([{'object': token, 'object_id': source}])
                manager = manager_for(runtime)
                first = manager.try_acquire('0:robot1:0', (source,))
                runtime.controller.next_event = runtime.controller.last_event
                def controller_step(payload):
                    event = runtime.controller.last_event
                    for agent_event in event.events:
                        agent_event.metadata['objects'] = [obj(target)]
                        agent_event.metadata['lastActionSuccess'] = True
                        agent_event.metadata['errorMessage'] = ''
                    return event
                runtime.controller.step = controller_step
                published, finish = threading.Event(), threading.Event()
                original = runtime.update_object_alias_after_action
                def delayed_alias(*a, **kw):
                    published.set()
                    if not finish.wait(1): raise RuntimeError('test barrier timed out')
                    return original(*a, **kw)
                runtime.update_object_alias_after_action = delayed_alias
                runtime.record_created_slice_object_names = lambda *a: None
                runtime.record_created_broken_egg_object_names = lambda *a: None
                runtime.record_operated_object_name = lambda *a: None
                errors = []
                def transform():
                    try:
                        runtime.object_action_by_object(action, 0, obj(source), goal_object_name=token)
                    except BaseException as exc: errors.append(exc)
                thread = threading.Thread(target=transform)
                thread.start()
                second = None
                try:
                    self.assertTrue(published.wait(.3))
                    snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
                    resolved = resolve_action_resources(runtime, snapshot, 'robot2', Action('PickupObject', {'args': (target,)}))
                    second = manager.try_acquire('0:robot2:0', resolved.keys)
                    self.assertIsNone(second, 'new physical ID escaped source lease before high-level repair')
                    self.assertEqual(manager.holders(), {source: ('0:robot1:0',)})
                finally:
                    finish.set()
                    thread.join(1)
                    if second is not None: second.release()
                    first.release()
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_put_replacement_retains_lease_before_high_level_repair(self):
        from executor_system.world_snapshot import SnapshotStore
        from executor_system.action_resources import manager_for
        source, target, table = 'Mug|1', 'Mug|2', 'Table|1'
        runtime = self.runtime([obj(source), obj(table)])
        runtime.controller.last_event.events[0].metadata['inventoryObjects'] = [{'objectId': source}]
        runtime.register_object_id_bindings([{'object': 'Mug1', 'object_id': source}])
        snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
        resolved = resolve_action_resources(runtime, snapshot, 'robot1', Action('PutObject', {'args': ('Mug1', table)}))
        manager = manager_for(runtime)
        first = manager.try_acquire('0:robot1:0', resolved.keys)
        published, finish = threading.Event(), threading.Event()
        def controller_step(payload):
            event = runtime.controller.last_event
            for agent_event in event.events:
                agent_event.metadata['objects'] = [obj(target, parentReceptacles=[table]), obj(table)]
                agent_event.metadata['inventoryObjects'] = []
                agent_event.metadata['lastActionSuccess'] = True
                agent_event.metadata['errorMessage'] = ''
            return event
        runtime.controller.step = controller_step
        original = runtime.update_object_alias_after_action
        def delayed_alias(*a, **kw):
            published.set()
            if not finish.wait(1): raise RuntimeError('test barrier timed out')
            return original(*a, **kw)
        runtime.update_object_alias_after_action = delayed_alias
        runtime.record_operated_object_name = lambda *a: None
        errors = []
        def put():
            try:
                with action_resource_scope(runtime, '0:robot1:0', resolved):
                    runtime.object_action_by_object('PutObject', 0, obj(table),
                                                  extra_object_resources=(source,), goal_object_name=table)
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=put)
        thread.start()
        second = None
        try:
            self.assertTrue(published.wait(.3))
            snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
            next_resources = resolve_action_resources(runtime, snapshot, 'robot2', Action('PickupObject', {'args': (target,)}))
            second = manager.try_acquire('0:robot2:0', next_resources.keys)
            self.assertIsNone(second, 'PutObject replacement escaped source lease before alias repair')
        finally:
            finish.set()
            thread.join(1)
            if second is not None: second.release()
            first.release()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(runtime.find_object('Mug1')['objectId'], target)

    def test_snapshot_aliases_are_frozen_and_selection_does_not_mutate_live_aliases(self):
        from executor_system.world_snapshot import SnapshotStore
        runtime = self.runtime([obj('Mug|1'), obj('Mug|2')])
        runtime.register_object_id_bindings([{'object': 'Mug1', 'object_id': 'Mug|1'}])
        snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
        with self.assertRaises(TypeError):
            snapshot.resource_metadata['aliases']['Mug1']['object_id'] = 'Mug|2'
        runtime.register_object_id_bindings([{'object': 'Mug1', 'object_id': 'Mug|2'}])
        resolved = resolve_action_resources(runtime, snapshot, 'robot1', Action('PickupObject', {'args': ('Mug1',)}))
        self.assertEqual(resolved.bindings['Mug1'], 'Mug|1')
        self.assertEqual(runtime.object_alias_current_id('Mug1'), 'Mug|2')

    def test_serialized_slice_commits_keep_all_descendants_and_instances_separate(self):
        from executor_system.action_resources import manager_for
        runtime = self.runtime([obj('Apple|1'), obj('Apple|2')])
        manager = manager_for(runtime)
        first = manager.try_acquire('a', ('Apple|1',))
        second = manager.try_acquire('b', ('Apple|2',))
        after = [[obj('AppleSliced|1a'), obj('AppleSliced|1b'), obj('Apple|2')],
                 [obj('AppleSliced|1a'), obj('AppleSliced|1b'), obj('AppleSliced|2')]]
        def step(payload):
            event = runtime.controller.last_event
            objects = after.pop(0)
            for agent_event in event.events:
                agent_event.metadata['objects'] = objects
            return event
        runtime.controller.step = step
        runtime._step_direct({'action': 'SliceObject', 'objectId': 'Apple|1'}, save_frame=False)
        runtime._step_direct({'action': 'SliceObject', 'objectId': 'Apple|2'}, save_frame=False)
        self.assertEqual(manager.blockers(('AppleSliced|1a',)), ('a',))
        self.assertEqual(manager.blockers(('AppleSliced|1b',)), ('a',))
        self.assertEqual(manager.blockers(('AppleSliced|2',)), ('b',))
        first.release()
        second.release()

    def test_failed_step_with_visible_descendant_still_preserves_identity(self):
        from executor_system.action_resources import manager_for
        runtime = self.runtime([obj('Egg|1')])
        manager = manager_for(runtime)
        lease = manager.try_acquire('a', ('Egg|1',))
        def step(payload):
            event = runtime.controller.last_event
            for agent_event in event.events:
                agent_event.metadata['objects'] = [obj('EggCracked|1')]
                agent_event.metadata['lastActionSuccess'] = False
                agent_event.metadata['errorMessage'] = 'response failed after visible transformation'
            return event
        runtime.controller.step = step
        runtime._step_direct({'action': 'BreakObject', 'objectId': 'Egg|1'},
                             check_success=False, save_frame=False)
        self.assertEqual(manager.blockers(('EggCracked|1',)), ('a',))
        lease.release()

    def test_snapshot_selection_uses_requesting_agent_visibility_and_distance(self):
        from executor_system.world_snapshot import SnapshotStore
        from executor_system.action_resources import snapshot_resource_view
        runtime = self.runtime([])
        for agent_id, event in enumerate(runtime.controller.last_event.events):
            event.metadata['objects'] = [
                obj('Mug|1', visible=agent_id == 0, distance=.5 if agent_id == 0 else 5,
                    isDirty=agent_id == 1),
                obj('Mug|2', visible=agent_id == 1, distance=.5 if agent_id == 1 else 5,
                    isDirty=agent_id == 1),
            ]
        # The active agent is robot2; robot1 must retain its own selection
        # evidence while consuming robot2's authoritative current world facts.
        snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
        action = Action('PickupObject', {'args': ('Mug',)})
        first = resolve_action_resources(runtime, snapshot, 'robot1', action)
        second = resolve_action_resources(runtime, snapshot, 'robot2', action)
        self.assertEqual(first.bindings['Mug'], 'Mug|1')
        self.assertEqual(second.bindings['Mug'], 'Mug|2')
        view = snapshot_resource_view(runtime, snapshot)
        self.assertTrue(view.find_object('Mug|1', agent_id=0)['isDirty'])
        with self.assertRaises(TypeError):
            snapshot.resource_metadata['agent_object_selection'][0]['Mug|1']['visible'] = False
        runtime.controller.last_event.events[0].metadata['objects'][0]['visible'] = False
        runtime.controller.last_event.events[0].metadata['objects'][1]['visible'] = True
        unchanged = resolve_action_resources(runtime, snapshot, 'robot1', action)
        self.assertEqual(unchanged.bindings['Mug'], 'Mug|1')

    def test_snapshot_auto_hand_container_uses_requesting_agent_perspective(self):
        from executor_system.world_snapshot import SnapshotStore
        runtime = self.runtime([])
        for agent_id, event in enumerate(runtime.controller.last_event.events):
            event.metadata['objects'] = [
                obj('Mug|target'), obj('Mug|held'),
                obj('CounterTop|1', visible=agent_id == 0, distance=.5 if agent_id == 0 else 5,
                    position={'x': 0, 'y': 1, 'z': 0}),
                obj('CounterTop|2', visible=agent_id == 1, distance=.5 if agent_id == 1 else 5,
                    position={'x': 5, 'y': 1, 'z': 0}),
            ]
        runtime.controller.last_event.events[0].metadata['inventoryObjects'] = [{'objectId': 'Mug|held'}]
        snapshot = SnapshotStore().capture(runtime, runtime.execution_control)
        resources = resolve_action_resources(runtime, snapshot, 'robot1',
                                             Action('PickupObject', {'args': ('Mug|target',)}))
        self.assertEqual(resources.bindings['@hand_receptacle'], 'CounterTop|1')
        self.assertIn('CounterTop|1', resources.keys)
        self.assertNotIn('CounterTop|2', resources.keys)


class AutomaticHandCapabilityTest(ResourceRuntimeMixin, unittest.TestCase):
    def test_clearance_checks_navigation_and_put_before_any_side_effect(self):
        for skills in (['PickupObject'], ['PickupObject', 'GoToObject'], ['PickupObject', 'PutObject']):
            with self.subTest(skills=skills):
                runtime = self.runtime([obj('Mug|1'), obj('Mug|2'), obj('CounterTop|1', position={'x': 0, 'y': 1, 'z': 0})])
                runtime.robots[0]['skills'] = skills
                runtime.agent_held_objects_for = lambda agent: {'Mug|2'} if agent == 0 else set()
                runtime.snapshot = replace(runtime.snapshot, held_objects={'robot1': ('Mug|2',), 'robot2': ()})
                resolved = resolve_action_resources(runtime, runtime.snapshot, 'robot1', Action('PickupObject', {'args': ('Mug|1',)}))
                effects = []
                runtime.navigate_to_object = lambda *args, **kwargs: effects.append('navigate')
                runtime.put_held_object_in_receptacle = lambda *args: effects.append('put') or SimpleNamespace(metadata={'lastActionSuccess': True})
                with action_resource_scope(runtime, 'owner', resolved):
                    with self.assertRaisesRegex(RuntimeError, 'missing_skill'):
                        runtime.place_held_objects_for_pickup('robot1', 'Mug|1')
                self.assertEqual(effects, [])
