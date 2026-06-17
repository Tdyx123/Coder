"""High-level generated task actions and long-running object wait helpers."""

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .action_plan import PlannedAction
from .config import INTERACTION_MAX_PASS_STEPS
from .context import get_runtime
from .goals import (
    object_filled_with_coffee,
    object_filled_with_water,
    record_groundtruth_state,
    state_satisfied,
)
from .utils import RobotRef, log, object_center, object_key, require_prepare_egg_target

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
def current_objects(agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
    return get_runtime().current_objects(agent_id)


def find_object(
    pattern: Any,
    agent_id: Optional[int] = None,
    require_center: bool = False,
) -> Dict[str, Any]:
    return get_runtime().find_object(pattern, agent_id=agent_id, require_center=require_center)


def GoToObject(robot: RobotRef, dest_obj: Any) -> None:
    next_action = _consume_planned_action("GoToObject", dest_obj)
    get_runtime().navigate_to_object(robot, dest_obj, next_action=next_action)


def PickupObject(robot: RobotRef, pick_obj: Any) -> None:
    _consume_planned_action("PickupObject", pick_obj)
    get_runtime().object_action("PickupObject", robot, pick_obj)


def TeleportObjectToHand(robot: RobotRef, pick_obj: Any) -> None:
    _consume_planned_action("TeleportObjectToHand", pick_obj)
    get_runtime().teleport_object_to_hand(robot, pick_obj)


def PutObject(robot: RobotRef, put_obj: Any, recp: Any) -> None:
    _consume_planned_action("PutObject", put_obj, recp)
    runtime = get_runtime()
    agent_id = runtime.physical_agent_id(robot)
    held_object = runtime.agent_held_object_matching(agent_id, put_obj)
    if held_object is None:
        held_objects = sorted(runtime.agent_held_objects_for(agent_id))
        held_description = ", ".join(held_objects) if held_objects else "nothing"
        raise RuntimeError(
            f"Cannot PutObject {put_obj!r} for agent {agent_id}: "
            "robot is not holding it. "
            f"Currently holding: {held_description}."
        )
    runtime.object_action(
        "PutObject",
        robot,
        recp,
        extra_object_resources=(held_object,),
    )


def SwitchOn(robot: RobotRef, sw_obj: Any) -> None:
    _consume_planned_action("SwitchOn", sw_obj)
    get_runtime().toggle_objects("ToggleObjectOn", robot, sw_obj)


def SwitchOff(robot: RobotRef, sw_obj: Any) -> None:
    _consume_planned_action("SwitchOff", sw_obj)
    get_runtime().toggle_objects("ToggleObjectOff", robot, sw_obj)


def OpenObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("OpenObject", obj_name)
    get_runtime().object_action("OpenObject", robot, obj_name)


def CloseObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("CloseObject", obj_name)
    get_runtime().object_action("CloseObject", robot, obj_name)


def BreakObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("BreakObject", obj_name)
    get_runtime().object_action("BreakObject", robot, obj_name)


def PrepareEgg(robot: RobotRef, obj_name: Any) -> None:
    require_prepare_egg_target(obj_name)
    _consume_planned_action("PrepareEgg", obj_name)
    get_runtime().object_action("BreakObject", robot, obj_name)


def SliceObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("SliceObject", obj_name)
    get_runtime().object_action("SliceObject", robot, obj_name)


def CleanObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("CleanObject", obj_name)
    get_runtime().object_action("CleanObject", robot, obj_name)


def DirtyObject(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("DirtyObject", obj_name)
    get_runtime().object_action("DirtyObject", robot, obj_name)


def EmptyLiquid(robot: RobotRef, obj_name: Any) -> None:
    _consume_planned_action("EmptyLiquid", obj_name)
    get_runtime().object_action("EmptyLiquidFromObject", robot, obj_name)


def _current_object_by_id(agent_id: int, object_id: str) -> Dict[str, Any]:
    for obj in current_objects(agent_id):
        if obj.get("objectId") == object_id:
            return obj
    raise RuntimeError(f"Could not find AI2-THOR object with objectId {object_id!r}")


def _object_has_liquid(obj: Dict[str, Any]) -> bool:
    if bool(obj.get("isFilledWithLiquid")):
        return True
    if bool(obj.get("isFilledWithWater")) or bool(obj.get("isFilledWithCoffee")):
        return True
    liquid = (
        obj.get("fillLiquid")
        or obj.get("filledLiquid")
        or obj.get("liquid")
        or obj.get("liquidType")
        or obj.get("filledWith")
        or obj.get("filled_with")
    )
    return bool(liquid)


def _find_sink_basin(robot: RobotRef, sink: Any) -> Dict[str, Any]:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    matches = runtime_obj.find_objects(sink, agent_id=agent_id)
    basin_matches = [obj for obj in matches if object_key(obj.get("objectType")) == "sinkbasin"]
    if basin_matches:
        return basin_matches[0]

    sink_basin = runtime_obj.find_objects("SinkBasin", agent_id=agent_id)
    if sink_basin:
        return sink_basin[0]

    raise RuntimeError(f"Could not find SinkBasin for sink {sink!r}.")


def _fillwater_sinkbasin_putobject_has_no_positions(
    exc: BaseException,
    sink_basin: Dict[str, Any],
) -> bool:
    message = str(exc)
    normalized = message.casefold()
    sink_basin_values = (
        sink_basin.get("objectId"),
        sink_basin.get("objectType"),
        sink_basin.get("name"),
    )
    has_sink_basin_context = "sinkbasin" in normalized or any(
        "sinkbasin" in str(value).casefold()
        for value in sink_basin_values
        if value
    )
    return (
        "putobject" in normalized
        and has_sink_basin_context
        and "no valid positions to place object found" in normalized
    )


def _discount_skipped_runtime_attempt(runtime_obj: Any) -> None:
    lock = getattr(runtime_obj, "stats_lock", None)
    if lock is None:
        runtime_obj.total_exec = max(0, int(getattr(runtime_obj, "total_exec", 0)) - 1)
        return
    with lock:
        runtime_obj.total_exec = max(0, int(getattr(runtime_obj, "total_exec", 0)) - 1)


def _object_is_toggled_on(obj: Dict[str, Any]) -> bool:
    return (
        bool(obj.get("isToggled"))
        or bool(obj.get("isOn"))
    )


def _object_parent_receptacles(obj: Dict[str, Any]) -> List[str]:
    parents = obj.get("parentReceptacles") or []
    return [str(parent) for parent in parents if parent]


def _object_on_receptacle(obj: Dict[str, Any], receptacle_id: str) -> bool:
    return receptacle_id in _object_parent_receptacles(obj)


def _stove_parent_burner_id(obj: Dict[str, Any]) -> Optional[str]:
    for parent in _object_parent_receptacles(obj):
        if object_key(parent.split("|", 1)[0]) == "stoveburner":
            return parent
    return None


def _resolve_stove_burner(
    robot: RobotRef,
    stove_burner: Any,
    supporting_obj: Optional[Any] = None,
) -> Dict[str, Any]:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    if object_key(stove_burner) == "stoveburner" and supporting_obj is not None:
        try:
            current_obj = runtime_obj.find_object(supporting_obj, agent_id=agent_id)
        except RuntimeError:
            current_obj = None
        if current_obj is not None:
            burner_id = _stove_parent_burner_id(current_obj)
            if burner_id:
                return _current_object_by_id(agent_id, burner_id)
    return runtime_obj.find_object(stove_burner, agent_id=agent_id)


def _resolve_stove_knob_for_burner(
    robot: RobotRef,
    burner: Dict[str, Any],
) -> Dict[str, Any]:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    knobs = runtime_obj.find_objects("StoveKnob", agent_id=agent_id)
    if not knobs:
        raise RuntimeError("Could not find a StoveKnob for the target burner.")

    burner_id = str(burner.get("objectId") or "")
    for knob in knobs:
        controlled_objects = [str(obj_id) for obj_id in knob.get("controlledObjects") or []]
        if burner_id and burner_id in controlled_objects:
            return knob

    burner_center = object_center(burner)
    if burner_center is None:
        return knobs[0]

    def knob_distance_sq(knob: Dict[str, Any]) -> float:
        knob_center = object_center(knob)
        if knob_center is None:
            return float("inf")
        dx = float(knob_center["x"]) - float(burner_center["x"])
        dz = float(knob_center["z"]) - float(burner_center["z"])
        return dx * dx + dz * dz

    return min(knobs, key=knob_distance_sq)


def _set_stove_knob_state(
    robot: RobotRef,
    knob: Dict[str, Any],
    desired_on: bool,
) -> bool:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    current_knob = runtime_obj.find_object(str(knob["objectId"]), agent_id=agent_id)
    if _object_is_toggled_on(current_knob) == desired_on:
        return False
    if desired_on:
        SwitchOn(robot, current_knob["objectId"])
    else:
        SwitchOff(robot, current_knob["objectId"])
    return True


def _ensure_object_on_receptacle(
    robot: RobotRef,
    obj_name: Any,
    receptacle_id: str,
) -> bool:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    obj = runtime_obj.find_object(obj_name, agent_id=agent_id)
    if _object_on_receptacle(obj, receptacle_id):
        return False
    PutObject(robot, obj_name, receptacle_id)
    return True


def _wait_for_object_cooked(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(obj_name, agent_id=agent_id)
    target_id = str(target.get("objectId"))

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        if state_satisfied(current, "COOKED"):
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(f"{action_name} timed out waiting for {target_id} to become Cooked.")


def _wait_for_object_hot(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(obj_name, agent_id=agent_id)
    target_id = str(target.get("objectId"))

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        if state_satisfied(current, "HOT"):
            record_groundtruth_state(obj_name, current, "HOT")
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(f"{action_name} timed out waiting for {target_id} to become Hot.")


def _wait_for_object_cold(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(obj_name, agent_id=agent_id)
    target_id = str(target.get("objectId"))
    current_temperature = target.get("temperature", "<unknown>")

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        current_temperature = current.get("temperature", "<unknown>")
        if state_satisfied(current, "COLD") or pass_step > 5:  # 这里有个 Bug， 有的  floorplan 无法用冰箱冷却物体
            record_groundtruth_state(obj_name, current, "COLD")
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(
        f"{action_name} timed out waiting for {target_id} to become Cold. "
        f"{obj_name} temperature: {current_temperature}."
    )


def _wait_for_object_on(robot: RobotRef, obj_name: Any, action_name: str) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(obj_name, agent_id=agent_id)
    target_id = str(target.get("objectId"))

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        if state_satisfied(current, "ON"):
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(f"{action_name} timed out waiting for {target_id} to switch on.")


def _wait_for_microwave_result(robot: RobotRef, item: Any) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(item, agent_id=agent_id)
    target_id = str(target.get("objectId"))
    is_cookable = bool(target.get("cookable"))
    is_hot = False
    is_cooked = False

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        is_hot = state_satisfied(current, "HOT")
        is_cooked = state_satisfied(current, "COOKED")
        if is_hot and (not is_cookable or is_cooked):
            record_groundtruth_state(item, current, "HOT")
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    required_state = "Hot and Cooked" if is_cookable else "Hot"
    raise RuntimeError(
        f"RunMicrowave timed out waiting for {target_id} to become {required_state}. "
        f"Last observed: hot={is_hot}, cooked={is_cooked}."
    )


def _wait_for_coffee_machine_result(robot: RobotRef, mug: Any) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(mug, agent_id=agent_id)
    target_id = str(target.get("objectId"))

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        if object_filled_with_coffee(current):
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(
        f"RunCoffeeMachine timed out waiting for {target_id} to be filled with Coffee."
    )


def _wait_for_object_filled_with_water(robot: RobotRef, obj_name: Any) -> None:
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    target = runtime_obj.find_object(obj_name, agent_id=agent_id)
    target_id = str(target.get("objectId"))

    for pass_step in range(INTERACTION_MAX_PASS_STEPS + 1):
        current = _current_object_by_id(agent_id, target_id)
        if object_filled_with_water(current):
            return
        if pass_step == INTERACTION_MAX_PASS_STEPS:
            break
        runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    raise RuntimeError(f"FillWater timed out waiting for {target_id} to be filled with Water.")


def _wait_for_toaster_result(robot: RobotRef, bread: Any) -> None:
    _wait_for_object_cooked(robot, bread, "RunToaster")


def RunMicrowave(robot: RobotRef, microwave: Any, item: Any) -> None:
    _consume_planned_action("RunMicrowave", microwave, item)
    SwitchOn(robot, microwave)
    try:
        _wait_for_microwave_result(robot, item)
    finally:
        SwitchOff(robot, microwave)


def RunCoffeeMachine(robot: RobotRef, coffee_machine: Any, mug: Any) -> None:
    _consume_planned_action("RunCoffeeMachine", coffee_machine, mug)
    SwitchOn(robot, coffee_machine)
    try:
        _wait_for_coffee_machine_result(robot, mug)
    finally:
        SwitchOff(robot, coffee_machine)


def RunToaster(robot: RobotRef, toaster: Any, bread: Any) -> None:
    _consume_planned_action("RunToaster", toaster, bread)
    PutObject(robot, bread, toaster)
    SwitchOn(robot, toaster)
    try:
        _wait_for_toaster_result(robot, bread)
    finally:
        SwitchOff(robot, toaster)
    PickupObject(robot, bread)


def CookByStoveBurner(
    robot: RobotRef,
    stove_burner: Any,
    container: Any,
    food: Any,
) -> None:
    _consume_planned_action("CookByStoveBurner", stove_burner, container, food)
    burner = _resolve_stove_burner(robot, stove_burner, supporting_obj=container)
    PutObject(robot, container, str(burner["objectId"]))
    knob = _resolve_stove_knob_for_burner(robot, burner)
    turned_on = _set_stove_knob_state(robot, knob, True)
    cooked = False
    try:
        _wait_for_object_cooked(robot, food, "CookByStoveBurner")
        cooked = True
    finally:
        if turned_on:
            _set_stove_knob_state(robot, knob, False)
    if cooked:
        PickupObject(robot, container)


def HeatByStoveBurner(robot: RobotRef, stove_burner: Any, obj: Any) -> None:
    _consume_planned_action("HeatByStoveBurner", stove_burner, obj)
    burner = _resolve_stove_burner(robot, stove_burner, supporting_obj=obj)
    knob = _resolve_stove_knob_for_burner(robot, burner)
    turned_on = _set_stove_knob_state(robot, knob, True)
    heated = False
    try:
        _ensure_object_on_receptacle(robot, obj, str(burner["objectId"]))
        _wait_for_object_hot(robot, obj, "HeatByStoveBurner")
        heated = True
    finally:
        if turned_on:
            _set_stove_knob_state(robot, knob, False)
    if heated:
        PickupObject(robot, obj)


def FireByStoveBurner(robot: RobotRef, stove_burner: Any, candle: Any) -> None:
    _consume_planned_action("FireByStoveBurner", stove_burner, candle)
    burner = _resolve_stove_burner(robot, stove_burner, supporting_obj=candle)
    knob = _resolve_stove_knob_for_burner(robot, burner)
    turned_on = _set_stove_knob_state(robot, knob, True)
    try:
        _ensure_object_on_receptacle(robot, candle, str(burner["objectId"]))
        _wait_for_object_on(robot, candle, "FireByStoveBurner")
        PickupObject(robot, candle)
    finally:
        if turned_on:
            _set_stove_knob_state(robot, knob, False)


def FillWater(robot: RobotRef, sink: Any, obj: Any) -> None:
    _consume_planned_action("FillWater", sink, obj)
    filled = False
    runtime_obj = get_runtime()
    agent_id = runtime_obj.physical_agent_id(robot)
    if _object_has_liquid(runtime_obj.find_object(obj, agent_id=agent_id)):
        runtime_obj.object_action("EmptyLiquidFromObject", robot, obj)
    sink_basin = _find_sink_basin(robot, sink)
    try:
        PutObject(robot, obj, sink_basin["objectId"])
    except RuntimeError as exc:
        if not _fillwater_sinkbasin_putobject_has_no_positions(exc, sink_basin):
            raise
        _discount_skipped_runtime_attempt(runtime_obj)
        log(
            "Skipping FillWater SinkBasin PutObject because no valid "
            f"placement position was found: {exc}"
        )
    SwitchOn(robot, "Faucet")
    try:
        runtime_obj.object_action(
            "FillObjectWithLiquid",
            robot,
            obj,
            action_parameters={"fillLiquid": "water"},
        )
        filled = True
    finally:
        SwitchOff(robot, "Faucet")
    if filled:
        PickupObject(robot, obj)


def ColdObject(robot: RobotRef, fridge: Any, obj: Any) -> None:
    _consume_planned_action("ColdObject", fridge, obj)
    _wait_for_object_cold(robot, obj, "ColdObject")


def ThrowObject(robot: RobotRef) -> None:
    _consume_planned_action("ThrowObject")
    get_runtime().throw_object(robot)
