import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import (
    AI2ThorAdapter,
    FAILURE_FAIL_ROBOT,
    FAILURE_FAIL_STAGE,
    FAILURE_RETRY,
    FAILURE_SKIP,
    FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE,
    FAILURE_WAIT_AND_RETRY,
    Action,
    StagePlan,
    TaskPlan,
    TaskRunner,
)
from executor_system.execution_control import (
    ExecutionCancelled,
    ExecutionControl,
    ensure_control,
)
from executor_system.execution_policy import (
    ExecutionPolicy,
    StageFailureDecisionError,
    resolve_failure,
)
from executor_system.runtime import ThorRuntime


class FakeEvent:
    metadata = {
        "lastActionSuccess": True,
        "agent": {"rotation": {"y": 0.0}},
    }


class FailurePolicyTest(unittest.TestCase):
    def test_fail_stage_is_explicit_in_strict_mode(self):
        action = Action("OpenObject", {"args": ("Cabinet",)})
        strict = resolve_failure(
            ExecutionPolicy.STRICT,
            action,
            attempts=1,
            effects_satisfied=None,
        )
        legacy = resolve_failure(
            ExecutionPolicy.LEGACY,
            action,
            attempts=1,
            effects_satisfied=None,
        )
        self.assertEqual(strict.kind, "fail_stage")
        self.assertEqual(legacy.kind, "skip")

    def test_failure_policy_table(self):
        cases = (
            ("strict fail robot", ExecutionPolicy.STRICT, "MoveAhead", FAILURE_FAIL_ROBOT, 1, 2, (), None, "fail_robot"),
            ("legacy fail robot", ExecutionPolicy.LEGACY, "MoveAhead", FAILURE_FAIL_ROBOT, 1, 2, (), None, "skip"),
            ("strict skip", ExecutionPolicy.STRICT, "MoveAhead", FAILURE_SKIP, 1, 2, (), None, "skip"),
            ("legacy skip", ExecutionPolicy.LEGACY, "MoveAhead", FAILURE_SKIP, 1, 2, (), None, "skip"),
            ("strict teleport retry", ExecutionPolicy.STRICT, "Teleport", FAILURE_RETRY, 1, 2, (), None, "retry"),
            ("legacy teleport retry", ExecutionPolicy.LEGACY, "Teleport", FAILURE_RETRY, 1, 2, (), None, "retry"),
            ("strict teleport wait retry", ExecutionPolicy.STRICT, "Teleport", FAILURE_WAIT_AND_RETRY, 1, 2, (), None, "wait_retry"),
            ("legacy teleport wait retry", ExecutionPolicy.LEGACY, "Teleport", FAILURE_WAIT_AND_RETRY, 1, 2, (), None, "wait_retry"),
            ("strict retry exhausted", ExecutionPolicy.STRICT, "Teleport", FAILURE_RETRY, 3, 2, (), None, "fail_stage"),
            ("legacy retry exhausted", ExecutionPolicy.LEGACY, "Teleport", FAILURE_RETRY, 3, 2, (), None, "skip"),
            ("strict non teleport retry", ExecutionPolicy.STRICT, "MoveAhead", FAILURE_RETRY, 1, 2, (), None, "fail_stage"),
            ("legacy non teleport retry", ExecutionPolicy.LEGACY, "MoveAhead", FAILURE_RETRY, 1, 2, (), None, "skip"),
            ("strict empty effects", ExecutionPolicy.STRICT, "OpenObject", FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE, 1, 2, (), None, "fail_stage"),
            ("legacy empty effects", ExecutionPolicy.LEGACY, "OpenObject", FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE, 1, 2, (), None, "skip"),
            ("strict unknown effects", ExecutionPolicy.STRICT, "OpenObject", FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE, 1, 2, (lambda _state: True,), None, "fail_stage"),
            ("legacy unsatisfied effects", ExecutionPolicy.LEGACY, "OpenObject", FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE, 1, 2, (lambda _state: True,), False, "skip"),
        )
        for name, policy, action_type, on_failure, attempts, max_retries, effects, satisfied, expected in cases:
            with self.subTest(name=name):
                action = Action(
                    action_type,
                    on_failure=on_failure,
                    max_retries=max_retries,
                    expected_effects=effects,
                )
                decision = resolve_failure(
                    policy,
                    action,
                    attempts=attempts,
                    effects_satisfied=satisfied,
                )
                self.assertEqual(decision.kind, expected)

    def test_max_retries_counts_extra_attempts_after_initial_attempt(self):
        action = Action("Teleport", on_failure=FAILURE_RETRY, max_retries=2)
        decisions = [
            resolve_failure(
                ExecutionPolicy.STRICT,
                action,
                attempts=attempts,
                effects_satisfied=None,
            )
            for attempts in (1, 2, 3)
        ]
        self.assertEqual(
            [(decision.kind, decision.retry_number) for decision in decisions],
            [("retry", 1), ("retry", 2), ("fail_stage", 2)],
        )
        self.assertEqual(decisions[-1].error_code, "retry_exhausted")

    def test_non_teleport_retry_reports_not_supported(self):
        action = Action("MoveAhead", on_failure=FAILURE_RETRY, max_retries=3)
        decision = resolve_failure(
            ExecutionPolicy.STRICT,
            action,
            attempts=1,
            effects_satisfied=None,
        )
        self.assertEqual(decision.kind, "fail_stage")
        self.assertEqual(decision.error_code, "retry_not_supported")

    def test_empty_effects_cannot_prove_success(self):
        action = Action(
            "OpenObject",
            on_failure=FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE,
        )
        decision = resolve_failure(
            ExecutionPolicy.STRICT,
            action,
            attempts=1,
            effects_satisfied=True,
        )
        self.assertEqual(decision.kind, "fail_stage")
        self.assertEqual(decision.error_code, "effects_missing")


