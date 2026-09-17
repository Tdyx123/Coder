"""Fixed action definitions shared by validation, admission and dispatch."""
import math
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple

from .action_resources import ResolvedActionResources, resolve_declared_resources
from .capability_checks import capability_failure
from .utils import require_break_egg_target


from .plan_types import DIRECT_PAYLOAD_FIELDS


@dataclass(frozen=True)
class ActionSpec:
    name: str
    helper_arity: Optional[int]
    direct_required: Tuple[str, ...]
    direct_allowed: bool
    executor: Callable
    resource_resolver: Callable


@dataclass(frozen=True)
class NormalizedAction:
    action: Any
    form: str
    parameters: Mapping[str, Any]

    def __post_init__(self):
        object.__setattr__(self, 'parameters', MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class PreparedAction:
    normalized: NormalizedAction
    resources: ResolvedActionResources


def runtime_robot(runtime, robot_id):
    matches = [robot for robot in getattr(runtime, 'robots', ())
               if isinstance(robot, dict) and robot.get('name') == robot_id]
    if len(matches) != 1:
        raise RuntimeError(f'validation_data_missing: Cannot uniquely resolve robot {robot_id!r}.')
    return matches[0]


def validate_runtime_capability(runtime, robot_id, action_type, obj=None):
    failure = capability_failure(runtime_robot(runtime, robot_id), action_type,
                                 object_name=(obj or {}).get('objectId'),
                                 object_mass=(obj or {}).get('mass'))
    if failure:
        raise RuntimeError(f"{failure['reason']}: {robot_id}: {failure['message']}")


def validate_recovery_capabilities(runtime, agent_id, *action_types):
    """Preflight all additional recovery/restoration skills before any effect."""
    robot_id = next((name for name, agent in runtime.robot_agent_map.items()
                     if agent == agent_id), None)
    for action_type in action_types:
        validate_runtime_capability(runtime, robot_id, action_type)


def _direct(runtime, robot_id, normalized, context):
    payload = {key: value for key, value in normalized.parameters.items()
               if key not in {'args', 'object_resources', 'objectResources'}}
    name = normalized.action.action_type
    payload['action'] = 'Pass' if name in {'Wait', 'WaitOneTick'} else name
    payload.setdefault('agentId', runtime.physical_agent_id(robot_id))
    return runtime.step(payload, check_success=name not in {'Done', 'WaitOneTick'})


def _helper(function, runtime, robot_id, normalized, context):
    from .object_interactor import ObjectInteractor
    return function(ObjectInteractor(runtime))(robot_id, *normalized.parameters['args'])


def _object(action_type, runtime, robot_id, normalized, context):
    return runtime.object_action(action_type, robot_id, normalized.parameters['args'][0])


def _navigate(runtime, robot_id, normalized, context):
    from .plan_types import (PlannedAction)
    following = context.get('next_action')
    return runtime.navigate_to_object(robot_id, normalized.parameters['args'][0],
        next_action=PlannedAction(following.action_type, following.args()) if following else None,
        phase_coordinator=context.get('phase_coordinator'), action_wave=context.get('action_wave'))


def _put(runtime, robot_id, normalized, context):
    args = normalized.parameters['args']
    put_obj, receptacle = args[:2]
    agent_id = runtime.physical_agent_id(robot_id)
    held_object = runtime.agent_held_object_matching(agent_id, put_obj)
    if held_object is None:
        held_objects = sorted(runtime.agent_held_objects_for(agent_id))
        held_description = ", ".join(held_objects) if held_objects else "nothing"
        raise RuntimeError(
            f"Cannot PutObject {put_obj!r} for agent {agent_id}: "
            "robot is not holding it. "
            f"Currently holding: {held_description}."
        )
    return runtime.object_action(
        "PutObject",
        robot_id,
        receptacle,
        extra_object_resources=(held_object,),
    )


def _wait_until(runtime, robot_id, normalized, context):
    return runtime.agent_event(runtime.physical_agent_id(robot_id))


class ActionRegistry:
    def __init__(self):
        specs = []
        def register(name, arity=None, *, direct=False, required=(), executor=None,
                     objects=(), navigation=False, pickup=None, special=None):
            specs.append(ActionSpec(name, arity, required, direct, executor or _direct,
                partial(resolve_declared_resources, object_arguments=objects,
                        navigation_recovery=navigation, pickup_index=pickup, special=special)))
        register('GoToObject', 1, executor=_navigate, navigation=True)
        register('PickupObject', 1, direct=True, required=('objectId',),
                 executor=partial(_object, 'PickupObject'), objects=(0,), navigation=True, pickup=0)
        register('TeleportObjectToHand', 1, executor=partial(_helper, lambda helpers: helpers.TeleportObjectToHand), objects=(0,))
        register('PutObject', 2, direct=True, required=('objectId',), executor=_put, objects=(0, 1))
        register('SwitchOn', 1, executor=partial(_helper, lambda helpers: helpers.SwitchOn), objects=(0,))
        register('SwitchOff', 1, executor=partial(_helper, lambda helpers: helpers.SwitchOff), objects=(0,))
        for name in ('OpenObject', 'CloseObject', 'BreakObject', 'SliceObject', 'CleanObject', 'DirtyObject'):
            register(name, 1, direct=True, required=('objectId',), executor=partial(_object, name),
                     objects=(0,), navigation=name == 'SliceObject')
        register('BreakEgg', 1, executor=partial(_object, 'BreakObject'), objects=(0,))
        register('PrepareEgg', 2, executor=partial(_object, 'BreakObject'), objects=(0, 1))
        register('EmptyLiquid', 1, executor=partial(_object, 'EmptyLiquidFromObject'), objects=(0,))
        register('RunMicrowave', 2, executor=partial(_helper, lambda helpers: helpers.RunMicrowave), objects=(0, 1))
        register('RunCoffeeMachine', 2, executor=partial(_helper, lambda helpers: helpers.RunCoffeeMachine), objects=(0, 1))
        register('ColdObject', 2, executor=partial(_helper, lambda helpers: helpers.ColdObject), objects=(0, 1))
        register('RunToaster', 2, executor=partial(_helper, lambda helpers: helpers.RunToaster), objects=(0, 1), navigation=True, pickup=1)
        register('CookByStoveBurner', 3, executor=partial(_helper, lambda helpers: helpers.CookByStoveBurner), objects=(0, 1, 2), navigation=True, pickup=1, special='stove')
        register('HeatByStoveBurner', 2, executor=partial(_helper, lambda helpers: helpers.HeatByStoveBurner), objects=(0, 1), navigation=True, pickup=1, special='stove')
        register('FireByStoveBurner', 2, executor=partial(_helper, lambda helpers: helpers.FireByStoveBurner), objects=(0, 1), navigation=True, pickup=1, special='stove')
        register('FillWater', 2, executor=partial(_helper, lambda helpers: helpers.FillWater), objects=(1,), navigation=True, pickup=1, special='sink')
        register('ThrowObject', 0, direct=True, executor=partial(_helper, lambda helpers: helpers.ThrowObject))
        register('WaitOneTick', 0, direct=True)
        register('WaitUntil', 0, executor=_wait_until)
        for name in ('MoveAhead', 'RotateLeft', 'RotateRight', 'LookUp', 'LookDown', 'Pass', 'Wait', 'Done'):
            register(name, direct=True)
        register('Teleport', direct=True, required=('position',))
        for name in ('ToggleObjectOn', 'ToggleObjectOff'):
            register(name, direct=True, required=('objectId',))
        self.specs = MappingProxyType({spec.name: spec for spec in specs})
        if len(self.specs) != len(specs):
            raise RuntimeError('Duplicate action registration')

    def object_names(self, action):
        spec = self.get(action.action_type)
        if 'object_resources' in action.resource_policy:
            return tuple(action.resource_policy['object_resources'])
        if 'objectId' in action.parameters:
            return (action.parameters['objectId'],)
        return tuple(action.args()) if spec.helper_arity else ()

    def object_action_names(self):
        return frozenset(spec.name for spec in self.specs.values()
                         if spec.resource_resolver.keywords['object_arguments']
                         or 'objectId' in spec.direct_required)

    def get(self, name):
        try:
            return self.specs[name]
        except KeyError:
            raise ValueError(f'Unsupported action {name!r}') from None

    def normalize(self, action):
        spec = self.get(action.action_type)
        params = dict(action.parameters)
        helper = 'args' in params or (spec.helper_arity == 0 and not params)
        if helper:
            args = action.args()
            if spec.helper_arity is None or len(args) != spec.helper_arity:
                if spec.name == 'BreakEgg':
                    raise RuntimeError('BreakEgg requires Egg as its only object argument.')
                if spec.name == 'PrepareEgg':
                    raise RuntimeError('PrepareEgg requires Egg and container targets; exactly two object arguments.')
                raise ValueError(f'{spec.name} requires exactly {spec.helper_arity} helper target arguments')
            if spec.name in {'BreakEgg', 'PrepareEgg'}:
                try:
                    require_break_egg_target(args[0])
                except RuntimeError as exc:
                    raise RuntimeError(f'{spec.name}: {exc}') from exc
            if params.get('objectId') is not None:
                target_index = 1 if spec.name == 'PutObject' else 0
                if target_index >= len(args) or str(args[target_index]) != str(params['objectId']):
                    raise ValueError(f'{spec.name}: args and objectId specify conflicting targets')
            params['args'] = args
        elif not spec.direct_allowed:
            raise ValueError(f'{spec.name} requires a helper target')
        else:
            for key in spec.direct_required:
                if key not in params or params[key] is None or params[key] == '':
                    raise ValueError(f'{spec.name} requires target {key}')
        if 'objectId' in params and (not isinstance(params['objectId'], str) or not params['objectId'].strip()):
            raise ValueError(f'{spec.name} objectId must be a nonempty string')
        for key in ('degrees', 'moveMagnitude', 'horizon', 'throwMagnitude'):
            if key in params:
                value = params[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f'{spec.name} {key} must be finite numeric')
        for field in ('position', 'rotation'):
            if field not in params:
                continue
            position = params[field]
            if (not isinstance(position, Mapping) or set(position) != {'x', 'y', 'z'} or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in position.values())):
                raise ValueError(f'Teleport {field} must be a finite three-dimensional vector')
        if 'agentId' in params and (type(params['agentId']) is not int or params['agentId'] < 0):
            raise ValueError('agentId must be a nonnegative integer')
        return NormalizedAction(action, 'helper' if helper else 'thor', params)

    def validate_shape(self, action):
        self.normalize(action)

    def _validate_robot(self, runtime, snapshot, robot_id, action):
        normalized = self.normalize(action)
        agent = runtime.physical_agent_id(robot_id)
        if robot_id not in snapshot.robot_positions or agent >= runtime.physical_agent_count:
            raise ValueError(f'Unknown robot {robot_id!r}')
        if normalized.parameters.get('agentId', agent) != agent:
            raise ValueError(f'agentId must match robot {robot_id!r} physical agent {agent}')
        robot = runtime_robot(runtime, robot_id)
        # Skills and capacity precede target lookup, as in generation.
        failure = capability_failure(robot, action.action_type, object_name='target', object_mass=0)
        if failure:
            raise RuntimeError(f"{failure['reason']}: {failure['message']}")
        if normalized.form == 'thor' and 'objectId' in normalized.parameters:
            if normalized.parameters['objectId'] not in snapshot.objects_by_id:
                raise ValueError(f'{action.action_type} objectId must identify an existing scene object')
        return normalized

    def _validate_mass(self, runtime, snapshot, robot_id, action, resources):
        if action.action_type == 'PickupObject':
            target = action.args()[0] if 'args' in action.parameters else action.parameters['objectId']
            obj = snapshot.objects_by_id.get(resources.bindings[str(target)])
            validate_runtime_capability(runtime, robot_id, action.action_type, obj)

    def validate_scene_action(self, runtime, snapshot, robot_id, action):
        normalized = self._validate_robot(runtime, snapshot, robot_id, action)
        # The same resolver validates compound fallback targets and implicit roles;
        # inventory and ownership are checked later at each admission.
        resources = self.get(action.action_type).resource_resolver(
            runtime, snapshot, robot_id, action, scene_only=True)
        self._validate_mass(runtime, snapshot, robot_id, action, resources)
        return normalized

    def prepare(self, runtime, snapshot, robot_id, action):
        normalized = self._validate_robot(runtime, snapshot, robot_id, action)
        resources = self.get(action.action_type).resource_resolver(runtime, snapshot, robot_id, action)
        self._validate_mass(runtime, snapshot, robot_id, action, resources)
        return PreparedAction(normalized, resources)

    def execute(self, runtime, robot_id, prepared, action_context):
        normalized = prepared.normalized
        action = normalized.action
        context = action_context or {}
        world = context.get('world_state')
        if (action.wait_until is not None and world is not None
                and not getattr(context.get('phase_coordinator'), 'scheduler_admissions', False)
                and not action.wait_until(world)):
            raise RuntimeError(f'wait condition for {action.action_type} is not satisfied')
        # Defend the physical queue boundary even when a caller supplies PreparedAction.
        agent = runtime.physical_agent_id(robot_id)
        if normalized.parameters.get('agentId', agent) != agent:
            raise ValueError('agentId must match the queue robot')
        from .object_interactor import ObjectInteractor
        return ObjectInteractor(runtime).execute(robot_id, prepared, context, registry=self)
