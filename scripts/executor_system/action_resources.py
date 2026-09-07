"""Resolve complete high-level demand once, then bind all nested interactions."""
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Tuple

from .resource_manager import ActionResourceManager


@dataclass(frozen=True)
class ResolvedActionResources:
    keys: Tuple[str, ...]
    bindings: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, 'bindings', MappingProxyType(dict(self.bindings)))


class ResourceBindingInvalid(RuntimeError):
    """An admitted target vanished or a helper tried an unapproved resource."""


class ResourceBindingDeferred(ResourceBindingInvalid):
    """No side effect started; release the whole grant and resolve next round."""


class ObjectHeldByOther(RuntimeError):
    pass


_LOCAL = threading.local()


def manager_for(runtime):
    manager = getattr(runtime, 'action_resource_manager', None)
    if manager is None:
        # Installed by the scheduler before starting workers.
        manager = runtime.action_resource_manager = ActionResourceManager()
    return manager


def active_resources(runtime):
    scope = getattr(_LOCAL, 'scope', None)
    return scope if scope is not None and scope.runtime is runtime else None


def snapshot_resource_view(runtime, snapshot):
    """Run the existing selection rules against detached, immutable evidence.

    No method on this view can read the live controller or update live aliases.
    This includes stove/sink helpers and hand-placement candidate selection.
    """
    from .runtime import ThorRuntime
    view = ThorRuntime.__new__(ThorRuntime)
    view.robot_agent_map = dict(runtime.robot_agent_map)
    view.physical_agent_count = runtime.physical_agent_count
    def current_objects(agent_id=None):
        evidence = snapshot.resource_metadata.get('agent_object_selection', {}).get(agent_id)
        if evidence is None:
            return list(snapshot.objects_by_id.values())
        # World facts remain authoritative from this captured event. Only the
        # requesting agent's visibility/distance are used for target ranking.
        # A newly published object absent from its view has unknown distance and
        # is not treated as visible merely because another robot can see it.
        return [dict(obj, **evidence.get(object_id, {'visible': False, 'distance': None}))
                for object_id, obj in snapshot.objects_by_id.items()]

    view.current_objects = current_objects
    view.operated_object_names = set(snapshot.resource_metadata.get('operated_names', ()))
    identities = snapshot.resource_metadata.get('identities', {})
    view.action_resource_manager = ActionResourceManager()
    for object_id, root in identities.items():
        view.action_resource_manager.bind_identity(root, object_id)

    def current_id(object_id):
        if object_id in snapshot.objects_by_id:
            return object_id
        root = identities.get(object_id, object_id)
        return next((candidate for candidate in snapshot.objects_by_id
                     if identities.get(candidate, candidate) == root), object_id)

    for binding in snapshot.resource_metadata.get('aliases', {}).values():
        copied = dict(binding)
        copied['object_id'] = current_id(str(copied.get('object_id', '')))
        view.register_object_id_bindings([copied])
    resolve_alias = view.resolve_object_alias
    view.resolve_object_alias = lambda pattern, agent_id=None: current_id(
        resolve_alias(pattern, agent_id=agent_id))
    return view


def resolve_action_resources(runtime, snapshot, robot_id, action):
    """Compatibility entry point; the registry owns resource declarations."""
    from .action_registry import ActionRegistry
    return ActionRegistry().prepare(runtime, snapshot, robot_id, action).resources


def resolve_declared_resources(runtime, snapshot, robot_id, action, *,
                               object_arguments=(), navigation_recovery=False,
                               pickup_index=None, special=None, scene_only=False):
    from . import actions, context
    manager = manager_for(runtime)
    view = snapshot_resource_view(runtime, snapshot)
    args = action.args()
    bindings, ids = {}, set()
    agent_id = runtime.physical_agent_id(robot_id)

    def bind(pattern, selected=None, role=None):
        if selected is None:
            selected = view.find_object(pattern, agent_id=agent_id)
        object_id = str(selected['objectId'])
        bindings[str(pattern)] = object_id
        bindings[object_id] = object_id
        if role:
            bindings[role] = object_id
        ids.add(object_id)
        return selected

    with context.runtime_scope(view):
        if action.action_type == 'GoToObject':
            view.find_object(args[0], agent_id=agent_id)
        if navigation_recovery:
            for candidate in snapshot.objects_by_id.values():
                if candidate.get('openable') and candidate.get('isOpen'):
                    bind(candidate['objectId'], candidate)
                    if candidate.get('name'):
                        bindings[str(candidate['name'])] = str(candidate['objectId'])
        if special == 'stove':
            burner = actions._resolve_stove_burner(robot_id, args[0], supporting_obj=args[1])
            bind(args[0], burner, '@burner')
            knob = actions._resolve_stove_knob_for_burner(robot_id, burner)
            bind(knob['objectId'], knob, '@knob')
        for index in object_arguments:
            if index < len(args) and str(args[index]) not in bindings:
                bind(args[index])
        if special == 'sink':
            basin = actions._find_sink_basin(robot_id, args[0])
            bind(args[0], basin, '@basin')
            bind('SinkBasin', basin)
            bind('Faucet', role='@faucet')
        for field in ('objectId',):
            if action.parameters.get(field) and action.action_type != 'GoToObject':
                bind(action.parameters[field])
        for pattern in (action.resource_policy.get('object_resources') or action.parameters.get('object_resources') or action.parameters.get('objectResources') or ()):
            bind(pattern)

        held = snapshot.held_objects.get(robot_id, ())
        if action.action_type == 'PutObject' and not scene_only:
            target = bindings.get(str(args[0])) if args else None
            if not held or (args and target not in held):
                raise RuntimeError(f'Cannot PutObject for {robot_id}: robot is not holding the requested object.')
        if action.action_type in ('PutObject', 'ThrowObject'):
            for object_id in held:
                bind(object_id)
        # These helpers may perform a nested PickupObject. Preselect the one
        # receptacle used if a different object is currently occupying the hand.
        if not scene_only and pickup_index is not None and pickup_index < len(args) and held:
            target = bindings.get(str(args[pickup_index]))
            other_held = sorted(set(held) - {target})
            if other_held:
                held_id = other_held[0]
                bind(held_id)
                candidates = view.compatible_receptacle_candidates(
                    agent_id, held_id, view.held_object_type(agent_id, held_id))
                if not candidates:
                    raise RuntimeError(f'No compatible receptacle for {held_id}')
                bind(candidates[0]['objectId'], candidates[0], '@hand_receptacle')

    keys = tuple(sorted({manager.canonical(object_id) for object_id in ids}))
    for other_robot, held in (() if scene_only else snapshot.held_objects.items()):
        if runtime.physical_agent_id(other_robot) == agent_id:
            continue
        conflicts = set(keys).intersection(manager.canonical(value) for value in held)
        if conflicts:
            raise ObjectHeldByOther(f'OBJECT_HELD_BY_OTHER: {sorted(conflicts)} held by {other_robot}')
    return ResolvedActionResources(keys, bindings)


