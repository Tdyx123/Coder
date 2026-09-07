"""Goal state evaluation and ground-truth bookkeeping."""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .demo_state import (
    get_ground_truth,
    ground_truth_lock,
    verified_ground_truth_goal_signatures,
)
from .evaluation import EvaluationContext, GoalSpec
from .utils import matches_object, object_key


def goal_states(goal: Dict[str, Any]) -> List[str]:
    states = goal.get("states")
    if states is None:
        state = goal.get("state")
        return [state] if state else []
    if isinstance(states, str):
        return [states] if states else []
    return [state for state in states if state]


def format_goal(goal: Dict[str, Any]) -> str:
    name = str(goal.get("name") or "<unknown>")
    parts = [name]
    states = [str(state).upper() for state in goal_states(goal)]
    contains = [str(item) for item in goal.get("contains") or []]
    if states:
        parts.append(f"states=[{', '.join(states)}]")
    if contains:
        parts.append(f"contains=[{', '.join(contains)}]")
    return " ".join(parts)


def goal_signature(goal: Dict[str, Any]) -> Tuple[str, Tuple[str, ...], Tuple[str, ...]]:
    states = tuple(sorted(str(state).upper() for state in goal_states(goal)))
    contains = tuple(sorted(object_key(item) for item in goal.get("contains") or []))
    return (object_key(goal.get("name", "")), states, contains)


def state_goal_signature(obj_name: Any, state: str) -> Tuple[str, Tuple[str, ...], Tuple[str, ...]]:
    return goal_signature(
        {
            "name": obj_name,
            "contains": [],
            "states": [str(state).upper()],
        }
    )


def record_verified_goal_state(
    obj_name: Any,
    state: str,
    object_id: Optional[str] = None,
    evaluation_context: Optional[EvaluationContext] = None,
) -> None:
    if evaluation_context is not None:
        evaluation_context.record_observation(str(obj_name), state, object_id)
        return
    signature = state_goal_signature(obj_name, state)
    if not signature[0] or not signature[1]:
        return
    with ground_truth_lock:
        verified_ground_truth_goal_signatures.add(signature)


def goal_state_verified(obj_name: Any, state: str) -> bool:
    signature = state_goal_signature(obj_name, state)
    with ground_truth_lock:
        return signature in verified_ground_truth_goal_signatures


def record_satisfied_temperature_goal_states(
    runtime_or_objects: Any,
    evaluation_context: Optional[EvaluationContext] = None,
) -> int:
    """Record satisfied HOT/COLD ground-truth states without mutating goals."""

    current_objects = getattr(runtime_or_objects, "current_objects", None)
    runtime = runtime_or_objects if callable(current_objects) else None
    objects = list(current_objects()) if runtime is not None else list(runtime_or_objects)
    goals = (
        list(evaluation_context.goals)
        if evaluation_context is not None
        else [GoalSpec.from_value(goal) for goal in get_ground_truth()]
    )

    recorded = 0
    temperature_states = {"HOT", "COLD"}
    for goal in goals:
        obj_name = goal.name
        states = [
            str(state).upper()
            for state in goal.states
            if str(state).upper() in temperature_states
        ]
        if not obj_name or not states:
            continue
        resolved_name = (
            runtime.resolve_object_alias(obj_name)
            if runtime is not None and callable(getattr(runtime, "resolve_object_alias", None))
            else obj_name
        )
        if str(resolved_name).casefold() != str(obj_name).casefold():
            matching_objects = [
                obj
                for obj in objects
                if str(obj.get("objectId") or "").casefold()
                == str(resolved_name).casefold()
            ]
        else:
            matching_objects = [obj for obj in objects if matches_object(obj_name, obj)]
        for state in states:
            if (
                evaluation_context is None
                and goal_state_verified(obj_name, state)
            ):
                continue
            for satisfying_obj in matching_objects:
                if not state_satisfied(satisfying_obj, state):
                    continue
                record_verified_goal_state(
                    obj_name,
                    state,
                    str(satisfying_obj.get("objectId") or "") or None,
                    evaluation_context,
                )
                recorded += 1
    return recorded


def object_filled_with_liquid(obj: Dict[str, Any], liquid_type: str) -> bool:
    liquid_key = object_key(liquid_type)
    direct_state_field = {
        "coffee": "isFilledWithCoffee",
        "water": "isFilledWithWater",
    }.get(liquid_key)
    if direct_state_field and bool(obj.get(direct_state_field)):
        return True

    liquid = (
        obj.get("fillLiquid")
        or obj.get("filledLiquid")
        or obj.get("liquid")
        or obj.get("liquidType")
        or obj.get("filledWith")
        or obj.get("filled_with")
    )
    return bool(obj.get("isFilledWithLiquid")) and object_key(liquid) == liquid_key


def object_filled_with_coffee(obj: Dict[str, Any]) -> bool:
    return object_filled_with_liquid(obj, "coffee")


def object_filled_with_water(obj: Dict[str, Any]) -> bool:
    return object_filled_with_liquid(obj, "water")


def state_satisfied(obj: Dict[str, Any], state: str) -> bool:
    state = str(state).upper()
    if state == "SLICED":
        return bool(obj.get("isSliced"))
    if state == "BROKEN":
        return bool(obj.get("isBroken"))
    if state == "OFF":
        return not bool(obj.get("isToggled"))
    if state == "ON":
        return bool(obj.get("isToggled"))
    if state == "HOT":
        return obj.get("temperature") == "Hot"
    if state == "COLD":
        return obj.get("temperature") == "Cold"
    if state == "COOKED":
        return bool(obj.get("isCooked"))
    if state == "OPENED":
        return bool(obj.get("isOpen"))
    if state == "CLOSED":
        return not bool(obj.get("isOpen"))
    if state == "PICKED":
        return bool(obj.get("isPickedUp"))
    if state == "CLEANED":
        return not bool(obj.get("isDirty"))
    if state == "FILLEDWITHCOFFEE":
        return object_filled_with_coffee(obj)
    if state == "FILLEDWITHWATER":
        return object_filled_with_water(obj)
    raise RuntimeError(f"Unsupported goal state: {state}")


def ground_truth_name_for_object(obj_name: Any, obj: Dict[str, Any]) -> str:
    if isinstance(obj_name, str) and obj_name:
        return obj_name
    for key in ("objectType", "name", "objectId"):
        value = obj.get(key)
        if value:
            return str(value)
    return str(obj_name)


def record_groundtruth_state(
    obj_name: Any,
    obj: Dict[str, Any],
    state: str,
    evaluation_context: Optional[EvaluationContext] = None,
) -> None:
    name = ground_truth_name_for_object(obj_name, obj)
    record_verified_goal_state(
        name,
        state,
        str(obj.get("objectId") or "") or None,
        evaluation_context,
    )


def contains_satisfied(obj: Dict[str, Any], contains: Sequence[str]) -> bool:
    receptacle_ids = obj.get("receptacleObjectIds") or []
    return all(
        any(
            str(contained).casefold() == str(receptacle_id).casefold()
            or object_key(contained) == object_key(str(receptacle_id).split("|", 1)[0])
            for receptacle_id in receptacle_ids
        )
        for contained in contains
    )
