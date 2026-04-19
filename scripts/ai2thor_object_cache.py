import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import ai2thor.controller


REPO_ROOT = Path(__file__).resolve().parent.parent
AI2THOR_OBJECTS_CACHE_DIR = REPO_ROOT / "data" / "ai2thor_objects_cache"

def get_ai2_thor_objects_cache_path(floor_plan: int) -> Path:
    """Return the cache path for the given floor plan."""
    return AI2THOR_OBJECTS_CACHE_DIR / f"FloorPlan{floor_plan}.json"


def fetch_ai2_thor_objects(
    floor_plan: int,
    convert_to_dict_objprop: Callable[[List[str], List[float]], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Fetch object metadata directly from AI2-THOR."""
    controller = None
    try:
        controller = ai2thor.controller.Controller(scene=f"FloorPlan{floor_plan}")
        object_types = [item["objectType"] for item in controller.last_event.metadata["objects"]]
        object_masses = [item["mass"] for item in controller.last_event.metadata["objects"]]
        return convert_to_dict_objprop(object_types, object_masses)
    finally:
        if controller is not None:
            controller.stop()


def get_ai2_thor_objects_cached(
    floor_plan: int,
    convert_to_dict_objprop: Callable[[List[str], List[float]], List[Dict[str, Any]]],
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Load objects from cache or fetch them from AI2-THOR and persist the result."""
    if not isinstance(floor_plan, int):
        raise TypeError(f"floor_plan must be int, got {type(floor_plan).__name__}")

    cache_path = get_ai2_thor_objects_cache_path(floor_plan)
    if not force_refresh and cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as cache_file:
            return json.load(cache_file)

    objects = fetch_ai2_thor_objects(floor_plan, convert_to_dict_objprop)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as cache_file:
        json.dump(objects, cache_file, ensure_ascii=False, indent=2)

    return objects
