"""Runtime adapter between navigation requests and the deterministic planner."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from multi_robot_avoidance import (
    Candidate,
    GeometryConflictModel,
    GlobalWalkableMap,
    GridPoint,
    RobotIntent,
    Scenario,
    WorldState,
    plan_scenario,
)

from .movement import (
    MovementConfig,
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
    StepNavigationError,
)
from .utils import position_inside_aabb_footprint


@dataclass(frozen=True)
class RuntimeWorldSnapshot:
    version: int
    positions: Dict[int, GridPoint]
    thor_positions: Dict[GridPoint, Dict[str, float]]
    walkable_map: GlobalWalkableMap


@dataclass
class ActiveNavigationState:
    request: NavigationRequest
    excluded_candidate_keys: Set[GridPoint] = field(default_factory=set)
    decision_trace: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[NavigationResult] = None


@dataclass(frozen=True)
class _ExecutionBoundary:
    kind: str
    release_tick: int
    snapshot: RuntimeWorldSnapshot
    failed_transitions: FrozenSet[Tuple[GridPoint, GridPoint]] = frozenset()
    reason: str = ""


class StepMovementCoordinator:
    def __init__(
        self,
        runtime,
        config: MovementConfig,
        metrics: NavigationMetrics,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.metrics = metrics
        robot_ids = tuple(
            str(agent_id)
            for agent_id in range(int(runtime.physical_agent_count))
        )
        self.walkable_map = GlobalWalkableMap(robot_ids, config.grid_size_m)

    def _grid_point(self, position: Mapping[str, float]) -> GridPoint:
        return GridPoint(
            round(float(position["x"]) / self.config.grid_size_m),
            round(float(position["z"]) / self.config.grid_size_m),
        )

    def _authoritative_grid_point(
        self,
        agent_id: int,
        position: Mapping[str, float],
        walkable: FrozenSet[GridPoint],
    ) -> GridPoint:
        point = self._grid_point(position)
        snapped_x = point.x * self.config.grid_size_m
        snapped_z = point.z * self.config.grid_size_m
        offset = math.hypot(
            float(position["x"]) - snapped_x,
            float(position["z"]) - snapped_z,
        )
        if offset > self.config.grid_snap_tolerance_m or point not in walkable:
            raise StepNavigationError(
                f"agent {agent_id} position {dict(position)} cannot be mapped "
                "to a reachable navigation grid point"
            )
        return point

    def refresh_world(self) -> RuntimeWorldSnapshot:
        snapshots = {
            str(agent_id): self.runtime.refresh_reachable_positions(agent_id)
            for agent_id in range(int(self.runtime.physical_agent_count))
        }
        update = self.walkable_map.replace_all(snapshots)

        raw_positions = [
            dict(position)
            for positions in snapshots.values()
            for position in positions
        ]
        raw_positions.sort(
            key=lambda position: (
                float(position["x"]),
                float(position["z"]),
                float(position.get("y", 0.0)),
            )
        )
        thor_positions: Dict[GridPoint, Dict[str, float]] = {}
        for position in raw_positions:
            thor_positions.setdefault(self._grid_point(position), position)

        positions = {}
        for agent_id in range(int(self.runtime.physical_agent_count)):
            actual = self.runtime.current_agent_position(agent_id)
            positions[agent_id] = self._authoritative_grid_point(
                agent_id,
                actual,
                update.walkable,
            )
        return RuntimeWorldSnapshot(
            version=update.version,
            positions=positions,
            thor_positions=thor_positions,
            walkable_map=self.walkable_map.copy(),
        )

    def _shortest_grid_path(
        self,
        start: GridPoint,
        goal: GridPoint,
        walkable: FrozenSet[GridPoint],
    ) -> Tuple[GridPoint, ...]:
        if start == goal:
            return (start,)
        frontier = [start]
        frontier_index = 0
        parents: Dict[GridPoint, Optional[GridPoint]] = {start: None}
        while frontier_index < len(frontier):
            current = frontier[frontier_index]
            frontier_index += 1
            neighbors = sorted(
                (
                    GridPoint(current.x - 1, current.z),
                    GridPoint(current.x + 1, current.z),
                    GridPoint(current.x, current.z - 1),
                    GridPoint(current.x, current.z + 1),
                )
            )
            for neighbor in neighbors:
                if neighbor not in walkable or neighbor in parents:
                    continue
                parents[neighbor] = current
                if neighbor == goal:
                    path = [goal]
                    cursor = goal
                    while parents[cursor] is not None:
                        cursor = parents[cursor]
                        path.append(cursor)
                    return tuple(reversed(path))
                frontier.append(neighbor)
        return ()

    def _active_corridor(
        self,
        active_requests: Sequence[NavigationRequest],
        snapshot: RuntimeWorldSnapshot,
    ) -> FrozenSet[GridPoint]:
        corridor = set()
        walkable = snapshot.walkable_map.walkable
        for request in sorted(active_requests, key=lambda item: item.agent_id):
            start = snapshot.positions[request.agent_id]
            for position in request.candidate_positions:
                goal = self._grid_point(position)
                if goal not in walkable:
                    continue
                corridor.update(self._shortest_grid_path(start, goal, walkable))
        return frozenset(corridor)

    def parking_candidates(
        self,
        agent_id: int,
        active_requests: Sequence[NavigationRequest],
        snapshot: RuntimeWorldSnapshot,
    ) -> Tuple[Dict[str, float], ...]:
        """Return up to three deterministic, physically clear parking points."""

        current_agent_id = int(agent_id)
        current = snapshot.positions[current_agent_id]
        active_targets = frozenset(
            self._grid_point(position)
            for request in active_requests
            for position in request.candidate_positions
            if self._grid_point(position) in snapshot.walkable_map.walkable
        )
        active_corridor = self._active_corridor(active_requests, snapshot)
        other_positions = tuple(
            position
            for other_agent_id, position in sorted(snapshot.positions.items())
            if other_agent_id != current_agent_id
        )
        conflict_model = GeometryConflictModel(
            self.config.grid_size_m,
            self.config.hard_clearance_m,
        )
        object_bounds = tuple(self.runtime.scene_object_bounds(current_agent_id))

        def conflicts_any(point: GridPoint, others: Sequence[GridPoint]) -> bool:
            return any(conflict_model.conflicts(point, other) for other in others)

        eligible = []
        for point in sorted(snapshot.walkable_map.walkable):
            if point == current:
                continue
            if conflicts_any(point, tuple(active_targets)):
                continue
            if conflicts_any(point, tuple(active_corridor)):
                continue
            if conflicts_any(point, other_positions):
                continue
            thor_position = snapshot.thor_positions.get(point)
            if thor_position is None:
                continue
            if any(
                position_inside_aabb_footprint(
                    thor_position,
                    bounds,
                    clearance=clearance,
                )
                for _object_id, bounds, clearance in object_bounds
            ):
                continue
            minimum_target_distance = min(
                (
                    math.hypot(point.x - target.x, point.z - target.z)
                    * self.config.grid_size_m
                    for target in active_targets
                ),
                default=999999.0,
            )
            minimum_agent_distance = min(
                (
                    math.hypot(point.x - other.x, point.z - other.z)
                    * self.config.grid_size_m
                    for other in other_positions
                ),
                default=999999.0,
            )
            eligible.append(
                (
                    (
                        -minimum_target_distance,
                        -minimum_agent_distance,
                        (point.x, point.z),
                    ),
                    dict(thor_position),
                )
            )
        eligible.sort(key=lambda item: item[0])
        return tuple(position for _key, position in eligible[:3])

    def _parking_candidate_map(
        self,
        completed_agent_ids: FrozenSet[int],
        active_requests: Sequence[NavigationRequest],
        snapshot: RuntimeWorldSnapshot,
    ) -> Dict[int, Tuple[Dict[str, float], ...]]:
        active_agent_ids = {request.agent_id for request in active_requests}
        corridor = self._active_corridor(active_requests, snapshot)
        conflict_model = GeometryConflictModel(
            self.config.grid_size_m,
            self.config.hard_clearance_m,
        )
        parking = {}
        for agent_id in sorted(completed_agent_ids - active_agent_ids):
            if agent_id not in snapshot.positions:
                continue
            current = snapshot.positions[agent_id]
            if not any(
                conflict_model.conflicts(current, corridor_point)
                for corridor_point in corridor
            ):
                continue
            candidates = self.parking_candidates(
                agent_id,
                active_requests,
                snapshot,
            )
            if candidates:
                parking[agent_id] = candidates
        return parking

    def _build_scenario(
        self,
        active_states: Mapping[int, ActiveNavigationState],
        snapshot: RuntimeWorldSnapshot,
        blocked_transitions: FrozenSet[Tuple[GridPoint, GridPoint]],
        parking_candidates: Optional[
            Mapping[int, Sequence[Mapping[str, float]]]
        ] = None,
    ) -> Scenario:
        robots = []
        for agent_id, start in sorted(snapshot.positions.items()):
            state = active_states.get(agent_id)
            if state is None or state.result is not None:
                parking = (parking_candidates or {}).get(agent_id, ())
                if parking:
                    candidates = tuple(
                        Candidate(
                            f"{agent_id}:parking:{index}",
                            self._grid_point(position),
                            float(index)
                            + math.hypot(
                                self._grid_point(position).x - start.x,
                                self._grid_point(position).z - start.z,
                            ),
                        )
                        for index, position in enumerate(parking)
                    )
                else:
                    candidates = (
                        Candidate(f"{agent_id}:static", start, 0.0),
                    )
            else:
                request = state.request
                seen = set()
                candidate_items = []
                for index, position in enumerate(request.candidate_positions):
                    point = self._grid_point(position)
                    if (
                        point in seen
                        or point in state.excluded_candidate_keys
                        or point not in snapshot.walkable_map.walkable
                    ):
                        continue
                    seen.add(point)
                    distance = math.hypot(point.x - start.x, point.z - start.z)
                    candidate_items.append(
                        Candidate(
                            f"{agent_id}:{index}",
                            point,
                            float(index) + distance,
                        )
                    )
                if not candidate_items:
                    raise StepNavigationError(
                        f"agent {agent_id} target {request.dest_obj!r} has no "
                        "walkable step-navigation candidate"
                    )
                candidates = tuple(candidate_items)
            robots.append(
                RobotIntent(
                    robot_id=str(agent_id),
                    start=start,
                    candidates=candidates,
                )
            )

        return Scenario(
            grid_size_m=self.config.grid_size_m,
            hard_clearance_m=self.config.hard_clearance_m,
            max_ticks=max(32, 2 * len(snapshot.walkable_map.walkable)),
            max_assignment_trials=self.config.max_assignment_trials,
            walkable=snapshot.walkable_map.walkable,
            conflicts=frozenset(),
            robots=tuple(robots),
            blocked_transitions=blocked_transitions,
        )

    def _plan(
        self,
        active_states: Mapping[int, ActiveNavigationState],
        snapshot: RuntimeWorldSnapshot,
        blocked_transitions: FrozenSet[Tuple[GridPoint, GridPoint]],
        parking_candidates: Optional[
            Mapping[int, Sequence[Mapping[str, float]]]
        ] = None,
    ):
        scenario = self._build_scenario(
            active_states,
            snapshot,
            blocked_transitions,
            parking_candidates,
        )
        world_state = WorldState(
            version=snapshot.version,
            positions={
                str(agent_id): point
                for agent_id, point in snapshot.positions.items()
            },
            statuses={
                str(agent_id): "ACTIVE"
                for agent_id in snapshot.positions
            },
        )
        self.metrics.increment("planning_batches")
        planning_started = time.perf_counter()
        planning = plan_scenario(scenario, world_state)
        self.metrics.add_planning_time(time.perf_counter() - planning_started)
        trace = planning.decision_trace.to_list()
        for state in active_states.values():
            if state.result is None:
                state.decision_trace.extend(trace)
        if planning.plan is not None:
            self.metrics.increment(
                "candidate_trials",
                int(planning.plan.metrics["assignment_trials"]),
            )
            self.metrics.increment(
                "priority_trials",
                int(planning.plan.metrics["priority_attempts"]),
            )
        return planning

    def _release_plan(
        self,
        plan,
        tick: int,
        active_states: Mapping[int, ActiveNavigationState],
        reason: str,
    ) -> None:
        plan.reservations.release_from(max(0, int(tick)))
        event = {
            "event": "reservations_released",
            "release_tick": max(0, int(tick)),
            "reason": reason,
        }
        for state in active_states.values():
            if state.result is None:
                state.decision_trace.append(dict(event))

    def _plan_remains_walkable(
        self,
        plan,
        next_micro_step_index: int,
        snapshot: RuntimeWorldSnapshot,
        active_states: Mapping[int, ActiveNavigationState],
    ) -> bool:
        walkable = snapshot.walkable_map.walkable
        if any(
            micro_step.target not in walkable
            for micro_step in plan.micro_steps[next_micro_step_index:]
        ):
            return False
        return all(
            plan.assignment[str(agent_id)].position in walkable
            for agent_id, state in active_states.items()
            if state.result is None
        )

    def _execute_until_boundary(
        self,
        plan,
        snapshot: RuntimeWorldSnapshot,
        active_states: Mapping[int, ActiveNavigationState],
        parking_agent_ids: FrozenSet[int] = frozenset(),
    ) -> _ExecutionBoundary:
        latest = snapshot
        for index, micro_step in enumerate(plan.micro_steps):
            agent_id = int(micro_step.robot_id)
            actual_source = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                latest.walkable_map.walkable,
            )
            if actual_source != micro_step.source:
                return _ExecutionBoundary(
                    kind="position_deviation",
                    release_tick=micro_step.tick,
                    snapshot=latest,
                    reason=(
                        f"agent {agent_id} source changed from "
                        f"{micro_step.source} to {actual_source}"
                    ),
                )
            if micro_step.target not in latest.walkable_map.walkable:
                return _ExecutionBoundary(
                    kind="map_changed",
                    release_tick=micro_step.tick,
                    snapshot=latest,
                    reason=f"planned target {micro_step.target} is no longer walkable",
                )

            target = latest.thor_positions[micro_step.target]
            try:
                moved = self.runtime.move_to_adjacent_position_direct(
                    agent_id,
                    target,
                )
            except Exception as exc:
                return _ExecutionBoundary(
                    kind="failed_transition",
                    release_tick=micro_step.tick,
                    snapshot=latest,
                    failed_transitions=frozenset(
                        {(micro_step.source, micro_step.target)}
                    ),
                    reason=(
                        f"agent {agent_id} failed movement edge "
                        f"{micro_step.source}->{micro_step.target}: {exc}"
                    ),
                )
            if not moved:
                return _ExecutionBoundary(
                    kind="failed_transition",
                    release_tick=micro_step.tick,
                    snapshot=latest,
                    failed_transitions=frozenset(
                        {(micro_step.source, micro_step.target)}
                    ),
                    reason=(
                        f"agent {agent_id} failed movement edge "
                        f"{micro_step.source}->{micro_step.target}"
                    ),
                )

            self.metrics.increment("micro_steps")
            if agent_id in parking_agent_ids:
                self.metrics.increment("parking_moves")
            latest = self.refresh_world()
            actual_target = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                latest.walkable_map.walkable,
            )
            if actual_target != micro_step.target:
                return _ExecutionBoundary(
                    kind="position_deviation",
                    release_tick=micro_step.tick + 1,
                    snapshot=latest,
                    reason=(
                        f"agent {agent_id} reached {actual_target}, expected "
                        f"{micro_step.target}"
                    ),
                )
            if not self._plan_remains_walkable(
                plan,
                index + 1,
                latest,
                active_states,
            ):
                return _ExecutionBoundary(
                    kind="map_changed",
                    release_tick=micro_step.tick + 1,
                    snapshot=latest,
                    reason="reachable map removed a remaining path or endpoint",
                )
        return _ExecutionBoundary(
            kind="arrived",
            release_tick=0,
            snapshot=latest,
        )

    def _finish_arrived_requests(
        self,
        plan,
        active_states: Mapping[int, ActiveNavigationState],
    ) -> bool:
        needs_replan = False
        for agent_id, state in sorted(active_states.items()):
            if state.result is not None:
                continue
            request = state.request
            self.runtime.face_position_direct(agent_id, request.center)
            destination = self.runtime.find_object(
                request.object_resource or request.dest_obj,
                agent_id=agent_id,
                require_center=True,
            )
            assigned = plan.assignment[str(agent_id)].position
            if not bool(destination.get("visible", False)):
                state.excluded_candidate_keys.add(assigned)
                state.decision_trace.append(
                    {
                        "event": "candidate_excluded",
                        "reason": "target_not_visible",
                        "candidate": assigned.to_list(),
                    }
                )
                self.metrics.increment("invisible_candidates")
                needs_replan = True
                continue
            state.result = NavigationResult(
                destination=dict(destination),
                position=self.runtime.current_agent_position(agent_id),
                decision_trace=tuple(state.decision_trace),
            )
        return needs_replan

    def _record_replan(
        self,
        active_states: Mapping[int, ActiveNavigationState],
        replan_count: int,
        reason: str,
    ) -> None:
        self.metrics.increment("replans")
        event = {
            "event": "replan_requested",
            "replan_count": replan_count,
            "reason": reason,
        }
        for state in active_states.values():
            if state.result is None:
                state.decision_trace.append(dict(event))
        if replan_count > self.config.max_replans:
            self.metrics.increment("budget_exhaustions")
            targets = ", ".join(
                f"agent {agent_id}={state.request.dest_obj!r}"
                for agent_id, state in sorted(active_states.items())
                if state.result is None
            )
            raise StepNavigationError(
                f"replan budget {self.config.max_replans} exhausted after "
                f"{replan_count} replans ({reason}); {targets}"
            )

    def execute_batch(
        self,
        requests: Sequence[NavigationRequest],
        completed_agent_ids: FrozenSet[int] = frozenset(),
    ) -> Dict[int, NavigationResult]:
        ordered = tuple(sorted(requests, key=lambda request: request.agent_id))
        if not ordered:
            return {}
        agent_ids = [request.agent_id for request in ordered]
        if len(set(agent_ids)) != len(agent_ids):
            raise StepNavigationError("navigation batch contains duplicate agent ids")
        if not set(agent_ids).issubset(
            set(range(int(self.runtime.physical_agent_count)))
        ):
            raise StepNavigationError("navigation batch contains unknown agent ids")

        active_states = {
            request.agent_id: ActiveNavigationState(request=request)
            for request in ordered
        }
        blocked_transitions: Set[Tuple[GridPoint, GridPoint]] = set()
        parking_candidates: Dict[int, Tuple[Dict[str, float], ...]] = {}
        parking_attempted = False
        replan_count = 0
        current_plan = None
        try:
            while any(state.result is None for state in active_states.values()):
                snapshot = self.refresh_world()
                planning = self._plan(
                    active_states,
                    snapshot,
                    frozenset(blocked_transitions),
                    parking_candidates,
                )
                if planning.plan is None:
                    snapshot = self.refresh_world()
                    planning = self._plan(
                        active_states,
                        snapshot,
                        frozenset(blocked_transitions),
                        parking_candidates,
                    )
                if planning.plan is None and not parking_attempted:
                    parking_attempted = True
                    parking_candidates = self._parking_candidate_map(
                        frozenset(completed_agent_ids),
                        tuple(
                            state.request
                            for state in active_states.values()
                            if state.result is None
                        ),
                        snapshot,
                    )
                    event = {
                        "event": "parking_recovery_attempted",
                        "parking_agent_ids": sorted(parking_candidates),
                    }
                    for state in active_states.values():
                        if state.result is None:
                            state.decision_trace.append(dict(event))
                    if parking_candidates:
                        planning = self._plan(
                            active_states,
                            snapshot,
                            frozenset(blocked_transitions),
                            parking_candidates,
                        )
                if planning.plan is None:
                    parking_context = (
                        "; parking recovery found no safe joint plan"
                        if parking_attempted
                        else ""
                    )
                    raise StepNavigationError(
                        "step navigation planning failed after full refresh "
                        f"with status {planning.status}{parking_context}"
                    )

                current_plan = planning.plan
                if parking_candidates:
                    parking_assignment = {
                        agent_id: current_plan.assignment[str(agent_id)]
                        .position.to_list()
                        for agent_id in sorted(parking_candidates)
                    }
                    event = {
                        "event": "parking_assignment",
                        "assignments": parking_assignment,
                    }
                    for state in active_states.values():
                        if state.result is None:
                            state.decision_trace.append(dict(event))
                boundary = self._execute_until_boundary(
                    current_plan,
                    snapshot,
                    active_states,
                    frozenset(parking_candidates),
                )
                if boundary.kind != "arrived":
                    self._release_plan(
                        current_plan,
                        boundary.release_tick,
                        active_states,
                        boundary.reason,
                    )
                    if boundary.kind == "failed_transition":
                        new_transitions = set(boundary.failed_transitions) - blocked_transitions
                        blocked_transitions.update(new_transitions)
                        self.metrics.increment(
                            "failed_transitions",
                            len(new_transitions),
                        )
                        if len(blocked_transitions) > self.config.max_failed_transitions:
                            self.metrics.increment("budget_exhaustions")
                            raise StepNavigationError(
                                "failed transition budget "
                                f"{self.config.max_failed_transitions} exhausted"
                            )
                    elif boundary.kind == "position_deviation":
                        self.metrics.increment("position_deviations")
                    replan_count += 1
                    self._record_replan(
                        active_states,
                        replan_count,
                        boundary.reason,
                    )
                    current_plan = None
                    continue

                needs_replan = self._finish_arrived_requests(
                    current_plan,
                    active_states,
                )
                if needs_replan:
                    self._release_plan(
                        current_plan,
                        0,
                        active_states,
                        "target not visible from assigned candidate",
                    )
                    replan_count += 1
                    self._record_replan(
                        active_states,
                        replan_count,
                        "target not visible from assigned candidate",
                    )
                    current_plan = None
                    continue

                self._release_plan(
                    current_plan,
                    0,
                    active_states,
                    "batch complete",
                )
                current_plan = None
            return {
                agent_id: state.result
                for agent_id, state in active_states.items()
                if state.result is not None
            }
        finally:
            if current_plan is not None:
                current_plan.reservations.release_from(0)
