#!/usr/bin/env python3
"""Deterministic L0/L1 multi-robot avoidance prototype.

This module deliberately does not import AI2-THOR or the existing executor
system.  It models AI2-THOR-aligned 0.25 m navigation points and a configurable
center-to-center clearance, then plans and executes against an in-memory world.
"""

from __future__ import annotations

import argparse
import json
import math
import heapq
import itertools
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)


class ScenarioValidationError(ValueError):
    """Raised when a scenario does not satisfy the public JSON contract."""


class WalkableUpdateError(ValueError):
    """Raised when a reachable-position update cannot be applied atomically."""


@dataclass(frozen=True, order=True)
class GridPoint:
    x: int
    z: int

    @classmethod
    def from_value(cls, value: Any, field_name: str = "point") -> "GridPoint":
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or isinstance(value[0], bool)
            or isinstance(value[1], bool)
            or not isinstance(value[0], int)
            or not isinstance(value[1], int)
        ):
            raise ScenarioValidationError(
                f"{field_name} must be a two-integer [x, z] grid point."
            )
        return cls(int(value[0]), int(value[1]))

    def to_list(self) -> list:
        return [self.x, self.z]


@dataclass(frozen=True)
class WalkableUpdate:
    version: int
    walkable: FrozenSet[GridPoint]
    changed: bool
    added: FrozenSet[GridPoint]
    removed: FrozenSet[GridPoint]


class GlobalWalkableMap:
    """Aggregate the latest per-robot reachable-position snapshots."""

    def __init__(self, robot_ids: Sequence[str], grid_size_m: float) -> None:
        normalized_ids = tuple(str(robot_id) for robot_id in robot_ids)
        if not normalized_ids or len(set(normalized_ids)) != len(normalized_ids):
            raise WalkableUpdateError("robot_ids must be non-empty and unique.")
        if not math.isfinite(float(grid_size_m)) or float(grid_size_m) <= 0:
            raise WalkableUpdateError("grid_size_m must be a positive finite number.")
        self.robot_ids = normalized_ids
        self.grid_size_m = float(grid_size_m)
        self._snapshots: Dict[str, FrozenSet[GridPoint]] = {}
        self._walkable: FrozenSet[GridPoint] = frozenset()
        self._version = 0

    @property
    def walkable(self) -> FrozenSet[GridPoint]:
        return self._walkable

    @property
    def version(self) -> int:
        return self._version

    def copy(self) -> "GlobalWalkableMap":
        copied = GlobalWalkableMap(self.robot_ids, self.grid_size_m)
        copied._snapshots = dict(self._snapshots)
        copied._walkable = self._walkable
        copied._version = self._version
        return copied

    def _normalize_positions(
        self,
        positions: Sequence[Mapping[str, Any]],
        field_name: str,
    ) -> FrozenSet[GridPoint]:
        if not isinstance(positions, (list, tuple)) or not positions:
            raise WalkableUpdateError(f"{field_name} must be a non-empty list of positions.")
        normalized = set()
        for index, position in enumerate(positions):
            item_name = f"{field_name}[{index}]"
            if not isinstance(position, Mapping):
                raise WalkableUpdateError(f"{item_name} must be a position object.")
            x = position.get("x")
            z = position.get("z")
            if (
                isinstance(x, bool)
                or isinstance(z, bool)
                or not isinstance(x, (int, float))
                or not isinstance(z, (int, float))
                or not math.isfinite(float(x))
                or not math.isfinite(float(z))
            ):
                raise WalkableUpdateError(
                    f"{item_name}.x and {item_name}.z must be finite numbers."
                )
            normalized.add(
                GridPoint(
                    round(float(x) / self.grid_size_m),
                    round(float(z) / self.grid_size_m),
                )
            )
        return frozenset(normalized)

    def _commit(
        self,
        snapshots: Mapping[str, FrozenSet[GridPoint]],
    ) -> WalkableUpdate:
        next_walkable = frozenset(
            point for snapshot in snapshots.values() for point in snapshot
        )
        previous_walkable = self._walkable
        self._snapshots = dict(snapshots)
        self._walkable = next_walkable
        self._version += 1
        return WalkableUpdate(
            version=self._version,
            walkable=next_walkable,
            changed=next_walkable != previous_walkable,
            added=frozenset(next_walkable - previous_walkable),
            removed=frozenset(previous_walkable - next_walkable),
        )

    def update_robot(
        self,
        robot_id: str,
        reachable_positions: Sequence[Mapping[str, Any]],
    ) -> WalkableUpdate:
        if robot_id not in self.robot_ids:
            raise WalkableUpdateError(f"Unknown robot id: {robot_id!r}.")
        if set(self._snapshots) != set(self.robot_ids):
            raise WalkableUpdateError(
                "All robot snapshots must be initialized before a single-robot update."
            )
        normalized = self._normalize_positions(
            reachable_positions,
            f"reachable_positions[{robot_id!r}]",
        )
        snapshots = dict(self._snapshots)
        snapshots[robot_id] = normalized
        return self._commit(snapshots)

    def replace_all(
        self,
        reachable_positions_by_robot: Mapping[
            str, Sequence[Mapping[str, Any]]
        ],
    ) -> WalkableUpdate:
        if not isinstance(reachable_positions_by_robot, Mapping):
            raise WalkableUpdateError("reachable_positions_by_robot must be an object.")
        expected = set(self.robot_ids)
        actual = set(reachable_positions_by_robot)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise WalkableUpdateError(
                "Full refresh must contain exactly all robots; "
                f"missing={missing}, unknown={unknown}."
            )
        snapshots = {
            robot_id: self._normalize_positions(
                reachable_positions_by_robot[robot_id],
                f"reachable_positions_by_robot[{robot_id!r}]",
            )
            for robot_id in self.robot_ids
        }
        return self._commit(snapshots)


@dataclass(frozen=True)
class WalkableEvent:
    at_committed_step: int
    event_type: str
    robot_id: Optional[str] = None
    reachable_positions: Tuple[Mapping[str, Any], ...] = ()
    reachable_positions_by_robot: Mapping[
        str, Tuple[Mapping[str, Any], ...]
    ] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    position: GridPoint
    cost: float


@dataclass(frozen=True)
class RobotIntent:
    robot_id: str
    start: GridPoint
    candidates: Tuple[Candidate, ...]


@dataclass(frozen=True)
class ExecutionConfig:
    failure_at_micro_step: Optional[int] = None
    external_version_bump_before_micro_step: Optional[int] = None
    walkable_events: Tuple[WalkableEvent, ...] = ()


