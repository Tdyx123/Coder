"""Pure utility helpers shared by the execution system."""

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .config import (
    AabbBounds,
    NAVIGATION_GRID_SIZE,
    OBJECT_FOOTPRINT_CLEARANCE,
    OPEN_OBJECT_FOOTPRINT_CLEARANCE,
)

def object_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


SLICEABLE_FOOD_KEYS = {"apple", "bread", "lettuce", "potato", "tomato"}
SLICEABLE_FOOD_DISPLAY_NAMES = {
    "apple": "Apple",
    "bread": "Bread",
    "lettuce": "Lettuce",
    "potato": "Potato",
    "tomato": "Tomato",
}

RobotRef = Union[str, Dict[str, Any]]

def robot_name(robot: RobotRef) -> str:
    if isinstance(robot, dict):
        return str(robot.get("name", ""))
    return str(robot)


def robot_agent_id(robot: RobotRef) -> int:
    name = robot_name(robot)
    match = re.search(r"(\d+)$", name)
    if not match:
        raise RuntimeError(f"Robot name does not end with an agent id: {robot}")
    return int(match.group(1)) - 1


def delimited_name_matches(pattern: Any, name: Any) -> bool:
    pattern_text = str(pattern or "")
    name_text = str(name or "")
    if not pattern_text or len(name_text) <= len(pattern_text):
        return False
    if not name_text.casefold().startswith(pattern_text.casefold()):
        return False
    return not name_text[len(pattern_text)].isalnum()


def matches_object(pattern: Any, obj: Dict[str, Any]) -> bool:
    pattern_text = str(pattern)
    pattern_key = object_key(pattern)
    object_id = str(obj.get("objectId") or "")
    object_id_base = object_id.split("|", 1)[0]
    object_type = obj.get("objectType") or object_id_base
    object_name = obj.get("name") or ""
    if object_id and object_id.casefold() == pattern_text.casefold():
        return True
    if (
        object_key(object_type) == pattern_key
        or object_key(object_id_base) == pattern_key
        or object_key(object_name) == pattern_key
        or delimited_name_matches(pattern_text, object_name)
    ):
        return True

    sliceable_key = sliceable_food_query_key(pattern)
    if sliceable_key is not None and is_sliced_food_object_for_key(sliceable_key, obj):
        return True
    return pattern_key == "egg" and is_broken_egg_object(obj)


def sliceable_food_query_key(pattern: Any) -> Optional[str]:
    pattern_key = object_key(pattern)
    if pattern_key in SLICEABLE_FOOD_KEYS:
        return pattern_key
    return None


def sliceable_food_base_key(pattern: Any) -> Optional[str]:
    pattern_text = str(pattern)
    if "|" in pattern_text:
        pattern_text = pattern_text.split("|", 1)[0]
    pattern_key = object_key(pattern_text)
    while pattern_key and pattern_key[-1].isdigit():
        pattern_key = pattern_key[:-1]
    for food_key in SLICEABLE_FOOD_KEYS:
        if pattern_key in {food_key, f"{food_key}sliced"}:
            return food_key
    return None


def sliced_food_logical_name(pattern: Any) -> Optional[str]:
    food_key = sliceable_food_base_key(pattern)
    if food_key is None:
        return None
    return f"{SLICEABLE_FOOD_DISPLAY_NAMES[food_key]}Sliced"


def stable_object_name(obj: Dict[str, Any]) -> str:
    name = obj.get("name")
    if name:
        return str(name)
    object_type = obj.get("objectType")
    if object_type:
        return str(object_type)
    object_id = str(obj.get("objectId", ""))
    if object_id:
        return object_id.split("|", 1)[0]
    return ""


def stable_object_name_key(obj: Dict[str, Any]) -> str:
    return object_key(stable_object_name(obj))


def operated_object_name(obj: Dict[str, Any]) -> str:
    name = stable_object_name(obj)
    name_key = object_key(name)
    type_keys = object_type_keys(obj)
    for food_key, display_name in SLICEABLE_FOOD_DISPLAY_NAMES.items():
        sliced_key = f"{food_key}sliced"
        is_sliced = sliced_key in type_keys or (
            bool(obj.get("isSliced")) and name_key.startswith(food_key)
        )
        if not is_sliced or name_key.startswith(sliced_key):
            continue
        match = re.match(re.escape(display_name), name, re.IGNORECASE)
        suffix = name[match.end():] if match else ""
        return f"{display_name}Sliced{suffix}"
    return name


