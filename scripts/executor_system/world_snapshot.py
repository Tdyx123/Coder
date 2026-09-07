"""Immutable, complete world views captured at the controller commit boundary."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .execution_control import ExecutionCancelled, PlanExecutionTimeout


class SnapshotReadError(RuntimeError):
    """A coherent world could not be read; this is an infrastructure failure."""


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported metadata value: {type(value).__name__}")


@dataclass(frozen=True)
class WorldSnapshot:
    version: int
    robot_positions: Mapping
    robot_rotations: Mapping
    held_objects: Mapping
    objects_by_id: Mapping
    held_object_sources: Mapping = field(default_factory=dict)
    resource_metadata: Mapping = field(default_factory=dict)

    def __post_init__(self):
        for name in ("robot_positions", "robot_rotations", "held_objects",
                     "objects_by_id", "held_object_sources", "resource_metadata"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))


class SnapshotStore:
    def capture(self, runtime: Any, control) -> WorldSnapshot:
        control.check()
        try:
            lock = runtime.controller_lock
            while not lock.acquire(timeout=0.05):
                control.check()
            try:
                control.check()
                return self._capture_locked(runtime, control)
            finally:
                lock.release()
        except (ExecutionCancelled, PlanExecutionTimeout):
            raise
        except BaseException as exc:
            control.cancel(f"snapshot read failed: {exc}")
            root_control = getattr(runtime, "execution_control", None)
            if root_control is not None and root_control is not control:
                root_control.cancel(f"snapshot read failed: {exc}")
            if not isinstance(exc, Exception):
                raise
            raise SnapshotReadError(f"could not capture world snapshot: {exc}") from exc

    def _capture_locked(self, runtime, control):
        event = runtime.controller.last_event
        events = getattr(event, "events", None) or [event]
        expected_count = runtime.physical_agent_count
        if len(events) != expected_count:
            raise ValueError(f"expected {expected_count} agent events, got {len(events)}")
        version = runtime.state_version
        metadata = [agent_event.metadata for agent_event in events]
        positions, rotations, inventories, sources, objects = {}, {}, {}, {}, {}
        agent_object_selection = {}
        committed = getattr(runtime, "_committed_held_object_overrides", {})
        for agent_id, agent_metadata in enumerate(metadata):
            agent = agent_metadata["agent"]
            positions[agent_id] = {
                axis: float(agent["position"][axis]) for axis in ("x", "y", "z")
            }
            rotations[agent_id] = float(agent["rotation"]["y"])
            inventory = agent_metadata["inventoryObjects"]
            object_list = agent_metadata["objects"]
            if not isinstance(inventory, (list, tuple)) or not isinstance(object_list, (list, tuple)):
                raise TypeError("inventoryObjects and objects must be sequences")
            held = {str(obj["objectId"]) for obj in inventory}
            evidence = {object_id: {"source": "inventory", "version": version} for object_id in held}
            for object_id, source in committed.get(agent_id, {}).items():
                if object_id not in held:
                    held.add(object_id)
                    evidence[object_id] = source
            inventories[agent_id] = frozenset(held)
            sources[agent_id] = evidence
            agent_object_selection[agent_id] = {
                str(obj['objectId']): {
                    'visible': obj.get('visible', False),
                    'distance': obj.get('distance'),
                } for obj in object_list
            }
            for obj in object_list:
                objects.setdefault(str(obj["objectId"]), obj)
        # The active agent's metadata is the authoritative view for duplicate
        # object ids; other agent events can contribute additional objects.
        for obj in event.metadata["objects"]:
            objects[str(obj["objectId"])] = obj
        robot_map = dict(runtime.robot_agent_map)
        for agent_id in range(expected_count):
            if agent_id not in robot_map.values():
                name = f"robot{agent_id + 1}"
                if name in robot_map:
                    name = f"agent{agent_id}"
                robot_map[name] = agent_id
        if any(agent_id not in positions for agent_id in robot_map.values()):
            raise ValueError("robot mapping refers to an absent physical agent")
        resource_metadata = {'agent_object_selection': agent_object_selection}
        for lock_name, attribute, key in (
            ('object_alias_lock', 'object_alias_bindings', 'aliases'),
            ('operated_object_names_lock', 'operated_object_names', 'operated_names'),
        ):
            lock = getattr(runtime, lock_name, None)
            if lock is not None:
                while not lock.acquire(timeout=0.05):
                    control.check()
            try:
                control.check()
                # Freeze while the metadata lock is held, not after it escapes.
                resource_metadata[key] = _freeze(getattr(runtime, attribute, {}))
            finally:
                if lock is not None:
                    lock.release()
        manager = getattr(runtime, 'action_resource_manager', None)
        resource_metadata['identities'] = manager.identity_snapshot() if manager is not None else {}
        return WorldSnapshot(
            version,
            {name: positions[agent_id] for name, agent_id in robot_map.items()},
            {name: rotations[agent_id] for name, agent_id in robot_map.items()},
            {name: inventories[agent_id] for name, agent_id in robot_map.items()},
            objects,
            {name: sources[agent_id] for name, agent_id in robot_map.items()},
            resource_metadata,
        )
