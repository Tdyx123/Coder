import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
for path in (_SCRIPT_DIR, _REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from ai2thor_object_cache import get_ai2_thor_objects_cached
from file_processor import PDDLError
from llm_handler import LLMHandler
from resources.robots import robots
from run_config import load_run_config
from special_task_skills import SPECIAL_TASK_SKILL_SET


def _repo_root() -> Path:
    return _REPO_ROOT


AI2THOR_OBJECT_PROPERTIES_PATH = _REPO_ROOT / "data" / "all_ai2thor_objects.json"
AI2THOR_OBJECT_PROPERTY_FIELDS = (
    "breakable",
    "pickupable",
    "sliceable",
    "openable",
    "receptacle",
    "toggleable",
    "dirtyable",
    "canFillWithLiquid",
    "cookable",
)

PUT_IN_RECEPTACLES = (
    "Drawer", "Cabinet", "Fridge", "Microwave", "LaundryHamper", "Box", "Cup", "Bowl",
    "GarbageCan", "Sink", "BathtubBasin", "Pan", "Pot",
)

MUST_OPEN_TO_PLACE_OBJECTS_IN = (
    "Drawer", "Cabinet", "LaundryHamper", "Microwave", "Fridge", "Box",
)

PLACEMENT_RESTRICTIONS = {
    "AlarmClock": ("Box", "Dresser", "Desk", "SideTable", "DiningTable", "TVStand", "CoffeeTable", "CounterTop", "Shelf", "Chair", "Stool"),
    "Apple": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "GarbageCan", "Dresser"),
    "AppleSliced": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "GarbageCan", "Dresser"),
    "BaseballBat": ("Bed", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "Floor"),
    "BasketBall": ("Sofa", "ArmChair", "Dresser", "Desk", "Bed", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Stool", "Chair", "Floor"),
    "Book": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "Desk", "Bed", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Stool", "Chair", "Floor"),
    "Boots": ("Floor",),
    "Bottle": ("Fridge", "Box", "Dresser", "Desk", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "GarbageCan"),
    "Bowl": ("Microwave", "Fridge", "Dresser", "Desk", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "Box": ("Sofa", "ArmChair", "Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Ottoman", "Stool", "Chair", "Floor"),
    "Bread": ("Microwave", "Fridge", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "GarbageCan", "Plate"),
    "BreadSliced": ("Microwave", "Fridge", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "GarbageCan", "Toaster", "Plate"),
    "ButterKnife": ("Pot", "Pan", "Bowl", "Mug", "Plate", "Cup", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "Desk", "CounterTop", "Drawer"),
    "Candle": ("Box", "Dresser", "Desk", "Toilet", "Cart", "Bathtub", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Stool", "Chair"),
    "CD": ("Box", "Ottoman", "Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Safe", "Sofa", "ArmChair", "Stool", "Chair", "Footstool"),
    "CellPhone": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "Desk", "Bed", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Safe", "Stool", "Chair", "Footstool"),
    "Cloth": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "LaundryHamper", "Desk", "Toilet", "Cart", "BathtubBasin", "Bathtub", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair", "Footstool", "Floor"),
    "CreditCard": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Stool", "Chair", "Footstool"),
    "Cup": ("Microwave", "Fridge", "Dresser", "Desk", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "DishSponge": ("Pot", "Pan", "Bowl", "Plate", "Box", "Toilet", "Cart", "BathtubBasin", "Bathtub", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan"),
    "Egg": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "EggCracked": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "Fork": ("Pot", "Pan", "Bowl", "Mug", "Plate", "Cup", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer"),
    "HandTowel": ("HandTowelHolder",),
    "Kettle": ("DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Sink", "SinkBasin", "Cabinet", "StoveBurner", "Shelf"),
    "KeyChain": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Safe", "Stool", "Chair"),
    "Knife": ("Pot", "Pan", "Bowl", "Mug", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer"),
    "Ladle": ("Pot", "Pan", "Bowl", "Plate", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer"),
    "Laptop": ("Sofa", "ArmChair", "Ottoman", "Dresser", "Desk", "Bed", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Stool", "Chair", "Footstool"),
    "Lettuce": ("Pot", "Pan", "Bowl", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "LettuceSliced": ("Pot", "Pan", "Bowl", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "Mug": ("CoffeeMachine", "Microwave", "Fridge", "Plate", "Box", "Dresser", "Desk", "Cart", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "Newspaper": ("Sofa", "ArmChair", "Ottoman", "Dresser", "Desk", "Bed", "Toilet", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair", "Footstool", "Floor"),
    "Pan": ("DiningTable", "CounterTop", "TVStand", "CoffeeTable", "SideTable", "Sink", "SinkBasin", "Cabinet", "StoveBurner", "Fridge"),
    "PaperTowelRoll": ("Box", "Toilet", "Cart", "Bathtub", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "GarbageCan"),
    "Pen": ("Mug", "Box", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair", "Footstool"),
    "Pencil": ("Mug", "Box", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair", "Footstool"),
    "PepperShaker": ("DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer", "Cabinet", "Shelf"),
    "Pillow": ("Sofa", "ArmChair", "Ottoman", "Bed", "Stool", "Chair"),
    "Plate": ("Microwave", "Fridge", "Dresser", "Desk", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "Plunger": ("Cart", "Cabinet", "Floor"),
    "Pot": ("StoveBurner", "Fridge", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "Potato": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "PotatoSliced": ("Pot", "Pan", "Bowl", "Microwave", "Fridge", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "GarbageCan"),
    "RemoteControl": ("Sofa", "ArmChair", "Box", "Ottoman", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Stool", "Chair", "Footstool"),
    "SaltShaker": ("DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer", "Cabinet", "Shelf"),
    "SoapBar": ("Toilet", "Cart", "Bathtub", "BathtubBasin", "Sink", "SinkBasin", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan"),
    "SoapBottle": ("Dresser", "Desk", "Toilet", "Cart", "Bathtub", "Sink", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan"),
    "Spatula": ("Pot", "Pan", "Bowl", "Plate", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer"),
    "Spoon": ("Pot", "Pan", "Bowl", "Mug", "Plate", "Cup", "Sink", "SinkBasin", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Drawer"),
    "SprayBottle": ("Dresser", "Desk", "Toilet", "Cart", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan"),
    "Statue": ("Box", "Dresser", "Desk", "Cart", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Safe"),
    "TeddyBear": ("Bed", "Sofa", "ArmChair", "Ottoman", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Safe", "Stool", "Chair"),
    "TennisRacket": ("Dresser", "Desk", "Bed", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Stool", "Chair", "Floor"),
    "TissueBox": ("Box", "Dresser", "Desk", "Toilet", "Cart", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair", "Footstool"),
    "ToiletPaper": ("Dresser", "Desk", "Toilet", "ToiletPaperHanger", "Cart", "Bathtub", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool", "Chair"),
    "ToiletPaperRoll": ("Dresser", "Desk", "Toilet", "ToiletPaperHanger", "Cart", "Bathtub", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "GarbageCan", "Stool"),
    "Tomato": ("DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Sink", "SinkBasin", "Pot", "Bowl", "Fridge", "GarbageCan", "Plate"),
    "TomatoSliced": ("DiningTable", "CounterTop", "TVStand", "CoffeeTable", "SideTable", "Sink", "SinkBasin", "Pot", "Bowl", "Fridge", "GarbageCan", "Plate"),
    "Towel": ("TowelHolder",),
    "Vase": ("Box", "Dresser", "Desk", "Cart", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Safe"),
    "Watch": ("Box", "Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Safe", "Stool", "Chair", "Footstool"),
    "WateringCan": ("Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Drawer", "Stool", "Chair", "Floor"),
    "WineBottle": ("Fridge", "Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "GarbageCan"),
    "Dumbbell": ("Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Bed", "Chair", "ArmChair", "Sofa", "Stool", "Footstool", "Floor"),
    "AluminumFoil": ("Dresser", "Drawer", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "TableTopDecor": ("Dresser", "Desk", "Cabinet", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf"),
    "TargetCircle": ("Dresser", "Desk", "DiningTable", "TVStand", "CoffeeTable", "SideTable", "CounterTop", "Shelf", "Floor"),
}

COOKABLE_OBJECTS = (
    "Egg", "EggCracked", "Potato", "PotatoSliced",
)

WATER_FILLABLE_OBJECTS = (
    "Bottle", "Bowl", "Cup", "Kettle", "Mug", "Pot", "WateringCan", "WineBottle",
)

MUG_OBJECTS = ("Mug",)
BREAD_OBJECTS = ("Bread",)

MICROWAVE_OBJECTS = ("Microwave",)
COFFEE_MACHINE_OBJECTS = ("CoffeeMachine",)
TOASTER_OBJECTS = ("Toaster",)
STOVE_BURNER_OBJECTS = ("StoveBurner",)
SINK_OBJECTS = ("Sink",)
FRIDGE_OBJECTS = ("Fridge",)


class ObjectPropertiesError(ValueError):
    """Raised when floor-plan-specific AI2-THOR object properties are invalid."""


def _normalize_scene_name(floor_plan: Union[int, str]) -> str:
    text = str(floor_plan).strip()
    if not text:
        raise ObjectPropertiesError("floor_plan is required")

    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]

    if not text.isdigit():
        raise ObjectPropertiesError(f"Invalid floor_plan: {floor_plan}")

    scene_id = int(text)
    if scene_id < 1:
        raise ObjectPropertiesError(f"Invalid floor_plan: {floor_plan}")

    return f"FloorPlan{scene_id}"


def _load_ai2thor_object_type_properties(
    floor_plan: Union[int, str],
    path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
) -> Dict[str, Dict[str, bool]]:
    scene_name = _normalize_scene_name(floor_plan)

    if not path.exists():
        raise FileNotFoundError(f"AI2-THOR object properties file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            objects = json.load(f)
    except json.JSONDecodeError as exc:
        raise ObjectPropertiesError(f"Invalid AI2-THOR object properties JSON: {path}") from exc

    if not isinstance(objects, list):
        raise ObjectPropertiesError(f"AI2-THOR object properties must be a list: {path}")

    object_type_properties: Dict[str, Dict[str, bool]] = {}
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} must be an object")

        scene = item.get("scene")
        if not isinstance(scene, str) or not scene:
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} is missing scene")
        if scene != scene_name:
            continue

        object_type = item.get("objectType")
        if not isinstance(object_type, str) or not object_type:
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} is missing objectType")

        properties = object_type_properties.setdefault(
            object_type,
            {field: False for field in AI2THOR_OBJECT_PROPERTY_FIELDS},
        )
        for field in AI2THOR_OBJECT_PROPERTY_FIELDS:
            properties[field] = properties[field] or bool(item.get(field, False))

    if not object_type_properties:
        raise ObjectPropertiesError(
            f"No AI2-THOR object properties found for scene {scene_name} in {path}"
        )

    return object_type_properties


def _objects_with_property(
    object_type_properties: Dict[str, Dict[str, bool]],
    property_name: str,
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get(property_name, False)
    )


def _objects_with_properties(
    object_type_properties: Dict[str, Dict[str, bool]],
    property_names: Tuple[str, ...],
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if all(properties.get(property_name, False) for property_name in property_names)
    )


def _openable_receptacles(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get("openable", False) and properties.get("receptacle", False)
    )


def _non_openable_receptacles(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get("receptacle", False) and not properties.get("openable", False)
    )


def _restricted_receptacles(
    object_type_properties: Dict[str, Dict[str, bool]],
    restrictions: Tuple[str, ...],
) -> List[str]:
    return sorted(
        object_type
        for object_type in restrictions
        if object_type_properties.get(object_type, {}).get("receptacle", False)
    )


def _filtered_placement_restrictions(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> Dict[str, List[str]]:
    object_types = set(object_type_properties)
    return {
        object_type: sorted(
            target for target in targets
            if target in object_types
        )
        for object_type, targets in PLACEMENT_RESTRICTIONS.items()
        if object_type in object_types
    }


def _objects_present(
    object_type_properties: Dict[str, Dict[str, bool]],
    object_types: Tuple[str, ...],
    require_pickupable: bool = False,
) -> List[str]:
    return sorted(
        object_type
        for object_type in object_types
        if object_type in object_type_properties
        and (
            not require_pickupable
            or object_type_properties[object_type].get("pickupable", False)
        )
    )


def _objects_with_property_or_fallback(
    object_type_properties: Dict[str, Dict[str, bool]],
    property_name: str,
    fallback_object_types: Tuple[str, ...],
    require_pickupable: bool = True,
) -> List[str]:
    candidates = set(_objects_with_property(object_type_properties, property_name))
    candidates.update(
        _objects_present(
            object_type_properties,
            fallback_object_types,
            require_pickupable=require_pickupable,
        )
    )
    if require_pickupable:
        candidates = {
            object_type for object_type in candidates
            if object_type_properties.get(object_type, {}).get("pickupable", False)
        }
    return sorted(candidates)


def _pickupable_objects_allowed_at(
    object_type_properties: Dict[str, Dict[str, bool]],
    receptacle: str,
) -> List[str]:
    if receptacle not in object_type_properties:
        return []

    object_types = set(object_type_properties)
    return sorted(
        object_type
        for object_type, targets in PLACEMENT_RESTRICTIONS.items()
        if object_type in object_types
        and receptacle in targets
        and object_type_properties[object_type].get("pickupable", False)
    )


def _build_object_skill_sets(
    floor_plan: Union[int, str],
    path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
) -> Dict[str, Any]:
    object_type_properties = _load_ai2thor_object_type_properties(floor_plan, path)
    return {
        "breakable_objects": _objects_with_property(object_type_properties, "breakable"),
        "pickupable_objects": _objects_with_property(object_type_properties, "pickupable"),
        "sliceable_objects": _objects_with_property(object_type_properties, "sliceable"),
        "washable_objects": _objects_with_properties(
            object_type_properties,
            ("dirtyable", "pickupable"),
        ),
        "openable_objects": _objects_with_property(object_type_properties, "openable"),
        "openable_containers": _openable_receptacles(object_type_properties),
        "has_placing_surface_objects": _non_openable_receptacles(object_type_properties),
        "switchable_objects": _objects_with_property(object_type_properties, "toggleable"),
        "put_in_receptacles": _restricted_receptacles(
            object_type_properties,
            PUT_IN_RECEPTACLES,
        ),
        "must_open_to_place_receptacles": _restricted_receptacles(
            object_type_properties,
            MUST_OPEN_TO_PLACE_OBJECTS_IN,
        ),
        "placement_restrictions": _filtered_placement_restrictions(object_type_properties),
        "cookable_objects": _objects_with_property_or_fallback(
            object_type_properties,
            "cookable",
            COOKABLE_OBJECTS,
        ),
        "fillable_objects": _objects_with_property_or_fallback(
            object_type_properties,
            "canFillWithLiquid",
            WATER_FILLABLE_OBJECTS,
        ),
        "mug_objects": _objects_present(
            object_type_properties,
            MUG_OBJECTS,
            require_pickupable=True,
        ),
        "bread_objects": _objects_present(
            object_type_properties,
            BREAD_OBJECTS,
            require_pickupable=True,
        ),
        "microwave_objects": _objects_present(object_type_properties, MICROWAVE_OBJECTS),
        "coffee_machine_objects": _objects_present(object_type_properties, COFFEE_MACHINE_OBJECTS),
        "toaster_objects": _objects_present(object_type_properties, TOASTER_OBJECTS),
        "stove_burner_objects": _objects_present(object_type_properties, STOVE_BURNER_OBJECTS),
        "sink_objects": _objects_present(object_type_properties, SINK_OBJECTS),
        "fridge_objects": _objects_present(object_type_properties, FRIDGE_OBJECTS),
        "microwave_placeable_objects": _pickupable_objects_allowed_at(
            object_type_properties,
            "Microwave",
        ),
        "stove_burner_placeable_objects": _pickupable_objects_allowed_at(
            object_type_properties,
            "StoveBurner",
        ),
        "fridge_coldable_objects": _pickupable_objects_allowed_at(
            object_type_properties,
            "Fridge",
        ),
    }

food = ['Apple', 'Bread', 'Egg', 'Lettuce', 'Potato', 'Tomato']
food_containers = ['Pot', 'Bowl', 'Plate', 'Pan']

SKILL_TO_ROBOT_SKILLS = {
    'Open': ['GoToObject', 'OpenObject'],
    'SwitchOn': ['GoToObject', 'SwitchOn'],
    'Wash': ['GoToObject', 'PickupObject', 'CleanObject'],
    'Break': ['GoToObject', 'BreakObject'],
    'Slice': ['GoToObject', 'PickupObject', 'SliceObject'],
    'PutOn': ['GoToObject', 'PickupObject', 'PutObject'],
    'PutIn': ['GoToObject', 'OpenObject', 'CloseObject', 'PickupObject', 'PutObject'],
}

MASS_OBJECTS = ['Knife']

ACTION_SKILL_CORE_REQUIREMENTS = {
    'RunMicrowave': ['GoToObject', 'PickupObject', 'PutObject', 'OpenObject', 'CloseObject'],
    'RunCoffeeMachine': ['GoToObject', 'PickupObject', 'PutObject'],
    'RunToaster': ['GoToObject', 'PickupObject', 'SliceObject'],
    'CookByStoveBurner': ['GoToObject', 'PickupObject', 'PutObject'],
    'HeatByStoveBurner': ['GoToObject', 'PickupObject'],
    'FillWater': ['GoToObject', 'PickupObject'],
    'ColdObject': ['GoToObject', 'PickupObject', 'PutObject', 'OpenObject', 'CloseObject', 'SwitchOn'],
}

ACTION_PAIR_SKILL_SETS = {
    'RunMicrowave': ('microwave_placeable_objects', 'microwave_objects'),
    'RunCoffeeMachine': ('mug_objects', 'coffee_machine_objects'),
    'RunToaster': ('bread_objects', 'toaster_objects'),
    'CookByStoveBurner': ('cookable_objects', 'stove_burner_objects'),
    'HeatByStoveBurner': ('stove_burner_placeable_objects', 'stove_burner_objects'),
    'FillWater': ('fillable_objects', 'sink_objects'),
    'ColdObject': ('fridge_coldable_objects', 'fridge_objects'),
}

SKILLS_REQUIRING_FIRST_OBJECT_PICKUP = {
    'Wash',
    'PutOn',
    'PutIn',
    'RunMicrowave',
    'RunCoffeeMachine',
    'RunToaster',
    'CookByStoveBurner',
    'HeatByStoveBurner',
    'FillWater',
    'ColdObject',
}


def _putin_requires_open_close(receptacle: str) -> bool:
    return receptacle in MUST_OPEN_TO_PLACE_OBJECTS_IN


def _required_pickupable_objects_for_subtask(subtask: Dict[str, Any]) -> List[str]:
    skill = subtask["skill"]
    objects = subtask.get("objects", [])

    if skill == "Slice":
        return ["Knife"]

    if skill in SKILLS_REQUIRING_FIRST_OBJECT_PICKUP and objects:
        return [objects[0]]

    return []


def _subtask_uses_pickupable_objects(subtask: Dict[str, Any], skill_sets: Dict[str, Any]) -> bool:
    pickupable_objects = set(skill_sets["pickupable_objects"])
    return all(
        obj in pickupable_objects
        for obj in _required_pickupable_objects_for_subtask(subtask)
    )


def _required_robot_skills_for_subtask(subtask: Dict[str, Any]) -> List[str]:
    skill = subtask["skill"]
    if skill == "PutIn" and not _putin_requires_open_close(subtask["objects"][1]):
        return ['GoToObject', 'PickupObject', 'PutObject']
    if skill in ACTION_SKILL_CORE_REQUIREMENTS:
        required_skills = list(ACTION_SKILL_CORE_REQUIREMENTS[skill])
        if skill in SPECIAL_TASK_SKILL_SET:
            required_skills.append(skill)
        return required_skills
    return SKILL_TO_ROBOT_SKILLS.get(skill, [])


def _can_place_with_skill(
    obj: str,
    receptacle: str,
    skill: str,
    skill_sets: Dict[str, Any],
) -> bool:
    if obj == receptacle or obj not in skill_sets["pickupable_objects"]:
        return False

    if skill == "PutOn":
        return receptacle in skill_sets["placement_restrictions"].get(obj, [])

    if skill != "PutIn":
        return False

    if receptacle not in skill_sets["put_in_receptacles"]:
        return False

    return receptacle in skill_sets["placement_restrictions"].get(obj, [])


def _can_pair_with_action_skill(
    obj: str,
    appliance: str,
    skill: str,
    skill_sets: Dict[str, Any],
) -> bool:
    if obj == appliance or skill not in ACTION_PAIR_SKILL_SETS:
        return False

    obj_set_name, appliance_set_name = ACTION_PAIR_SKILL_SETS[skill]
    return (
        obj in skill_sets[obj_set_name]
        and appliance in skill_sets[appliance_set_name]
    )


def _can_generate_action_skill(
    skill: str,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    object_types = set(all_objects)

    if skill == "RunToaster":
        return "Knife" in object_types and "Knife" in skill_sets["pickupable_objects"]

    if skill == "CookByStoveBurner":
        return any(
            container in object_types
            for container in skill_sets["stove_burner_placeable_objects"]
        )

    return True


def _is_cookable_object(obj: str) -> bool:
    return obj in COOKABLE_OBJECTS


MUTUALLY_EXCLUSIVE_STATES = {
    "OPENED": "CLOSED",
    "CLOSED": "OPENED",
    "ON": "OFF",
    "OFF": "ON",
    "HOT": "COLD",
    "COLD": "HOT",
    "FILLEDWITHWATER": "FILLEDWITHCOFFEE",
    "FILLEDWITHCOFFEE": "FILLEDWITHWATER",
}


class DataEngine:

    def __init__(self, model: str = "deepseek-chat"):
        self.config = load_run_config(_repo_root(), error_cls=PDDLError)
        self.llm = LLMHandler(self.config)
        self.model = model
        seed = int(time.time())
        random.seed(seed)

    def extract_subtask_skill(self, folder: str) -> List[dict]:
        folder_path = Path(folder)
        if not folder_path.exists():
            raise FileNotFoundError(f"Folder not found: {folder}")

        results = []
        txt_files = list(folder_path.glob("*.txt"))

        for file_path in txt_files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            subtask_match = re.search(r"#Subtask\s+\d+(?:[:.]\s*)?(.+)", content)
            if not subtask_match:
                continue

            subtask_name = subtask_match.group(1).strip()
            skill_matches = re.findall(r"^([A-Za-z]+Object):", content, re.MULTILINE)
            skills = list(dict.fromkeys(skill_matches))

            results.append({"subtask": subtask_name, "skills": skills})

        return results
    
    def get_robot(self, robot_idx: int) -> List[str]:
        if 1 <= robot_idx <= len(robots):
            return robots[robot_idx - 1]
        return None

    def get_objects(self, floor_plan: int) -> List[str]:
        def convert_to_name_list(objs: List[str], obj_mass: List[float]) -> List[str]:
            return objs
        return get_ai2_thor_objects_cached(floor_plan, convert_to_name_list)

    def get_objects_with_mass(self, floor_plan: int) -> List[Dict[str, Any]]:
        def convert_to_dict(objs: List[str], obj_mass: List[float]) -> List[Dict[str, Any]]:
            return [{'name': obj, 'mass': mass} for obj, mass in zip(objs, obj_mass)]
        return get_ai2_thor_objects_cached(floor_plan, convert_to_dict)

    def _get_object_mass(self, obj_name: str, obj_mass_map: Dict[str, float]) -> float:
        return obj_mass_map.get(obj_name, 0.0)

    def subtask_to_str(self, subtask: Dict) -> str:
        skill = subtask['skill']
        objs = subtask['objects']
        obj_strs = [o.lower() for o in objs]

        if skill == 'Open':
            return f"open the {obj_strs[0]}"
        elif skill == 'SwitchOn':
            return f"switch on the {obj_strs[0]}"
        elif skill == 'Wash':
            return f"wash the {obj_strs[0]}"
        elif skill == 'Break':
            return f"break the {obj_strs[0]}"
        elif skill == 'Slice':
            return f"slice the {obj_strs[0]}"
        elif skill == 'PutOn':
            return f"put {obj_strs[0]} on {obj_strs[1]}"
        elif skill == 'PutIn':
            return f"put {obj_strs[0]} in {obj_strs[1]}"
        elif skill == 'RunMicrowave':
            return f"microwave the {obj_strs[0]} in the {obj_strs[1]}"
        elif skill == 'RunCoffeeMachine':
            return f"make coffee in the {obj_strs[0]} using the {obj_strs[1]}"
        elif skill == 'RunToaster':
            return f"toast the {obj_strs[0]} in the {obj_strs[1]}"
        elif skill == 'CookByStoveBurner':
            return f"cook the {obj_strs[0]} on the {obj_strs[1]}"
        elif skill == 'HeatByStoveBurner':
            return f"heat the {obj_strs[0]} on the {obj_strs[1]}"
        elif skill == 'FillWater':
            return f"fill the {obj_strs[0]} with water"
        elif skill == 'ColdObject':
            return f"cool the {obj_strs[0]} in the {obj_strs[1]}"
        else:
            return f"unknown skill: {skill}"

    def get_subtask_final_state(self, subtask: Dict) -> List[Dict]:
        skill = subtask['skill']
        objs = subtask['objects']
        results = []

        if skill == 'Open':
            results.append({"name": objs[0], "contains": [], "states": ["OPENED"]})
        elif skill == 'Close':
            results.append({"name": objs[0], "contains": [], "states": ["CLOSED"]})
        elif skill == 'SwitchOn':
            results.append({"name": objs[0], "contains": [], "states": ["ON"]})
        elif skill == 'SwitchOff':
            results.append({"name": objs[0], "contains": [], "states": ["OFF"]})
        elif skill == 'Wash':
            results.append({"name": objs[0], "contains": [], "states": ["CLEANED"]})
        elif skill == 'Break':
            results.append({"name": objs[0], "contains": [], "states": ["BROKEN"]})
        elif skill == 'Slice':
            results.append({"name": objs[0], "contains": [], "states": ["SLICED"]})
        elif skill == 'PutOn':
            results.append({"name": objs[1], "contains": [objs[0]], "states": []})
        elif skill == 'PutIn':
            results.append({"name": objs[1], "contains": [objs[0]], "states": []})
        elif skill == 'RunMicrowave':
            states = ["HOT"]
            if _is_cookable_object(objs[0]):
                states.append("COOKED")
            results.append({"name": objs[0], "contains": [], "states": states})
        elif skill == 'RunCoffeeMachine':
            results.append({"name": objs[0], "contains": [], "states": ["FILLEDWITHCOFFEE"]})
        elif skill == 'RunToaster':
            results.append({"name": objs[0], "contains": [], "states": ["HOT", "COOKED"]})
        elif skill == 'CookByStoveBurner':
            results.append({"name": objs[0], "contains": [], "states": ["COOKED"]})
        elif skill == 'HeatByStoveBurner':
            results.append({"name": objs[0], "contains": [], "states": ["HOT"]})
        elif skill == 'FillWater':
            results.append({"name": objs[0], "contains": [], "states": ["FILLEDWITHWATER"]})
        elif skill == 'ColdObject':
            results.append({"name": objs[0], "contains": [], "states": ["COLD"]})

        return results

    def get_task_final_state(self, subtasks: List[Dict]) -> List[Dict]:
        contains_by_name: Dict[str, List[str]] = {}
        states_by_name: Dict[str, List[str]] = {}
        names: List[str] = []

        for subtask in subtasks:
            partial_states = self.get_subtask_final_state(subtask)
            for ps in partial_states:
                name = ps["name"]
                contains_by_name.setdefault(name, [])
                states_by_name.setdefault(name, [])
                if name not in names:
                    names.append(name)

                if ps["contains"]:
                    for contained in ps["contains"]:
                        if contained not in contains_by_name[name]:
                            contains_by_name[name].append(contained)

                for state in ps["states"]:
                    opposite = MUTUALLY_EXCLUSIVE_STATES.get(state)
                    if opposite in states_by_name[name]:
                        states_by_name[name].remove(opposite)
                    if state not in states_by_name[name]:
                        states_by_name[name].append(state)

        return [
            {
                "name": name,
                "contains": contains_by_name.get(name, []),
                "states": states_by_name.get(name, []),
            }
            for name in names
        ]

    def _robot_can_complete_subtask(self, robot: Dict, subtask: Dict, obj_mass_map: Dict[str, float]) -> bool:
        required_skills = _required_robot_skills_for_subtask(subtask)
        if not all(s in robot['skills'] for s in required_skills):
            return False

        if 'PickupObject' in required_skills:
            if subtask['skill'] == 'Slice':
                obj_name = 'Knife'
            else:
                obj_name = subtask['objects'][0]
            obj_mass = self._get_object_mass(obj_name, obj_mass_map)
            if obj_mass > robot['mass_capacity']:
                return False

        return True

    def get_all_objects_from_cache(self) -> List[str]:
        cache_dir = self.config.ai2thor_objects_cache_dir
        if not cache_dir.exists():
            return []

        results = []
        for cache_file in cache_dir.glob("FloorPlan*.json"):
            with open(cache_file, "r", encoding="utf-8") as f:
                objects = json.load(f)
                if isinstance(objects, list):
                    results.extend(objects)
        
        return list(set([result["name"] for result in results]))

    def create_singe_task_back(self, floor_plan: int, robot_idxs: List[int], complexity: int = 0) -> Union[str, List[any]]:
        task_prompt = ""
        with open("resources/prompt_generate_task.txt", 'r', encoding='utf-8') as f:
            task_prompt = f.read()
        
        final_state_prompt = ""
        with open("resources/prompt_generate_final_state.txt", 'r', encoding='utf-8') as f:
            final_state_prompt = f.read()
        
        objects = self.get_objects(floor_plan)
        robots = []
        for idx, robot_idx in enumerate(robot_idxs):
            robot = self.get_robot(robot_idx)
            robot["name"] = f"robot{idx + 1}"
            robots.append(robot)
       
        task_prompt = task_prompt.replace("CurrentObjects", f"{objects}")
        task_prompt = task_prompt.replace("CurrentRobots", f"{robots}")

        _, task = self.llm.query_model(task_prompt, self.model)

        final_state_prompt = final_state_prompt.replace("CurrentObjects", f"{objects}")
        final_state_prompt = final_state_prompt.replace("CurrentTask", "task")
        _, final_state = self.llm.query_model(final_state_prompt, self.model)

        final_state_lines = final_state.strip().split('\n')
        parsed_final_state = []
        for line in final_state_lines:
            line = line.strip()
            if not line:
                continue
            parsed_final_state.append(json.loads(line))

        return task, parsed_final_state

    def create_singe_task(self, floor_plan: int, created_set: set, complexity: int = 0) -> Tuple[List[Dict], List[str]]:
        MAX_TASK_ATTEMPTS = 3
        MAX_ROBOTS_ATTEMPTS = 3
        skill_sets = _build_object_skill_sets(floor_plan)

        for _ in range(MAX_TASK_ATTEMPTS):
            objects = self.get_objects_with_mass(floor_plan)
            obj_mass_map = {o['name']: o['mass'] for o in objects}

            num = 1
            if complexity == 0:
                num = random.choices([1, 2], weights=[1, 2], k=1)[0]
            elif complexity == 1:
                num = random.choices([3, 4, 5], weights=[3, 3, 1], k=1)[0]

            subtasks = []
            while json.dumps(subtasks, sort_keys=True) in created_set:
                subtasks = self.generate_task([o['name'] for o in objects], num, skill_sets)

            if not self.check_subtasks(subtasks, skill_sets):
                continue

            for _ in range(MAX_ROBOTS_ATTEMPTS):
                num_robots = random.randint(2, 4)
                robot_indices = random.sample(range(1, len(robots) + 1), num_robots)
                selected_robots = [self.get_robot(idx) for idx in robot_indices]

                assigned_robots = []
                valid = True
                for subtask in subtasks:
                    assigned = None
                    for robot in selected_robots:
                        if self._robot_can_complete_subtask(robot, subtask, obj_mass_map):
                            assigned = robot['name']
                            break
                    if assigned is None:
                        valid = False
                        break
                    assigned_robots.append(assigned)

                if valid:
                    return subtasks, assigned_robots, selected_robots

        raise RuntimeError("Could not generate task with assignable robots")

    def check_and_tran2nl(self, subtasks: List[any]):
        prompt = """# Determine if the following subtasks are logically valid. Only output "No" if they are extremely unreasonable or contradictory.
- A single subtask is considered logically valid unless it is physically impossible or nonsensical.
- When multiple subtasks are present, they are logically valid as long as they can be executed in some sensible order. Minor inefficiencies or non-ideal sequencing do not make them invalid.

# If they are logically valid, describe them together in a complete natural language sentence. If they are not logically valid (i.e., very unreasonable), output "No".

# Example1

# Subtasks
wash the mug
put mug in cabinet

# Output
wash the mug, then put it in the cabinet.

# Example2

# Subtasks
        
put vase on countertop
break vase

# Output
No

# Example3

# Subtasks
        
break the plate
wash the fork
wash the butterknife

# Output
break the plate, then wash the fork and the butterknife.

# Output
wash the mug, then put it in the cabinet.

# Example4

# Subtasks
        
put sink on saltshaker
put ladle on sinkbasin 

# Output
put sink on saltshaker, then put ladle on sinkbasin 

# CurrentScene
# Subtasks"""

        for subtask in subtasks:
            prompt += f"\n{self.subtask_to_str(subtask)}"

        prompt+="\n# Output"

        _, txt = self.llm.query_model(prompt, self.model)

        task_nl = None
        lines = txt.splitlines()
        for i in reversed(range(len(lines))):
            line = lines[i].strip()
            if line != "":
                task_nl = line
                break

        if not task_nl or task_nl.strip().lower().startswith("no"):
            return None

        return task_nl

    def check_subtasks(self, subtasks, skill_sets: Optional[Dict[str, Any]] = None):
        # 包含重复的 subtask
        if len(subtasks) > len(set([json.dumps(subtask, sort_keys=True) for subtask in subtasks])):
            return False

        if skill_sets is not None:
            for subtask in subtasks:
                if not _subtask_uses_pickupable_objects(subtask, skill_sets):
                    return False
        
        # 将同一件东西搬来搬去
        mving_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn" or subtask["skill"] == "PutOn":
                if subtask["objects"][0] in mving_objects:
                    return False
                mving_objects.append(subtask["objects"][0])

        # 先 Break 再 Wash 同一件东西
        broken_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "Break":
                broken_objects.append(subtask["objects"][0])

            if subtask["skill"] == "Wash" and subtask["objects"][0] in broken_objects:
                return False
        
        # 将 PutIn / PutOn 再 Wash 同一件东西
        mving_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn" or subtask["skill"] == "PutOn":
                mving_objects.append(subtask["objects"][0])

            if subtask["skill"] == "Wash" and subtask["objects"][0] in mving_objects:
                return False

        # 先打开容器 再放入物品
        opened_containers = []
        for subtask in subtasks:
            if subtask["skill"] == "Open":
                opened_containers.append(subtask["objects"][0])

            if (
                subtask["skill"] == "PutIn"
                and _putin_requires_open_close(subtask["objects"][1])
                and subtask["objects"][1] in opened_containers
            ):
                return False

        # 先放入物品 再打开容器
        containers = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn" and _putin_requires_open_close(subtask["objects"][1]):
                containers.append(subtask["objects"][1])
            if subtask["skill"] == "Open" and subtask["objects"][0] in containers:
                return False

        # FillWater 与 RunCoffeeMachine 的前置条件冲突
        water_filled_objects = []
        coffee_filled_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "FillWater":
                obj = subtask["objects"][0]
                if obj in coffee_filled_objects:
                    return False
                water_filled_objects.append(obj)

            if subtask["skill"] == "RunCoffeeMachine":
                obj = subtask["objects"][0]
                if obj in water_filled_objects or obj in coffee_filled_objects:
                    return False
                coffee_filled_objects.append(obj)

        # RunToaster 需要 sliced；如果同一面包出现在后续 Slice 中，顺序无效
        toasted_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "RunToaster":
                toasted_objects.append(subtask["objects"][0])
            if subtask["skill"] == "Slice" and subtask["objects"][0] in toasted_objects:
                return False
            
        return True

    def create_tasks(self, foor_plan: int, count: int, complexity: int = 0) -> List[Tuple[List[Dict], List[str]]]:
        """
        Generate multiple unique tasks.

        Args:
            foor_plan: floor plan number
            count: number of tasks to generate
            complexity: task complexity level

        Returns:
            List of (subtasks, robot_names) tuples (may be fewer than count if duplicates exhausted)
        """

        CREATED_FILE = Path("data/final_test_new_created_subtasks.jsonl")
        CREATED_FILE.parent.mkdir(parents=True, exist_ok=True)

        created_set = set()
        created_set.add(json.dumps([], sort_keys=True))
        if CREATED_FILE.exists():
            with open(CREATED_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        created_set.add(line)

        task_folder = f"data/final_test_new_0609_{complexity}"
        task_folder_path = Path(task_folder)
        task_folder_path.mkdir(parents=True, exist_ok=True)
        TASK_FILE = task_folder_path.joinpath(f"FloorPlan{foor_plan}.jsonl")

        MAX_RETRIES = 30

        for _ in range(count):
            task_found = False

            for _ in range(MAX_RETRIES):
                try:
                    subtasks, assigned_robots, selected_robots = self.create_singe_task(foor_plan, created_set, complexity)
                    subtasks_key = json.dumps(subtasks, sort_keys=True)
                    if subtasks_key in created_set:
                        continue

                    created_set.add(subtasks_key)
                    with open(CREATED_FILE, "a", encoding="utf-8") as f:
                        f.write(subtasks_key + "\n")

                    # 包含重复的 subtask
                    if not self.check_subtasks(subtasks):
                        continue

                    task_nl = self.check_and_tran2nl(subtasks)
                    if not task_nl:
                        continue
                    
                    object_states = self.get_task_final_state(subtasks)
                    result= {
                        "task":task_nl,
                        "robot list":[int(r["name"].replace("robot", "")) for r in selected_robots],
                        "object_states":object_states,
                        "trans":0,
                        "max_trans":0,
                        "subtasks": subtasks,
                        "assigned_robots": [int(r.replace("robot", "")) for r in assigned_robots]
                        }

                    with open(TASK_FILE, "a", encoding="utf-8") as f:
                        f.write(f"{json.dumps(result)}\n")
                    task_found = True
                    break
                except (ObjectPropertiesError, FileNotFoundError):
                    raise
                except Exception:
                    continue

            if not task_found:
                break

    
    # ---------- 技能匹配逻辑 ----------
    def get_applicable_skills(
        self,
        obj: str,
        all_objects: List[str],
        skill_sets: Dict[str, Any],
    ) -> List[Dict]:
        """
        返回当前对象可以参与的所有技能描述。
        每项为字典：
        - 单对象技能: {'skill': 技能名, 'type': 'single'}
        - 双对象技能: {'skill': 技能名, 'role': 'obj1'/'obj2', 'needed_set': 集合名称}
        """
        skills = []
        openable_objects = skill_sets["openable_objects"]
        switchable_objects = skill_sets["switchable_objects"]
        washable_objects = skill_sets["washable_objects"]
        breakable_objects = skill_sets["breakable_objects"]
        sliceable_objects = skill_sets["sliceable_objects"]
        pickupable_objects = skill_sets["pickupable_objects"]
        all_object_types = set(all_objects)

        # 单对象技能
        if obj in openable_objects:
            skills.append({'skill': 'Open', 'type': 'single'})
        if obj in switchable_objects:
            skills.append({'skill': 'SwitchOn', 'type': 'single'})
        if obj in washable_objects and "Sink" in all_objects:
            skills.append({'skill': 'Wash', 'type': 'single'})
        if obj in breakable_objects:
            skills.append({'skill': 'Break', 'type': 'single'})
        if obj in sliceable_objects and "Knife" in all_objects and "Knife" in pickupable_objects:
            skills.append({'skill': 'Slice', 'type': 'single'})

        # 双对象技能 PutOn
        if obj in pickupable_objects and any(
            target in all_object_types and _can_place_with_skill(obj, target, "PutOn", skill_sets)
            for target in skill_sets["placement_restrictions"].get(obj, [])
        ):
            skills.append({
                'skill': 'PutOn',
                'type': 'double',
                'role': 'obj1'
            })
        if any(
            pickup in all_object_types and _can_place_with_skill(pickup, obj, "PutOn", skill_sets)
            for pickup in pickupable_objects
        ):
            skills.append({
                'skill': 'PutOn',
                'type': 'double',
                'role': 'obj2'
            })

        # 双对象技能 PutIn
        if obj in pickupable_objects and any(
            target in all_object_types and _can_place_with_skill(obj, target, "PutIn", skill_sets)
            for target in skill_sets["put_in_receptacles"]
        ):
            skills.append({
                'skill': 'PutIn',
                'type': 'double',
                'role': 'obj1',
                'needed_set': 'put_in_receptacles'
            })
        if obj in skill_sets["put_in_receptacles"] and any(
            pickup in all_object_types and _can_place_with_skill(pickup, obj, "PutIn", skill_sets)
            for pickup in pickupable_objects
        ):
            skills.append({
                'skill': 'PutIn',
                'type': 'double',
                'role': 'obj2',
                'needed_set': 'pickupable_objects'
            })

        for action_skill, (obj_set_name, appliance_set_name) in ACTION_PAIR_SKILL_SETS.items():
            if not _can_generate_action_skill(action_skill, all_objects, skill_sets):
                continue

            obj_set = skill_sets[obj_set_name]
            appliance_set = skill_sets[appliance_set_name]

            if obj in obj_set and any(
                appliance in all_object_types
                and _can_pair_with_action_skill(obj, appliance, action_skill, skill_sets)
                for appliance in appliance_set
            ):
                skills.append({
                    'skill': action_skill,
                    'type': 'double',
                    'role': 'obj1',
                    'needed_set': appliance_set_name
                })

            if obj in appliance_set and any(
                target in all_object_types
                and _can_pair_with_action_skill(target, obj, action_skill, skill_sets)
                for target in obj_set
            ):
                skills.append({
                    'skill': action_skill,
                    'type': 'double',
                    'role': 'obj2',
                    'needed_set': obj_set_name
                })

        return skills


    def sample_second_object(
        self,
        all_objects: List[str],
        needed_set_name: Optional[str] = None,
        skill_sets: Optional[Dict[str, Any]] = None,
        exclude: Optional[str] = None,
        skill: Optional[str] = None,
        role: Optional[str] = None,
    ) -> str:
        """从指定集合中随机抽取一个对象，可排除某个对象。"""
        if skill_sets is None:
            raise ValueError("skill_sets is required")

        if skill == "PutOn" and role and exclude is not None:
            candidates = [o for o in all_objects if o != exclude]
            if role == "obj1":
                candidates = [
                    receptacle for receptacle in candidates
                    if _can_place_with_skill(exclude, receptacle, skill, skill_sets)
                ]
            else:
                candidates = [
                    pickup for pickup in candidates
                    if _can_place_with_skill(pickup, exclude, skill, skill_sets)
                ]
        else:
            if needed_set_name is None:
                raise ValueError(f"没有足够的候选对象")

            needed_set = skill_sets[needed_set_name]
            candidates = [o for o in all_objects if o != exclude and o in needed_set]

        if skill in ACTION_PAIR_SKILL_SETS and role and exclude is not None:
            if role == "obj1":
                candidates = [
                    appliance for appliance in candidates
                    if _can_pair_with_action_skill(exclude, appliance, skill, skill_sets)
                ]
            else:
                candidates = [
                    target for target in candidates
                    if _can_pair_with_action_skill(target, exclude, skill, skill_sets)
                ]
        elif skill == "PutIn" and role and exclude is not None:
            if role == "obj1":
                candidates = [
                    receptacle for receptacle in candidates
                    if _can_place_with_skill(exclude, receptacle, skill, skill_sets)
                ]
            else:
                candidates = [
                    pickup for pickup in candidates
                    if _can_place_with_skill(pickup, exclude, skill, skill_sets)
                ]
        if not candidates:
            raise ValueError(f"没有足够的候选对象")
        return random.choice(candidates)


    def generate_task(
        self,
        all_objects: List[str],
        num_subtasks: int,
        skill_sets: Dict[str, Any],
        keep_prob: float = 0.5,
        seed: Optional[int] = None,
    ) -> List[Dict]:
        """
        生成一系列子任务。

        参数:
            num_subtasks: 需要的子任务数量
            keep_prob: 每次生成后保留当前对象的概率（0~1）

        返回:
            list[dict]: 每个子任务包含 'skill' 和 'objects' 字段
        """

        if seed is not None:
            random.seed(seed)

        subtasks = []
        current_obj = None

        while len(subtasks) < num_subtasks:
            if current_obj is None or not self.get_applicable_skills(current_obj, all_objects, skill_sets):
                while True:
                    current_obj = random.choice(all_objects)
                    if self.get_applicable_skills(current_obj, all_objects, skill_sets):
                        break

            applicable = self.get_applicable_skills(current_obj, all_objects, skill_sets)

            filtered_applicable = [
                skill for skill in applicable
                if skill['skill'] not in ('PutOn', 'PutIn') or random.random() > 0.8
            ]

            # 当前 current_obj 无法抽取出合适的 skill，重新抽 current_obj 
            if not filtered_applicable:
                current_obj = None
                continue

            retry = 0
            while retry < 3:
                choice = random.choice(filtered_applicable)

                if choice['type'] == 'single':
                    # 单对象技能
                    subtask = {
                        'skill': choice['skill'],
                        'objects': [current_obj]
                    }
                else:
                    # 双对象技能，需要抽取第二个对象
                    needed_set = choice.get('needed_set')
                    try:
                        second_obj = self.sample_second_object(
                            all_objects,
                            needed_set,
                            skill_sets,
                            exclude=current_obj,
                            skill=choice['skill'],
                            role=choice['role'],
                        )
                    except ValueError:
                        # 无可选对象，换一个技能重试（简单从 applicable 中另选）
                        retry+=1
                        continue

                    if choice['role'] == 'obj1':
                        objects = [current_obj, second_obj]
                    else:  # role == 'obj2'
                        objects = [second_obj, current_obj]

                    subtask = {
                        'skill': choice['skill'],
                        'objects': objects
                    }

                    # 超过 50 % current_obj 变成 second_obj
                    if random.random() > keep_prob:
                        current_obj = second_obj
                break

            # 当前 current_obj 无法抽取出合适的 skill，重新抽 current_obj 
            if retry == 3:
                current_obj = None
                continue

            subtasks.append(subtask)

            # 达到目标数量则终止
            if len(subtasks) == num_subtasks:
                break

            # 决定是否保留当前对象
            if random.random() > keep_prob:
                current_obj = None # 不保留 current_obj，下一轮将重新随机抽取
                

        return subtasks

if __name__ == "__main__":
    # seed = int(time.time())
    # random.seed(seed)
    # print(random.sample(range(1, 31), 5))
    # print(random.sample(range(201, 231), 5))
    # print(random.sample(range(301, 331), 5))
    # print(random.sample(range(401, 431), 5))

    # [8, 6, 14, 201, 211, 218, 306, 310, 322, 405, 412, 428, 16, 203, 212, 28, 309, 312, 404, 408, 425]
    data_engine = DataEngine()
    for base in [0, 200, 300, 400]:
        for floor_plan in range(1, 31):
            # data_engine.create_tasks(base + floor_plan, 5)
            data_engine.create_tasks(base + floor_plan, 30, 1)
