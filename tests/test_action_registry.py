import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.action_plan import Action, AI2ThorAdapter, StagePlan, TaskPlan
from executor_system.action_registry import ActionRegistry
from executor_system.plan_validator import PlanValidator
from executor_system.capability_checks import capability_failure, finite_nonnegative_number, normalize_skill_name
from executor_system.execution_control import ensure_control
from executor_system.world_snapshot import SnapshotStore
from tests.snapshot_fakes import FakeRuntime


HELPERS = {
    0: 'WaitOneTick ThrowObject WaitUntil',
    1: 'GoToObject PickupObject TeleportObjectToHand SwitchOn SwitchOff OpenObject CloseObject BreakObject BreakEgg SliceObject CleanObject DirtyObject EmptyLiquid',
    2: 'PutObject PrepareEgg RunMicrowave RunCoffeeMachine RunToaster HeatByStoveBurner FireByStoveBurner FillWater ColdObject',
    3: 'CookByStoveBurner',
}


def runtime_with_objects():
    runtime = FakeRuntime()
    runtime.robots = [{'name': 'robot1', 'skills': ['PickupObject', 'PutObject', 'GoToObject', 'OpenObject', 'FillWater'], 'mass_capacity': 2},
                      {'name': 'robot2', 'skills': ['PickupObject'], 'mass_capacity': 2}]
    runtime.objects = [dict(objectId='Apple|1', name='Apple', objectType='Apple', mass=1, pickupable=True),
                       dict(objectId='CounterTop|1', name='CounterTop', objectType='CounterTop', receptacle=True),
                       dict(objectId='SinkBasin|1', name='SinkBasin', objectType='SinkBasin', receptacle=True),
                       dict(objectId='Faucet|1', name='Faucet', objectType='Faucet', toggleable=True)]
    return runtime


def snapshot(runtime):
    return SnapshotStore().capture(runtime, ensure_control(runtime))


