import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

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
DEFAULT_BAD_SUBTASKS_CONFIG = _REPO_ROOT / "data" / "bad_single_subtasks.json"
DEFAULT_NO_VALID_POSITIONS_PATH = _REPO_ROOT / "data" / "no_valid_positions.json"
PRE_TASK_ROBOT_ID = "robot1"
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
    "Potato", "Bread",
)

WATER_FILLABLE_OBJECTS = (
    "Bottle", "Bowl", "Cup", "Kettle", "Mug", "Pot", "WateringCan", "WineBottle",
)

MUG_OBJECTS = ("Mug",)
BREAD_OBJECTS = ("Bread",)
EGG_OBJECTS = ("Egg",)

MICROWAVE_OBJECTS = ("Microwave",)
COFFEE_MACHINE_OBJECTS = ("CoffeeMachine",)
TOASTER_OBJECTS = ("Toaster",)
STOVE_BURNER_OBJECTS = ("StoveBurner",)
SINK_OBJECTS = ("Sink",)
FRIDGE_OBJECTS = ("Fridge",)


TextBuilder = Callable[[List[str]], str]
FinalStateBuilder = Callable[[List[str]], List[Dict[str, Any]]]
RobotSkillResolver = Callable[[Dict[str, Any]], List[str]]
GenerationGate = Callable[[List[str], Dict[str, Any]], bool]
PairValidator = Callable[[str, str, List[str], Dict[str, Any]], bool]


@dataclass(frozen=True)
class SkillConfig:
    name: str
    arity: int
    primary_set: Optional[str]
    target_set: Optional[str]
    roles: Tuple[str, ...]
    relation: str
    needed_set_by_role: Dict[str, str]
    robot_skills: Tuple[str, ...]
    required_pickup: Tuple[Union[int, str], ...]
    text_builder: TextBuilder
    final_state_builder: FinalStateBuilder
    generation_probability: float = 1.0
    robot_skills_resolver: Optional[RobotSkillResolver] = None
    generation_gate: Optional[GenerationGate] = None
    pair_validator: Optional[PairValidator] = None


@dataclass(frozen=True)
class BadSubtaskRule:
    floor_plan: Optional[int]
    subtask_key: str


def _putin_requires_open_close(receptacle: str) -> bool:
    return receptacle in MUST_OPEN_TO_PLACE_OBJECTS_IN


def _is_cookable_object(obj: str) -> bool:
    return obj in COOKABLE_OBJECTS


def _lower_objects(objs: List[str]) -> List[str]:
    return [obj.lower() for obj in objs]


def _single_state_builder(state: str) -> FinalStateBuilder:
    return lambda objs: [{"name": objs[0], "contains": [], "states": [state]}]


def _contains_state_builder(objs: List[str]) -> List[Dict[str, Any]]:
    return [{"name": objs[1], "contains": [objs[0]], "states": []}]


def _run_microwave_state_builder(objs: List[str]) -> List[Dict[str, Any]]:
    states = ["HOT"]
    if _is_cookable_object(objs[0]):
        states.append("COOKED")
    return [{"name": objs[0], "contains": [], "states": states}]


def _putin_robot_skills(subtask: Dict[str, Any]) -> List[str]:
    objects = subtask.get("objects", [])
    if len(objects) > 1 and not _putin_requires_open_close(objects[1]):
        return ["GoToObject", "PickupObject", "PutObject"]
    return ["GoToObject", "OpenObject", "CloseObject", "PickupObject", "PutObject"]


def _requires_object_in_scene(object_name: str) -> GenerationGate:
    return lambda all_objects, skill_sets: (
        object_name in set(all_objects)
        and object_name in skill_sets.get("pickupable_objects", [])
    )


