import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.movement import (
    MovementConfig,
    NavigationMetrics,
    NavigationRequest,
    NoInteractionPoseError,
    StepNavigationError,
    create_movement_strategy,
)
from executor_system.step_movement import StepMovementStrategy
from executor_system.utils import position_to_grid_key
from tests.movement_fakes import GridThorRuntime


def thor_position(x, z=0.0):
    return {"x": float(x), "y": 0.0, "z": float(z)}


def single_navigation_request(candidate_x=0.5):
    destination = {
        "objectId": "Apple|1",
        "objectType": "Apple",
        "visible": True,
        "position": {"x": 0.75, "y": 0.9, "z": 0.0},
    }
    request = NavigationRequest(
        robot="robot1",
        agent_id=0,
        dest_obj="Apple|1",
        destination=dict(destination),
        center=dict(destination["position"]),
        candidate_positions=(thor_position(candidate_x),),
        object_resource="Apple|1",
        next_action=None,
        phase_coordinator=None,
    )
    runtime = GridThorRuntime(
        positions={0: thor_position(0.0)},
        walkable_by_agent={
            0: [thor_position(0.0), thor_position(0.25), thor_position(0.5)]
        },
        objects=[destination],
    )
    return runtime, request


def navigation_case(*, walkable, candidates, start=(0, 0)):
    destination = {
        "objectId": "Apple|1",
        "objectType": "Apple",
        "visible": True,
        "position": {
            "x": (candidates[-1][0] + 1) * 0.25,
            "y": 0.9,
            "z": candidates[-1][1] * 0.25,
        },
    }
    request = NavigationRequest(
        robot="robot1",
        agent_id=0,
        dest_obj="Apple|1",
        destination=dict(destination),
        center=dict(destination["position"]),
        candidate_positions=tuple(
            thor_position(x * 0.25, z * 0.25)
            for x, z in candidates
        ),
        object_resource="Apple|1",
        next_action=None,
        phase_coordinator=None,
    )
    runtime = GridThorRuntime(
        positions={0: thor_position(start[0] * 0.25, start[1] * 0.25)},
        walkable_by_agent={
            0: [thor_position(x * 0.25, z * 0.25) for x, z in walkable]
        },
        objects=[destination],
    )
    return runtime, request


class StepMovementHappyPathTest(unittest.TestCase):
    def test_step_strategy_moves_each_grid_edge_and_never_teleports(self):
        runtime, request = single_navigation_request()
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(
            [action[0] for action in runtime.actions],
            ["MoveAhead", "MoveAhead", "Face"],
        )
        self.assertNotIn("Teleport", [action[0] for action in runtime.actions])
        self.assertEqual(metrics.to_dict()["planning_batches"], 1)
        self.assertEqual(metrics.to_dict()["micro_steps"], 2)

    def test_step_mode_factory_builds_step_strategy(self):
        runtime, _request = single_navigation_request()
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        strategy = create_movement_strategy(runtime, config, metrics)

        self.assertIsInstance(strategy, StepMovementStrategy)