class ChildExecutionControlTest(unittest.TestCase):
    def test_parent_cancellation_reaches_child(self):
        parent = ExecutionControl()
        child = parent.child()

        parent.cancel("task cancelled")

        self.assertTrue(child.cancelled)
        self.assertEqual(child.reason, "task cancelled")
        with self.assertRaisesRegex(ExecutionCancelled, "task cancelled"):
            child.check()

    def test_stage_cancellation_does_not_cancel_next_stage_control(self):
        parent = ExecutionControl()
        first_stage = parent.child()
        second_stage = parent.child()

        first_stage.cancel("stage failed")

        self.assertTrue(first_stage.cancelled)
        self.assertFalse(parent.cancelled)
        self.assertFalse(second_stage.cancelled)

    def test_child_uses_earlier_deadline(self):
        parent_deadline = time.monotonic() + 60
        child_deadline = parent_deadline - 10
        parent = ExecutionControl(parent_deadline)

        self.assertEqual(parent.child(deadline=child_deadline).deadline, child_deadline)
        self.assertEqual(parent.child(deadline=parent_deadline + 10).deadline, parent_deadline)

    def test_strict_stage_cancel_blocks_sibling_substep_at_controller_boundary(self):
        runtime = object.__new__(ThorRuntime)
        runtime.reusable = True
        runtime.execution_quiescent = True
        runtime.worker_errors = []
        runtime.physical_agent_count = 2
        runtime.controller_lock = threading.RLock()
        controller_calls = []
        from tests.snapshot_fakes import FakeRuntime as SnapshotFakeRuntime
        snapshot_runtime = SnapshotFakeRuntime()
        runtime.robot_agent_map = snapshot_runtime.robot_agent_map
        runtime.state_version = 0
        runtime.controller = snapshot_runtime.controller
        runtime.controller.step = lambda payload: (
            controller_calls.append(payload["action"]) or runtime.controller.last_event
        )
        runtime.save_frames = lambda _event: None
        runtime.physical_agent_id = lambda robot_id: {"robot1": 0, "robot2": 1}[robot_id]
        runtime.current_agent_position = lambda agent_id: {
            "x": float(agent_id),
            "y": 0.0,
            "z": 0.0,
        }
        runtime.agent_event = lambda _agent_id: FakeEvent()
        runtime.agent_held_objects_for = lambda _agent_id: set()
        runtime.current_objects = lambda _agent_id=None: []

        first_substep_done = threading.Event()
        scoped_controls = []
        stage_controls = []

        def execute(_adapter, robot_id, _action, **kwargs):
            if robot_id == "robot1":
                if not first_substep_done.wait(1):
                    raise RuntimeError("robot2 did not submit its first substep")
                raise RuntimeError("strict stage failure")

            stage_control = kwargs["phase_coordinator"].control
            stage_controls.append(stage_control)
            scoped_controls.append(ensure_control(runtime))
            runtime._step_direct(
                {"action": "FirstSubstep", "agentId": 1},
                save_frame=False,
            )
            first_substep_done.set()
            if not stage_control.wait(1):
                raise RuntimeError("strict failure did not cancel stage")
            runtime._step_direct(
                {"action": "SubstepAfterStageCancellation", "agentId": 1},
                save_frame=False,
            )
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "stage",
                    {
                        "robot1": [Action("OpenObject")],
                        "robot2": [Action("OpenObject")],
                    },
                )
            ],
        )

        with patch.object(AI2ThorAdapter, "execute", execute):
            with self.assertRaisesRegex(StageFailureDecisionError, "strict stage failure"):
                TaskRunner(runtime, execution_policy=ExecutionPolicy.STRICT).execute(plan)

        root_control = runtime.execution_control
        self.assertEqual(controller_calls, ["FirstSubstep"])
        self.assertIs(scoped_controls[0], stage_controls[0])
        self.assertTrue(stage_controls[0].cancelled)
        self.assertFalse(root_control.cancelled)

        next_stage_control = root_control.child()
        with runtime.action_deadline_scope(control=next_stage_control):
            runtime._step_direct(
                {"action": "NextStageSubstep", "agentId": 1},
                save_frame=False,
            )
        self.assertEqual(controller_calls, ["FirstSubstep", "NextStageSubstep"])
        self.assertFalse(next_stage_control.cancelled)


if __name__ == "__main__":
    unittest.main()