@dataclass(frozen=True)
class Scenario:
    grid_size_m: float
    hard_clearance_m: float
    max_ticks: int
    max_assignment_trials: int
    walkable: FrozenSet[GridPoint]
    conflicts: FrozenSet[Tuple[GridPoint, GridPoint]]
    robots: Tuple[RobotIntent, ...]
    blocked_transitions: FrozenSet[Tuple[GridPoint, GridPoint]] = frozenset()
    fixed_robot_ids: FrozenSet[str] = frozenset()
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


class GeometryConflictModel:
    def __init__(self, grid_size_m: float, hard_clearance_m: float) -> None:
        self.grid_size_m = float(grid_size_m)
        self.hard_clearance_m = float(hard_clearance_m)

    def conflicts(self, left: GridPoint, right: GridPoint) -> bool:
        dx = (left.x - right.x) * self.grid_size_m
        dz = (left.z - right.z) * self.grid_size_m
        return math.hypot(dx, dz) <= self.hard_clearance_m + 1e-12


class TableConflictModel:
    def __init__(self, conflicting_pairs: Iterable[Tuple[GridPoint, GridPoint]]) -> None:
        self.conflicting_pairs = frozenset(
            tuple(sorted((left, right))) for left, right in conflicting_pairs
        )

    def conflicts(self, left: GridPoint, right: GridPoint) -> bool:
        return tuple(sorted((left, right))) in self.conflicting_pairs


class CompositeConflictModel:
    def __init__(self, models: Sequence[Any]) -> None:
        self.models = tuple(models)

    def conflicts(self, left: GridPoint, right: GridPoint) -> bool:
        return any(model.conflicts(left, right) for model in self.models)


@dataclass
class DecisionTrace:
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, event: str, **details: Any) -> None:
        self.entries.append({"event": event, **details})

    def to_list(self) -> List[Dict[str, Any]]:
        return [dict(entry) for entry in self.entries]


@dataclass
class WorldState:
    version: int
    positions: Dict[str, GridPoint]
    statuses: Dict[str, str]

    @classmethod
    def from_scenario(cls, scenario: Scenario, version: int = 0) -> "WorldState":
        return cls(
            version=version,
            positions={robot.robot_id: robot.start for robot in scenario.robots},
            statuses={robot.robot_id: "ACTIVE" for robot in scenario.robots},
        )

    def copy(self) -> "WorldState":
        return WorldState(self.version, dict(self.positions), dict(self.statuses))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "positions": {
                robot_id: position.to_list()
                for robot_id, position in sorted(self.positions.items())
            },
            "statuses": dict(sorted(self.statuses.items())),
        }


@dataclass(frozen=True)
class MicroStep:
    index: int
    tick: int
    robot_id: str
    source: GridPoint
    target: GridPoint
    expected_world_version: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "tick": self.tick,
            "robot_id": self.robot_id,
            "source": self.source.to_list(),
            "target": self.target.to_list(),
            "expected_world_version": self.expected_world_version,
        }


@dataclass
class ReservationTable:
    positions_by_tick: Dict[int, Dict[str, GridPoint]]
    released_from_tick: Optional[int] = None

    @classmethod
    def from_paths(
        cls,
        paths: Mapping[str, Tuple[GridPoint, ...]],
        horizon: int,
    ) -> "ReservationTable":
        positions_by_tick = {}
        for tick in range(horizon + 1):
            positions_by_tick[tick] = {
                robot_id: _path_position(path, tick)
                for robot_id, path in sorted(paths.items())
            }
        return cls(positions_by_tick)

    def release_from(self, tick: int) -> None:
        self.released_from_tick = tick
        self.positions_by_tick = {
            reserved_tick: positions
            for reserved_tick, positions in self.positions_by_tick.items()
            if reserved_tick < tick
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            str(tick): {
                robot_id: position.to_list()
                for robot_id, position in sorted(positions.items())
            }
            for tick, positions in sorted(self.positions_by_tick.items())
        }


@dataclass
class JointPlan:
    base_world_version: int
    initial_positions: Dict[str, GridPoint]
    assignment: Dict[str, Candidate]
    paths: Dict[str, Tuple[GridPoint, ...]]
    priority_order: Tuple[str, ...]
    reservations: ReservationTable
    micro_steps: Tuple[MicroStep, ...]
    metrics: Dict[str, Any]


@dataclass
class PlanningResult:
    status: str
    plan: Optional[JointPlan]
    decision_trace: DecisionTrace
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        plan = self.plan
        return {
            "status": self.status,
            "message": self.message,
            "base_world_version": None if plan is None else plan.base_world_version,
            "assignment": (
                {}
                if plan is None
                else {
                    robot_id: {
                        "candidate_id": candidate.candidate_id,
                        "position": candidate.position.to_list(),
                        "cost": candidate.cost,
                    }
                    for robot_id, candidate in sorted(plan.assignment.items())
                }
            ),
            "paths": (
                {}
                if plan is None
                else {
                    robot_id: [position.to_list() for position in path]
                    for robot_id, path in sorted(plan.paths.items())
                }
            ),
            "priority_order": [] if plan is None else list(plan.priority_order),
            "reservations": {} if plan is None else plan.reservations.to_dict(),
            "micro_steps": (
                [] if plan is None else [step.to_dict() for step in plan.micro_steps]
            ),
            "metrics": {} if plan is None else dict(plan.metrics),
            "execution": None,
            "decision_trace": self.decision_trace.to_list(),
        }


@dataclass
class ExecutionResult:
    status: str
    world_state: WorldState
    committed_micro_steps: int
    decision_trace: DecisionTrace
    failed_robot_id: Optional[str] = None
    message: str = ""
    walkable: FrozenSet[GridPoint] = frozenset()
    walkable_version: int = 0
    replan_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "committed_micro_steps": self.committed_micro_steps,
            "failed_robot_id": self.failed_robot_id,
            "world_state": self.world_state.to_dict(),
            "walkable": [point.to_list() for point in sorted(self.walkable)],
            "walkable_version": self.walkable_version,
            "replan_count": self.replan_count,
        }


