"""Behavioral regression cases for bounded step navigation recovery."""
import unittest
from dataclasses import replace
from tests.test_step_movement import navigation_case, thor_position
from tests.test_navigation_batch_failures import make_case, position
from executor_system.movement import MovementConfig, NavigationMetrics, NoInteractionPoseError
from executor_system.step_movement import StepMovementStrategy
from executor_system.runtime import ThorRuntime


class StepNavigationRecoveryTest(unittest.TestCase):
    def strategy(self, runtime, mode='full'):
        config = replace(MovementConfig.resolve('step', environ={}), reachable_refresh_mode=mode)
        return StepMovementStrategy(runtime, config, NavigationMetrics(config.mode))

    def camera(self, runtime, visible_at):
        runtime.horizon = 35.0
        runtime.camera_horizon = lambda agent_id: runtime.horizon
        def look(agent_id, horizon, **kwargs):
            runtime.actions.append(('Look', horizon))
            runtime.horizon = horizon
            return True
        runtime.try_look_to_camera_horizon = look
        original = runtime.find_object
        def find(*args, **kwargs):
            obj = original(*args, **kwargs)
            obj['visible'] = runtime.horizon == visible_at
            return obj
        runtime.find_object = find

    def test_look_recovers_without_moving_to_another_candidate(self):
        for mode in ('full', 'event'):
            with self.subTest(mode=mode):
                runtime, request = navigation_case(walkable=[(0,0), (1,0)], candidates=[(1,0)])
                self.camera(runtime, 45.0)
                result = self.strategy(runtime, mode).navigate(request)
                self.assertEqual(runtime.horizon, 45.0)
                self.assertEqual(result.position, thor_position(.25))
                self.assertTrue(any(e['event'] == 'visibility_recovered' for e in result.decision_trace))

    def test_failed_look_restores_horizon_and_keeps_failure_trace(self):
        runtime, request = navigation_case(walkable=[(0,0), (1,0)], candidates=[(1,0)])
        self.camera(runtime, 999)
        with self.assertRaises(NoInteractionPoseError) as caught:
            self.strategy(runtime).navigate(request)
        self.assertEqual(runtime.horizon, 35.0)
        self.assertEqual(len([a for a in runtime.actions if a[0] == 'Look']), 7)
        self.assertTrue(caught.exception.decision_trace)

    def test_eleventh_candidate_is_tried_once_without_reviving_exclusions(self):
        for mode in ('full', 'event'):
            with self.subTest(mode=mode):
                runtime, request = navigation_case(walkable=[(i,0) for i in range(12)], candidates=[(i,0) for i in range(1,11)])
                runtime.object_visibility_by_position['Apple|1'] = {(i,0): i == 11 for i in range(12)}
                runtime.refresh_navigation_candidates = lambda req, limit: tuple(thor_position(i*.25) for i in range(12))[:limit]
                request = replace(request, excluded_pose_keys=frozenset({(0,0)}))
                result = self.strategy(runtime, mode).navigate(request)
                self.assertEqual(result.position, thor_position(2.75))
                self.assertEqual(sum(e['event'] == 'candidate_expansion' for e in result.decision_trace), 1)
                faces = [a for a in runtime.actions if a[0] == 'Face']
                self.assertEqual(len(faces), 11)

    def test_joint_arrival_failure_preserves_peer_success(self):
        runtime, strategy, requests = make_case(open_grid=True)
        requests = (requests[0], replace(requests[1], candidate_positions=(position(2,4),)))
        runtime.objects['Target|0']['visible'] = False
        result = strategy.coordinator.execute_batch(requests)
        self.assertEqual(set(result.results), {1})
        self.assertEqual(set(result.failed_agent_errors), {0})

    def test_empty_candidate_request_does_not_abort_peer(self):
        runtime, strategy, requests = make_case(open_grid=True)
        requests = (replace(requests[0], candidate_positions=()), replace(requests[1], candidate_positions=(position(2,4),)))
        result = strategy.coordinator.execute_batch(requests)
        self.assertEqual(set(result.results), {1})
        self.assertEqual(set(result.failed_agent_errors), {0})

    def test_expansion_preserves_bound_interaction_target(self):
        runtime, request = navigation_case(walkable=[(0,0),(1,0)], candidates=[(1,0)])
        runtime.teleport_candidate_positions = lambda center, **kwargs: [thor_position(.25)]
        runtime.check_navigation_deadline = lambda: None
        calls = []
        original = runtime.find_object
        def find(target, **kwargs):
            calls.append(target)
            return original(target, **kwargs)
        runtime.find_object = find
        ThorRuntime.refresh_navigation_candidates(runtime, request, 30)
        self.assertEqual(calls, ['Apple|1'])

    def test_timeout_during_look_does_not_restore_or_become_agent_failure(self):
        runtime, request = navigation_case(walkable=[(0,0), (1,0)], candidates=[(1,0)])
        self.camera(runtime, 999)
        calls = []
        def look(*args, **kwargs):
            calls.append(args)
            raise TimeoutError('look timed out')
        runtime.try_look_to_camera_horizon = look
        with self.assertRaisesRegex(TimeoutError, 'look timed out'):
            self.strategy(runtime).navigate(request)
        self.assertEqual(len(calls), 1)

    def test_exhausted_expansion_stops_and_preserves_failure_diagnostics(self):
        runtime, request = navigation_case(walkable=[(0,0), (1,0), (2,0)], candidates=[(1,0)])
        runtime.objects['Apple|1']['visible'] = False
        calls = []
        def refresh(req, limit):
            calls.append(limit)
            return (thor_position(.25), thor_position(.5))
        runtime.refresh_navigation_candidates = refresh
        with self.assertRaises(NoInteractionPoseError) as caught:
            self.strategy(runtime).navigate(request)
        self.assertEqual(calls, [30])
        self.assertEqual(len([a for a in runtime.actions if a[0] == 'Face']), 2)
        self.assertEqual(caught.exception.decision_trace[-1]['event'], 'navigation_failed')

    def test_planning_failure_expands_to_reachable_candidate(self):
        runtime, request = navigation_case(walkable=[(0,0), (1,0), (8,0)], candidates=[(8,0)])
        runtime.refresh_navigation_candidates = lambda req, limit: (thor_position(2), thor_position(.25))
        result = self.strategy(runtime).navigate(request)
        self.assertEqual(result.position, thor_position(.25))
        self.assertTrue(any(e['event'] == 'candidate_expansion' for e in result.decision_trace))

    def test_look_helper_propagates_timeout(self):
        from executor_system.object_interactor import ObjectInteractor
        from types import SimpleNamespace
        def step(*args, **kwargs):
            raise TimeoutError('controller deadline')
        runtime = SimpleNamespace(camera_horizon=lambda aid: 35., step=step,
                                  _object_interaction_settings=lambda: {'log': lambda msg: None})
        with self.assertRaises(TimeoutError):
            ObjectInteractor(runtime).try_look_to_camera_horizon(0, 45., action_name='GoToObject')

    def test_cancellation_during_look_does_not_restore_camera(self):
        from executor_system.execution_control import ExecutionCancelled
        runtime, request = navigation_case(walkable=[(0,0), (1,0)], candidates=[(1,0)])
        self.camera(runtime, 999)
        calls = []
        def look(*args, **kwargs):
            calls.append(args)
            raise ExecutionCancelled('cancelled')
        runtime.try_look_to_camera_horizon = look
        with self.assertRaises(ExecutionCancelled):
            self.strategy(runtime).navigate(request)
        self.assertEqual(len(calls), 1)

    def test_failure_trace_is_serialized_by_runner(self):
        from executor_system.parallel_runner import TolerantRunStats
        from executor_system.action_plan import Action
        runtime, request = navigation_case(walkable=[(0,0), (1,0)], candidates=[(1,0)])
        self.camera(runtime, 999)
        try:
            self.strategy(runtime).navigate(request)
        except NoInteractionPoseError as exc:
            stats = TolerantRunStats()
            stats.record_failure('phase', 'robot1', Action('GoToObject', 'Apple|1'), 0, exc)
        self.assertEqual(stats.robot_failures[0]['navigation_decision_trace'][-1]['event'], 'navigation_failed')

    def test_configuration_rejects_unbounded_or_reversed_candidate_limits(self):
        from executor_system.movement import MovementConfigurationError
        for changes in ({'initial_candidate_limit': 0}, {'expanded_candidate_limit': 9},
                        {'expanded_candidate_limit': 30.5}, {'initial_candidate_limit': True}):
            with self.subTest(changes=changes), self.assertRaises(MovementConfigurationError):
                replace(MovementConfig.resolve('step', environ={}), **changes)

    def test_candidate_refresh_error_is_local_to_empty_request(self):
        runtime, strategy, requests = make_case(open_grid=True)
        requests = (replace(requests[0], candidate_positions=()),
                    replace(requests[1], candidate_positions=(position(2,4),)))
        def refresh(req, limit):
            raise RuntimeError('bound target disappeared')
        runtime.refresh_navigation_candidates = refresh
        result = strategy.coordinator.execute_batch(requests)
        self.assertEqual(set(result.results), {1})
        self.assertIn('bound target disappeared', str(result.failed_agent_errors[0]))

    def test_changed_candidate_set_still_obeys_total_limit(self):
        runtime, request = navigation_case(walkable=[(i,0) for i in range(41)], candidates=[(i,0) for i in range(1,11)])
        runtime.objects['Apple|1']['visible'] = False
        runtime.refresh_navigation_candidates = lambda req, limit: tuple(thor_position(i*.25) for i in range(11,41))
        with self.assertRaises(NoInteractionPoseError):
            self.strategy(runtime).navigate(request)
        self.assertEqual(len([a for a in runtime.actions if a[0] == 'Face']), 30)

    def test_event_target_refresh_failure_does_not_abort_peer(self):
        runtime, strategy, requests = make_case(open_grid=True, config_changes={'reachable_refresh_mode': 'event'})
        requests = (requests[0], replace(requests[1], candidate_positions=(position(2,4),)))
        runtime.objects['Target|0']['position'] = position(3,0)
        def build(robot, target, **kwargs):
            raise RuntimeError('target refresh failed')
        runtime.build_navigation_request = build
        result = strategy.coordinator.execute_batch(requests)
        self.assertEqual(set(result.results), {1})
        self.assertIn('target refresh failed', str(result.failed_agent_errors[0]))

    def test_expansion_can_supplement_thirty_global_positions(self):
        runtime = ThorRuntime.__new__(ThorRuntime)
        runtime.reachable_positions = [thor_position(0)]
        runtime.global_reachable_positions = [thor_position(i*.25) for i in range(31)]
        runtime.scene_object_bounds = lambda aid: ()
        runtime.teleport_position_clear_of_scene_objects = lambda *args: True
        runtime.check_navigation_deadline = lambda: None
        runtime.refresh_reachable_positions = lambda aid: None
        runtime.find_object = lambda *args, **kwargs: {'position': thor_position(0)}
        _, request = navigation_case(walkable=[(0,0)], candidates=[(0,0)])
        self.assertEqual(len(runtime.refresh_navigation_candidates(request, 30)), 30)

    def test_midpath_event_refresh_failure_stops_failed_robot(self):
        runtime, strategy, requests = make_case(open_grid=True, config_changes={'reachable_refresh_mode': 'event'})
        requests = (replace(requests[0], candidate_positions=(position(4,0),)),
                    replace(requests[1], candidate_positions=(position(4,4),)))
        runtime.objects_after_successful_moves[1] = {
            key: dict(obj) for key, obj in runtime.objects.items()}
        runtime.objects_after_successful_moves[1]['Target|0']['position'] = position(3,1)
        def build(robot, target, **kwargs):
            raise RuntimeError('midpath target refresh failed')
        runtime.build_navigation_request = build
        result = strategy.coordinator.execute_batch(requests)
        self.assertEqual(set(result.results), {1})
        self.assertIn('midpath target refresh failed', str(result.failed_agent_errors[0]))
        self.assertEqual(len([a for a in runtime.actions if a[0] == 'MoveAhead' and a[1] == 0]), 1)