class ActionResourceScope:
    def __init__(self, runtime, owner, resolved):
        self.runtime, self.owner, self.resolved = runtime, owner, resolved
        self.effects_started = False

    def invalid(self, message):
        error = ResourceBindingInvalid if self.effects_started else ResourceBindingDeferred
        raise error(f'RESOURCE_BINDING_INVALID: {message}')

    def bound_objects(self, pattern, objects):
        object_id = self.resolved.bindings.get(str(pattern))
        if object_id is None:
            return None
        # Alias transformations of an admitted object retain their original
        # grant. Do not reselect a different same-type instance on disappearance.
        manager = manager_for(self.runtime)
        matches = [obj for obj in objects if str(obj.get('objectId')) == object_id]
        if not matches:
            matches = [obj for obj in objects if manager.canonical(obj.get('objectId')) == manager.canonical(object_id)]
        if not matches:
            self.invalid(f'{pattern!r} was bound to {object_id}')
        return matches

    def validate(self):
        objects = self.runtime.current_objects()
        for object_id in set(self.resolved.bindings.values()):
            self.bound_objects(object_id, objects)

    def before_step(self, payload, *, mark_effects=True):
        self.validate()
        if payload.get('action') in ('GetReachablePositions', 'GetInteractablePoses'):
            return
        manager = manager_for(self.runtime)
        own_agent = payload.get('agentId', 0)
        for agent_id in range(getattr(self.runtime, 'physical_agent_count', 0)):
            if agent_id == own_agent:
                continue
            held = self.runtime.agent_held_objects_for(agent_id)
            conflicts = {manager.canonical(value) for value in held}.intersection(
                manager.canonical(value) for value in self.resolved.keys)
            if conflicts:
                raise ObjectHeldByOther(f'OBJECT_HELD_BY_OTHER: {sorted(conflicts)} held by agent {agent_id}')
        ids = list(payload.get('objectResources') or ())
        if payload.get('objectId'):
            ids.append(payload['objectId'])
        if payload.get('action') in ('PutObject', 'ThrowObject'):
            ids.extend(self.runtime.agent_held_objects_for(payload.get('agentId', 0)))
        manager = manager_for(self.runtime)
        keys = {manager.canonical(key) for key in self.resolved.keys}
        for object_id in ids:
            if manager.canonical(object_id) not in keys:
                self.invalid(f'unleased helper target {object_id}')
        if payload.get('action') == 'PickupObject':
            from .action_registry import validate_runtime_capability
            robot_id = next((name for name, agent in self.runtime.robot_agent_map.items()
                             if agent == own_agent), None)
            obj = next((obj for obj in self.runtime.current_objects()
                        if obj.get('objectId') == payload.get('objectId')), {})
            validate_runtime_capability(self.runtime, robot_id, 'PickupObject', obj)
        if mark_effects:
            self.effects_started = True


@contextmanager
def action_resource_scope(runtime, owner, resolved):
    previous = getattr(_LOCAL, 'scope', None)
    if previous is not None and previous.runtime is runtime:
        if previous.owner != owner or not set(resolved.keys).issubset(previous.resolved.keys):
            previous.invalid('nested helper attempted a different grant')
        yield previous
        return
    scope = ActionResourceScope(runtime, owner, resolved)
    _LOCAL.scope = scope
    try:
        scope.validate()
        yield scope
        scope.validate()
    finally:
        _LOCAL.scope = previous