class FakeRuntime:
    """Executes a JointPlan without AI2-THOR or thread timing."""

    def __init__(
        self,
        world_state: WorldState,
        *,
        scenario: Optional[Scenario] = None,
    ) -> None:
        self.world_state = world_state.copy()
        self.scenario = scenario
        self.walkable_map: Optional[GlobalWalkableMap] = None
        self.replan_count = 0
        self._walkable_events_by_step: Dict[int, Tuple[WalkableEvent, ...]] = {}
        if scenario is not None and scenario.execution.walkable_events:
            robot_ids = tuple(robot.robot_id for robot in scenario.robots)
            self.walkable_map = GlobalWalkableMap(robot_ids, scenario.grid_size_m)
            grouped: Dict[int, List[WalkableEvent]] = {}
            for event in scenario.execution.walkable_events:
                grouped.setdefault(event.at_committed_step, []).append(event)
            self._walkable_events_by_step = {
                step: tuple(events) for step, events in grouped.items()
            }

    def _walkable_result_fields(self) -> Dict[str, Any]:
        if self.walkable_map is None:
            return {
                "walkable": (
                    frozenset()
                    if self.scenario is None
                    else self.scenario.walkable
                ),
                "walkable_version": 0,
                "replan_count": self.replan_count,
            }
        return {
            "walkable": self.walkable_map.walkable,
            "walkable_version": self.walkable_map.version,
            "replan_count": self.replan_count,
        }

    def _invalid_walkable_result(
        self,
        plan: JointPlan,
        trace: DecisionTrace,
        committed: int,
        message: str,
    ) -> ExecutionResult:
        plan.reservations.release_from(0)
        trace.add("reservations_released", from_tick=0)
        self._abort_active_robots()
        trace.add(
            "execution_finished",
            status="INVALID_WALKABLE_UPDATE",
            committed_micro_steps=committed,
            message=message,
        )
        return ExecutionResult(
            status="INVALID_WALKABLE_UPDATE",
            world_state=self.world_state.copy(),
            committed_micro_steps=committed,
            decision_trace=trace,
            message=message,
            **self._walkable_result_fields(),
        )

    def _apply_walkable_events(
        self,
        committed: int,
        moved_robot_id: Optional[str],
        trace: DecisionTrace,
    ) -> Optional[str]:
        if self.walkable_map is None:
            return None
        events = self._walkable_events_by_step.get(committed, ())
        robot_events = [event for event in events if event.event_type == "robot_step"]
        if committed == 0:
            if robot_events:
                return "A robot_step event cannot occur before the first committed move."
        elif len(robot_events) != 1:
            return f"Committed step {committed} requires exactly one robot_step event."
        elif robot_events[0].robot_id != moved_robot_id:
            return (
                f"Committed step {committed} moved robot {moved_robot_id!r}, but its "
                f"walkable event targets {robot_events[0].robot_id!r}."
            )

        candidate = self.walkable_map.copy()
        applied = []
        try:
            for event in events:
                if event.event_type == "robot_step":
                    update = candidate.update_robot(
                        str(event.robot_id),
                        event.reachable_positions,
                    )
                else:
                    update = candidate.replace_all(
                        event.reachable_positions_by_robot
                    )
                applied.append((event, update))
            missing_positions = {
                robot_id: position
                for robot_id, position in self.world_state.positions.items()
                if position not in candidate.walkable
            }
            if missing_positions:
                rendered = {
                    robot_id: position.to_list()
                    for robot_id, position in sorted(missing_positions.items())
                }
                raise WalkableUpdateError(
                    f"Updated global walkable omits robot positions: {rendered}."
                )
        except WalkableUpdateError as exc:
            return str(exc)

        self.walkable_map = candidate
        for event, update in applied:
            trace.add(
                "walkable_updated",
                at_committed_step=committed,
                update_type=event.event_type,
                robot_id=event.robot_id,
                walkable_version=update.version,
                changed=update.changed,
                added=[point.to_list() for point in sorted(update.added)],
                removed=[point.to_list() for point in sorted(update.removed)],
            )
        return None

    def _invalidated_plan_points(
        self,
        plan: JointPlan,
        next_step_index: int,
    ) -> FrozenSet[GridPoint]:
        if self.walkable_map is None:
            return frozenset()
        required = {
            candidate.position for candidate in plan.assignment.values()
        }
        required.update(
            step.target for step in plan.micro_steps[next_step_index:]
        )
        return frozenset(required - self.walkable_map.walkable)

    def _replan_failed_result(
        self,
        trace: DecisionTrace,
        committed: int,
        planning_result: PlanningResult,
    ) -> ExecutionResult:
        self._abort_active_robots()
        message = (
            "Dynamic walkable update invalidated the active plan, and replanning "
            f"finished with {planning_result.status}."
        )
        trace.add(
            "replanning_finished",
            status=planning_result.status,
            replan_count=self.replan_count,
            message=planning_result.message,
        )
        trace.add(
            "execution_finished",
            status="REPLAN_FAILED",
            committed_micro_steps=committed,
            message=message,
        )
        return ExecutionResult(
            status="REPLAN_FAILED",
            world_state=self.world_state.copy(),
            committed_micro_steps=committed,
            decision_trace=trace,
            message=message,
            **self._walkable_result_fields(),
        )

    def _abort_active_robots(self, failed_robot_id: Optional[str] = None) -> None:
        for robot_id in sorted(self.world_state.statuses):
            self.world_state.statuses[robot_id] = (
                "FAILED" if robot_id == failed_robot_id else "ABORTED"
            )

    def _stale_result(
        self,
        plan: JointPlan,
        trace: DecisionTrace,
        committed: int,
        release_from_tick: int,
        message: str,
    ) -> ExecutionResult:
        plan.reservations.release_from(release_from_tick)
        trace.add("reservations_released", from_tick=release_from_tick)
        self._abort_active_robots()
        trace.add(
            "execution_finished",
            status="COMMIT_REJECTED_STALE_WORLD",
            committed_micro_steps=committed,
            message=message,
        )
        return ExecutionResult(
            status="COMMIT_REJECTED_STALE_WORLD",
            world_state=self.world_state.copy(),
            committed_micro_steps=committed,
            decision_trace=trace,
            message=message,
            **self._walkable_result_fields(),
        )

    def execute(
        self,
        plan: JointPlan,
        *,
        failure_at_micro_step: Optional[int] = None,
        external_version_bump_before_micro_step: Optional[int] = None,
    ) -> ExecutionResult:
        trace = DecisionTrace()
        if (
            self.world_state.version != plan.base_world_version
            or self.world_state.positions != plan.initial_positions
        ):
            return self._stale_result(
                plan,
                trace,
                0,
                0,
                "World version or initial robot positions changed before execution.",
            )

        committed = 0
        attempted = 0
        active_plan = plan
        next_step_index = 0

        walkable_error = self._apply_walkable_events(0, None, trace)
        if walkable_error is not None:
            return self._invalid_walkable_result(
                active_plan,
                trace,
                committed,
                walkable_error,
            )

        while True:
            invalidated = self._invalidated_plan_points(
                active_plan,
                next_step_index,
            )
            if invalidated:
                release_from_tick = (
                    0
                    if next_step_index == 0
                    else active_plan.micro_steps[next_step_index - 1].tick
                )
                active_plan.reservations.release_from(release_from_tick)
                trace.add("reservations_released", from_tick=release_from_tick)
                trace.add(
                    "plan_invalidated",
                    at_committed_step=committed,
                    missing_points=[point.to_list() for point in sorted(invalidated)],
                )
                if self.scenario is None or self.walkable_map is None:
                    return self._invalid_walkable_result(
                        active_plan,
                        trace,
                        committed,
                        "Dynamic replanning requires a scenario and global walkable map.",
                    )
                self.replan_count += 1
                trace.add(
                    "replanning_started",
                    replan_count=self.replan_count,
                    world_version=self.world_state.version,
                )
                self.scenario = replace(
                    self.scenario,
                    walkable=self.walkable_map.walkable,
                )
                planning_result = plan_scenario(self.scenario, self.world_state)
                if planning_result.status != "PLANNED" or planning_result.plan is None:
                    return self._replan_failed_result(
                        trace,
                        committed,
                        planning_result,
                    )
                active_plan = planning_result.plan
                next_step_index = 0
                trace.add(
                    "replanning_finished",
                    status="PLANNED",
                    replan_count=self.replan_count,
                    base_world_version=active_plan.base_world_version,
                    micro_steps=len(active_plan.micro_steps),
                )
                continue

            if next_step_index >= len(active_plan.micro_steps):
                break

            step = active_plan.micro_steps[next_step_index]
            if external_version_bump_before_micro_step == attempted:
                self.world_state.version += 1
                trace.add(
                    "external_world_change",
                    before_micro_step=attempted,
                    world_version=self.world_state.version,
                )

            if self.world_state.version != step.expected_world_version:
                return self._stale_result(
                    active_plan,
                    trace,
                    committed,
                    step.tick + 1,
                    "World version changed while executing the reserved plan.",
                )
            if self.world_state.positions.get(step.robot_id) != step.source:
                return self._stale_result(
                    active_plan,
                    trace,
                    committed,
                    step.tick + 1,
                    f"Robot {step.robot_id!r} is not at its reserved source position.",
                )

            expected_tick_positions = active_plan.reservations.positions_by_tick.get(
                step.tick + 1
            )
            if (
                expected_tick_positions is None
                or expected_tick_positions.get(step.robot_id) != step.target
            ):
                return self._stale_result(
                    active_plan,
                    trace,
                    committed,
                    step.tick + 1,
                    "Target position is no longer reserved for this micro-step.",
                )

            if failure_at_micro_step == attempted:
                active_plan.reservations.release_from(step.tick + 1)
                trace.add("reservations_released", from_tick=step.tick + 1)
                self._abort_active_robots(step.robot_id)
                trace.add(
                    "micro_step_failed",
                    index=step.index,
                    tick=step.tick,
                    robot_id=step.robot_id,
                    position=step.source.to_list(),
                )
                trace.add(
                    "execution_finished",
                    status="EXECUTION_FAILED",
                    committed_micro_steps=committed,
                    failed_robot_id=step.robot_id,
                )
                return ExecutionResult(
                    status="EXECUTION_FAILED",
                    world_state=self.world_state.copy(),
                    committed_micro_steps=committed,
                    decision_trace=trace,
                    failed_robot_id=step.robot_id,
                    message=f"Injected failure at micro-step {attempted}.",
                    **self._walkable_result_fields(),
                )

            self.world_state.positions[step.robot_id] = step.target
            self.world_state.version += 1
            committed += 1
            trace.add(
                "micro_step_committed",
                index=step.index,
                execution_index=attempted,
                tick=step.tick,
                robot_id=step.robot_id,
                position=step.target.to_list(),
                world_version=self.world_state.version,
            )
            attempted += 1
            next_step_index += 1
            walkable_error = self._apply_walkable_events(
                committed,
                step.robot_id,
                trace,
            )
            if walkable_error is not None:
                return self._invalid_walkable_result(
                    active_plan,
                    trace,
                    committed,
                    walkable_error,
                )

        for robot_id in sorted(self.world_state.statuses):
            self.world_state.statuses[robot_id] = "DONE"
        trace.add(
            "execution_finished",
            status="EXECUTED",
            committed_micro_steps=committed,
        )
        return ExecutionResult(
            status="EXECUTED",
            world_state=self.world_state.copy(),
            committed_micro_steps=committed,
            decision_trace=trace,
            **self._walkable_result_fields(),
        )