class ActionRegistryTest(unittest.TestCase):
    def test_direct_pickup_is_not_dispatched_as_empty_helper(self):
        normalized = ActionRegistry().normalize(Action.from_any({'action': 'PickupObject', 'objectId': 'Apple|1'}))
        self.assertEqual(normalized.form, 'thor')
        self.assertEqual(normalized.parameters['objectId'], 'Apple|1')

    def test_missing_pickup_target_fails_before_execution(self):
        with self.assertRaisesRegex(ValueError, 'PickupObject.*target'):
            ActionRegistry().validate_shape(Action('PickupObject'))

    def test_helper_arity_table(self):
        for arity, names in HELPERS.items():
            for name in names.split():
                args = ('Egg', 'CounterTop', 'Apple')[:arity]
                with self.subTest(name=name):
                    self.assertEqual(ActionRegistry().normalize(Action(name, {'args': args})).form, 'helper')
                    for invalid in (args + ('extra',), args[:-1] if arity else ('extra',)):
                        with self.assertRaises((ValueError, RuntimeError)):
                            ActionRegistry().validate_shape(Action(name, {'args': invalid}))

    def test_direct_forms(self):
        for name in 'PickupObject PutObject OpenObject CloseObject BreakObject SliceObject CleanObject DirtyObject ToggleObjectOn ToggleObjectOff'.split():
            with self.subTest(name=name):
                self.assertEqual(ActionRegistry().normalize(Action(name, {'objectId': 'Apple|1'})).form, 'thor')
        for name in 'MoveAhead RotateLeft RotateRight LookUp LookDown Pass Wait Done ThrowObject'.split():
            with self.subTest(name=name):
                ActionRegistry().validate_shape(Action(name))

    def test_invalid_shape(self):
        cases = [Action('Unknown'), Action('PickupObject', {'args': ('Apple',), 'objectId': 'Mug|1'}),
                 Action('PutObject', {'args': ('Apple', 'CounterTop'), 'objectId': 'Apple'}),
                 Action('Teleport'), Action('Teleport', {'position': {'x': 0, 'y': 0, 'z': float('nan')}}),
                 Action('MoveAhead', {'moveMagnitude': float('inf')}), Action('LookUp', {'degrees': True}),
                 Action('BreakEgg', {'args': ('Apple',)}), Action('PrepareEgg', {'args': ('Apple', 'Bowl')})]
        for action in cases:
            with self.subTest(action=action), self.assertRaises((ValueError, RuntimeError)):
                ActionRegistry().validate_shape(action)

    def test_agent_override_rejected(self):
        runtime = runtime_with_objects()
        with self.assertRaisesRegex(ValueError, 'agentId'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('Wait', {'agentId': 1}))

    def test_direct_put_checks_and_leases_held_object(self):
        runtime = runtime_with_objects()
        action = Action('PutObject', {'objectId': 'CounterTop|1'})
        with self.assertRaisesRegex(RuntimeError, 'holding'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', action)
        runtime.agent_event(0).metadata['inventoryObjects'] = [{'objectId': 'Apple|1'}]
        prepared = ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', action)
        self.assertEqual(set(prepared.resources.keys), {'Apple|1', 'CounterTop|1'})

    def test_pickup_capability_uses_snapshot_mass(self):
        runtime = runtime_with_objects()
        action = Action('PickupObject', {'objectId': 'Apple|1'})
        runtime.robots[0]['mass_capacity'] = .5
        with self.assertRaisesRegex(RuntimeError, 'mass_exceeded'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', action)
        runtime.robots[0]['skills'] = []
        with self.assertRaisesRegex(RuntimeError, 'missing_skill'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', action)

    def test_fillwater_binds_implicit_faucet(self):
        runtime = runtime_with_objects()
        prepared = ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('FillWater', {'args': ('SinkBasin', 'Apple')}))
        self.assertEqual(set(prepared.resources.keys), {'SinkBasin|1', 'Faucet|1', 'Apple|1'})
        self.assertEqual(prepared.resources.bindings['@faucet'], 'Faucet|1')

    def test_adapter_direct_pickup_submits_correct_payload(self):
        runtime = runtime_with_objects()
        payloads = []
        runtime.step = lambda payload, **kwargs: payloads.append(payload) or payload
        AI2ThorAdapter(runtime).execute('robot1', Action('PickupObject', {'objectId': 'Apple|1'}))
        self.assertEqual(payloads, [{'action': 'PickupObject', 'objectId': 'Apple|1', 'agentId': 0}])

    def test_scene_errors_locate_queue_cursor(self):
        runtime = runtime_with_objects()
        for robot, action in [('robot9', Action('Wait')), ('robot1', Action('PickupObject', {'args': ('Missing',)}))]:
            with self.subTest(robot=robot), self.assertRaisesRegex((ValueError, RuntimeError), 'stage.*s.*robot.*cursor.*0'):
                PlanValidator(runtime).validate(TaskPlan('t', [StagePlan('s', {robot: [action]})]))

    def test_scene_validation_allows_future_put(self):
        runtime = runtime_with_objects()
        PlanValidator(runtime).validate(TaskPlan('t', [StagePlan('s', {'robot1': [Action('PickupObject', {'args': ('Apple',)}), Action('PutObject', {'args': ('Apple', 'CounterTop')})]})]))

    def test_nested_pickup_rechecks_mass_and_skill(self):
        from executor_system.action_resources import ActionResourceScope, ResolvedActionResources
        runtime = runtime_with_objects()
        scope = ActionResourceScope(runtime, 'owner', ResolvedActionResources(('Apple|1',), {'Apple|1': 'Apple|1'}))
        runtime.robots[0]['mass_capacity'] = .1
        with self.assertRaisesRegex(RuntimeError, 'mass_exceeded'):
            scope.before_step({'action': 'PickupObject', 'agentId': 0, 'objectId': 'Apple|1'})
        runtime.robots[0]['skills'] = []
        with self.assertRaisesRegex(RuntimeError, 'missing_skill'):
            scope.before_step({'action': 'PickupObject', 'agentId': 0, 'objectId': 'Apple|1'})
        self.assertFalse(scope.effects_started)

    def test_shared_capability_rules(self):
        self.assertEqual(normalize_skill_name('PrepareEgg'), normalize_skill_name('BreakEgg'))
        for bad in (True, None, -1, float('nan'), float('inf')):
            self.assertIsNone(finite_nonnegative_number(bad))
        self.assertEqual(capability_failure({'skills': []}, 'PickupObject')['reason'], 'missing_skill')
        self.assertIsNone(capability_failure({}, 'Wait'))

    def test_registry_can_be_imported_before_action_plan(self):
        import subprocess
        completed = subprocess.run([sys.executable, '-c',
            'from executor_system.action_registry import ActionRegistry; ActionRegistry()'],
            cwd=Path(__file__).resolve().parents[1] / 'scripts', capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_teleport_angles_are_finite(self):
        for extra in ({'rotation': {'x': 0, 'y': float('inf'), 'z': 0}}, {'horizon': float('nan')}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ActionRegistry().normalize(Action('Teleport', dict(position={'x': 0, 'y': 0, 'z': 0}, **extra)))

    def test_shape_validation_precedes_controller_creation(self):
        from executor_system import generated_plan_runtime as generated
        from types import SimpleNamespace
        from unittest.mock import patch
        bundle = SimpleNamespace(task_plan=TaskPlan('t', [StagePlan('s', {'robot1': [Action('PickupObject')]})]),
                                 object_mapping_warnings=[], noop_subtasks=[])
        with patch.object(generated, '_runtime_inputs', return_value=({}, 1, [], [], bundle)), \
             patch.object(generated, 'ThorRuntime', side_effect=AssertionError('controller was created')):
            with self.assertRaisesRegex(ValueError, 'PickupObject.*target'):
                generated.run_standalone({}, 'unused', 0)

    def test_navigation_forwards_following_action_and_wave(self):
        from executor_system.action_resources import action_resource_scope, ResolvedActionResources
        runtime = runtime_with_objects()
        calls = []
        runtime.navigate_to_object = lambda *args, **kwargs: calls.append((args, kwargs)) or 'arrived'
        coordinator, wave = object(), object()
        with action_resource_scope(runtime, 'owner', ResolvedActionResources((), {})):
            result = AI2ThorAdapter(runtime).execute('robot1', Action('GoToObject', {'args': ('Apple',)}),
                next_action=Action('PickupObject', {'args': ('Apple',)}), phase_coordinator=coordinator, action_wave=wave)
        self.assertEqual(result, 'arrived')
        self.assertEqual(calls[0][0], ('robot1', 'Apple'))
        self.assertEqual(calls[0][1]['next_action'].args, ('Apple',))
        self.assertIs(calls[0][1]['phase_coordinator'], coordinator)
        self.assertIs(calls[0][1]['action_wave'], wave)

    def test_direct_object_id_is_a_concrete_snapshot_id(self):
        runtime = runtime_with_objects()
        with self.assertRaisesRegex((ValueError, RuntimeError), 'objectId'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('PickupObject', {'objectId': 'Apple'}))
        for target in (12, [], {}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                ActionRegistry().validate_shape(Action('PickupObject', {'objectId': target}))

    def test_navigation_target_missing_at_admission_fails_without_step(self):
        runtime = runtime_with_objects()
        with self.assertRaisesRegex(RuntimeError, 'Missing'):
            ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('GoToObject', {'args': ('Missing',)}))
        self.assertEqual(runtime.state_version, 0)

    def test_flat_direct_payload_rejects_nonfinite_angles_and_throw(self):
        for payload in (
            {'action': 'Teleport', 'position': {'x': 0, 'y': 0, 'z': 0}, 'rotation': {'x': 0, 'y': float('inf'), 'z': 0}},
            {'action': 'Teleport', 'position': {'x': 0, 'y': 0, 'z': 0}, 'horizon': float('nan')},
            {'action': 'ThrowObject', 'throwMagnitude': float('inf')},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ActionRegistry().normalize(Action.from_any(payload))

    def test_flat_direct_payload_preserves_angles_and_throw_on_submission(self):
        runtime = runtime_with_objects()
        runtime.robots[0]['skills'].extend(['Teleport', 'ThrowObject'])
        payloads = []
        runtime.step = lambda payload, **kwargs: payloads.append(payload) or payload
        for payload in (
            {'action': 'Teleport', 'position': {'x': 1, 'y': 0, 'z': 2}, 'rotation': {'x': 0, 'y': 90, 'z': 0}, 'horizon': 30, 'standing': True},
            {'action': 'ThrowObject', 'throwMagnitude': 12},
        ):
            with self.subTest(payload=payload):
                AI2ThorAdapter(runtime).execute('robot1', Action.from_any(payload))
                self.assertEqual(payloads[-1], dict(payload, agentId=0))

    def test_blocker_recovery_checks_close_and_restoration_before_submission(self):
        from types import SimpleNamespace
        from executor_system.runtime import ThorRuntime
        from executor_system.action_resources import action_resource_scope
        for skills in (['GoToObject'], ['GoToObject', 'CloseObject'], ['GoToObject', 'OpenObject']):
            with self.subTest(skills=skills):
                runtime = runtime_with_objects()
                runtime.robots[0]['skills'] = skills
                blocker = dict(objectId='Cabinet|1', name='Cabinet_1', objectType='Cabinet', openable=True, isOpen=True)
                runtime.objects = runtime.objects + [blocker]
                prepared = ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('GoToObject', {'args': ('Apple',)}))
                runtime.find_object = lambda *args, **kwargs: blocker
                calls = []
                runtime._step_direct = lambda payload, **kwargs: calls.append(payload) or SimpleNamespace(metadata={'lastActionSuccess': True})
                failed = SimpleNamespace(metadata={'errorMessage': 'Cabinet_1 is blocking Agent 0 from moving by (0.2500, 0.0000, 0.0000).'})
                with action_resource_scope(runtime, 'owner', prepared.resources):
                    with self.assertRaisesRegex(RuntimeError, 'missing_skill'):
                        ThorRuntime.retry_move_past_open_object_blocker(runtime, 0, {'action': 'MoveAhead', 'agentId': 0}, failed)
                self.assertEqual(calls, [])
                self.assertTrue(blocker['isOpen'])

    def test_compound_declared_skill_does_not_require_normal_primitive_skills(self):
        from executor_system.action_resources import action_resource_scope
        runtime = runtime_with_objects()
        runtime.robots[0]['skills'] = ['RunMicrowave']
        runtime.objects = runtime.objects + [dict(objectId='Microwave|1', objectType='Microwave', openable=True, isOpen=False)]
        prepared = ActionRegistry().prepare(runtime, snapshot(runtime), 'robot1', Action('RunMicrowave', {'args': ('Microwave', 'Apple')}))
        with action_resource_scope(runtime, 'owner', prepared.resources) as scope:
            scope.before_step({'action': 'OpenObject', 'objectId': 'Microwave|1', 'agentId': 0})
            self.assertTrue(scope.effects_started)
