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

    def test_removed_remaining_path_is_not_attempted_and_replans(self):
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
        runtime.walkable_after_successful_moves[1] = {
            0: [thor_position(x * 0.25, z * 0.25) for x, z in replacement]
        }
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (3, 0))
        self.assertEqual(runtime.edge_attempts[((1, 0), (2, 0))], 0)
        self.assertIn(((1, 0), (1, 1)), runtime.successful_edges)
        self.assertEqual(metrics.to_dict()["replans"], 1)

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

    def test_invisible_first_candidate_is_excluded_then_second_succeeds(self):
        runtime, request = navigation_case(
            walkable=[(0, 0), (1, 0), (2, 0)],
            candidates=[(1, 0), (2, 0)],
        )
        runtime.object_visibility_by_position["Apple|1"] = {
            (1, 0): False,
            (2, 0): True,
        }
        config = MovementConfig.resolve("step", environ={})
        metrics = NavigationMetrics(config.mode)

        result = StepMovementStrategy(runtime, config, metrics).navigate(request)

        self.assertEqual(position_to_grid_key(result.position), (2, 0))
        self.assertEqual(metrics.to_dict()["invisible_candidates"], 1)
        self.assertEqual(metrics.to_dict()["replans"], 1)
        self.assertEqual([action[0] for action in runtime.actions].count("Face"), 2)

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