def _path_position(path: Sequence[GridPoint], tick: int) -> GridPoint:
    return path[min(tick, len(path) - 1)]


def _conflict_model(scenario: Scenario) -> CompositeConflictModel:
    return CompositeConflictModel(
        (
            GeometryConflictModel(scenario.grid_size_m, scenario.hard_clearance_m),
            TableConflictModel(scenario.conflicts),
        )
    )


def _assignment_conflict(
    assignment: Sequence[Candidate],
    conflict_model: CompositeConflictModel,
) -> Optional[Tuple[str, str]]:
    for index, candidate in enumerate(assignment):
        for other in assignment[index + 1 :]:
            if conflict_model.conflicts(candidate.position, other.position):
                return candidate.candidate_id, other.candidate_id
    return None


def _candidate_assignments(
    scenario: Scenario,
    conflict_model: CompositeConflictModel,
    trace: DecisionTrace,
) -> List[Tuple[float, Tuple[str, ...], Dict[str, Candidate]]]:
    assignments = []
    for candidate_tuple in itertools.product(
        *(robot.candidates for robot in scenario.robots)
    ):
        candidate_ids = tuple(candidate.candidate_id for candidate in candidate_tuple)
        invalid_candidates = [
            candidate.candidate_id
            for candidate in candidate_tuple
            if candidate.position not in scenario.walkable
        ]
        if invalid_candidates:
            trace.add(
                "assignment_rejected",
                candidates=list(candidate_ids),
                reason="candidate_not_walkable",
                invalid_candidates=invalid_candidates,
            )
            continue
        conflict = _assignment_conflict(candidate_tuple, conflict_model)
        if conflict is not None:
            trace.add(
                "assignment_rejected",
                candidates=list(candidate_ids),
                reason="endpoint_conflict",
                conflict=list(conflict),
            )
            continue
        total_cost = round(sum(candidate.cost for candidate in candidate_tuple), 9)
        assignments.append(
            (
                total_cost,
                candidate_ids,
                {
                    robot.robot_id: candidate
                    for robot, candidate in zip(scenario.robots, candidate_tuple)
                },
            )
        )
    assignments.sort(key=lambda item: (item[0], item[1]))
    return assignments


def _other_path_transition(
    path: Sequence[GridPoint],
    tick: int,
) -> Tuple[GridPoint, GridPoint]:
    return _path_position(path, tick), _path_position(path, tick + 1)


def _transition_is_reserved(
    source: GridPoint,
    target: GridPoint,
    tick: int,
    planned_paths: Mapping[str, Tuple[GridPoint, ...]],
    conflict_model: CompositeConflictModel,
) -> bool:
    for path in planned_paths.values():
        other_source, other_target = _other_path_transition(path, tick)
        if conflict_model.conflicts(target, other_target):
            return True
        if source == other_target and target == other_source:
            return True
    return False


