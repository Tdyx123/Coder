import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import Action, FAILURE_FAIL_STAGE, FAILURE_RETRY
from executor_system.executor import Executor
from executor_system.runtime import ThorRuntime
from executor_system.utils import position_to_grid_key


class FakeEvent:
    def __init__(self, success=True, error_message=""):
        self.metadata = {"lastActionSuccess": success}
        if error_message:
            self.metadata["errorMessage"] = error_message


def runtime_without_init():
    runtime = object.__new__(ThorRuntime)
    runtime.log_retry = lambda *_args, **_kwargs: None
    return runtime


class ExecutorRetryPolicyTest(unittest.TestCase):
    def test_actions_default_to_fail_stage(self):
        self.assertEqual(Action("MoveAhead").on_failure, FAILURE_FAIL_STAGE)
        self.assertEqual(
            Action.from_any({"action_type": "PickupObject"}).on_failure,
            FAILURE_FAIL_STAGE,
        )

    def test_non_teleport_step_does_not_retry_even_when_requested(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            return FakeEvent(False, "MoveAhead failed")

        runtime._step_direct = step_direct

        event = runtime.step(
            {"action": "MoveAhead", "agentId": 0},
            check_success=False,
            retry_on_failure=True,
            max_retries=3,
        )

        self.assertFalse(event.metadata["lastActionSuccess"])
        self.assertEqual(len(calls), 1)

    def test_teleport_step_retries_until_success(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            if len(calls) == 1:
                return FakeEvent(False, "Teleport failed")
            return FakeEvent(True)

        runtime._step_direct = step_direct

        event = runtime.step(
            {
                "action": "Teleport",
                "agentId": 0,
                "position": {"x": 1.0, "y": 0.0, "z": 1.0},
            },
            check_success=False,
            retry_on_failure=True,
            max_retries=3,
        )

        self.assertTrue(event.metadata["lastActionSuccess"])
        self.assertEqual(len(calls), 2)

    def test_non_teleport_action_retry_policy_fails_without_rerunning(self):
        class FailingRuntime:
            physical_agent_count = 1

            def __init__(self):
                self.step_calls = 0

            def physical_agent_id(self, _robot_id):
                return 0

            def current_agent_position(self, _agent_id):
                return {"x": 0.0, "y": 0.0, "z": 0.0}

            def agent_event(self, _agent_id):
                event = FakeEvent(True)
                event.metadata["agent"] = {"rotation": {"y": 0.0}}
                return event

            def agent_held_objects_for(self, _agent_id):
                return set()

            def step(self, _payload, **_kwargs):
                self.step_calls += 1
                raise RuntimeError("MoveAhead failed")

        runtime = FailingRuntime()
        action = Action("MoveAhead", on_failure=FAILURE_RETRY, max_retries=2)
        executor = Executor(runtime, "robot1", [action])

        with self.assertRaisesRegex(RuntimeError, "MoveAhead failed"):
            executor.execute()

        self.assertEqual(runtime.step_calls, 1)

    def test_teleport_and_face_retries_next_candidate_after_rotation_failure(self):
        runtime = runtime_without_init()
        candidates = [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 1.0, "y": 0.0, "z": 0.0},
        ]
        teleport_calls = []
        face_calls = []

        def select_teleport_position(
            _agent_id,
            _search_center,
            *,
            candidate_positions,
            excluded_grid_keys,
            restrict_to_candidate_positions=False,
        ):
            for position in candidate_positions:
                if position_to_grid_key(position) not in excluded_grid_keys:
                    return dict(position)
            raise RuntimeError("No reachable teleport candidate")

        def try_teleport_to_position_direct(_agent_id, target_position, *, max_retries=3):
            teleport_calls.append(dict(target_position))
            return FakeEvent(True)

        def face_position_direct(_agent_id, face_target):
            face_calls.append(dict(face_target))
            if len(face_calls) == 1:
                raise RuntimeError("Rotate failed")

        runtime.select_teleport_position = select_teleport_position
        runtime.try_teleport_to_position_direct = try_teleport_to_position_direct
        runtime.face_position_direct = face_position_direct

        with patch("executor_system.runtime.log", lambda _message: None):
            result = runtime.teleport_and_face_first_working_candidate(
                0,
                candidates,
                face_target={"x": 3.0, "y": 0.0, "z": 3.0},
                search_center=candidates[0],
            )

        self.assertEqual(result, candidates[1])
        self.assertEqual(teleport_calls, candidates)
        self.assertEqual(len(face_calls), 2)


if __name__ == "__main__":
    unittest.main()
