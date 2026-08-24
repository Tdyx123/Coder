import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.movement import (
    MovementMode,
    NavigationMetrics,
    NavigationRequest,
)
from executor_system.runtime import ThorRuntime
from executor_system.teleport_movement import TeleportMovementStrategy


def navigation_request(
    destination,
    candidate,
    *,
    phase_coordinator=None,
):
    center = dict(destination["position"])
    return NavigationRequest(
        robot="robot1",
        agent_id=0,
        dest_obj="Apple",
        destination=dict(destination),
        center=center,
        candidate_positions=(dict(candidate),),
        object_resource=destination["objectId"],
        next_action=None,
        phase_coordinator=phase_coordinator,
    )


class TeleportMovementStrategyTest(unittest.TestCase):
    def test_runtime_defaults_to_teleport_strategy(self):
        runtime = object.__new__(ThorRuntime)

        runtime.configure_movement(None, environ={})

        self.assertIs(runtime.movement_config.mode, MovementMode.TELEPORT)
        self.assertIsInstance(runtime.movement_strategy, TeleportMovementStrategy)

    def test_wait_rebuild_move_notify_order_uses_rebuilt_destination(self):
        order = []
        first_destination = {
            "objectId": "Apple|first",
            "objectType": "Apple",
            "position": {"x": 1.0, "y": 0.9, "z": 0.0},
        }
        second_destination = {
            "objectId": "Apple|second",
            "objectType": "Apple",
            "position": {"x": 1.1, "y": 0.9, "z": 0.0},
        }
        first_candidate = {"x": 0.75, "y": 0.0, "z": 0.0}
        second_candidate = {"x": 1.25, "y": 0.0, "z": 0.0}

        class RecordingCoordinator:
            def wait_until_goto_candidates_clear(self, agent_id, positions):
                order.append(("wait", agent_id, tuple(dict(item) for item in positions)))
                return True

            def notify_agent_position_changed(self, agent_id):
                order.append(("notify", agent_id))

        coordinator = RecordingCoordinator()
        initial_request = navigation_request(
            first_destination,
            first_candidate,
            phase_coordinator=coordinator,
        )
        rebuilt_request = navigation_request(
            second_destination,
            second_candidate,
            phase_coordinator=coordinator,
        )

        class RecordingRuntime:
            def build_navigation_request(self, *args, **kwargs):
                order.append(("rebuild", args, kwargs))
                return rebuilt_request

            def teleport_and_face_candidate_positions(
                self,
                agent_id,
                positions,
                *,
                face_target,
                object_resource,
                search_center,
                restrict_to_candidate_positions,
            ):
                order.append(
                    (
                        "teleport_and_face",
                        agent_id,
                        tuple(dict(item) for item in positions),
                        dict(face_target),
                        object_resource,
                        dict(search_center),
                        restrict_to_candidate_positions,
                    )
                )
                return dict(positions[0])

        metrics = NavigationMetrics(MovementMode.TELEPORT)
        strategy = TeleportMovementStrategy(RecordingRuntime(), metrics)

        result = strategy.navigate(initial_request)

        self.assertEqual(
            order,
            [
                ("wait", 0, (first_candidate,)),
                (
                    "rebuild",
                    ("robot1", "Apple"),
                    {
                        "next_action": None,
                        "phase_coordinator": coordinator,
                        "action_wave": None,
                    },
                ),
                (
                    "teleport_and_face",
                    0,
                    (second_candidate,),
                    second_destination["position"],
                    "Apple|second",
                    second_destination["position"],
                    True,
                ),
                ("notify", 0),
            ],
        )
        self.assertEqual(result.destination, second_destination)
        self.assertEqual(result.position, second_candidate)


if __name__ == "__main__":
    unittest.main()
