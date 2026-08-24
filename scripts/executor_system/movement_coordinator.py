"""Runtime adapter between navigation requests and the deterministic planner."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Sequence, Tuple

from multi_robot_avoidance import (
    Candidate,
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


@dataclass(frozen=True)
class RuntimeWorldSnapshot:
    version: int
    positions: Dict[int, GridPoint]
    thor_positions: Dict[GridPoint, Dict[str, float]]
    walkable_map: GlobalWalkableMap


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

    def _build_scenario(
        self,
        requests: Sequence[NavigationRequest],
        snapshot: RuntimeWorldSnapshot,
    ) -> Scenario:
        request_by_agent = {request.agent_id: request for request in requests}
        robots = []
        for agent_id, start in sorted(snapshot.positions.items()):
            request = request_by_agent.get(agent_id)
            if request is None:
                candidates = (
                    Candidate(f"{agent_id}:static", start, 0.0),
                )
            else:
                seen = set()
                candidate_items = []
                for index, position in enumerate(request.candidate_positions):
                    point = self._grid_point(position)
                    if point in seen or point not in snapshot.walkable_map.walkable:
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
        )

    def execute_batch(
        self,
        requests: Sequence[NavigationRequest],
        completed_agent_ids: FrozenSet[int] = frozenset(),
    ) -> Dict[int, NavigationResult]:
        del completed_agent_ids
        ordered = tuple(sorted(requests, key=lambda request: request.agent_id))
        if not ordered:
            return {}
        agent_ids = [request.agent_id for request in ordered]
        if len(set(agent_ids)) != len(agent_ids):
            raise StepNavigationError("navigation batch contains duplicate agent ids")

        self.metrics.increment("planning_batches")
        snapshot = self.refresh_world()
        scenario = self._build_scenario(ordered, snapshot)
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
        planning_started = time.perf_counter()
        planning = plan_scenario(scenario, world_state)
        self.metrics.add_planning_time(time.perf_counter() - planning_started)
        if planning.plan is None:
            raise StepNavigationError(
                f"step navigation planning failed with status {planning.status}"
            )
        self.metrics.increment(
            "candidate_trials",
            int(planning.plan.metrics["assignment_trials"]),
        )
        self.metrics.increment(
            "priority_trials",
            int(planning.plan.metrics["priority_attempts"]),
        )

        for micro_step in planning.plan.micro_steps:
            agent_id = int(micro_step.robot_id)
            actual_source = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                snapshot.walkable_map.walkable,
            )
            if actual_source != micro_step.source:
                raise StepNavigationError(
                    f"agent {agent_id} moved from planned source "
                    f"{micro_step.source} to {actual_source}"
                )
            target = snapshot.thor_positions[micro_step.target]
            moved = self.runtime.move_to_adjacent_position_direct(agent_id, target)
            if not moved:
                raise StepNavigationError(
                    f"agent {agent_id} failed movement edge "
                    f"{micro_step.source}->{micro_step.target}"
                )
            actual_target = self._authoritative_grid_point(
                agent_id,
                self.runtime.current_agent_position(agent_id),
                snapshot.walkable_map.walkable,
            )
            if actual_target != micro_step.target:
                raise StepNavigationError(
                    f"agent {agent_id} reached {actual_target}, expected "
                    f"{micro_step.target}"
                )
            self.metrics.increment("micro_steps")

        trace = tuple(planning.decision_trace.to_list())
        results = {}
        for request in ordered:
            self.runtime.face_position_direct(request.agent_id, request.center)
            destination = self.runtime.find_object(
                request.object_resource or request.dest_obj,
                agent_id=request.agent_id,
                require_center=True,
            )
            results[request.agent_id] = NavigationResult(
                destination=dict(destination),
                position=self.runtime.current_agent_position(request.agent_id),
                decision_trace=trace,
            )
        return results