def sliceable_object_name_alias_keys(name_key: str) -> Set[str]:
    aliases = {name_key} if name_key else set()
    for food_key in SLICEABLE_FOOD_KEYS:
        sliced_key = f"{food_key}sliced"
        if name_key.startswith(sliced_key):
            aliases.add(f"{food_key}{name_key[len(sliced_key):]}")
        elif name_key.startswith(food_key):
            aliases.add(f"{sliced_key}{name_key[len(food_key):]}")
    return aliases


def operated_object_name_candidate_keys(obj: Dict[str, Any]) -> Set[str]:
    return sliceable_object_name_alias_keys(stable_object_name_key(obj))


def object_type_keys(obj: Dict[str, Any]) -> Set[str]:
    object_id = str(obj.get("objectId", ""))
    object_type = obj.get("objectType", object_id.split("|", 1)[0])
    object_name = obj.get("name", "")
    return {
        object_key(object_type),
        object_key(object_id.split("|", 1)[0]),
        object_key(object_name),
    }


def is_egg_query(pattern: Any) -> bool:
    pattern_key = egg_query_base_key(pattern)
    return pattern_key in {"egg", "eggcracked"}


def egg_query_base_key(pattern: Any) -> str:
    pattern_text = str(pattern)
    if "|" in pattern_text:
        pattern_text = pattern_text.split("|", 1)[0]
    pattern_key = object_key(pattern_text)
    while pattern_key and pattern_key[-1].isdigit():
        pattern_key = pattern_key[:-1]
    return pattern_key


def is_prepare_egg_target(pattern: Any) -> bool:
    return egg_query_base_key(pattern) == "egg"


def require_prepare_egg_target(pattern: Any) -> None:
    if not is_prepare_egg_target(pattern):
        raise RuntimeError(f"PrepareEgg can only target Egg; got {pattern!r}.")


def is_broken_egg_object(obj: Dict[str, Any]) -> bool:
    type_keys = object_type_keys(obj)
    if any(key == "eggcracked" or key.startswith("eggcracked") for key in type_keys):
        return True
    return bool(obj.get("isBroken")) and "egg" in type_keys


def is_sliced_food_object_for_query(pattern: Any, obj: Dict[str, Any]) -> bool:
    food_key = sliceable_food_query_key(pattern)
    return is_sliced_food_object_for_key(food_key, obj)


def is_sliced_food_object_for_base(pattern: Any, obj: Dict[str, Any]) -> bool:
    food_key = sliceable_food_base_key(pattern)
    return is_sliced_food_object_for_key(food_key, obj)


def is_sliced_food_object_for_key(
    food_key: Optional[str],
    obj: Dict[str, Any],
) -> bool:
    if food_key is None:
        return False
    type_keys = object_type_keys(obj)
    sliced_key = f"{food_key}sliced"
    if sliced_key in type_keys:
        return True
    return bool(obj.get("isSliced")) and bool(type_keys & {food_key, sliced_key})


def sliced_food_query_rank(pattern: Any, obj: Dict[str, Any]) -> int:
    if is_sliced_food_object_for_query(pattern, obj):
        return 0
    return 1


def operated_sliced_food_query_rank(
    pattern: Any,
    obj: Dict[str, Any],
    operated_object_names: Set[str],
) -> int:
    is_operated_name = bool(
        operated_object_name_candidate_keys(obj) & operated_object_names
    )
    if is_sliced_food_object_for_query(pattern, obj):
        return 0 if is_operated_name else 1
    return 2 if is_operated_name else 3


def object_mass(obj: Dict[str, Any]) -> float:
    try:
        return float(obj.get("mass") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def object_center(obj: Dict[str, Any]) -> Optional[Dict[str, float]]:
    bbox = obj.get("axisAlignedBoundingBox") or {}
    center = bbox.get("center")
    if center and center != {"x": 0.0, "y": 0.0, "z": 0.0}:
        return center

    position = obj.get("position")
    if position and {"x", "y", "z"}.issubset(position):
        return position

    object_id = obj.get("objectId", "")
    if "|" in object_id:
        parts = object_id.split("|")
        if len(parts) >= 4:
            try:
                return {"x": float(parts[1]), "y": float(parts[2]), "z": float(parts[3])}
            except ValueError:
                pass
    return center


def object_aabb_bounds(
    obj: Dict[str, Any],
) -> Optional[AabbBounds]:
    bbox = obj.get("axisAlignedBoundingBox") or {}
    corner_points = bbox.get("cornerPoints") or []
    if corner_points:
        try:
            xs = [float(point["x"]) for point in corner_points]
            ys = [float(point.get("y", 0.0)) for point in corner_points]
            zs = [float(point["z"]) for point in corner_points]
        except (KeyError, TypeError, ValueError):
            return None
        return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))

    center = bbox.get("center")
    size = bbox.get("size")
    if not center or not size:
        return None
    try:
        half_x = float(size.get("x", 0.0)) / 2.0
        half_y = float(size.get("y", 0.0)) / 2.0
        half_z = float(size.get("z", 0.0)) / 2.0
        center_x = float(center["x"])
        center_y = float(center.get("y", 0.0))
        center_z = float(center["z"])
    except (KeyError, TypeError, ValueError):
        return None
    return (
        center_x - half_x,
        center_x + half_x,
        center_y - half_y,
        center_y + half_y,
        center_z - half_z,
        center_z + half_z,
    )