def _goal_can_remain_reserved(
    goal: GridPoint,
    arrival_tick: int,
    max_ticks: int,
    planned_paths: Mapping[str, Tuple[GridPoint, ...]],
    conflict_model: CompositeConflictModel,
) -> bool:
    return all(
        not any(
            conflict_model.conflicts(goal, _path_position(path, tick))
            for path in planned_paths.values()
        )
        for tick in range(arrival_tick, max_ticks + 1)
    )


def _neighbors(point: GridPoint, walkable: FrozenSet[GridPoint]) -> List[GridPoint]:
    adjacent = [
        GridPoint(point.x - 1, point.z),
        GridPoint(point.x, point.z - 1),
        GridPoint(point.x, point.z + 1),
        GridPoint(point.x + 1, point.z),
    ]
    return [point, *(neighbor for neighbor in adjacent if neighbor in walkable)]


def _reconstruct_path(
    parents: Mapping[Tuple[GridPoint, int], Optional[Tuple[GridPoint, int]]],
    state: Tuple[GridPoint, int],
) -> Tuple[GridPoint, ...]:
    reversed_path = []
    current: Optional[Tuple[GridPoint, int]] = state
    while current is not None:
        reversed_path.append(current[0])
        current = parents[current]
    reversed_path.reverse()
    return tuple(reversed_path)


def _space_time_a_star(
    start: GridPoint,
    goal: GridPoint,
    scenario: Scenario,
    planned_paths: Mapping[str, Tuple[GridPoint, ...]],
    conflict_model: CompositeConflictModel,
) -> Optional[Tuple[GridPoint, ...]]:
    permanent_obstacles = tuple(
        path[0]
        for path in planned_paths.values()
        if len(set(path)) == 1
    )
    if permanent_obstacles and not _static_grid_path_exists(
        start,
        goal,
        scenario,
        permanent_obstacles,
        conflict_model,
    ):
        return None

    start_state = (start, 0)
    parents: Dict[
        Tuple[GridPoint, int], Optional[Tuple[GridPoint, int]]
    ] = {start_state: None}
    frontier = []
    sequence = itertools.count()
    start_h = abs(start.x - goal.x) + abs(start.z - goal.z)
    heapq.heappush(frontier, (start_h, 0, start.x, start.z, next(sequence), start_state))

    while frontier:
        _score, tick, _x, _z, _sequence_id, state = heapq.heappop(frontier)
        point, state_tick = state
        if state_tick != tick:
            continue
        if point == goal and _goal_can_remain_reserved(
            goal,
            tick,
            scenario.max_ticks,
            planned_paths,
            conflict_model,
        ):
            return _reconstruct_path(parents, state)
        if tick >= scenario.max_ticks:
            continue

        for neighbor in _neighbors(point, scenario.walkable):
            if (point, neighbor) in scenario.blocked_transitions:
                continue
            next_tick = tick + 1
            next_state = (neighbor, next_tick)
            if next_state in parents:
                continue
            if _transition_is_reserved(
                point,
                neighbor,
                tick,
                planned_paths,
                conflict_model,
            ):
                continue
            parents[next_state] = state
            heuristic = abs(neighbor.x - goal.x) + abs(neighbor.z - goal.z)
            heapq.heappush(
                frontier,
                (
                    next_tick + heuristic,
                    next_tick,
                    neighbor.x,
                    neighbor.z,
                    next(sequence),
                    next_state,
                ),
            )
    return None


def _static_grid_path_exists(
    start: GridPoint,
    goal: GridPoint,
    scenario: Scenario,
    permanent_obstacles: Sequence[GridPoint],
    conflict_model: CompositeConflictModel,
) -> bool:
    """Quickly reject routes blocked by robots that never leave their start."""

    frontier = [start]
    visited = {start}
    while frontier:
        current = frontier.pop()
        if current == goal:
            return True
        for neighbor in _neighbors(current, scenario.walkable)[1:]:
            if neighbor in visited:
                continue
            if (current, neighbor) in scenario.blocked_transitions:
                continue
            if any(
                conflict_model.conflicts(neighbor, obstacle)
                for obstacle in permanent_obstacles
            ):
                continue
            visited.add(neighbor)
            frontier.append(neighbor)
    return False


def _segment_distance_m(
    source: GridPoint,
    target: GridPoint,
    other: GridPoint,
    grid_size_m: float,
) -> float:
    sx, sz = source.x * grid_size_m, source.z * grid_size_m
    tx, tz = target.x * grid_size_m, target.z * grid_size_m
    ox, oz = other.x * grid_size_m, other.z * grid_size_m
    dx, dz = tx - sx, tz - sz
    length_squared = dx * dx + dz * dz
    if length_squared == 0:
        return math.hypot(ox - sx, oz - sz)
    fraction = ((ox - sx) * dx + (oz - sz) * dz) / length_squared
    fraction = max(0.0, min(1.0, fraction))
    closest_x = sx + fraction * dx
    closest_z = sz + fraction * dz
    return math.hypot(ox - closest_x, oz - closest_z)


def _build_serial_micro_steps(
    paths: Mapping[str, Tuple[GridPoint, ...]],
    priority_order: Tuple[str, ...],
    scenario: Scenario,
    base_world_version: int,
    conflict_model: CompositeConflictModel,
) -> Optional[Tuple[MicroStep, ...]]:
    makespan = max(len(path) - 1 for path in paths.values())
    micro_steps = []
    for tick in range(makespan):
        positions = {
            robot_id: _path_position(path, tick)
            for robot_id, path in paths.items()
        }
        expected_positions = {
            robot_id: _path_position(path, tick + 1)
            for robot_id, path in paths.items()
        }
        intermediate = dict(positions)
        for robot_id in priority_order:
            source = intermediate[robot_id]
            target = expected_positions[robot_id]
            if source == target:
                continue
            for other_id, other_position in intermediate.items():
                if other_id == robot_id:
                    continue
                if conflict_model.conflicts(target, other_position):
                    return None
                if (
                    _segment_distance_m(
                        source,
                        target,
                        other_position,
                        scenario.grid_size_m,
                    )
                    <= scenario.hard_clearance_m + 1e-12
                ):
                    return None
            micro_steps.append(
                MicroStep(
                    index=len(micro_steps),
                    tick=tick,
                    robot_id=robot_id,
                    source=source,
                    target=target,
                    expected_world_version=base_world_version + len(micro_steps),
                )
            )
            intermediate[robot_id] = target
        if intermediate != expected_positions:
            return None
    return tuple(micro_steps)


