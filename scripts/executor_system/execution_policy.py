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


class PlanExecutionError(RuntimeError):
    """An unsuccessful plan with its completed, serializable execution report."""

    def __init__(self, report):
        self.report = report
        errors = report.get('errors') or ()
        detail = ': ' + str(errors[-1].get('message', '')) if errors else ''
        super().__init__(f"Plan {report.get('task_id', '')} {report.get('execution_status', 'failed')}{detail}")


def condition_evidence(condition, world):
    """Return detached evidence for one condition at the supplied version."""
    if callable(condition):
        try:
            value = bool(condition(world))
        except Exception as exc:
            from .execution_control import raise_if_execution_aborted
            raise_if_execution_aborted(world.runtime, exc)
            raise ConditionEvaluationError(str(exc)) from exc
        return {'kind': 'callable', 'name': getattr(condition, '__name__', type(condition).__name__),
                'satisfied': value, 'world_version': world.version}
    from .evaluation import EvaluationContext
    from .action_resources import snapshot_resource_view
    context = EvaluationContext.from_goals([condition])
    view = snapshot_resource_view(world.runtime, world.snapshot)
    result = context.evaluate_goal(view, context.goals[0],
                                   objects=tuple(world.objects_by_id.values()))
    return {'kind': 'goal', 'satisfied': {'satisfied': True, 'unsatisfied': False, 'unknown': None}[result['status']],
            'world_version': world.version, 'goal': result}


def evaluate_condition(condition, world):
    return condition_evidence(condition, world)['satisfied']


def conditions_evidence(conditions, world):
    return [condition_evidence(condition, world) for condition in conditions]


def conditions_satisfied(evidence):
    values = [record['satisfied'] for record in evidence]
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def evaluate_conditions(conditions, world):
    return conditions_satisfied(conditions_evidence(conditions, world))


def snapshot_report(snapshot):
    """Serialize snapshot data without leaking mutable or controller objects."""
    from collections.abc import Mapping
    def plain(value):
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list, set, frozenset)):
            return [plain(item) for item in value]
        return value
    return {'version': snapshot.version, **{name: plain(getattr(snapshot, name)) for name in
        ('robot_positions', 'robot_rotations', 'held_objects', 'objects_by_id', 'held_object_sources')}}


def resolve_stage_outcome(policy, stage, robot_outcomes, condition_satisfied):
    """Resolve robot failure separately from queue exhaustion and stage truth.

    A strict fail_robot also fails the stage after peers finish. SKIP action
    failures produce a partial stage; FAIL_STAGE and false conditions produce
    a failed stage. Legacy failures are partial unless the condition is false.
    """
    selected = ExecutionPolicy(policy)
    outcomes = tuple(robot_outcomes.values() if isinstance(robot_outcomes, dict) else robot_outcomes)
    errors = tuple(error for outcome in outcomes for error in outcome.errors)
    snapshot = next((o.snapshot for o in reversed(outcomes) if o.snapshot is not None), None)
    for status in ('timeout', 'cancelled'):
        if any(o.status == status for o in outcomes):
            return StageOutcome(status, False, errors, snapshot)
    failed = condition_satisfied is False or any(o.status == 'failed' for o in outcomes)
    if failed:
        return StageOutcome('failed', selected is ExecutionPolicy.LEGACY or stage.stage_failure_policy == 'SKIP', errors, snapshot)
    return StageOutcome('partial' if errors or any(o.status == 'partial' for o in outcomes) else 'completed',
                        True, errors, snapshot)