def object_footprint_clearance(obj: Dict[str, Any]) -> float:
    if bool(obj.get("isOpen")):
        return OPEN_OBJECT_FOOTPRINT_CLEARANCE
    return OBJECT_FOOTPRINT_CLEARANCE


def position_inside_aabb_footprint(
    position: Dict[str, float],
    bounds: AabbBounds,
    *,
    clearance: float = OBJECT_FOOTPRINT_CLEARANCE,
) -> bool:
    min_x, max_x, _min_y, _max_y, min_z, max_z = bounds
    x = float(position["x"])
    z = float(position["z"])
    return (
        min_x - clearance <= x <= max_x + clearance
        and min_z - clearance <= z <= max_z + clearance
    )


def distance_to_aabb_footprint(
    position: Dict[str, float],
    bounds: AabbBounds,
) -> float:
    min_x, max_x, _min_y, _max_y, min_z, max_z = bounds
    x = float(position["x"])
    z = float(position["z"])
    dx = max(min_x - x, 0.0, x - max_x)
    dz = max(min_z - z, 0.0, z - max_z)
    return (dx ** 2 + dz ** 2) ** 0.5


def position_to_tuple(position: Dict[str, float]) -> Tuple[float, float, float]:
    return (float(position["x"]), float(position.get("y", 0.0)), float(position["z"]))


def position_to_grid_key(position: Dict[str, float]) -> Tuple[int, int]:
    return (
        round(float(position["x"]) / NAVIGATION_GRID_SIZE),
        round(float(position["z"]) / NAVIGATION_GRID_SIZE),
    )


def distance_pts(p1: Tuple[float, float, float], p2: Tuple[float, float, float]) -> float:
    return ((p1[0] - p2[0]) ** 2 + (p1[2] - p2[2]) ** 2) ** 0.5


def object_distance(obj: Dict[str, Any]) -> float:
    try:
        return float(obj.get("distance") or 999999.0)
    except (TypeError, ValueError):
        return 999999.0


def yaw_to_face(agent_position: Dict[str, float], target: Dict[str, float]) -> Optional[float]:
    dx = float(target["x"]) - float(agent_position["x"])
    dz = float(target["z"]) - float(agent_position["z"])
    if abs(dx) < 1e-9 and abs(dz) < 1e-9:
        return None
    return (math.degrees(math.atan2(dx, dz)) + 360.0) % 360.0


def shortest_yaw_delta(target_yaw: float, current_yaw: float) -> float:
    return (target_yaw - current_yaw + 180.0) % 360.0 - 180.0


def event_cv2_frame(event) -> Optional[Any]:
    frame = getattr(event, "frame", None)
    if frame is None:
        return None
    try:
        return event.cv2img
    except (AttributeError, TypeError):
        return frame[..., ::-1]
def log(message: str) -> None:
    print(message, flush=True)


def step_event_failed(event: Any) -> bool:
    metadata = getattr(event, "metadata", {}) or {}
    error = metadata.get("errorMessage")
    return not metadata.get("lastActionSuccess", not bool(error))


def event_error_message(event: Any) -> str:
    metadata = getattr(event, "metadata", {}) or {}
    return str(metadata.get("errorMessage") or "")


def teleport_collision_object_id(event: Any) -> Optional[str]:
    error = event_error_message(event)
    match = re.search(r"Collided with:\s*(\S+)", error)
    if not match:
        return None
    return match.group(1).split("..", 1)[0].rstrip(".")


def step_failure_error(payload: Dict[str, Any], event: Any, attempts: int) -> RuntimeError:
    metadata = getattr(event, "metadata", {}) or {}
    action = payload.get("action", "<unknown>")
    agent_id = payload.get("agentId", "n/a")
    error = metadata.get("errorMessage") or "no error message returned"
    return RuntimeError(
        f"{action} failed for agent {agent_id} after {attempts} attempt(s): {error}"
    )
