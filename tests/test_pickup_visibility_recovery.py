"""Pickup recovery must change physical pose without changing the bound object."""
import unittest
from contextlib import contextmanager

from tests.test_executor_retry_policy import object_action_runtime, FakeEvent
from executor_system import runtime as runtime_module
from executor_system.plan_types import PlannedAction
from executor_system.movement import NoInteractionPoseError
from executor_system.execution_control import ExecutionCancelled
from tests import test_navigation_execution_scope as scope_tests

VISIBILITY = runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR
CLIP = 'Picking up object would cause it to collide and clip into something!'
TARGET = 'Apple|0'


def pickup_case(*, first_error=VISIBILITY, final_error='', throws=False):
    runtime, calls = object_action_runtime([
        {'objectId': TARGET, 'objectType': 'Apple', 'name': 'Apple',
         'visible': True, 'distance': 1.0,
         'position': {'x': .5, 'y': .9, 'z': 0}},
    ])
    runtime.configure_movement('step', environ={})
    base_step = runtime.step
    navigation = []

    def navigate(robot, target, **kwargs):
        navigation.append((robot, target, kwargs))
        runtime._test_state['position']['x'] = .25

    def step(payload, **kwargs):
        if payload['action'] != 'PickupObject':
            return base_step(payload, **kwargs)
        calls.append(dict(payload))
        error = final_error if navigation else first_error
        if error and throws:
            raise RuntimeError(error)
        metadata = runtime.agent_event(0).metadata
        metadata.update(lastActionSuccess=not bool(error), errorMessage=error)
        return FakeEvent(metadata=metadata)

    runtime.navigate_to_object = navigate
    runtime.step = step
    return runtime, calls, navigation


