import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import (
    FAILURE_FAIL_ROBOT,
    FAILURE_FAIL_STAGE,
    FAILURE_RETRY,
    FAILURE_SKIP,
    FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE,
    FAILURE_WAIT_AND_RETRY,
    Action,
)
from executor_system.execution_control import ExecutionCancelled, ExecutionControl
from executor_system.execution_policy import ExecutionPolicy, resolve_failure


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


if __name__ == "__main__":
    unittest.main()
