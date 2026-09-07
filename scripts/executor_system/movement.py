"""Shared contracts for pluggable robot movement strategies."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Protocol, Tuple

from .plan_types import (PlannedAction)
from .utils import RobotRef


class MovementConfigurationError(RuntimeError):
    """Raised when the selected movement mode is invalid."""


class StepNavigationError(RuntimeError):
    """Raised when step navigation cannot complete safely."""


class NavigationBatchAborted(StepNavigationError):
    """Raised for requests aborted by another request in the same batch."""


class NavigationDeferred(RuntimeError):
    """Signals that a navigation action must be resubmitted in the next wave."""

    def __init__(
        self,
        agent_id: int,
        *,
        fallback_status: Optional[str] = None,
    ) -> None:
        self.agent_id = int(agent_id)
        self.fallback_status = fallback_status
        suffix = f" after {fallback_status}" if fallback_status else ""
        super().__init__(f"navigation deferred for agent {self.agent_id}{suffix}")


class NoInteractionPoseError(StepNavigationError):
    """Raised when no candidate pose can interact with the requested object."""


class MovementMode(str, Enum):
    TELEPORT = "teleport"
    STEP = "step"


@dataclass(frozen=True)
class MovementConfig:
    mode: MovementMode
    grid_size_m: float = 0.25
    hard_clearance_m: float = 0.35
    grid_snap_tolerance_m: float = 0.125001
    max_replans: int = 8
    max_failed_transitions: int = 8
    max_assignment_trials: int = 256
    max_invisible_candidate_replans: int = 1

    @classmethod
    def resolve(
        cls,
        explicit_mode: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "MovementConfig":
        source = os.environ if environ is None else environ
        raw = (
            source.get("LAMMAP_MOVEMENT_MODE", MovementMode.STEP.value)
            if explicit_mode is None
            else explicit_mode
        )
        try:
            mode = MovementMode(str(raw).strip().lower())
        except ValueError as exc:
            raise MovementConfigurationError(
                "movement mode must be one of: teleport, step"
            ) from exc
        return cls(mode=mode)


@dataclass(frozen=True)
class ActionWave:
    wave_id: int
    navigation_agent_ids: Tuple[int, ...]


@dataclass(frozen=True)
class NavigationRequest:
    robot: RobotRef
    agent_id: int
    dest_obj: Any
    destination: Dict[str, Any]
    center: Dict[str, float]
    candidate_positions: Tuple[Dict[str, float], ...]
    object_resource: Optional[str]
    next_action: Optional[PlannedAction]
    phase_coordinator: Optional[Any]
    action_wave: Optional[ActionWave] = None
    interaction_target: Any = None
    interaction_destination: Optional[Dict[str, Any]] = None
    interaction_center: Optional[Dict[str, float]] = None
    interaction_object_resource: Optional[str] = None
    interaction_target_replaced: bool = False


@dataclass(frozen=True)
class NavigationResult:
    destination: Dict[str, Any]
    position: Dict[str, float]
    decision_trace: Tuple[Dict[str, Any], ...] = ()


@dataclass(frozen=True)
class NavigationBatchResult(Mapping[int, NavigationResult]):
    results: Dict[int, NavigationResult]
    deferred_agent_ids: FrozenSet[int] = frozenset()
    fallback_status: Optional[str] = None
    failed_agent_errors: Dict[int, Exception] = field(default_factory=dict)

    def __getitem__(self, agent_id: int) -> NavigationResult:
        return self.results[int(agent_id)]

    def __iter__(self) -> Iterator[int]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)


@dataclass
class NavigationMetrics:
    mode: MovementMode
    requests: int = 0
    successes: int = 0
    failures: int = 0
    planning_batches: int = 0
    candidate_trials: int = 0
    priority_trials: int = 0
    replans: int = 0
    micro_steps: int = 0
    waits: int = 0
    parking_moves: int = 0
    failed_transitions: int = 0
    position_deviations: int = 0
    invisible_candidates: int = 0
    interaction_target_substitutions: int = 0
    invisible_interaction_candidates: int = 0
    interaction_repositions: int = 0
    suppressed_reachable_removals: int = 0
    serial_fallback_batches: int = 0
    deferred_requests: int = 0
    budget_exhaustions: int = 0
    planning_time_seconds: float = 0.0
    planning_timing_count: int = 0
    planning_durations_seconds: List[float] = field(default_factory=list)
    action_counts: Dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request_started(self) -> None:
        self.increment("requests")

    def record_request_succeeded(self) -> None:
        self.increment("successes")

    def record_request_failed(self) -> None:
        self.increment("failures")

    def record_action(self, action: str) -> None:
        with self._lock:
            self.action_counts[action] = self.action_counts.get(action, 0) + 1

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            value = getattr(self, field_name, None)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AttributeError(f"{field_name!r} is not an integer metric")
            setattr(self, field_name, value + int(amount))

    def add_planning_time(self, seconds: float) -> None:
        with self._lock:
            duration = float(seconds)
            self.planning_time_seconds += duration
            self.planning_timing_count += 1
            self.planning_durations_seconds.append(duration)
            # Compatibility preview only; full percentiles use benchmark runs.
            del self.planning_durations_seconds[:-128]

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode.value,
                "requests": self.requests,
                "successes": self.successes,
                "failures": self.failures,
                "planning_batches": self.planning_batches,
                "candidate_trials": self.candidate_trials,
                "priority_trials": self.priority_trials,
                "replans": self.replans,
                "micro_steps": self.micro_steps,
                "waits": self.waits,
                "parking_moves": self.parking_moves,
                "failed_transitions": self.failed_transitions,
                "position_deviations": self.position_deviations,
                "invisible_candidates": self.invisible_candidates,
                "interaction_target_substitutions": self.interaction_target_substitutions,
                "invisible_interaction_candidates": self.invisible_interaction_candidates,
                "interaction_repositions": self.interaction_repositions,
                "suppressed_reachable_removals": self.suppressed_reachable_removals,
                "serial_fallback_batches": self.serial_fallback_batches,
                "deferred_requests": self.deferred_requests,
                "budget_exhaustions": self.budget_exhaustions,
                "planning_time_seconds": self.planning_time_seconds,
                "planning_durations_sample_limit": 128,
                "planning_durations_semantics": "bounded preview of latest calls; not a full-run distribution",
                "planning_durations_truncated": self.planning_timing_count > 128,
                "planning_timing_count": self.planning_timing_count,
                "planning_durations_seconds": list(
                    self.planning_durations_seconds
                ),
                "action_counts": dict(self.action_counts),
            }


class MovementStrategy(Protocol):
    def navigate(self, request: NavigationRequest) -> NavigationResult:
        raise NotImplementedError


def create_movement_strategy(
    runtime: Any,
    config: MovementConfig,
    metrics: NavigationMetrics,
) -> MovementStrategy:
    if config.mode is MovementMode.TELEPORT:
        from .teleport_movement import TeleportMovementStrategy

        return TeleportMovementStrategy(runtime, metrics)

    from .step_movement import StepMovementStrategy

    return StepMovementStrategy(runtime, config, metrics)
