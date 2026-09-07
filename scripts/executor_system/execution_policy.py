"""Explicit execution-policy decisions shared by plan executors."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .world_snapshot import WorldSnapshot


class ExecutionPolicy(str, Enum):
    LEGACY = "legacy"
    STRICT = "strict"


@dataclass(frozen=True)
class FailureDecision:
    kind: str
    error_code: str
    retry_number: int


class StageFailureDecisionError(RuntimeError):
    """Internal signal for a policy decision that cancels only one stage."""


def resolve_failure(
    policy: ExecutionPolicy,
    action: Any,
    *,
    attempts: int,
    effects_satisfied: Optional[bool],
) -> FailureDecision:
    """Return the one policy decision for a failed action attempt.

    ``attempts`` includes the initial attempt.  Consequently, retry number one
    is selected after ``attempts == 1`` and ``max_retries`` is the number of
    extra attempts allowed after the initial one.
    """

    selected_policy = ExecutionPolicy(policy)
    attempt_count = max(1, int(attempts))
    retry_limit = max(0, int(getattr(action, "max_retries", 0)))
    retry_number = min(attempt_count, retry_limit)
    requested = str(getattr(action, "on_failure", "FAIL_STAGE")).upper()
    strict_fallback = "fail_stage" if selected_policy is ExecutionPolicy.STRICT else "skip"

    if requested == "SKIP":
        return FailureDecision("skip", "action_failed", 0)
    if requested == "FAIL_ROBOT":
        kind = "fail_robot" if selected_policy is ExecutionPolicy.STRICT else "skip"
        return FailureDecision(kind, "action_failed", 0)
    if requested == "FAIL_STAGE":
        return FailureDecision(strict_fallback, "action_failed", 0)
    if requested in {"RETRY", "WAIT_AND_RETRY"}:
        if str(getattr(action, "action_type", "")) != "Teleport":
            return FailureDecision(strict_fallback, "retry_not_supported", 0)
        if attempt_count <= retry_limit:
            kind = "wait_retry" if requested == "WAIT_AND_RETRY" else "retry"
            return FailureDecision(kind, "action_failed", attempt_count)
        return FailureDecision(strict_fallback, "retry_exhausted", retry_limit)
    if requested == "SKIP_IF_EFFECT_ALREADY_TRUE":
        expected_effects = tuple(getattr(action, "expected_effects", ()) or ())
        if not expected_effects:
            error_code = "effects_missing"
        elif effects_satisfied is None:
            error_code = "effects_unknown"
        elif effects_satisfied:
            # Callers handle this as success before failure resolution.  Keep a
            # defensive result in the declared decision vocabulary.
            error_code = "effects_already_satisfied"
        else:
            error_code = "effects_unsatisfied"
        return FailureDecision(strict_fallback, error_code, 0)
    raise ValueError(f"Unsupported action failure policy: {requested!r}")


@dataclass(frozen=True)
class StageOutcome:
    status: str
    continue_task: bool
    errors: Tuple[Dict[str, Any], ...] = ()
    snapshot: Optional[WorldSnapshot] = None


class ConditionEvaluationError(RuntimeError):
    """A callback raised instead of returning condition truth."""