def _cook_by_stove_burner_has_valid_container(
    food_obj: str,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    scene_objects = set(all_objects)
    return any(
        container in scene_objects
        and _can_place_with_skill(food_obj, container, "PutIn", skill_sets)
        for container in skill_sets.get("stove_burner_placeable_objects", [])
    )


def _cook_by_stove_burner_generation_gate(
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    scene_objects = set(all_objects)
    return any(
        food_obj in scene_objects
        and _cook_by_stove_burner_has_valid_container(food_obj, all_objects, skill_sets)
        for food_obj in skill_sets.get("cookable_objects", [])
    )


def _cook_by_stove_burner_pair_validator(
    food_obj: str,
    stove_burner: str,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    scene_objects = set(all_objects)
    return (
        stove_burner in scene_objects
        and _cook_by_stove_burner_has_valid_container(food_obj, all_objects, skill_sets)
    )


def _prepare_egg_container_objects(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> List[str]:
    egg_receptacles = set(PLACEMENT_RESTRICTIONS.get("Egg", ()))
    stove_placeable_objects = set(
        _pickupable_objects_allowed_at(object_type_properties, "StoveBurner")
    )
    return sorted(
        container
        for container in egg_receptacles.intersection(stove_placeable_objects)
        if object_type_properties.get(container, {}).get("receptacle", False)
    )


def _prepare_egg_generation_gate(
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    scene_objects = set(all_objects)
    return (
        any(egg in scene_objects for egg in skill_sets.get("egg_objects", []))
        and any(
            container in scene_objects
            for container in skill_sets.get("prepare_egg_container_objects", [])
        )
    )


def _scene_has_stove_burner(
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    scene_objects = set(all_objects)
    return any(
        stove_burner in scene_objects
        for stove_burner in skill_sets.get("stove_burner_objects", [])
    )


def _cook_egg_generation_gate(
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    return (
        _prepare_egg_generation_gate(all_objects, skill_sets)
        and _scene_has_stove_burner(all_objects, skill_sets)
    )


def _cook_egg_pair_validator(
    _egg_obj: str,
    _container: str,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    return _scene_has_stove_burner(all_objects, skill_sets)


def _special_robot_skills(skill: str, core_skills: Tuple[str, ...]) -> Tuple[str, ...]:
    if skill in SPECIAL_TASK_SKILL_SET:
        return core_skills + (skill,)
    return core_skills


SKILL_CONFIGS: Dict[str, SkillConfig] = {
    "Open": SkillConfig(
        name="Open",
        arity=1,
        primary_set="openable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "OpenObject"),
        required_pickup=(),
        text_builder=lambda objs: f"open the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("OPENED"),
    ),
    "Close": SkillConfig(
        name="Close",
        arity=1,
        primary_set="openable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "CloseObject"),
        required_pickup=(),
        text_builder=lambda objs: f"close the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("CLOSED"),
        generation_probability=0.0,
    ),
    "SwitchOn": SkillConfig(
        name="SwitchOn",
        arity=1,
        primary_set="switchable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "SwitchOn"),
        required_pickup=(),
        text_builder=lambda objs: f"switch on the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("ON"),
    ),
    "SwitchOff": SkillConfig(
        name="SwitchOff",
        arity=1,
        primary_set="switchable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "SwitchOff"),
        required_pickup=(),
        text_builder=lambda objs: f"switch off the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("OFF"),
        generation_probability=0.0,
    ),
    "Wash": SkillConfig(
        name="Wash",
        arity=1,
        primary_set="washable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "PickupObject", "CleanObject"),
        required_pickup=(0,),
        text_builder=lambda objs: f"wash the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("CLEANED"),
        generation_gate=lambda all_objects, _: "Sink" in all_objects,
    ),
    "Break": SkillConfig(
        name="Break",
        arity=1,
        primary_set="breakable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "BreakObject"),
        required_pickup=(),
        text_builder=lambda objs: f"break the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("BROKEN"),
    ),
    "Slice": SkillConfig(
        name="Slice",
        arity=1,
        primary_set="sliceable_objects",
        target_set=None,
        roles=("obj1",),
        relation="single",
        needed_set_by_role={},
        robot_skills=("GoToObject", "PickupObject", "SliceObject"),
        required_pickup=("Knife",),
        text_builder=lambda objs: f"slice the {_lower_objects(objs)[0]}",
        final_state_builder=_single_state_builder("SLICED"),
        generation_gate=_requires_object_in_scene("Knife"),
    ),
    "PutOn": SkillConfig(
        name="PutOn",
        arity=2,
        primary_set="pickupable_objects",
        target_set=None,
        roles=("obj1", "obj2"),
        relation="placement",
        needed_set_by_role={},
        robot_skills=("GoToObject", "PickupObject", "PutObject"),
        required_pickup=(0,),
        text_builder=lambda objs: f"put {_lower_objects(objs)[0]} on {_lower_objects(objs)[1]}",
        final_state_builder=_contains_state_builder,
        generation_probability=0.2,
    ),
    "PutIn": SkillConfig(
        name="PutIn",
        arity=2,
        primary_set="pickupable_objects",
        target_set="put_in_receptacles",
        roles=("obj1", "obj2"),
        relation="placement",
        needed_set_by_role={"obj1": "put_in_receptacles", "obj2": "pickupable_objects"},
        robot_skills=("GoToObject", "OpenObject", "CloseObject", "PickupObject", "PutObject"),
        required_pickup=(0,),
        text_builder=lambda objs: f"put {_lower_objects(objs)[0]} in {_lower_objects(objs)[1]}",
        final_state_builder=_contains_state_builder,
        generation_probability=0.2,
        robot_skills_resolver=_putin_robot_skills,
    ),
    "RunMicrowave": SkillConfig(
        name="RunMicrowave",
        arity=2,
        primary_set="microwave_placeable_objects",
        target_set="microwave_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "microwave_objects", "obj2": "microwave_placeable_objects"},
        robot_skills=_special_robot_skills(
            "RunMicrowave",
            ("GoToObject", "PickupObject", "PutObject", "OpenObject", "CloseObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"microwave the {_lower_objects(objs)[0]} in the {_lower_objects(objs)[1]}",
        final_state_builder=_run_microwave_state_builder,
    ),
    "RunCoffeeMachine": SkillConfig(
        name="RunCoffeeMachine",
        arity=2,
        primary_set="mug_objects",
        target_set="coffee_machine_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "coffee_machine_objects", "obj2": "mug_objects"},
        robot_skills=_special_robot_skills(
            "RunCoffeeMachine",
            ("GoToObject", "PickupObject", "PutObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"make coffee in the {_lower_objects(objs)[0]} using the {_lower_objects(objs)[1]}",
        final_state_builder=_single_state_builder("FILLEDWITHCOFFEE"),
    ),
    "RunToaster": SkillConfig(
        name="RunToaster",
        arity=2,
        primary_set="bread_objects",
        target_set="toaster_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "toaster_objects", "obj2": "bread_objects"},
        robot_skills=_special_robot_skills(
            "RunToaster",
            ("GoToObject", "PickupObject", "SliceObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"toast the {_lower_objects(objs)[0]} in the {_lower_objects(objs)[1]}",
        final_state_builder=lambda objs: [{"name": objs[0], "contains": [], "states": ["HOT", "COOKED"]}],
        generation_gate=_requires_object_in_scene("Knife"),
    ),
    "CookByStoveBurner": SkillConfig(
        name="CookByStoveBurner",
        arity=2,
        primary_set="cookable_objects",
        target_set="stove_burner_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "stove_burner_objects", "obj2": "cookable_objects"},
        robot_skills=_special_robot_skills(
            "CookByStoveBurner",
            ("GoToObject", "PickupObject", "PutObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"cook the {_lower_objects(objs)[0]} on the {_lower_objects(objs)[1]}",
        final_state_builder=_single_state_builder("COOKED"),
        generation_gate=_cook_by_stove_burner_generation_gate,
        pair_validator=_cook_by_stove_burner_pair_validator,
    ),
    "PrepareEgg": SkillConfig(
        name="PrepareEgg",
        arity=2,
        primary_set="egg_objects",
        target_set="prepare_egg_container_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "prepare_egg_container_objects", "obj2": "egg_objects"},
        robot_skills=_special_robot_skills(
            "PrepareEgg",
            ("GoToObject", "PickupObject", "PutObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"prepare the {_lower_objects(objs)[0]} in the {_lower_objects(objs)[1]}",
        final_state_builder=_single_state_builder("BROKEN"),
        generation_gate=_prepare_egg_generation_gate,
    ),
    "CookEgg": SkillConfig(
        name="CookEgg",
        arity=2,
        primary_set="egg_objects",
        target_set="prepare_egg_container_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "prepare_egg_container_objects", "obj2": "egg_objects"},
        robot_skills=("GoToObject", "PickupObject", "PutObject", "PrepareEgg"),
        required_pickup=(0,),
        text_builder=lambda objs: f"cook the {_lower_objects(objs)[0]} in the {_lower_objects(objs)[1]}",
        final_state_builder=lambda objs: [{"name": objs[0], "contains": [], "states": ["BROKEN", "COOKED"]}],
        generation_gate=_cook_egg_generation_gate,
        pair_validator=_cook_egg_pair_validator,
    ),
    "HeatByStoveBurner": SkillConfig(
        name="HeatByStoveBurner",
        arity=2,
        primary_set="stove_burner_placeable_objects",
        target_set="stove_burner_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "stove_burner_objects", "obj2": "stove_burner_placeable_objects"},
        robot_skills=_special_robot_skills(
            "HeatByStoveBurner",
            ("GoToObject", "PickupObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"heat the {_lower_objects(objs)[0]} on the {_lower_objects(objs)[1]}",
        final_state_builder=_single_state_builder("HOT"),
    ),
    "FillWater": SkillConfig(
        name="FillWater",
        arity=2,
        primary_set="fillable_objects",
        target_set="sink_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "sink_objects", "obj2": "fillable_objects"},
        robot_skills=_special_robot_skills(
            "FillWater",
            ("GoToObject", "PickupObject"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"fill the {_lower_objects(objs)[0]} with water",
        final_state_builder=_single_state_builder("FILLEDWITHWATER"),
    ),
    "ColdObject": SkillConfig(
        name="ColdObject",
        arity=2,
        primary_set="fridge_coldable_objects",
        target_set="fridge_objects",
        roles=("obj1", "obj2"),
        relation="action_pair",
        needed_set_by_role={"obj1": "fridge_objects", "obj2": "fridge_coldable_objects"},
        robot_skills=_special_robot_skills(
            "ColdObject",
            ("GoToObject", "PickupObject", "PutObject", "OpenObject", "CloseObject", "SwitchOn"),
        ),
        required_pickup=(0,),
        text_builder=lambda objs: f"cool the {_lower_objects(objs)[0]} in the {_lower_objects(objs)[1]}",
        final_state_builder=_single_state_builder("COLD"),
    ),
}


class ObjectPropertiesError(ValueError):
    """Raised when floor-plan-specific AI2-THOR object properties are invalid."""


def normalize_floor_plan(value: Any) -> int:
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"Invalid floor plan: {value!r}")
    return int(text)


def _subtask_key(subtask: Dict[str, Any]) -> str:
    return json.dumps(subtask, ensure_ascii=False, sort_keys=True)


def _bad_subtask_config_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid bad subtask config {path}: {message}")


def _normalize_bad_subtask(value: Any, path: Path, entry_index: int) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad_subtask_config_error(
            path,
            f"bad_subtasks[{entry_index}].subtask must be an object",
        )

    allowed_keys = {"skill", "objects"}
    extra_keys = sorted(set(value) - allowed_keys)
    if extra_keys:
        raise _bad_subtask_config_error(
            path,
            f"bad_subtasks[{entry_index}].subtask has unsupported keys: {extra_keys}",
        )

    skill = value.get("skill")
    if not isinstance(skill, str) or not skill:
        raise _bad_subtask_config_error(
            path,
            f"bad_subtasks[{entry_index}].subtask.skill must be a non-empty string",
        )

    objects = value.get("objects")
    if not isinstance(objects, list) or not all(isinstance(item, str) for item in objects):
        raise _bad_subtask_config_error(
            path,
            f"bad_subtasks[{entry_index}].subtask.objects must be a list of strings",
        )

    return {"skill": skill, "objects": list(objects)}


def load_bad_subtask_rules(
    path: Path = DEFAULT_BAD_SUBTASKS_CONFIG,
    *,
    missing_ok: bool = True,
) -> List[BadSubtaskRule]:
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        if missing_ok:
            return []
        raise FileNotFoundError(f"Bad subtask config not found: {config_path}")

    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _bad_subtask_config_error(config_path, str(exc)) from exc

    if not isinstance(raw_config, dict):
        raise _bad_subtask_config_error(config_path, "top-level value must be an object")

    version = raw_config.get("version", 1)
    if version != 1:
        raise _bad_subtask_config_error(config_path, "version must be 1")

    raw_rules = raw_config.get("bad_subtasks")
    if not isinstance(raw_rules, list):
        raise _bad_subtask_config_error(config_path, "bad_subtasks must be a list")

    rules: List[BadSubtaskRule] = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise _bad_subtask_config_error(
                config_path,
                f"bad_subtasks[{index}] must be an object",
            )

        raw_floor_plan = raw_rule.get("floor_plan")
        try:
            floor_plan = (
                None
                if raw_floor_plan is None
                else normalize_floor_plan(raw_floor_plan)
            )
        except ValueError as exc:
            raise _bad_subtask_config_error(
                config_path,
                f"bad_subtasks[{index}].floor_plan is invalid: {raw_floor_plan!r}",
            ) from exc
        subtask = _normalize_bad_subtask(raw_rule.get("subtask"), config_path, index)
        rules.append(BadSubtaskRule(floor_plan=floor_plan, subtask_key=_subtask_key(subtask)))

    return rules


def _bad_subtask_rule_sets(
    bad_subtask_rules: Sequence[BadSubtaskRule],
) -> Tuple[Set[str], Set[Tuple[int, str]]]:
    global_rules: Set[str] = set()
    floor_scoped: Set[Tuple[int, str]] = set()
    for rule in bad_subtask_rules:
        if rule.floor_plan is None:
            global_rules.add(rule.subtask_key)
        else:
            floor_scoped.add((rule.floor_plan, rule.subtask_key))
    return global_rules, floor_scoped


def subtask_matches_bad_subtask_rule(
    floor_plan: Optional[int],
    subtask: Dict[str, Any],
    bad_subtask_rules: Sequence[BadSubtaskRule],
) -> bool:
    subtask_key = _subtask_key(subtask)
    global_rules, floor_scoped = _bad_subtask_rule_sets(bad_subtask_rules)
    return (
        subtask_key in global_rules
        or (
            floor_plan is not None
            and (normalize_floor_plan(floor_plan), subtask_key) in floor_scoped
        )
    )


def _filter_item_floor_and_subtask(
    item: Any,
    default_floor_plan: Optional[int],
) -> Tuple[Optional[int], Dict[str, Any]]:
    if isinstance(item, dict) and "skill" in item:
        return default_floor_plan, item

    subtask = getattr(item, "subtask", None)
    if isinstance(subtask, dict):
        floor_plan = getattr(item, "floor_plan", default_floor_plan)
        return floor_plan, subtask

    if isinstance(item, dict) and isinstance(item.get("subtask"), dict):
        floor_plan = item.get("floor_plan", default_floor_plan)
        return floor_plan, item["subtask"]

    raise ValueError(f"Invalid subtask filter item: {item!r}")


def filter_bad_subtasks(
    subtasks: Sequence[Any],
    bad_subtask_rules: Sequence[BadSubtaskRule],
    floor_plan: Optional[int] = None,
) -> Tuple[List[Any], int]:
    global_rules, floor_scoped = _bad_subtask_rule_sets(bad_subtask_rules)
    filtered: List[Any] = []
    excluded_count = 0
    for item in subtasks:
        item_floor_plan, subtask = _filter_item_floor_and_subtask(item, floor_plan)
        subtask_key = _subtask_key(subtask)
        normalized_floor_plan = (
            None if item_floor_plan is None else normalize_floor_plan(item_floor_plan)
        )
        if (
            subtask_key in global_rules
            or (
                normalized_floor_plan is not None
                and (normalized_floor_plan, subtask_key) in floor_scoped
            )
        ):
            excluded_count += 1
            continue
        filtered.append(item)
    return filtered, excluded_count


def _no_valid_positions_config_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid no valid positions config {path}: {message}")


def load_no_valid_position_rules(
    path: Path = DEFAULT_NO_VALID_POSITIONS_PATH,
    *,
    missing_ok: bool = True,
) -> Set[Tuple[int, str, str]]:
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        if missing_ok:
            return set()
        raise FileNotFoundError(f"No valid positions config not found: {config_path}")

    try:
        raw_records = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _no_valid_positions_config_error(config_path, str(exc)) from exc

    if not isinstance(raw_records, list):
        raise _no_valid_positions_config_error(
            config_path,
            "top-level value must be a list",
        )

    rules: Set[Tuple[int, str, str]] = set()
    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, dict):
            raise _no_valid_positions_config_error(
                config_path,
                f"records[{index}] must be an object",
            )

        try:
            floor_plan = normalize_floor_plan(raw_record.get("floorplan"))
        except ValueError as exc:
            raise _no_valid_positions_config_error(
                config_path,
                f"records[{index}].floorplan is invalid: {raw_record.get('floorplan')!r}",
            ) from exc

        object_name = raw_record.get("object")
        if not isinstance(object_name, str) or not object_name.strip():
            raise _no_valid_positions_config_error(
                config_path,
                f"records[{index}].object must be a non-empty string",
            )

        receptacle = raw_record.get("receptacle")
        if not isinstance(receptacle, str) or not receptacle.strip():
            raise _no_valid_positions_config_error(
                config_path,
                f"records[{index}].receptacle must be a non-empty string",
            )

        rules.add((floor_plan, object_name.strip(), receptacle.strip()))

    return rules


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


def _load_ai2thor_objects_for_floor(
    floor_plan: Union[int, str],
    path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
) -> List[Dict[str, Any]]:
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

    floor_objects: List[Dict[str, Any]] = []
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} must be an object")

        scene = item.get("scene")
        if not isinstance(scene, str) or not scene:
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} is missing scene")
        if scene == scene_name:
            floor_objects.append(item)

    if not floor_objects:
        raise ObjectPropertiesError(
            f"No AI2-THOR objects found for scene {scene_name} in {path}"
        )
    return floor_objects


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
        "receptacle_objects": _objects_with_property(object_type_properties, "receptacle"),
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
        "egg_objects": _objects_present(
            object_type_properties,
            EGG_OBJECTS,
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
        "prepare_egg_container_objects": _prepare_egg_container_objects(
            object_type_properties,
        ),
        "fridge_coldable_objects": _pickupable_objects_allowed_at(
            object_type_properties,
            "Fridge",
        ),
    }

food = ['Apple', 'Bread', 'Egg', 'Lettuce', 'Potato', 'Tomato']
food_containers = ['Pot', 'Bowl', 'Plate', 'Pan']

MASS_OBJECTS = ['Knife']

SKILL_TO_ROBOT_SKILLS = {
    skill: list(config.robot_skills)
    for skill, config in SKILL_CONFIGS.items()
    if (
        skill not in SPECIAL_TASK_SKILL_SET
        and skill not in {"Close", "SwitchOff"}
        and config.generation_probability > 0
    )
}

ACTION_SKILL_CORE_REQUIREMENTS = {
    skill: [robot_skill for robot_skill in config.robot_skills if robot_skill != skill]
    for skill, config in SKILL_CONFIGS.items()
    if skill in SPECIAL_TASK_SKILL_SET
}

ACTION_PAIR_SKILL_SETS = {
    skill: (config.primary_set, config.target_set)
    for skill, config in SKILL_CONFIGS.items()
    if config.relation == "action_pair"
    and config.primary_set is not None
    and config.target_set is not None
}

SKILLS_REQUIRING_FIRST_OBJECT_PICKUP = {
    skill
    for skill, config in SKILL_CONFIGS.items()
    if 0 in config.required_pickup
}


def _skill_can_generate(
    config: SkillConfig,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    if config.generation_probability <= 0:
        return False
    if config.generation_gate is None:
        return True
    return config.generation_gate(all_objects, skill_sets)


def _can_match_skill_pair(
    obj: str,
    target: str,
    config: SkillConfig,
    skill_sets: Dict[str, Any],
    all_objects: Optional[List[str]] = None,
) -> bool:
    if obj == target:
        return False

    if config.relation == "placement":
        if obj not in skill_sets["pickupable_objects"]:
            return False
        receptacle_objects = skill_sets.get("receptacle_objects")
        if receptacle_objects is not None and target not in receptacle_objects:
            return False
        if config.name == "PutIn" and target not in skill_sets["put_in_receptacles"]:
            return False
        return target in skill_sets["placement_restrictions"].get(obj, [])

    if config.relation == "action_pair":
        if config.primary_set is None or config.target_set is None:
            return False
        if not (
            obj in skill_sets[config.primary_set]
            and target in skill_sets[config.target_set]
        ):
            return False
        if config.pair_validator is not None and all_objects is not None:
            return config.pair_validator(obj, target, all_objects, skill_sets)
        return True

    return False


def _required_pickupable_objects_for_subtask(subtask: Dict[str, Any]) -> List[str]:
    skill = subtask["skill"]
    objects = subtask.get("objects", [])
    config = SKILL_CONFIGS.get(skill)

    if config is None:
        return []

    required_objects = []
    for requirement in config.required_pickup:
        if isinstance(requirement, int):
            if requirement < len(objects):
                required_objects.append(objects[requirement])
        else:
            required_objects.append(requirement)

    return required_objects


def _subtask_uses_pickupable_objects(subtask: Dict[str, Any], skill_sets: Dict[str, Any]) -> bool:
    pickupable_objects = set(skill_sets["pickupable_objects"])
    return all(
        obj in pickupable_objects
        for obj in _required_pickupable_objects_for_subtask(subtask)
    )


def _required_robot_skills_for_subtask(subtask: Dict[str, Any]) -> List[str]:
    skill = subtask["skill"]
    config = SKILL_CONFIGS.get(skill)
    if config is None:
        return []
    if config.robot_skills_resolver is not None:
        return config.robot_skills_resolver(subtask)
    return list(config.robot_skills)


def _can_place_with_skill(
    obj: str,
    receptacle: str,
    skill: str,
    skill_sets: Dict[str, Any],
) -> bool:
    config = SKILL_CONFIGS.get(skill)
    if config is None or config.relation != "placement":
        return False
    return _can_match_skill_pair(obj, receptacle, config, skill_sets)


def _can_pair_with_action_skill(
    obj: str,
    appliance: str,
    skill: str,
    skill_sets: Dict[str, Any],
    all_objects: Optional[List[str]] = None,
) -> bool:
    config = SKILL_CONFIGS.get(skill)
    if config is None or config.relation != "action_pair":
        return False
    return _can_match_skill_pair(obj, appliance, config, skill_sets, all_objects)


def _can_generate_action_skill(
    skill: str,
    all_objects: List[str],
    skill_sets: Dict[str, Any],
) -> bool:
    config = SKILL_CONFIGS.get(skill)
    if config is None:
        return False
    return _skill_can_generate(config, all_objects, skill_sets)


def _object_type_from_reference(value: Any) -> str:
    return str(value).strip().split("|", 1)[0]


def _select_stove_container(food: str, skill_sets: Dict[str, Any]) -> Optional[str]:
    for container in sorted(skill_sets.get("stove_burner_placeable_objects", [])):
        if _can_place_with_skill(food, container, "PutIn", skill_sets):
            return container
    return None


def _put_object_pairs_for_subtask(
    subtask: Dict[str, Any],
    skill_sets: Dict[str, Any],
) -> List[Tuple[str, str]]:
    objects = list(subtask.get("objects", []))
    skill = subtask.get("skill")

    if skill in {"PutOn", "PutIn"} and len(objects) >= 2:
        return [(objects[0], objects[1])]
    if skill == "RunMicrowave" and len(objects) >= 2:
        return [(objects[0], objects[1])]
    if skill == "RunCoffeeMachine" and len(objects) >= 2:
        return [(objects[0], objects[1])]
    if skill == "CookByStoveBurner" and len(objects) >= 1:
        container = _select_stove_container(objects[0], skill_sets)
        return [] if container is None else [(objects[0], container)]
    if skill in {"PrepareEgg", "CookEgg"} and len(objects) >= 2:
        return [(objects[0], objects[1])]
    if skill == "ColdObject" and len(objects) >= 2:
        return [(objects[0], objects[1])]

    return []


def subtask_matches_no_valid_position_rule(
    floor_plan: int,
    subtask: Dict[str, Any],
    no_valid_position_rules: Set[Tuple[int, str, str]],
    skill_sets: Dict[str, Any],
) -> bool:
    if not no_valid_position_rules:
        return False

    normalized_floor_plan = normalize_floor_plan(floor_plan)
    return any(
        (
            normalized_floor_plan,
            _object_type_from_reference(obj),
            _object_type_from_reference(receptacle),
        )
        in no_valid_position_rules
        for obj, receptacle in _put_object_pairs_for_subtask(subtask, skill_sets)
    )


def filter_no_valid_position_subtasks(
    subtasks: Sequence[Any],
    no_valid_position_rules: Set[Tuple[int, str, str]],
    object_properties_path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
    *,
    floor_plan: Optional[int] = None,
    skill_sets: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Any], int]:
    if not no_valid_position_rules:
        return list(subtasks), 0

    skill_sets_by_floor: Dict[int, Dict[str, Any]] = {}
    filtered: List[Any] = []
    excluded_count = 0

    for item in subtasks:
        item_floor_plan, subtask = _filter_item_floor_and_subtask(item, floor_plan)
        if item_floor_plan is None:
            filtered.append(item)
            continue

        normalized_floor_plan = normalize_floor_plan(item_floor_plan)
        if skill_sets is not None and floor_plan is not None:
            item_skill_sets = skill_sets
        else:
            if normalized_floor_plan not in skill_sets_by_floor:
                skill_sets_by_floor[normalized_floor_plan] = _build_object_skill_sets(
                    normalized_floor_plan,
                    object_properties_path,
                )
            item_skill_sets = skill_sets_by_floor[normalized_floor_plan]

        if subtask_matches_no_valid_position_rule(
            normalized_floor_plan,
            subtask,
            no_valid_position_rules,
            item_skill_sets,
        ):
            excluded_count += 1
            continue
        filtered.append(item)

    return filtered, excluded_count


def _task_action(action_type: str, *args: Any) -> Dict[str, Any]:
    return {
        "action_type": action_type,
        "parameters": {"args": list(args)},
        "robot_id": PRE_TASK_ROBOT_ID,
    }


LIQUID_BOOL_FIELDS = (
    "isFilledWithLiquid",
    "isFilledWithWater",
    "isFilledWithCoffee",
)

LIQUID_VALUE_FIELDS = (
    "fillLiquid",
    "filledLiquid",
    "liquid",
    "liquidType",
    "filledWith",
    "filled_with",
)


def _object_has_initial_liquid(item: Dict[str, Any]) -> bool:
    if any(bool(item.get(field)) for field in LIQUID_BOOL_FIELDS):
        return True
    return any(bool(item.get(field)) for field in LIQUID_VALUE_FIELDS)


def _object_metadata_aliases(item: Dict[str, Any]) -> Set[str]:
    aliases: Set[str] = set()
    for field in ("objectType", "name", "objectId"):
        value = item.get(field)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        aliases.add(text)
        if field == "objectId":
            object_type = text.split("|", 1)[0].strip()
            if object_type:
                aliases.add(object_type)
    return aliases


def _object_metadata_matches_target(item: Dict[str, Any], target: Any) -> bool:
    target_text = str(target).strip()
    return bool(target_text) and target_text in _object_metadata_aliases(item)


def _target_has_initial_liquid(
    target: Any,
    floor_objects: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    if not floor_objects:
        return False
    return any(
        _object_metadata_matches_target(item, target) and _object_has_initial_liquid(item)
        for item in floor_objects
    )


def _build_pre_task_actions_for_subtask(
    subtask: Dict[str, Any],
    floor_objects: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    objects = list(subtask.get("objects", []))
    if not objects:
        return []

    if subtask.get("skill") == "Wash":
        return [_task_action("DirtyObject", objects[0])]

    if subtask.get("skill") == "FillWater" and _target_has_initial_liquid(
        objects[0],
        floor_objects,
    ):
        return [_task_action("EmptyLiquid", objects[0])]

    return []


def _build_pre_task_actions_for_subtasks(
    subtasks: Sequence[Dict[str, Any]],
    floor_objects: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    pre_task_actions: List[Dict[str, Any]] = []
    for subtask in subtasks:
        pre_task_actions.extend(
            _build_pre_task_actions_for_subtask(subtask, floor_objects)
        )
    return pre_task_actions


def _load_floor_objects_for_pre_task_actions(floor_plan: int) -> List[Dict[str, Any]]:
    try:
        return _load_ai2thor_objects_for_floor(floor_plan)
    except (FileNotFoundError, ObjectPropertiesError):
        return []


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
        config = SKILL_CONFIGS.get(skill)
        if config is None:
            return f"unknown skill: {skill}"
        return config.text_builder(objs)

    def get_subtask_final_state(self, subtask: Dict) -> List[Dict]:
        skill = subtask['skill']
        objs = subtask['objects']
        config = SKILL_CONFIGS.get(skill)
        if config is None:
            return []
        return config.final_state_builder(objs)

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

    def create_singe_task(
        self,
        floor_plan: int,
        created_set: set,
        complexity: int = 0,
        bad_subtask_rules: Optional[Sequence[BadSubtaskRule]] = None,
        no_valid_position_rules: Optional[Set[Tuple[int, str, str]]] = None,
    ) -> Tuple[List[Dict], List[str]]:
        MAX_TASK_ATTEMPTS = 3
        MAX_ROBOTS_ATTEMPTS = 3
        skill_sets = _build_object_skill_sets(floor_plan)
        if bad_subtask_rules is None:
            bad_subtask_rules = load_bad_subtask_rules()
        if no_valid_position_rules is None:
            no_valid_position_rules = load_no_valid_position_rules()

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

            filtered_subtasks, excluded_bad_subtasks = filter_bad_subtasks(
                subtasks,
                bad_subtask_rules,
                floor_plan,
            )
            if excluded_bad_subtasks or len(filtered_subtasks) != len(subtasks):
                continue

            filtered_subtasks, excluded_no_valid_positions = filter_no_valid_position_subtasks(
                subtasks,
                no_valid_position_rules,
                floor_plan=floor_plan,
                skill_sets=skill_sets,
            )
            if excluded_no_valid_positions or len(filtered_subtasks) != len(subtasks):
                continue

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
            if subtask["skill"] in {"Break", "PrepareEgg", "CookEgg"}:
                obj = subtask["objects"][0]
                if obj in broken_objects:
                    return False
                broken_objects.append(obj)

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

        task_folder = f"data/final_test_new_0623_{complexity}"
        task_folder_path = Path(task_folder)
        task_folder_path.mkdir(parents=True, exist_ok=True)
        TASK_FILE = task_folder_path.joinpath(f"FloorPlan{foor_plan}.jsonl")

        MAX_RETRIES = 30
        bad_subtask_rules = load_bad_subtask_rules()
        no_valid_position_rules = load_no_valid_position_rules()
        floor_objects = _load_floor_objects_for_pre_task_actions(foor_plan)

        for _ in range(count):
            task_found = False

            for _ in range(MAX_RETRIES):
                try:
                    subtasks, assigned_robots, selected_robots = self.create_singe_task(
                        foor_plan,
                        created_set,
                        complexity,
                        bad_subtask_rules,
                        no_valid_position_rules,
                    )
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
                    pre_task_actions = _build_pre_task_actions_for_subtasks(
                        subtasks,
                        floor_objects,
                    )
                    result= {
                        "task":task_nl,
                        "robot list":[int(r["name"].replace("robot", "")) for r in selected_robots],
                        "object_states":object_states,
                        "pre_task_actions": pre_task_actions,
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
        all_object_types = set(all_objects)

        for config in SKILL_CONFIGS.values():
            if not _skill_can_generate(config, all_objects, skill_sets):
                continue

            if config.arity == 1:
                if config.primary_set is None or obj in skill_sets[config.primary_set]:
                    skills.append({'skill': config.name, 'type': 'single'})
                continue

            if config.arity != 2:
                continue

            for role in config.roles:
                if role == "obj1":
                    if config.primary_set is not None and obj not in skill_sets[config.primary_set]:
                        continue
                    can_use_role = any(
                        target in all_object_types
                        and _can_match_skill_pair(obj, target, config, skill_sets, all_objects)
                        for target in all_objects
                    )
                elif role == "obj2":
                    if config.target_set is not None and obj not in skill_sets[config.target_set]:
                        continue
                    can_use_role = any(
                        pickup in all_object_types
                        and _can_match_skill_pair(pickup, obj, config, skill_sets, all_objects)
                        for pickup in all_objects
                    )
                else:
                    continue

                if not can_use_role:
                    continue

                skill_entry = {
                    'skill': config.name,
                    'type': 'double',
                    'role': role,
                }
                needed_set = config.needed_set_by_role.get(role)
                if needed_set is not None:
                    skill_entry['needed_set'] = needed_set
                skills.append(skill_entry)

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

        config = SKILL_CONFIGS.get(skill) if skill is not None else None
        if config is not None and role and exclude is not None:
            candidates = [o for o in all_objects if o != exclude]
            if needed_set_name is not None:
                needed_set = skill_sets[needed_set_name]
                candidates = [o for o in candidates if o in needed_set]
            elif role == "obj1" and config.target_set is not None:
                candidates = [o for o in candidates if o in skill_sets[config.target_set]]
            elif role == "obj2" and config.primary_set is not None:
                candidates = [o for o in candidates if o in skill_sets[config.primary_set]]

            if role == "obj1":
                candidates = [
                    target for target in candidates
                    if _can_match_skill_pair(exclude, target, config, skill_sets, all_objects)
                ]
            else:
                candidates = [
                    pickup for pickup in candidates
                    if _can_match_skill_pair(pickup, exclude, config, skill_sets, all_objects)
                ]
        elif needed_set_name is not None:
            needed_set = skill_sets[needed_set_name]
            candidates = [o for o in all_objects if o != exclude and o in needed_set]
        else:
            raise ValueError(f"没有足够的候选对象")

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

            filtered_applicable = []
            for skill in applicable:
                config = SKILL_CONFIGS.get(skill['skill'])
                generation_probability = 1.0
                if config is not None:
                    generation_probability = config.generation_probability
                if random.random() <= generation_probability:
                    filtered_applicable.append(skill)

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
    for base in [0]:   # [0, 200, 300, 400]
        for floor_plan in range(1, 31):
            # data_engine.create_tasks(base + floor_plan, 5)
            data_engine.create_tasks(base + floor_plan, 60, 1)
