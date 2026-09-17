"""High-level generated task actions and long-running object wait helpers."""

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .plan_types import (PlannedAction)
from .config import INTERACTION_MAX_PASS_STEPS
from .context import get_runtime
from .goals import (
    object_filled_with_coffee,
    object_filled_with_water,
    record_groundtruth_state,
    state_satisfied,
)
from .utils import RobotRef, log, object_center, object_key, require_break_egg_target

@dataclass
class _PlannedActionContext:
    actions: List[PlannedAction]
    index: int = 0


_planned_action_local = threading.local()


def _planned_arg_matches(expected: Any, actual: Any) -> bool:
    if expected == actual:
        return True
    if isinstance(expected, str) and isinstance(actual, str):
        return object_key(expected) == object_key(actual)
    return False


def _planned_action_matches(
    action: PlannedAction,
    name: str,
    args: Tuple[Any, ...],
) -> bool:
    if action.name != name:
        return False
    if not action.args:
        return True
    if len(action.args) > len(args):
        return False
    return all(
        _planned_arg_matches(expected, actual)
        for expected, actual in zip(action.args, args)
    )


def _consume_planned_action(name: str, *args: Any) -> Optional[PlannedAction]:
    context = getattr(_planned_action_local, "context", None)
    if context is None:
        return None
    action_args = tuple(args)
    for index in range(context.index, len(context.actions)):
        action = context.actions[index]
        if not _planned_action_matches(action, name, action_args):
            continue
        context.index = index + 1
        if context.index < len(context.actions):
            return context.actions[context.index]
        return None
    return None
def _interactor():
    from .object_interactor import ObjectInteractor
    return ObjectInteractor(get_runtime(), helper_log=log,
                            max_pass_steps=INTERACTION_MAX_PASS_STEPS,
                            planned_action_consumer=_consume_planned_action)


