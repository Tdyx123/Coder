"""Navigation map evidence, separate from planning and robot reservations.

Event refresh keeps static candidates, current occupancy and confirmed topology
changes separate. Only fresh all-agent queries can add/remove map evidence;
metadata can invalidate evidence but cannot invent new reachable positions.
"""
from contextlib import nullcontext
from dataclasses import dataclass
import json
import math
from typing import Dict

from multi_robot_avoidance import GlobalWalkableMap, GridPoint
from .execution_control import ensure_control
from .movement import StepNavigationError
from .runtime_metrics import metrics_for


@dataclass(frozen=True)
class RuntimeWorldSnapshot:
    version: int
    positions: Dict[int, GridPoint]
    thor_positions: Dict[GridPoint, Dict[str, float]]
    walkable_map: GlobalWalkableMap


class ReachableMapCache:
    def __init__(self, config, robot_ids):
        self.config = config
        self.walkable_map = GlobalWalkableMap(robot_ids, config.grid_size_m)
        self.static_thor_positions = {}
        self._known = {}
        self._temporary = set()
        self._occupancy = None
        self._fingerprint = None
        self._dirty = True
        self._successful_steps = 0
        self._initialized = False

    def invalidate(self):
        self._dirty = True

    def record_successful_step(self):
        self._successful_steps += 1

    def observe_action(self, payload):
        # Preserve even open/close transitions that occur between cache reads.
        # Failed actions may have partially changed the scene, so invalidate too.
        if payload.get('action') in {
            'OpenObject', 'CloseObject', 'PickupObject', 'PutObject',
            'ThrowObject', 'DropHandObject', 'TeleportObject',
            'TeleportObjectToHand', 'MoveHandAhead', 'MoveHandBack',
            'MoveHandLeft', 'MoveHandRight', 'MoveHandUp', 'MoveHandDown',
            'RotateHand', 'SliceObject', 'BreakObject',
        }:
            self.invalidate()

    def _grid_point(self, position):
        return GridPoint(round(float(position['x']) / self.config.grid_size_m),
                         round(float(position['z']) / self.config.grid_size_m))

    def _positions(self, runtime):
        positions = {}
        actuals = {}
        for agent_id in range(int(runtime.physical_agent_count)):
            actual = runtime.current_agent_position(agent_id)
            point = self._grid_point(actual)
            offset = math.hypot(actual['x'] - point.x * self.config.grid_size_m,
                                actual['z'] - point.z * self.config.grid_size_m)
            if offset > self.config.grid_snap_tolerance_m:
                raise StepNavigationError(
                    f'agent {agent_id} position {dict(actual)} cannot be mapped '
                    'to a reachable navigation grid point')
            positions[agent_id] = point
            actuals[agent_id] = actual
        return positions, actuals

    def _navigation_fingerprint(self, runtime):
        # Visibility, distance, temperature, rotation of the camera, rendering,
        # frame IDs and actionReturn are intentionally absent.
        fields = ('objectId', 'position', 'rotation', 'axisAlignedBoundingBox',
                  'objectOrientedBoundingBox', 'isOpen', 'openness',
                  'isPickedUp', 'isSliced', 'isBroken', 'parentReceptacles',
                  'receptacleObjectIds')
        objects = {}
        for agent_id in range(int(runtime.physical_agent_count)):
            for obj in runtime.current_objects(agent_id):
                object_id = str(obj.get('objectId', ''))
                objects[(agent_id, object_id)] = {key: obj.get(key) for key in fields}
        held = getattr(runtime, 'agent_held_objects_for', None)
        inventory = [sorted(held(agent_id)) if callable(held) else []
                     for agent_id in range(int(runtime.physical_agent_count))]
        return json.dumps([list(sorted(objects.items())), inventory],
                          sort_keys=True, separators=(',', ':'))

    def _query_all(self, runtime):
        control = ensure_control(runtime)
        control.check()
        metrics_for(runtime).increment('reachable_full_refreshes')
        try:
            snapshots = {}
            for agent_id in range(int(runtime.physical_agent_count)):
                control.check()
                snapshots[str(agent_id)] = runtime.refresh_reachable_positions(agent_id)
            # Use the existing strict finite/nonempty position validation. A bad
            # agent response cannot be hidden by another agent's valid union.
            validated = GlobalWalkableMap(self.walkable_map.robot_ids, self.config.grid_size_m)
            validated.replace_all(snapshots)
            result = {}
            for positions in snapshots.values():
                for position in positions:
                    result.setdefault(self._grid_point(position), dict(position))
            control.check()
            return result
        except BaseException as exc:
            control.cancel(f'reachable map query failed: {exc}')
            root = getattr(runtime, 'execution_control', None)
            if root is not None and root is not control:
                root.cancel(f'reachable map query failed: {exc}')
            raise

    def refresh(self, runtime, *, force: bool) -> RuntimeWorldSnapshot:
        # Coordinate metadata/positions and all query events under the same lock
        # used to publish controller state. Runtime navigation owns an RLock.
        with getattr(runtime, 'controller_lock', nullcontext()):
            return self._refresh_locked(runtime, force=force)

    def _refresh_locked(self, runtime, *, force):
        ensure_control(runtime).check()
        fingerprint = self._navigation_fingerprint(runtime)
        topology_changed = self._initialized and fingerprint != self._fingerprint
        try:
            positions, actuals = self._positions(runtime)
        except StepNavigationError:
            # Position evidence is unusable. Refresh for diagnosis, then stop;
            # never snap/teleport an invalid position to continue the plan.
            self._query_all(runtime)
            raise
        occupancy = tuple((aid, actual['x'], actual['z']) for aid, actual in sorted(actuals.items()))
        occupancy_changed = occupancy != self._occupancy
        refresh = (force or not self._initialized or self._dirty or topology_changed
                   or self._successful_steps >= self.config.full_refresh_interval_steps
                   or (self._temporary and occupancy_changed))
        if refresh:
            fresh = self._query_all(runtime)
            if not self._initialized:
                self.static_thor_positions = {
                    self._grid_point(p): dict(p)
                    for p in getattr(runtime, 'global_reachable_positions', ()) or ()}
                self.static_thor_positions.update(fresh)
                self._known = dict(self.static_thor_positions)
            missing = set(self._known) - set(fresh)
            confirmed = set()
            if missing:
                metrics_for(runtime).increment('reachable_suspected_removals', len(missing))
                second = self._query_all(runtime)
                # Both all-agent queries must omit the point, outside every
                # robot's physical 0.35m occupancy neighborhood.
                positions, actuals = self._positions(runtime)
                confirmed = {point for point in missing - set(second)
                             if not any(math.hypot(
                                 self._known[point]['x'] - actual['x'],
                                 self._known[point]['z'] - actual['z']) < self.config.hard_clearance_m
                                 for actual in actuals.values())}
                fresh = second
            known = dict(self._known)
            for point in confirmed:
                del known[point]
            known.update(fresh)
            # Unconfirmed omissions stay in static evidence but may not be
            # entered. Occupied source nodes remain so their robots can leave;
            # the planner's existing geometry/reservations govern those nodes.
            temporary = set(known) - set(fresh)
            effective = {p: value for p, value in known.items()
                         if p not in temporary or p in positions.values()}
            if (not self._initialized or set(effective) != self.walkable_map.walkable
                    or topology_changed or self._dirty or confirmed):
                self.walkable_map.replace_all({
                    robot_id: list(effective.values()) for robot_id in self.walkable_map.robot_ids})
            self._known = known
            self._temporary = temporary
            self._successful_steps = 0
            self._dirty = False
            self._initialized = True
            self._fingerprint = self._navigation_fingerprint(runtime)
            self._occupancy = tuple((aid, actual['x'], actual['z']) for aid, actual in sorted(actuals.items()))
            if confirmed:
                metrics_for(runtime).increment('reachable_confirmed_removals', len(confirmed))
        else:
            metrics_for(runtime).increment('reachable_cache_hits')
        # Always use the latest event after queries as well as on cache hits.
        positions, _ = self._positions(runtime)
        for agent_id, point in positions.items():
            if point not in self.walkable_map.walkable:
                if not refresh:
                    return self._refresh_locked(runtime, force=True)
                raise StepNavigationError(
                    f'agent {agent_id} position cannot be mapped to a reachable navigation grid point')
        return RuntimeWorldSnapshot(
            version=self.walkable_map.version, positions=positions,
            thor_positions={p: dict(self._known[p]) for p in self.walkable_map.walkable},
            walkable_map=self.walkable_map.copy())