def _plan_for_priority_order(
    scenario: Scenario,
    world_state: WorldState,
    assignment: Mapping[str, Candidate],
    priority_order: Tuple[str, ...],
    conflict_model: CompositeConflictModel,
) -> Optional[Tuple[Dict[str, Tuple[GridPoint, ...]], Tuple[MicroStep, ...]]]:
    paths: Dict[str, Tuple[GridPoint, ...]] = {}
    for robot_id in priority_order:
        path = _space_time_a_star(
            world_state.positions[robot_id],
            assignment[robot_id].position,
            scenario,
            paths,
            conflict_model,
        )
        if path is None:
            return None
        paths[robot_id] = path
    sorted_paths = {robot_id: paths[robot_id] for robot_id in sorted(paths)}
    micro_steps = _build_serial_micro_steps(
        sorted_paths,
        priority_order,
        scenario,
        world_state.version,
        conflict_model,
    )
    if micro_steps is None:
        return None
    return sorted_paths, micro_steps


def plan_scenario(scenario: Scenario, world_state: WorldState) -> PlanningResult:
    """Create a deterministic joint assignment and collision-free grid plan."""

    trace = DecisionTrace()
    expected_ids = [robot.robot_id for robot in scenario.robots]
    if sorted(world_state.positions) != expected_ids:
        raise ScenarioValidationError("WorldState positions do not match scenario robots.")

    conflict_model = _conflict_model(scenario)
    assignments = _candidate_assignments(scenario, conflict_model, trace)
    if not assignments:
        trace.add("planning_finished", status="NO_PLAN_FOUND")
        return PlanningResult("NO_PLAN_FOUND", None, trace, "No conflict-free endpoint assignment.")

    best_plan: Optional[JointPlan] = None
    best_score: Optional[Tuple[Any, ...]] = None
    best_candidate_cost: Optional[float] = None
    assignment_trials = 0
    priority_attempts = 0
    hit_assignment_limit = False

    for candidate_cost, candidate_ids, assignment in assignments:
        if best_candidate_cost is not None and candidate_cost > best_candidate_cost:
            break
        if assignment_trials >= scenario.max_assignment_trials:
            hit_assignment_limit = True
            break
        assignment_trials += 1
        trace.add(
            "assignment_attempt",
            trial=assignment_trials,
            candidate_cost=candidate_cost,
            candidates=list(candidate_ids),
        )
        stationary_ids = tuple(
            robot_id for robot_id in expected_ids if robot_id in scenario.fixed_robot_ids
        )
        moving_ids = tuple(
            robot_id for robot_id in expected_ids if robot_id not in stationary_ids
        )
        priority_orders = (
            stationary_ids + moving_order
            for moving_order in itertools.permutations(moving_ids)
        )
        for priority_order in priority_orders:
            priority_attempts += 1
            planned = _plan_for_priority_order(
                scenario,
                world_state,
                assignment,
                tuple(priority_order),
                conflict_model,
            )
            if planned is None:
                trace.add(
                    "priority_rejected",
                    candidates=list(candidate_ids),
                    priority_order=list(priority_order),
                    reason="path_or_serialization_conflict",
                )
                continue
            paths, micro_steps = planned
            makespan = max(len(path) - 1 for path in paths.values())
            total_moves = sum(
                1
                for path in paths.values()
                for source, target in zip(path, path[1:])
                if source != target
            )
            score = (makespan, total_moves, tuple(priority_order), candidate_ids)
            trace.add(
                "priority_feasible",
                candidates=list(candidate_ids),
                priority_order=list(priority_order),
                makespan=makespan,
                total_moves=total_moves,
            )
            if best_score is None or score < best_score:
                best_candidate_cost = candidate_cost
                best_score = score
                best_plan = JointPlan(
                    base_world_version=world_state.version,
                    initial_positions=dict(world_state.positions),
                    assignment=dict(sorted(assignment.items())),
                    paths=paths,
                    priority_order=tuple(priority_order),
                    reservations=ReservationTable.from_paths(paths, scenario.max_ticks),
                    micro_steps=micro_steps,
                    metrics={
                        "candidate_cost": candidate_cost,
                        "makespan": makespan,
                        "total_moves": total_moves,
                    },
                )

    if best_plan is None:
        status = "SEARCH_LIMIT_REACHED" if hit_assignment_limit else "NO_PLAN_FOUND"
        trace.add(
            "planning_finished",
            status=status,
            assignment_trials=assignment_trials,
            priority_attempts=priority_attempts,
        )
        return PlanningResult(status, None, trace, "No plan found by prioritized search.")

    best_plan.metrics.update(
        {
            "assignment_trials": assignment_trials,
            "priority_attempts": priority_attempts,
            "search_truncated": hit_assignment_limit,
        }
    )
    trace.add(
        "reservations_created",
        horizon=scenario.max_ticks,
        reserved_ticks=len(best_plan.reservations.positions_by_tick),
        micro_steps=len(best_plan.micro_steps),
    )
    trace.add(
        "planning_finished",
        status="PLANNED",
        assignment={
            robot_id: candidate.candidate_id
            for robot_id, candidate in best_plan.assignment.items()
        },
        priority_order=list(best_plan.priority_order),
    )
    return PlanningResult("PLANNED", best_plan, trace)


def build_output(
    planning_result: PlanningResult,
    execution_result: Optional[ExecutionResult] = None,
) -> Dict[str, Any]:
    output = planning_result.to_dict()
    if execution_result is None:
        return output
    output["status"] = execution_result.status
    output["message"] = execution_result.message
    output["execution"] = execution_result.to_dict()
    output["decision_trace"] = [
        *planning_result.decision_trace.to_list(),
        *execution_result.decision_trace.to_list(),
    ]
    return output


def _basic_demo_data() -> Dict[str, Any]:
    return {
        "grid_size_m": 0.25,
        "hard_clearance_m": 0.35,
        "max_ticks": 32,
        "max_assignment_trials": 256,
        "walkable": [[x, z] for x in range(5) for z in range(5)],
        "conflicts": [],
        "robots": [
            {
                "id": "A",
                "start": [0, 0],
                "candidates": [
                    {"id": "A1", "position": [4, 0], "cost": 1.0}
                ],
            },
            {
                "id": "B",
                "start": [0, 4],
                "candidates": [
                    {"id": "B1", "position": [4, 4], "cost": 1.0}
                ],
            },
        ],
    }


def _crossing_demo_data() -> Dict[str, Any]:
    return {
        "grid_size_m": 0.25,
        "hard_clearance_m": 0.1,
        "max_ticks": 32,
        "walkable": [[0, 1], [1, 1], [2, 1], [1, 0], [1, 2]],
        "robots": [
            {
                "id": "A",
                "start": [0, 1],
                "candidates": [
                    {"id": "A1", "position": [2, 1], "cost": 1.0}
                ],
            },
            {
                "id": "B",
                "start": [1, 0],
                "candidates": [
                    {"id": "B1", "position": [1, 2], "cost": 1.0}
                ],
            },
        ],
    }


