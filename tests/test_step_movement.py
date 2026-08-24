import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.movement import (
    MovementConfig,
    NavigationMetrics,
    NavigationRequest,
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


if __name__ == "__main__":
    unittest.main()
