"""Runtime adapter between navigation requests and the deterministic planner."""

from __future__ import annotations

import math
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
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

from .runtime_metrics import measured, metrics_for
from .reachable_map import (ReachableMapCache, RuntimeWorldSnapshot,
                            navigation_object_state)
from .execution_control import (raise_if_execution_aborted, ExecutionCancelled,
                                ExecutionShutdownTimeout)
from .movement import (
    MovementConfig,
    NavigationBatchResult,
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
    NoInteractionPoseError,
    StepNavigationError,
)
from .utils import position_inside_aabb_footprint


@dataclass
class ActiveNavigationState:
    request: NavigationRequest
    excluded_candidate_keys: Set[GridPoint] = field(default_factory=set)
    decision_trace: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[NavigationResult] = None
    candidates_expanded: bool = False


@dataclass(frozen=True)
class _ExecutionBoundary:
    kind: str
    release_tick: int
    snapshot: RuntimeWorldSnapshot
    failed_transitions: FrozenSet[Tuple[GridPoint, GridPoint]] = frozenset()
    reason: str = ""
    failed_agent_errors: Mapping[int, Exception] = field(default_factory=dict)


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
        self.reachable_map_cache = ReachableMapCache(config, robot_ids)
        if config.reachable_refresh_mode == "event":
            runtime.reachable_map_cache = self.reachable_map_cache
        self._stable_thor_positions: Dict[GridPoint, Dict[str, float]] = {}
        for position in getattr(runtime, "global_reachable_positions", ()) or ():
            normalized = dict(position)
            self._stable_thor_positions.setdefault(
                self._grid_point(normalized),
                normalized,
            )

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

    def refresh_world(self, *, force: bool = False) -> RuntimeWorldSnapshot:
        self._check_deadline()
        if self.config.reachable_refresh_mode == "event":
            snapshot = self.reachable_map_cache.refresh(self.runtime, force=force)
            self.walkable_map = self.reachable_map_cache.walkable_map
            return snapshot
        metrics_for(self.runtime).increment("reachable_full_refreshes")
        live_snapshots = {
            str(agent_id): self.runtime.refresh_reachable_positions(agent_id)
            for agent_id in range(int(self.runtime.physical_agent_count))
        }
        raw_live_positions = [
            dict(position)
            for positions in live_snapshots.values()
            for position in positions
        ]
        live_by_key: Dict[GridPoint, Dict[str, float]] = {}
        for position in raw_live_positions:
            live_by_key.setdefault(self._grid_point(position), position)

        suppressed_removals = len(
            set(self._stable_thor_positions) - set(live_by_key)
        )
        if suppressed_removals:
            self.metrics.increment(
                "suppressed_reachable_removals",
                suppressed_removals,
            )
        for point, position in live_by_key.items():
            self._stable_thor_positions.setdefault(point, dict(position))

        raw_positions = list(self._stable_thor_positions.values())
        raw_positions.sort(
            key=lambda position: (
                float(position["x"]),
                float(position["z"]),
                float(position.get("y", 0.0)),
            )
        )
        stable_snapshots = {
            robot_id: [dict(position) for position in raw_positions]
            for robot_id in self.walkable_map.robot_ids
        }
        update = self.walkable_map.replace_all(stable_snapshots)

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
        fixed_robot_ids = set()
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
                    fixed_robot_ids.add(str(agent_id))
            else:
                request = state.request
                seen = set()
                candidate_items = []
                for index, position in enumerate(request.candidate_positions):
                    point = self._grid_point(position)
                    if (
                        point in seen
                        or point in state.excluded_candidate_keys
                        or (point.x, point.z) in request.excluded_pose_keys
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
                    raise NoInteractionPoseError(
                        "NO_INTERACTION_POSE: agent "
                        f"{agent_id} target {request.interaction_target or request.dest_obj!r} "
                        "has no walkable step-navigation candidate"
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
            fixed_robot_ids=frozenset(fixed_robot_ids),
        )

    @measured("navigation_planning", "navigation_plans")
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

    def _refresh_event_requests(self, active_states):
        errors = {}
        for agent_id, state in active_states.items():
            try:
                self._refresh_event_request({agent_id: state})
            except TimeoutError:
                raise
            except RuntimeError as exc:
                raise_if_execution_aborted(self.runtime, exc)
                errors[agent_id] = exc
        return errors

    def _refresh_event_request(self, active_states):
        """Rebuild affected target candidates using the existing runtime resolver.

        Fresh map evidence alone need not change a valid trajectory. A target's
        own motion/geometry/identity can change its interaction candidates even
        when the reachable grid is unchanged, so validate that separately.
        """
        for state in active_states.values():
            if state.result is not None:
                continue
            request = state.request
            targets = [(request.dest_obj, request.destination)]
            if request.interaction_target is not None:
                targets.append((request.interaction_target,
                                request.interaction_destination or request.destination))
            changed = any(
                navigation_object_state(self.runtime.find_object(
                    target, agent_id=request.agent_id, require_center=True))
                != navigation_object_state(previous)
                for target, previous in targets)
            if not changed:
                continue
            refreshed = self.runtime.build_navigation_request(
                request.robot, request.dest_obj, next_action=request.next_action,
                phase_coordinator=request.phase_coordinator, action_wave=request.action_wave)
            positions = refreshed.candidate_positions
            if state.candidates_expanded:
                refresh = getattr(self.runtime, "refresh_navigation_candidates", None)
                if callable(refresh):
                    positions = refresh(refreshed, self.config.expanded_candidate_limit)
            candidates = []
            for position in positions:
                point = self._grid_point(position)
                if ((point.x, point.z) not in request.excluded_pose_keys
                        and point not in state.excluded_candidate_keys):
                    candidates.append(position)
            state.request = replace(
                refreshed, candidate_positions=tuple(candidates),
                excluded_pose_keys=request.excluded_pose_keys)
            state.decision_trace.append({"event": "navigation_candidates_refreshed",
                                         "reason": "target metadata changed"})

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
            and (self.config.reachable_refresh_mode != "event"
                 or plan.assignment[str(agent_id)].position in {
                     self._grid_point(position) for position in state.request.candidate_positions})
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
        scene_revision = self.reachable_map_cache.scene_revision
        for index, micro_step in enumerate(plan.micro_steps):
            self._check_deadline()
            agent_id = int(micro_step.robot_id)
            if self.config.reachable_refresh_mode == "event":
                observed = self.refresh_world()
                if self.reachable_map_cache.scene_revision != scene_revision:
                    errors = self._refresh_event_requests(active_states)
                    if errors:
                        return _ExecutionBoundary(
                            kind="request_failed", release_tick=micro_step.tick,
                            snapshot=observed, reason="target refresh failed",
                            failed_agent_errors=errors)
                    scene_revision = self.reachable_map_cache.scene_revision
                if not self._plan_remains_walkable(plan, index, observed, active_states):
                    return _ExecutionBoundary(
                        kind="map_changed", release_tick=micro_step.tick,
                        snapshot=observed, reason="remaining path or target candidate changed before move")
                if observed.positions != latest.positions:
                    return _ExecutionBoundary(
                        kind="position_deviation", release_tick=micro_step.tick,
                        snapshot=self.refresh_world(force=True),
                        reason="agent positions changed before move")
                latest = observed
            actual_source = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                latest.walkable_map.walkable,
            )
            if actual_source != micro_step.source:
                if self.config.reachable_refresh_mode == "event":
                    latest = self.refresh_world(force=True)
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
            except TimeoutError:
                raise
            except Exception as exc:
                raise_if_execution_aborted(self.runtime, exc)
                if self.config.reachable_refresh_mode == "event":
                    latest = self.refresh_world(force=True)
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
                if self.config.reachable_refresh_mode == "event":
                    latest = self.refresh_world(force=True)
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
            expected_positions = dict(latest.positions)
            expected_positions[agent_id] = micro_step.target
            if self.config.reachable_refresh_mode == "event":
                self.reachable_map_cache.record_successful_step()
            latest = self.refresh_world()
            actual_target = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                latest.walkable_map.walkable,
            )
            if (actual_target != micro_step.target
                    or (self.config.reachable_refresh_mode == "event"
                        and latest.positions != expected_positions)):
                if self.config.reachable_refresh_mode == "event":
                    latest = self.refresh_world(force=True)
                return _ExecutionBoundary(
                    kind="position_deviation",
                    release_tick=micro_step.tick + 1,
                    snapshot=latest,
                    reason=(
                        f"agent {agent_id} reached {actual_target}, expected "
                        f"{micro_step.target}"
                        + (f"; observed agent positions {latest.positions}"
                           if self.config.reachable_refresh_mode == "event" else "")
                    ),
                )
            if (self.config.reachable_refresh_mode == "event"
                    and self.reachable_map_cache.scene_revision != scene_revision):
                errors = self._refresh_event_requests(active_states)
                if errors:
                    return _ExecutionBoundary(
                        kind="request_failed", release_tick=micro_step.tick + 1,
                        snapshot=latest, reason="target refresh failed",
                        failed_agent_errors=errors)
                scene_revision = self.reachable_map_cache.scene_revision
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

    def _expand_candidates(self, state):
        if state.candidates_expanded:
            return False
        state.candidates_expanded = True
        refresh = getattr(self.runtime, "refresh_navigation_candidates", None)
        if not callable(refresh):
            return False
        self._check_deadline()
        request = state.request
        positions = refresh(request, self.config.expanded_candidate_limit)
        old = {self._grid_point(p) for p in request.candidate_positions}
        excluded = state.excluded_candidate_keys | {
            GridPoint(x, z) for x, z in request.excluded_pose_keys}
        added = []
        remaining = max(0, self.config.expanded_candidate_limit - len(old))
        for position in positions:
            if len(added) >= remaining:
                break
            point = self._grid_point(position)
            if point not in old and point not in excluded:
                added.append(dict(position))
                old.add(point)
        self.metrics.increment("candidate_expansions")
        state.decision_trace.append({"event": "candidate_expansion", "added": len(added),
                                     "limit": self.config.expanded_candidate_limit})
        if not added:
            return False
        state.request = replace(request, candidate_positions=(
            *request.candidate_positions, *added))
        return True

    def _recover_visibility(self, state):
        request = state.request
        camera = getattr(self.runtime, "camera_horizon", None)
        look = getattr(self.runtime, "try_look_to_camera_horizon", None)
        if not callable(camera) or not callable(look):
            return False
        initial = camera(request.agent_id)
        if initial is None:
            return False
        recovered = False
        interrupted = False
        try:
            for offset in (-10., -20., -30., 10., 20., 30.):
                self._check_deadline()
                horizon = initial + offset
                self.metrics.increment("visibility_look_attempts")
                state.decision_trace.append({"event": "visibility_look_attempt", "horizon": horizon})
                if not look(request.agent_id, horizon, action_name="GoToObject"):
                    continue
                target = self.runtime.find_object(
                    request.interaction_object_resource or request.interaction_target
                    or request.object_resource or request.dest_obj,
                    agent_id=request.agent_id, require_center=True)
                if target.get("visible", False):
                    recovered = True
                    self.metrics.increment("visibility_recoveries")
                    state.decision_trace.append({"event": "visibility_recovered", "horizon": horizon})
                    return True
            return False
        except (TimeoutError, ExecutionCancelled, ExecutionShutdownTimeout):
            interrupted = True
            raise
        finally:
            if not recovered and not interrupted:
                # A cancelled operation must not issue another controller action.
                self._check_deadline()
                look(request.agent_id, initial, action_name="GoToObject")

    def _finish_arrived_requests(self, plan, active_states, *, selected_agent_id=None):
        needs_replan = False
        errors = {}
        for agent_id, state in sorted(active_states.items()):
            if state.result is not None:
                continue
            try:
                retry, error = self._finish_arrived_request(
                    plan, {agent_id: state}, selected_agent_id=agent_id)
                if isinstance(error, NoInteractionPoseError) and self._expand_candidates(state):
                    retry, error = True, None
            except TimeoutError:
                raise
            except RuntimeError as exc:
                raise_if_execution_aborted(self.runtime, exc)
                # Object resolution and final facing errors belong to this request.
                retry, error = False, exc
            needs_replan |= retry
            if error is not None:
                errors[agent_id] = error
        return needs_replan, errors

    def _finish_arrived_request(
        self,
        plan,
        active_states: Mapping[int, ActiveNavigationState],
        *,
        selected_agent_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[Exception]]:
        needs_replan = False
        for agent_id, state in sorted(active_states.items()):
            if state.result is not None:
                continue
            self._check_deadline()
            request = state.request
            assigned = plan.assignment[str(agent_id)].position
            interaction_center = request.interaction_center or request.center
            try:
                self.runtime.face_position_direct(agent_id, interaction_center)
            except TimeoutError:
                raise
            except Exception as exc:
                raise_if_execution_aborted(self.runtime, exc)
                held_item_failure = getattr(
                    self.runtime,
                    "held_item_rotation_failure",
                    lambda _exc: False,
                )(exc)
                if not held_item_failure:
                    if agent_id == selected_agent_id:
                        return False, exc
                    raise
                state.excluded_candidate_keys.add(assigned)
                state.decision_trace.append(
                    {
                        "event": "candidate_excluded",
                        "reason": "held_item_rotation_failure",
                        "candidate": assigned.to_list(),
                    }
                )
                if self.config.reachable_refresh_mode == "event":
                    self.refresh_world(force=True)
                if not self._has_remaining_candidate(state, assigned):
                    error = NoInteractionPoseError(
                        "NO_INTERACTION_POSE: held-item rotation failed at "
                        f"every candidate for agent {agent_id} target "
                        f"{request.interaction_target or request.dest_obj!r}"
                    )
                    if agent_id == selected_agent_id:
                        error.__cause__ = exc
                        return False, error
                    raise error from exc
                needs_replan = True
                continue

            destination = self.runtime.find_object(
                request.interaction_object_resource
                or request.interaction_target
                or request.object_resource
                or request.dest_obj,
                agent_id=agent_id,
                require_center=True,
            )
            if not bool(destination.get("visible", False)) and not self._recover_visibility(state):
                self.metrics.increment("invisible_candidates")
                self.metrics.increment("invisible_interaction_candidates")
                state.excluded_candidate_keys.add(assigned)
                state.decision_trace.append(
                    {
                        "event": "candidate_excluded",
                        "reason": "target_not_visible",
                        "candidate": assigned.to_list(),
                    }
                )
                if self.config.reachable_refresh_mode == "event":
                    self.refresh_world(force=True)
                if not self._has_remaining_candidate(state, assigned):
                    error = NoInteractionPoseError(
                        "NO_INTERACTION_POSE: interaction target is invisible "
                        f"from every candidate for agent {agent_id} target "
                        f"{request.interaction_target or request.dest_obj!r}"
                    )
                    if agent_id == selected_agent_id:
                        return False, error
                    raise error
                needs_replan = True
                continue
            state.result = NavigationResult(
                destination=dict(request.destination),
                position=self.runtime.current_agent_position(agent_id),
                decision_trace=tuple(state.decision_trace),
            )
        return needs_replan, None

    def _has_remaining_candidate(
        self,
        state: ActiveNavigationState,
        assigned: GridPoint,
    ) -> bool:
        return any(
            self._grid_point(position) != assigned
            and self._grid_point(position) not in state.excluded_candidate_keys
            and (self._grid_point(position).x, self._grid_point(position).z) not in state.request.excluded_pose_keys
            and self._grid_point(position) in self.walkable_map.walkable
            for position in state.request.candidate_positions
        )

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
    ) -> NavigationBatchResult:
        # Request collection happens in PhaseCoordinator before this callback.
        # Hold the runtime lock until _execute_batch has released reservations.
        scope = getattr(self.runtime, "navigation_execution_scope", None)
        with scope() if callable(scope) else nullcontext():
            return self._execute_batch(requests, completed_agent_ids)

    def _check_deadline(self) -> None:
        check = getattr(self.runtime, "check_navigation_deadline", None)
        if callable(check):
            check()

    def _execute_batch(
        self,
        requests: Sequence[NavigationRequest],
        completed_agent_ids: FrozenSet[int] = frozenset(),
    ) -> NavigationBatchResult:
        ordered = tuple(sorted(requests, key=lambda request: request.agent_id))
        if not ordered:
            return NavigationBatchResult(results={})
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
        deferred_agent_ids: FrozenSet[int] = frozenset()
        fallback_status: Optional[str] = None
        selected_agent_id: Optional[int] = None

        failed_agent_errors = {}

        def fail_agent(agent_id, error):
            state = active_states.pop(agent_id)
            state.decision_trace.append({"event": "navigation_failed", "agent_id": agent_id, "reason": str(error)})
            error.decision_trace = tuple(state.decision_trace)
            failed_agent_errors[agent_id] = error

        def try_expand(agent_id, state):
            try:
                return self._expand_candidates(state)
            except TimeoutError:
                raise
            except RuntimeError as exc:
                raise_if_execution_aborted(self.runtime, exc)
                fail_agent(agent_id, exc)
                return False

        def batch_result(error: Optional[Exception] = None) -> NavigationBatchResult:
            if error is not None:
                for agent_id, state in list(active_states.items()):
                    if state.result is None:
                        fail_agent(agent_id, StepNavigationError(str(error)))
            return NavigationBatchResult(
                results={
                    agent_id: state.result
                    for agent_id, state in active_states.items()
                    if state.result is not None
                },
                failed_agent_errors=dict(failed_agent_errors),
                deferred_agent_ids=deferred_agent_ids,
                fallback_status=fallback_status,
            )

        try:
            first_refresh = True
            while any(state.result is None for state in active_states.values()):
                snapshot = self.refresh_world(force=first_refresh)
                if self.config.reachable_refresh_mode == "event":
                    for agent_id, error in self._refresh_event_requests(active_states).items():
                        fail_agent(agent_id, error)
                first_refresh = False
                for agent_id, state in list(active_states.items()):
                    if state.result is not None:
                        continue
                    def has_candidate():
                        return any(self._grid_point(p) in snapshot.walkable_map.walkable
                                   and self._grid_point(p) not in state.excluded_candidate_keys
                                   and (self._grid_point(p).x, self._grid_point(p).z) not in state.request.excluded_pose_keys
                                   for p in state.request.candidate_positions)
                    if not has_candidate():
                        try_expand(agent_id, state)
                        if agent_id not in active_states:
                            continue
                        if not has_candidate():
                            fail_agent(agent_id, NoInteractionPoseError(
                                f"NO_INTERACTION_POSE: agent {agent_id} has no walkable step-navigation candidate"))
                if not any(state.result is None for state in active_states.values()):
                    break
                planning = self._plan(
                    active_states,
                    snapshot,
                    frozenset(blocked_transitions),
                    parking_candidates,
                )
                if planning.plan is None:
                    snapshot = self.refresh_world(force=True)
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
                    eligible_fallback_status = planning.status in {
                        "NO_PLAN_FOUND",
                        "SEARCH_LIMIT_REACHED",
                    }
                    unresolved_agent_ids = tuple(
                        sorted(
                            agent_id
                            for agent_id, state in active_states.items()
                            if state.result is None
                        )
                    )
                    serial_planning = None
                    serial_agent_id = None
                    if eligible_fallback_status and len(unresolved_agent_ids) > 1:
                        for candidate_agent_id in unresolved_agent_ids:
                            candidate_state = active_states[candidate_agent_id]
                            candidate_planning = self._plan(
                                {candidate_agent_id: candidate_state},
                                snapshot,
                                frozenset(blocked_transitions),
                                {},
                            )
                            if candidate_planning.plan is not None:
                                serial_agent_id = candidate_agent_id
                                serial_planning = candidate_planning
                                break
                    if serial_planning is not None and serial_agent_id is not None:
                        selected_agent_id = serial_agent_id
                        fallback_status = planning.status
                        deferred_agent_ids = frozenset(
                            agent_id
                            for agent_id in unresolved_agent_ids
                            if agent_id != serial_agent_id
                        )
                        selected_state = active_states[serial_agent_id]
                        selected_state.decision_trace.append(
                            {
                                "event": "serial_navigation_fallback",
                                "joint_status": fallback_status,
                                "selected_agent_id": serial_agent_id,
                                "deferred_agent_ids": sorted(deferred_agent_ids),
                            }
                        )
                        active_states = {
                            agent_id: state
                            for agent_id, state in active_states.items()
                            if state.result is not None or agent_id == serial_agent_id
                        }
                        parking_candidates = {}
                        planning = serial_planning
                        self.metrics.increment("serial_fallback_batches")
                        self.metrics.increment(
                            "deferred_requests",
                            len(deferred_agent_ids),
                        )

                if planning.plan is None and eligible_fallback_status:
                    expanded = False
                    previous_failures = len(failed_agent_errors)
                    for agent_id, state in list(active_states.items()):
                        if state.result is None:
                            expanded |= try_expand(agent_id, state)
                    if expanded or len(failed_agent_errors) != previous_failures:
                        parking_attempted = False
                        parking_candidates = {}
                        continue

                if planning.plan is None:
                    parking_context = (
                        "; parking recovery found no safe joint plan"
                        if parking_attempted
                        else ""
                    )
                    error = StepNavigationError(
                        "step navigation planning failed after full refresh "
                        f"with status {planning.status}{parking_context}"
                    )
                    if not eligible_fallback_status:
                        raise error
                    return batch_result(error)

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
                    if boundary.kind == "request_failed":
                        for agent_id, error in boundary.failed_agent_errors.items():
                            fail_agent(agent_id, error)
                        current_plan = None
                        continue
                    if boundary.kind == "failed_transition":
                        new_transitions = set(boundary.failed_transitions) - blocked_transitions
                        blocked_transitions.update(new_transitions)
                        self.metrics.increment(
                            "failed_transitions",
                            len(new_transitions),
                        )
                        if len(blocked_transitions) > self.config.max_failed_transitions:
                            self.metrics.increment("budget_exhaustions")
                            return batch_result(StepNavigationError(
                                "failed transition budget "
                                f"{self.config.max_failed_transitions} exhausted"
                            ))
                    elif boundary.kind == "position_deviation":
                        self.metrics.increment("position_deviations")
                    replan_count += 1
                    try:
                        self._record_replan(
                            active_states,
                            replan_count,
                            boundary.reason,
                        )
                    except StepNavigationError as exc:
                        return batch_result(exc)
                    current_plan = None
                    continue

                needs_replan, arrival_error = self._finish_arrived_requests(
                    current_plan,
                    active_states,
                    selected_agent_id=selected_agent_id,
                )
                for agent_id, error in arrival_error.items():
                    fail_agent(agent_id, error)
                if needs_replan:
                    self._release_plan(
                        current_plan,
                        0,
                        active_states,
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
            return batch_result()
        finally:
            if current_plan is not None:
                current_plan.reservations.release_from(0)