def _joint_assignment_demo_data() -> Dict[str, Any]:
    data = _basic_demo_data()
    data["robots"][0]["candidates"] = [
        {"id": "A1", "position": [4, 2], "cost": 1.0},
        {"id": "A2", "position": [4, 0], "cost": 2.0},
    ]
    data["robots"][1]["candidates"] = [
        {"id": "B1", "position": [4, 2], "cost": 1.0},
        {"id": "B2", "position": [4, 4], "cost": 2.0},
    ]
    return data


def demo_scenario_data(name: str) -> Dict[str, Any]:
    if name == "joint-assignment":
        return _joint_assignment_demo_data()
    if name == "crossing":
        return _crossing_demo_data()
    if name == "failure":
        data = _basic_demo_data()
        data["execution"] = {"failure_at_micro_step": 1}
        return data
    if name == "stale-world":
        data = _basic_demo_data()
        data["execution"] = {"external_version_bump_before_micro_step": 0}
        return data
    raise ScenarioValidationError(f"Unknown demo scenario: {name!r}.")


def _empty_output(status: str, message: str) -> Dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "base_world_version": None,
        "assignment": {},
        "paths": {},
        "priority_order": [],
        "reservations": {},
        "micro_steps": [],
        "metrics": {},
        "execution": None,
        "decision_trace": [],
    }