class StepMovementRecoveryTest(unittest.TestCase):
    def test_no_walkable_interaction_candidate_uses_dedicated_error(self):
        runtime, request = navigation_case(
            walkable=[(0, 0)],
            candidates=[(2, 0)],
        )
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        with self.assertRaisesRegex(NoInteractionPoseError, "NO_INTERACTION_POSE"):
            StepMovementStrategy(runtime, config, metrics).navigate(request)

    def test_failed_edge_is_blocked_and_planner_uses_detour(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)],
            candidates=[(2, 0)],
        )
        runtime.failed_edges.add(((0, 0), (1, 0)))
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(runtime.edge_attempts[((0, 0), (1, 0))], 1)
        self.assertIn(((0, 0), (0, 1)), runtime.successful_edges)
        self.assertEqual(metrics.to_dict()["replans"], 1)
        self.assertEqual(metrics.to_dict()["failed_transitions"], 1)
        self.assertNotIn("Teleport", [action[0] for action in runtime.actions])

    def test_raised_edge_error_is_blocked_and_planner_uses_detour(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)],
            candidates=[(2, 0)],
        )
        runtime.edge_errors[((0, 0), (1, 0))] = RuntimeError(
            "RotateRight failed"
        )
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(runtime.edge_attempts[((0, 0), (1, 0))], 1)
        self.assertIn(((0, 0), (0, 1)), runtime.successful_edges)
        self.assertEqual(metrics.to_dict()["replans"], 1)
        self.assertEqual(metrics.to_dict()["failed_transitions"], 1)

    def test_temporary_reachable_snapshot_removal_keeps_stable_path(self):
        initial_walkable = [
            (0, 0),
            (1, 0),
            (2, 0),
            (3, 0),
            (0, 1),
            (1, 1),
            (2, 1),
            (3, 1),
        ]
        runtime, request = navigation_case(
            walkable=initial_walkable,
            candidates=[(3, 0)],
        )
        replacement = [point for point in initial_walkable if point != (2, 0)]
        original_refresh = runtime.refresh_reachable_positions

        def refresh(agent_id):
            if runtime.successful_move_count >= 1:
                return [thor_position(x * 0.25, z * 0.25) for x, z in replacement]
            return original_refresh(agent_id)

        runtime.refresh_reachable_positions = refresh
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (3, 0))
        self.assertEqual(runtime.edge_attempts[((1, 0), (2, 0))], 1)
        self.assertNotIn(((1, 0), (1, 1)), runtime.successful_edges)
        self.assertEqual(metrics.to_dict()["replans"], 0)
        self.assertGreater(metrics.to_dict()["suppressed_reachable_removals"], 0)

    def test_legal_position_deviation_replans_from_observed_grid(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)],
            candidates=[(2, 0)],
        )
        runtime.position_overrides[((0, 0), (1, 0))] = thor_position(0.0, 0.25)
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertTrue(
            any(source == (0, 1) for source, _target in runtime.successful_edges[1:])
        )
        self.assertEqual(metrics.to_dict()["position_deviations"], 1)
        self.assertEqual(metrics.to_dict()["replans"], 1)

    def test_unmappable_position_deviation_stops_without_more_actions(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)],
            candidates=[(2, 0)],
        )
        runtime.position_overrides[((0, 0), (1, 0))] = thor_position(0.125, 0.125)
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        with self.assertRaisesRegex(
            StepNavigationError,
            "cannot be mapped to a reachable navigation grid point",
        ):
            StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual([action[0] for action in runtime.actions], ["MoveAhead"])

    def test_two_invisible_candidates_are_excluded_then_third_succeeds(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0), (3, 0)],
            candidates=[(1, 0), (2, 0), (3, 0)],
        )
        runtime.object_visibility_by_position["Apple|1"] = {
            (1, 0): False,
            (2, 0): False,
            (3, 0): True,
        }
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (3, 0))
        self.assertEqual(metrics.to_dict()["invisible_candidates"], 2)
        self.assertEqual(metrics.to_dict()["invisible_interaction_candidates"], 2)
        self.assertEqual(metrics.to_dict()["replans"], 0)
        self.assertEqual([action[0] for action in runtime.actions].count("Face"), 3)

    def test_all_invisible_candidates_report_no_interaction_pose(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0)],
            candidates=[(1, 0), (2, 0)],
        )
        runtime.object_visibility_by_position["Apple|1"] = {
            (1, 0): False,
            (2, 0): False,
        }
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        with self.assertRaisesRegex(NoInteractionPoseError, "NO_INTERACTION_POSE"):
            StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(metrics.to_dict()["invisible_candidates"], 2)
        self.assertEqual(metrics.to_dict()["invisible_interaction_candidates"], 2)
        self.assertEqual(metrics.to_dict()["replans"], 0)

    def test_arrival_faces_and_checks_interaction_target_not_original_surface(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0)],
            candidates=[(1, 0), (2, 0)],
        )
        counter = {
            "objectId": "CounterTop|1",
            "objectType": "CounterTop",
            "visible": True,
            "position": {"x": 0.0, "y": 0.9, "z": 0.0},
        }
        card = {
            "objectId": "CreditCard|1",
            "objectType": "CreditCard",
            "visible": True,
            "position": {"x": 0.75, "y": 0.9, "z": 0.0},
        }
        runtime.objects = {
            counter["objectId"]: dict(counter),
            card["objectId"]: dict(card),
        }
        runtime.object_visibility_by_position[card["objectId"]] = {
            (1, 0): False,
            (2, 0): True,
        }
        request = replace(
            request,
            dest_obj="CounterTop",
            destination=dict(counter),
            center=dict(counter["position"]),
            object_resource=counter["objectId"],
            interaction_target="CreditCard",
            interaction_destination=dict(card),
            interaction_center=dict(card["position"]),
            interaction_object_resource=card["objectId"],
            interaction_target_replaced=True,
        )
        metrics = NavigationMetrics(MovementConfig.resolve("step", environ={}).mode)

        result = StepMovementStrategy(
            runtime,
            MovementConfig.resolve("step", environ={}),
            metrics,
        ).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(result.destination["objectId"], counter["objectId"])
        face_targets = [action[2] for action in runtime.actions if action[0] == "Face"]
        self.assertEqual(face_targets, [card["position"], card["position"]])

    def test_held_item_rotation_failure_excludes_candidate(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0)],
            candidates=[(1, 0), (2, 0)],
        )
        original_face = runtime.face_position_direct
        face_attempts = {"count": 0}

        def face(agent_id, target):
            face_attempts["count"] += 1
            if face_attempts["count"] == 1:
                raise RuntimeError("RotateLeft failed while holding an item")
            return original_face(agent_id, target)

        runtime.face_position_direct = face
        runtime.held_item_rotation_failure = lambda exc: "holding" in str(exc)
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(face_attempts["count"], 2)
        self.assertEqual(metrics.to_dict()["replans"], 0)

    def test_replan_budget_exhaustion_stops_deterministically(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (0, 1), (1, 1)],
            candidates=[(1, 0)],
        )
        runtime.failed_edges.update(
            {
                ((0, 0), (1, 0)),
                ((0, 0), (0, 1)),
            }
        )
        config = replace(
            MovementConfig.resolve("step", environ={}),
            max_replans=1,
        )
        metrics = NavigationMetrics(config.mode)

        with self.assertRaisesRegex(
            StepNavigationError,
            "replan budget 1 exhausted",
        ):
            StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(metrics.to_dict()["budget_exhaustions"], 1)
        self.assertEqual(metrics.to_dict()["replans"], 2)
        self.assertNotIn("Teleport", [action[0] for action in runtime.actions])


if __name__ == "__main__":
    unittest.main()