class PickupVisibilityRecoveryTests(unittest.TestCase):
    def pickup(self, runtime):
        return runtime.object_action('PickupObject', 'robot1', TARGET)

    def test_visibility_failure_repositions_same_target_and_counts_one_action(self):
        for throws in (False, True):
            with self.subTest(throws=throws):
                runtime, calls, navigation = pickup_case(throws=throws)
                try:
                    event = self.pickup(runtime)
                except RuntimeError as exc:
                    self.fail(f'Pickup did not recover by changing pose: {exc}')
                self.assertTrue(event.metadata['lastActionSuccess'])
                self.assertEqual(len(navigation), 1)
                robot, target, kwargs = navigation[0]
                self.assertEqual((robot, target), ('robot1', TARGET))
                self.assertEqual(kwargs['next_action'], PlannedAction('PickupObject', (TARGET,)))
                self.assertFalse(kwargs['allow_hand_preparation'])
                self.assertTrue(kwargs['exclude_current_position'])
                self.assertNotIn('phase_coordinator', kwargs)
                pickups = [c for c in calls if c['action'] == 'PickupObject']
                self.assertEqual(len(pickups), 8)
                self.assertTrue(all(c['objectId'] == TARGET and not c.get('forceAction') for c in pickups))
                self.assertFalse(any(c['action'].startswith('Teleport') for c in calls))
                self.assertEqual((runtime.total_exec, runtime.success_exec), (1, 1))

    def test_missing_camera_horizon_still_repositions(self):
        runtime, calls, navigation = pickup_case()
        runtime.camera_horizon = lambda *args: None
        try:
            self.pickup(runtime)
        except RuntimeError as exc:
            self.fail(f'Missing camera metadata prevented pose recovery: {exc}')
        self.assertEqual(len(navigation), 1)
        self.assertEqual(sum(c['action'] == 'PickupObject' for c in calls), 2)

    def test_look_success_does_not_reposition(self):
        runtime, calls, navigation = pickup_case()
        step = runtime.step
        def visible_step(payload, **kwargs):
            if payload['action'] == 'PickupObject' and runtime._test_state['horizon'] == 25:
                return FakeEvent()
            return step(payload, **kwargs)
        runtime.step = visible_step
        self.pickup(runtime)
        self.assertEqual(navigation, [])
        self.assertEqual(runtime._test_state['horizon'], 25)

    def test_clip_recovery_uses_step_and_shared_budget(self):
        for first, final in ((CLIP, ''), (CLIP, VISIBILITY), (VISIBILITY, CLIP)):
            with self.subTest(first=first, final=final):
                runtime, calls, navigation = pickup_case(first_error=first, final_error=final)
                if final:
                    with self.assertRaises(RuntimeError):
                        self.pickup(runtime)
                else:
                    self.pickup(runtime)
                self.assertFalse(any(c['action'].startswith('Teleport') for c in calls))
                self.assertEqual(len(navigation), 1)
                self.assertEqual(runtime.success_exec, int(not final))

    def test_failed_reposition_preserves_original_error_and_reason(self):
        runtime, calls, navigation = pickup_case()
        def fail(*args, **kwargs):
            raise NoInteractionPoseError('no candidate away from current pose')
        runtime.navigate_to_object = fail
        with self.assertRaises(RuntimeError) as caught:
            self.pickup(runtime)
        self.assertIn(VISIBILITY, str(caught.exception))
        self.assertIn('no candidate away from current pose', str(caught.exception))
        self.assertEqual((runtime.total_exec, runtime.success_exec), (1, 0))

    def test_remaining_visibility_failure_stops_after_one_reposition(self):
        runtime, calls, navigation = pickup_case(final_error=VISIBILITY)
        with self.assertRaisesRegex(RuntimeError, 'specified visibility'):
            self.pickup(runtime)
        self.assertEqual(len(navigation), 1)
        self.assertEqual(sum(c['action'] == 'PickupObject' for c in calls), 8)
        self.assertEqual(runtime.success_exec, 0)

    def test_original_failure_context_survives_camera_retries(self):
        runtime, calls, navigation = pickup_case(first_error=VISIBILITY + ' [original context]')
        original = runtime.step
        def step(payload, **kwargs):
            if payload['action'] == 'PickupObject' and calls:
                return FakeEvent(False, VISIBILITY + ' [last retry context]')
            return original(payload, **kwargs)
        def fail(*args, **kwargs):
            raise NoInteractionPoseError('no alternate pose')
        runtime.step, runtime.navigate_to_object = step, fail
        with self.assertRaises(RuntimeError) as caught:
            self.pickup(runtime)
        self.assertIn('[original context]', str(caught.exception))
        self.assertIn('no alternate pose', str(caught.exception))

    def test_budget_resets_for_next_planned_pickup(self):
        runtime, calls, navigation = pickup_case(final_error=VISIBILITY)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.pickup(runtime)
        self.assertEqual(len(navigation), 2)

    def test_non_visibility_failure_does_not_navigate(self):
        runtime, calls, navigation = pickup_case(first_error='hand is occupied')
        with self.assertRaisesRegex(RuntimeError, 'hand is occupied'):
            self.pickup(runtime)
        self.assertEqual(navigation, [])
        self.assertEqual(len(calls), 1)

    def test_teleport_mode_retains_visibility_behavior(self):
        runtime, calls, navigation = pickup_case()
        runtime.configure_movement('teleport', environ={})
        with self.assertRaisesRegex(RuntimeError, 'specified visibility'):
            self.pickup(runtime)
        self.assertEqual(navigation, [])

    def test_teleport_clip_exception_exhaustion_keeps_original_retry_limit(self):
        runtime, calls, navigation = pickup_case(first_error=CLIP, throws=True)
        runtime.configure_movement('teleport', environ={})
        with self.assertRaisesRegex(RuntimeError, 'collide and clip'):
            self.pickup(runtime)
        self.assertEqual(sum(c['action'] == 'Teleport' for c in calls), 3)
        self.assertEqual(sum(c['action'] == 'PickupObject' for c in calls), 4)
        self.assertEqual(navigation, [])

    def test_camera_metadata_timeout_does_not_start_reposition(self):
        runtime, calls, navigation = pickup_case()
        error = TimeoutError('metadata timed out')
        def fail(*args):
            raise error
        runtime.agent_event = fail
        runtime.current_agent_position = lambda _agent: {'x': 0, 'y': .9, 'z': 0}
        with self.assertRaises(TimeoutError) as caught:
            runtime.retry_object_action_after_target_visibility_error(
                'PickupObject', 0,
                {'action': 'PickupObject', 'agentId': 0, 'objectId': TARGET},
                FakeEvent(False, VISIBILITY),
            )
        self.assertIs(caught.exception, error)
        self.assertEqual(navigation, [])

    def test_reposition_requires_navigation_capability(self):
        runtime, calls, navigation = pickup_case()
        runtime.robots = [{'name': 'robot1', 'skills': ['PickupObject'], 'mass_capacity': 1}]
        with self.assertRaisesRegex(RuntimeError, 'missing_skill'):
            self.pickup(runtime)
        self.assertEqual(navigation, [])

    def test_reposition_and_final_pickup_share_execution_scope(self):
        runtime, calls, navigation = pickup_case()
        depth = [0]
        @contextmanager
        def scope():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1
        runtime.navigation_execution_scope = scope
        navigate, step = runtime.navigate_to_object, runtime.step
        def scoped_navigation(*args, **kwargs):
            self.assertGreater(depth[0], 0)
            return navigate(*args, **kwargs)
        def scoped_step(payload, **kwargs):
            if navigation and payload['action'] == 'PickupObject':
                self.assertGreater(depth[0], 0)
            return step(payload, **kwargs)
        runtime.navigate_to_object, runtime.step = scoped_navigation, scoped_step
        try:
            self.pickup(runtime)
        except RuntimeError as exc:
            self.fail(str(exc))
        self.assertEqual(depth[0], 0)

    def test_timeout_during_look_exits_without_restore_or_reposition(self):
        runtime, calls, navigation = pickup_case()
        looks = []
        def fail(*args, **kwargs):
            looks.append(args)
            raise TimeoutError('camera timed out')
        runtime.try_look_to_camera_horizon = fail
        with self.assertRaisesRegex(TimeoutError, 'camera timed out'):
            self.pickup(runtime)
        self.assertEqual(len(looks), 1)
        self.assertEqual(navigation, [])

    def test_timeout_during_reposition_propagates_and_releases_budget(self):
        runtime, calls, navigation = pickup_case()
        original = runtime.navigate_to_object
        def fail(*args, **kwargs):
            raise TimeoutError('navigation timed out')
        runtime.navigate_to_object = fail
        with self.assertRaisesRegex(TimeoutError, 'navigation timed out'):
            self.pickup(runtime)
        runtime.navigate_to_object = original
        self.pickup(runtime)
        self.assertEqual(runtime.success_exec, 1)

    def test_final_pickup_timeout_and_cancellation_propagate_unchanged(self):
        for error in (TimeoutError('pickup timed out'), ExecutionCancelled('cancelled')):
            with self.subTest(error=type(error).__name__):
                runtime, calls, navigation = pickup_case()
                original = runtime.step
                def fail(payload, **kwargs):
                    if navigation and payload['action'] == 'PickupObject':
                        raise error
                    return original(payload, **kwargs)
                runtime.step = fail
                with self.assertRaises(type(error)) as caught:
                    self.pickup(runtime)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(navigation), 1)
                self.assertEqual(runtime.success_exec, 0)
                self.assertEqual(runtime._interaction_reposition_state.active_keys, set())

    def test_cancelled_look_does_not_restore_camera(self):
        runtime, calls, navigation = pickup_case()
        error = ExecutionCancelled('cancelled')
        looks = []
        def fail(*args, **kwargs):
            looks.append(args)
            raise error
        runtime.try_look_to_camera_horizon = fail
        with self.assertRaises(ExecutionCancelled) as caught:
            self.pickup(runtime)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(looks), 1)
        self.assertEqual(navigation, [])

    def test_ordinary_retry_error_restores_camera_for_existing_modes(self):
        for mode, action in [('teleport', 'PickupObject'), ('step', 'SliceObject')]:
            with self.subTest(mode=mode, action=action):
                runtime, calls, navigation = pickup_case()
                runtime.configure_movement(mode, environ={})
                original = runtime.step
                error = RuntimeError('ordinary interaction failure')
                def fail(payload, **kwargs):
                    if payload['action'] == action:
                        raise error
                    return original(payload, **kwargs)
                runtime.step = fail
                with self.assertRaises(RuntimeError) as caught:
                    runtime.retry_object_action_after_target_visibility_error(
                        action, 0, {'action': action, 'agentId': 0, 'objectId': TARGET},
                        FakeEvent(False, VISIBILITY),
                    )
                self.assertIs(caught.exception, error)
                self.assertEqual(runtime._test_state['horizon'], 35)
                self.assertEqual(navigation, [])

    def test_clip_during_look_retains_initial_visibility_error(self):
        for throws in (False, True):
            with self.subTest(throws=throws):
                runtime, calls, navigation = pickup_case()
                original = runtime.step
                def step(payload, **kwargs):
                    if payload['action'] == 'PickupObject' and runtime._test_state['horizon'] == 25:
                        if throws:
                            raise RuntimeError(CLIP)
                        return FakeEvent(False, CLIP)
                    return original(payload, **kwargs)
                def fail(*args, **kwargs):
                    raise NoInteractionPoseError('no alternate pose')
                runtime.step = step
                runtime.navigate_to_object = fail
                with self.assertRaises(RuntimeError) as caught:
                    self.pickup(runtime)
                self.assertIn(VISIBILITY, str(caught.exception))
                self.assertIn('no alternate pose', str(caught.exception))
                self.assertFalse(any(c['action'].startswith('Teleport') for c in calls))


class PickupNavigationScopeTests(unittest.TestCase):
    """Exercise the shared recovery with real grid planning and competing threads."""
    start_worker = scope_tests.NavigationExecutionScopeTests.start_worker
    finish_workers = scope_tests.NavigationExecutionScopeTests.finish_workers
    test_serialized_through_final_pickup = scope_tests.NavigationExecutionScopeTests.test_recoveries_are_serialized_through_final_slice
    test_waits_for_normal_batch = scope_tests.NavigationExecutionScopeTests.test_recovery_waits_for_normal_navigation_batch
    test_deadline_between_microsteps = scope_tests.NavigationExecutionScopeTests.test_expired_deadline_stops_before_next_microstep_and_releases_lock

    def recover(self, runtime, agent_id):
        return runtime._get_object_interactor().retry_object_action_after_interaction_reposition(
            'PickupObject', agent_id,
            {'action': 'PickupObject', 'agentId': agent_id, 'objectId': f'Tomato|{agent_id}'},
            FakeEvent(False, VISIBILITY),
        )