def _write_output(output: Mapping[str, Any], pretty: bool) -> None:
    if pretty:
        rendered = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        rendered = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    sys.stdout.write(rendered + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan deterministic multi-robot avoidance without AI2-THOR.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenario-file", type=Path)
    source.add_argument(
        "--demo",
        choices=("joint-assignment", "crossing", "failure", "stale-world"),
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.scenario_file is not None:
            with args.scenario_file.open("r", encoding="utf-8") as scenario_file:
                raw_data = json.load(scenario_file)
        else:
            raw_data = demo_scenario_data(args.demo)
        scenario = load_scenario(raw_data)
    except (OSError, json.JSONDecodeError, ScenarioValidationError) as exc:
        _write_output(_empty_output("INVALID_SCENARIO", str(exc)), args.pretty)
        return 2

    world_state = WorldState.from_scenario(scenario)
    planning_result = plan_scenario(scenario, world_state)
    if planning_result.status != "PLANNED":
        _write_output(planning_result.to_dict(), args.pretty)
        return 3
    if args.plan_only:
        _write_output(planning_result.to_dict(), args.pretty)
        return 0

    runtime = FakeRuntime(world_state, scenario=scenario)
    execution_result = runtime.execute(
        planning_result.plan,
        failure_at_micro_step=scenario.execution.failure_at_micro_step,
        external_version_bump_before_micro_step=(
            scenario.execution.external_version_bump_before_micro_step
        ),
    )
    _write_output(build_output(planning_result, execution_result), args.pretty)
    return 0 if execution_result.status == "EXECUTED" else 4


def _positive_number(data: Mapping[str, Any], name: str, default: float) -> float:
    raw = data.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ScenarioValidationError(f"{name} must be a positive finite number.")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ScenarioValidationError(f"{name} must be a positive finite number.")
    return value


def _positive_int(data: Mapping[str, Any], name: str, default: int) -> int:
    raw = data.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ScenarioValidationError(f"{name} must be a positive integer.")
    return raw


def _optional_step(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScenarioValidationError(f"{field_name} must be null or a non-negative integer.")
    return value


def _parse_walkable_events(
    raw_events: Any,
    robot_ids: Sequence[str],
    grid_size_m: float,
) -> Tuple[WalkableEvent, ...]:
    if raw_events is None:
        return ()
    if not isinstance(raw_events, list):
        raise ScenarioValidationError("execution.walkable_events must be a list.")
    if not raw_events:
        return ()

    expected_robot_ids = set(robot_ids)
    events = []
    seen_by_step: Dict[int, Set[str]] = {}
    previous_step = -1
    for index, raw_event in enumerate(raw_events):
        field_name = f"execution.walkable_events[{index}]"
        if not isinstance(raw_event, Mapping):
            raise ScenarioValidationError(f"{field_name} must be an object.")
        committed_step = _optional_step(
            raw_event.get("at_committed_step"),
            f"{field_name}.at_committed_step",
        )
        if committed_step is None:
            raise ScenarioValidationError(
                f"{field_name}.at_committed_step must be a non-negative integer."
            )
        if committed_step < previous_step:
            raise ScenarioValidationError(
                "execution.walkable_events must be ordered by at_committed_step."
            )
        previous_step = committed_step

        event_type = raw_event.get("type")
        if event_type not in {"robot_step", "active_refresh"}:
            raise ScenarioValidationError(
                f"{field_name}.type must be 'robot_step' or 'active_refresh'."
            )
        seen_types = seen_by_step.setdefault(committed_step, set())
        if event_type in seen_types:
            raise ScenarioValidationError(
                f"Duplicate {event_type!r} event at committed step {committed_step}."
            )
        if event_type == "robot_step" and "active_refresh" in seen_types:
            raise ScenarioValidationError(
                "robot_step must precede active_refresh at the same committed step."
            )
        seen_types.add(event_type)

        try:
            if event_type == "robot_step":
                if committed_step == 0:
                    raise ScenarioValidationError(
                        "robot_step cannot occur before any movement is committed."
                    )
                robot_id = raw_event.get("robot_id")
                if robot_id not in expected_robot_ids:
                    raise ScenarioValidationError(
                        f"{field_name}.robot_id must identify a configured robot."
                    )
                positions = raw_event.get("reachable_positions")
                validator = GlobalWalkableMap((str(robot_id),), grid_size_m)
                validator.replace_all({str(robot_id): positions})
                events.append(
                    WalkableEvent(
                        at_committed_step=committed_step,
                        event_type=event_type,
                        robot_id=str(robot_id),
                        reachable_positions=tuple(dict(position) for position in positions),
                    )
                )
            else:
                positions_by_robot = raw_event.get("reachable_positions_by_robot")
                validator = GlobalWalkableMap(tuple(robot_ids), grid_size_m)
                validator.replace_all(positions_by_robot)
                events.append(
                    WalkableEvent(
                        at_committed_step=committed_step,
                        event_type=event_type,
                        reachable_positions_by_robot={
                            robot_id: tuple(
                                dict(position)
                                for position in positions_by_robot[robot_id]
                            )
                            for robot_id in robot_ids
                        },
                    )
                )
        except WalkableUpdateError as exc:
            raise ScenarioValidationError(f"{field_name}: {exc}") from exc

    first = events[0]
    if first.at_committed_step != 0 or first.event_type != "active_refresh":
        raise ScenarioValidationError(
            "Dynamic walkable mode requires an active_refresh at committed step 0."
        )
    return tuple(events)


def load_scenario(data: Mapping[str, Any]) -> Scenario:
    """Validate and normalize a JSON-compatible scenario mapping."""

    if not isinstance(data, Mapping):
        raise ScenarioValidationError("Scenario must be a JSON object.")

    grid_size_m = _positive_number(data, "grid_size_m", 0.25)
    hard_clearance_m = _positive_number(data, "hard_clearance_m", 0.35)

    raw_walkable = data.get("walkable")
    if not isinstance(raw_walkable, list) or not raw_walkable:
        raise ScenarioValidationError("walkable must be a non-empty list of grid points.")
    walkable = frozenset(
        GridPoint.from_value(value, f"walkable[{index}]")
        for index, value in enumerate(raw_walkable)
    )
    if len(walkable) != len(raw_walkable):
        raise ScenarioValidationError("walkable contains duplicate grid points.")

    max_ticks = _positive_int(data, "max_ticks", max(32, 2 * len(walkable)))
    max_assignment_trials = _positive_int(data, "max_assignment_trials", 256)

    raw_conflicts = data.get("conflicts", [])
    if not isinstance(raw_conflicts, list):
        raise ScenarioValidationError("conflicts must be a list of point pairs.")
    conflict_pairs = set()
    for index, pair in enumerate(raw_conflicts):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ScenarioValidationError(f"conflicts[{index}] must contain two points.")
        left = GridPoint.from_value(pair[0], f"conflicts[{index}][0]")
        right = GridPoint.from_value(pair[1], f"conflicts[{index}][1]")
        if left not in walkable or right not in walkable:
            raise ScenarioValidationError("conflict points must be walkable.")
        conflict_pairs.add(tuple(sorted((left, right))))

    raw_blocked_transitions = data.get("blocked_transitions", [])
    if not isinstance(raw_blocked_transitions, list):
        raise ScenarioValidationError(
            "blocked_transitions must be a list of directed point pairs."
        )
    blocked_transitions = set()
    for index, pair in enumerate(raw_blocked_transitions):
        field_name = f"blocked_transitions[{index}]"
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ScenarioValidationError(
                f"{field_name} must contain two points."
            )
        source = GridPoint.from_value(pair[0], f"{field_name}[0]")
        target = GridPoint.from_value(pair[1], f"{field_name}[1]")
        if source == target:
            raise ScenarioValidationError(
                f"{field_name} must contain two different points."
            )
        if source not in walkable or target not in walkable:
            raise ScenarioValidationError(
                "blocked transition points must be walkable."
            )
        blocked_transitions.add((source, target))

    raw_robots = data.get("robots")
    if not isinstance(raw_robots, list) or not 1 <= len(raw_robots) <= 4:
        raise ScenarioValidationError("Scenario must define 1 to 4 robots.")

    robot_ids = set()
    candidate_ids = set()
    robots = []
    for robot_index, raw_robot in enumerate(raw_robots):
        if not isinstance(raw_robot, Mapping):
            raise ScenarioValidationError(f"robots[{robot_index}] must be an object.")
        robot_id = raw_robot.get("id")
        if not isinstance(robot_id, str) or not robot_id:
            raise ScenarioValidationError(f"robots[{robot_index}].id must be non-empty.")
        if robot_id in robot_ids:
            raise ScenarioValidationError(f"Duplicate robot id: {robot_id!r}.")
        robot_ids.add(robot_id)
        start = GridPoint.from_value(raw_robot.get("start"), f"robots[{robot_index}].start")
        if start not in walkable:
            raise ScenarioValidationError(f"Robot {robot_id!r} start is not walkable.")

        raw_candidates = raw_robot.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise ScenarioValidationError(f"Robot {robot_id!r} needs at least one candidate.")
        candidates = []
        for candidate_index, raw_candidate in enumerate(raw_candidates):
            if not isinstance(raw_candidate, Mapping):
                raise ScenarioValidationError("Each candidate must be an object.")
            candidate_id = raw_candidate.get("id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ScenarioValidationError("Candidate id must be a non-empty string.")
            if candidate_id in candidate_ids:
                raise ScenarioValidationError(f"Duplicate candidate id: {candidate_id!r}.")
            candidate_ids.add(candidate_id)
            position = GridPoint.from_value(
                raw_candidate.get("position"),
                f"robots[{robot_index}].candidates[{candidate_index}].position",
            )
            if position not in walkable:
                raise ScenarioValidationError(
                    f"Candidate {candidate_id!r} position is not walkable."
                )
            raw_cost = raw_candidate.get("cost", 0.0)
            if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)):
                raise ScenarioValidationError("Candidate cost must be finite and non-negative.")
            cost = float(raw_cost)
            if not math.isfinite(cost) or cost < 0:
                raise ScenarioValidationError("Candidate cost must be finite and non-negative.")
            candidates.append(Candidate(candidate_id, position, cost))
        robots.append(
            RobotIntent(
                robot_id=robot_id,
                start=start,
                candidates=tuple(sorted(candidates, key=lambda item: (item.cost, item.candidate_id))),
            )
        )

    robots.sort(key=lambda robot: robot.robot_id)
    initial_conflicts = CompositeConflictModel(
        (
            GeometryConflictModel(grid_size_m, hard_clearance_m),
            TableConflictModel(conflict_pairs),
        )
    )
    for index, robot in enumerate(robots):
        for other in robots[index + 1 :]:
            if initial_conflicts.conflicts(robot.start, other.start):
                raise ScenarioValidationError(
                    f"Robots {robot.robot_id!r} and {other.robot_id!r} have an "
                    "initial conflict."
                )

    raw_execution = data.get("execution", {})
    if raw_execution is None:
        raw_execution = {}
    if not isinstance(raw_execution, Mapping):
        raise ScenarioValidationError("execution must be an object.")
    execution = ExecutionConfig(
        failure_at_micro_step=_optional_step(
            raw_execution.get("failure_at_micro_step"),
            "execution.failure_at_micro_step",
        ),
        external_version_bump_before_micro_step=_optional_step(
            raw_execution.get("external_version_bump_before_micro_step"),
            "execution.external_version_bump_before_micro_step",
        ),
        walkable_events=_parse_walkable_events(
            raw_execution.get("walkable_events"),
            [robot.robot_id for robot in robots],
            grid_size_m,
        ),
    )

    return Scenario(
        grid_size_m=grid_size_m,
        hard_clearance_m=hard_clearance_m,
        max_ticks=max_ticks,
        max_assignment_trials=max_assignment_trials,
        walkable=walkable,
        conflicts=frozenset(conflict_pairs),
        robots=tuple(robots),
        blocked_transitions=frozenset(blocked_transitions),
        execution=execution,
    )


if __name__ == "__main__":
    raise SystemExit(main())