def current_objects(agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
    return _interactor().current_objects(agent_id)


def find_object(
    pattern: Any,
    agent_id: Optional[int] = None,
    require_center: bool = False,
) -> Dict[str, Any]:
    return _interactor().find_object(pattern, agent_id, require_center)


def WaitOneTick(robot: RobotRef) -> None:
    return _interactor().WaitOneTick(robot)


def GoToObject(robot: RobotRef, dest_obj: Any) -> None:
    return _interactor().GoToObject(robot, dest_obj)


def PickupObject(robot: RobotRef, pick_obj: Any) -> None:
    return _interactor().PickupObject(robot, pick_obj)


def TeleportObjectToHand(robot: RobotRef, pick_obj: Any) -> None:
    return _interactor().TeleportObjectToHand(robot, pick_obj)


def PutObject(robot: RobotRef, put_obj: Any, recp: Any) -> None:
    return _interactor().PutObject(robot, put_obj, recp)


def SwitchOn(robot: RobotRef, sw_obj: Any) -> None:
    return _interactor().SwitchOn(robot, sw_obj)


def SwitchOff(robot: RobotRef, sw_obj: Any) -> None:
    return _interactor().SwitchOff(robot, sw_obj)


def OpenObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().OpenObject(robot, obj_name)


def CloseObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().CloseObject(robot, obj_name)


def BreakObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().BreakObject(robot, obj_name)


def BreakEgg(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().BreakEgg(robot, obj_name)


def PrepareEgg(robot: RobotRef, obj_name: Any, container_name: Any) -> None:
    return _interactor().PrepareEgg(robot, obj_name, container_name)


def SliceObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().SliceObject(robot, obj_name)


def CleanObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().CleanObject(robot, obj_name)


def DirtyObject(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().DirtyObject(robot, obj_name)


def EmptyLiquid(robot: RobotRef, obj_name: Any) -> None:
    return _interactor().EmptyLiquid(robot, obj_name)


def _current_object_by_id(agent_id: int, object_id: str) -> Dict[str, Any]:
    return _interactor()._current_object_by_id(agent_id, object_id)


def _object_has_liquid(obj: Dict[str, Any]) -> bool:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._object_has_liquid(obj)


def _bound_helper_object(role):
    return _interactor()._bound_helper_object(role)


def _find_sink_basin(robot: RobotRef, sink: Any) -> Dict[str, Any]:
    return _interactor()._find_sink_basin(robot, sink)


def _fillwater_sinkbasin_putobject_has_no_positions(
    exc: BaseException,
    sink_basin: Dict[str, Any],
) -> bool:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._fillwater_sinkbasin_putobject_has_no_positions(exc, sink_basin)


def _discount_skipped_runtime_attempt(runtime_obj: Any) -> None:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._discount_skipped_runtime_attempt(runtime_obj)


def _object_is_toggled_on(obj: Dict[str, Any]) -> bool:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._object_is_toggled_on(obj)


def _object_parent_receptacles(obj: Dict[str, Any]) -> List[str]:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._object_parent_receptacles(obj)


def _object_on_receptacle(obj: Dict[str, Any], receptacle_id: str) -> bool:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._object_on_receptacle(obj, receptacle_id)


def _stove_parent_burner_id(obj: Dict[str, Any]) -> Optional[str]:
    from .object_interactor import ObjectInteractor
    return ObjectInteractor._stove_parent_burner_id(obj)


def _resolve_stove_burner(
    robot: RobotRef,
    stove_burner: Any,
    supporting_obj: Optional[Any] = None,
) -> Dict[str, Any]:
    return _interactor()._resolve_stove_burner(robot, stove_burner, supporting_obj)


def _resolve_stove_knob_for_burner(
    robot: RobotRef,
    burner: Dict[str, Any],
) -> Dict[str, Any]:
    return _interactor()._resolve_stove_knob_for_burner(robot, burner)


def _set_stove_knob_state(
    robot: RobotRef,
    knob: Dict[str, Any],
    desired_on: bool,
) -> bool:
    return _interactor()._set_stove_knob_state(robot, knob, desired_on)


def _ensure_object_on_receptacle(
    robot: RobotRef,
    obj_name: Any,
    receptacle_id: str,
) -> bool:
    return _interactor()._ensure_object_on_receptacle(robot, obj_name, receptacle_id)


def _wait_for_object_cooked(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    return _interactor()._wait_for_object_cooked(robot, obj_name, action_name)


def _wait_for_object_hot(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    return _interactor()._wait_for_object_hot(robot, obj_name, action_name)


def _wait_for_object_cold(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    return _interactor()._wait_for_object_cold(robot, obj_name, action_name)


def _wait_for_object_on(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    return _interactor()._wait_for_object_on(robot, obj_name, action_name)


def _wait_for_microwave_result(robot: RobotRef, item: Any) -> None:
    return _interactor()._wait_for_microwave_result(robot, item)


def _wait_for_coffee_machine_result(robot: RobotRef, mug: Any) -> None:
    return _interactor()._wait_for_coffee_machine_result(robot, mug)


def _wait_for_object_filled_with_water(robot: RobotRef, obj_name: Any) -> None:
    return _interactor()._wait_for_object_filled_with_water(robot, obj_name)


def _wait_for_toaster_result(robot: RobotRef, bread: Any) -> None:
    return _interactor()._wait_for_toaster_result(robot, bread)


def RunMicrowave(robot: RobotRef, microwave: Any, item: Any) -> None:
    return _interactor().RunMicrowave(robot, microwave, item)


def RunCoffeeMachine(robot: RobotRef, coffee_machine: Any, mug: Any) -> None:
    return _interactor().RunCoffeeMachine(robot, coffee_machine, mug)


def RunToaster(robot: RobotRef, toaster: Any, bread: Any) -> None:
    return _interactor().RunToaster(robot, toaster, bread)


def CookByStoveBurner(
    robot: RobotRef,
    stove_burner: Any,
    container: Any,
    food: Any,
) -> None:
    return _interactor().CookByStoveBurner(robot, stove_burner, container, food)


def HeatByStoveBurner(robot: RobotRef, stove_burner: Any, obj: Any) -> None:
    return _interactor().HeatByStoveBurner(robot, stove_burner, obj)


def FireByStoveBurner(robot: RobotRef, stove_burner: Any, candle: Any) -> None:
    return _interactor().FireByStoveBurner(robot, stove_burner, candle)


def FillWater(robot: RobotRef, sink: Any, obj: Any) -> None:
    return _interactor().FillWater(robot, sink, obj)


def ColdObject(robot: RobotRef, fridge: Any, obj: Any) -> None:
    return _interactor().ColdObject(robot, fridge, obj)


def ThrowObject(robot: RobotRef) -> None:
    return _interactor().ThrowObject(robot)
