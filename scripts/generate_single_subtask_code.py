#!/usr/bin/env python3
"""Generate executable scripts for every single feasible data_engine subtask."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
for path in (SCRIPT_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

import data_engine


DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "single_subtask_code"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "ai2thor_objects_cache"
DEFAULT_OBJECT_PROPERTIES_PATH = REPO_ROOT / "data" / "all_ai2thor_objects.json"
DEFAULT_BAD_SUBTASKS_CONFIG = REPO_ROOT / "data" / "bad_single_subtasks.json"
DEFAULT_NO_VALID_POSITIONS_PATH = REPO_ROOT / "data" / "no_valid_positions.json"
ROBOT_ID = "robot1"
BREAK_OBJECT_BLACKLIST: Set[str] = {"CoffeeMachine"}

ALL_GENERATED_EXECUTOR_SKILLS = [
    "GoToObject",
    "PickupObject",
    "PutObject",
    "OpenObject",
    "CloseObject",
    "SwitchOn",
    "SwitchOff",
    "BreakObject",
    "SliceObject",
    "CleanObject",
    "DirtyObject",
    "PrepareEgg",
    "RunMicrowave",
    "RunCoffeeMachine",
    "RunToaster",
    "CookByStoveBurner",
    "HeatByStoveBurner",
    "FireByStoveBurner",
    "FillWater",
    "ColdObject",
    "ThrowObject",
]

PARENT_CONTAINER_ACCESS_ACTIONS = {
    "PickupObject",
    "BreakObject",
    "SliceObject",
}


@dataclass(frozen=True)
class EnumeratedSubtask:
    floor_plan: int
    subtask: Dict[str, Any]


@dataclass(frozen=True)
class BadSubtaskRule:
    floor_plan: Optional[int]
    subtask_key: str


@dataclass(frozen=True)
class GeneratedSubtask:
    floor_plan: int
    floor_index: int
    global_index: int
    subtask: Dict[str, Any]
    task_text: str
    object_states: List[Dict[str, Any]]
    actions: List[Dict[str, Any]]
    pre_task_actions: List[Dict[str, Any]]


def normalize_floor_plan(value: Any) -> int:
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"Invalid floor plan: {value!r}")
    return int(text)


def discover_floor_plans(cache_dir: Path = DEFAULT_CACHE_DIR) -> List[int]:
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"AI2-THOR object cache directory not found: {cache_dir}")

    floor_plans = []
    for path in cache_dir.glob("FloorPlan*.json"):
        match = re.fullmatch(r"FloorPlan(\d+)\.json", path.name)
        if match:
            floor_plans.append(int(match.group(1)))
    return sorted(set(floor_plans))


def load_floor_object_names(floor_plan: int, cache_dir: Path = DEFAULT_CACHE_DIR) -> List[str]:
    path = cache_dir / f"FloorPlan{floor_plan}.json"
    if not path.is_file():
        raise FileNotFoundError(f"AI2-THOR object cache file not found: {path}")

    raw_objects = json.loads(path.read_text(encoding="utf-8"))
    names = []
    for item in raw_objects:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return sorted(set(names))


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


def filter_bad_subtasks(
    enumerated: Sequence[EnumeratedSubtask],
    bad_subtask_rules: Sequence[BadSubtaskRule],
) -> Tuple[List[EnumeratedSubtask], int]:
    floor_scoped: Set[Tuple[int, str]] = set()
    global_rules: Set[str] = set()
    for rule in bad_subtask_rules:
        if rule.floor_plan is None:
            global_rules.add(rule.subtask_key)
        else:
            floor_scoped.add((rule.floor_plan, rule.subtask_key))

    filtered: List[EnumeratedSubtask] = []
    excluded_count = 0
    for item in enumerated:
        subtask_key = _subtask_key(item.subtask)
        if subtask_key in global_rules or (item.floor_plan, subtask_key) in floor_scoped:
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


def _object_type_from_action_arg(value: Any) -> str:
    return str(value).strip().split("|", 1)[0]


def _is_break_object_blacklisted(value: Any) -> bool:
    return _object_type_from_action_arg(value) in BREAK_OBJECT_BLACKLIST


def _put_object_no_valid_position_key(
    floor_plan: int,
    action_item: Dict[str, Any],
) -> Optional[Tuple[int, str, str]]:
    if action_item.get("action_type") != "PutObject":
        return None

    args = action_item.get("parameters", {}).get("args", [])
    if len(args) < 2:
        return None

    return (
        floor_plan,
        _object_type_from_action_arg(args[0]),
        _object_type_from_action_arg(args[1]),
    )


def filter_no_valid_position_subtasks(
    enumerated: Sequence[EnumeratedSubtask],
    no_valid_position_rules: Set[Tuple[int, str, str]],
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> Tuple[List[EnumeratedSubtask], int]:
    if not no_valid_position_rules:
        return list(enumerated), 0

    skill_sets_by_floor: Dict[int, Dict[str, Any]] = {}
    filtered: List[EnumeratedSubtask] = []
    excluded_count = 0

    for item in enumerated:
        if item.floor_plan not in skill_sets_by_floor:
            skill_sets_by_floor[item.floor_plan] = data_engine._build_object_skill_sets(
                item.floor_plan,
                object_properties_path,
            )

        actions = build_actions_for_subtask(
            item.subtask,
            skill_sets_by_floor[item.floor_plan],
        )
        if any(
            _put_object_no_valid_position_key(item.floor_plan, action_item)
            in no_valid_position_rules
            for action_item in actions
        ):
            excluded_count += 1
            continue

        filtered.append(item)

    return filtered, excluded_count


def enumerate_single_subtasks_for_floor(
    floor_plan: int,
    object_names: Sequence[str],
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> List[Dict[str, Any]]:
    """Enumerate object-feasible single subtasks without robot feasibility checks."""

    engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
    skill_sets = data_engine._build_object_skill_sets(floor_plan, object_properties_path)
    all_objects = sorted(set(str(name) for name in object_names))
    subtasks_by_key: Dict[str, Dict[str, Any]] = {}

    for obj in all_objects:
        for skill_entry in engine.get_applicable_skills(obj, all_objects, skill_sets):
            skill_name = skill_entry["skill"]
            if skill_entry["type"] == "single":
                if skill_name == "Break" and _is_break_object_blacklisted(obj):
                    continue
                subtask = {"skill": skill_name, "objects": [obj]}
                if engine.check_subtasks([subtask], skill_sets):
                    subtasks_by_key[_subtask_key(subtask)] = subtask
                continue

            if skill_entry.get("role") != "obj1":
                continue

            config = data_engine.SKILL_CONFIGS[skill_name]
            for target in all_objects:
                if not data_engine._can_match_skill_pair(
                    obj,
                    target,
                    config,
                    skill_sets,
                    all_objects,
                ):
                    continue
                subtask = {"skill": skill_name, "objects": [obj, target]}
                if engine.check_subtasks([subtask], skill_sets):
                    subtasks_by_key[_subtask_key(subtask)] = subtask

    return sorted(
        subtasks_by_key.values(),
        key=lambda item: (item["skill"], tuple(item.get("objects", []))),
    )


def enumerate_single_subtasks(
    floor_plans: Sequence[int],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> List[EnumeratedSubtask]:
    results: List[EnumeratedSubtask] = []
    for floor_plan in floor_plans:
        object_names = load_floor_object_names(floor_plan, cache_dir)
        for subtask in enumerate_single_subtasks_for_floor(
            floor_plan,
            object_names,
            object_properties_path,
        ):
            results.append(EnumeratedSubtask(floor_plan=floor_plan, subtask=subtask))
    return results


def action(action_type: str, *args: Any) -> Dict[str, Any]:
    return {
        "action_type": action_type,
        "parameters": {"args": list(args)},
        "robot_id": ROBOT_ID,
    }


def load_floor_object_metadata(
    floor_plan: int,
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> List[Dict[str, Any]]:
    scene_name = data_engine._normalize_scene_name(floor_plan)
    path = Path(object_properties_path)
    if not path.is_file():
        raise FileNotFoundError(f"AI2-THOR object properties file not found: {path}")

    try:
        raw_objects = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise data_engine.ObjectPropertiesError(
            f"Invalid AI2-THOR object properties JSON: {path}"
        ) from exc

    if not isinstance(raw_objects, list):
        raise data_engine.ObjectPropertiesError(
            f"AI2-THOR object properties must be a list: {path}"
        )

    floor_objects: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_objects):
        if not isinstance(item, dict):
            raise data_engine.ObjectPropertiesError(
                f"AI2-THOR object entry #{index} must be an object"
            )
        scene = item.get("scene")
        if not isinstance(scene, str) or not scene:
            raise data_engine.ObjectPropertiesError(
                f"AI2-THOR object entry #{index} is missing scene"
            )
        if scene == scene_name:
            floor_objects.append(item)

    if not floor_objects:
        raise data_engine.ObjectPropertiesError(
            f"No AI2-THOR objects found for scene {scene_name} in {path}"
        )
    return floor_objects


def build_openable_parent_container_map(
    floor_objects: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    openable_container_ids = {
        str(item["objectId"])
        for item in floor_objects
        if item.get("objectId")
        and bool(item.get("openable", False))
        and bool(item.get("receptacle", False))
    }
    candidates: Dict[str, List[str]] = {}

    for item in floor_objects:
        object_type = item.get("objectType")
        if not isinstance(object_type, str) or not object_type:
            continue

        parents = item.get("parentReceptacles") or []
        if isinstance(parents, str):
            parents = [parents]
        for parent in parents:
            parent_id = str(parent)
            if parent_id in openable_container_ids:
                candidates.setdefault(object_type, []).append(parent_id)

    return {
        object_type: sorted(set(parent_ids))[0]
        for object_type, parent_ids in sorted(candidates.items())
    }


def _parent_container_for_action_arg(
    target: Any,
    open_parent_by_object: Dict[str, str],
) -> Optional[str]:
    target_text = str(target)
    return open_parent_by_object.get(target_text) or open_parent_by_object.get(
        target_text.split("|", 1)[0]
    )


def _action_args(item: Dict[str, Any]) -> List[Any]:
    args = item.get("parameters", {}).get("args", [])
    return args if isinstance(args, list) else []


def _last_goto_action_index(actions: Sequence[Dict[str, Any]], target: Any) -> Optional[int]:
    for index in range(len(actions) - 1, -1, -1):
        item = actions[index]
        if item.get("action_type") != "GoToObject":
            continue
        if _action_args(item)[:1] == [target]:
            return index
    return None


def _container_aliases(container: Any) -> Set[str]:
    container_text = str(container)
    # Parent metadata uses objectId, while generated templates may use objectType.
    return {container_text, container_text.split("|", 1)[0]}


def _container_is_open(container: Any, opened_containers: Set[str]) -> bool:
    return bool(_container_aliases(container) & opened_containers)


def _mark_container_open(container: Any, opened_containers: Set[str]) -> None:
    opened_containers.update(_container_aliases(container))


def _mark_container_closed(container: Any, opened_containers: Set[str]) -> None:
    opened_containers.difference_update(_container_aliases(container))


def _parent_access_actions(parent: str, opened_containers: Set[str]) -> List[Dict[str, Any]]:
    actions = [action("GoToObject", parent)]
    if not _container_is_open(parent, opened_containers):
        actions.append(action("OpenObject", parent))
        _mark_container_open(parent, opened_containers)
    return actions


def _insert_parent_open_actions(
    actions: List[Dict[str, Any]],
    open_parent_by_object: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    if not open_parent_by_object:
        return actions

    updated: List[Dict[str, Any]] = []
    opened_containers: Set[str] = set()
    for item in actions:
        action_type = item.get("action_type")
        args = _action_args(item)

        if action_type == "OpenObject" and args:
            target = args[0]
            if _container_is_open(target, opened_containers):
                continue
            _mark_container_open(target, opened_containers)
            updated.append(item)
            continue

        if action_type == "CloseObject" and args:
            _mark_container_closed(args[0], opened_containers)
            updated.append(item)
            continue

        if action_type in PARENT_CONTAINER_ACCESS_ACTIONS:
            if args:
                target = args[0]
                parent = _parent_container_for_action_arg(target, open_parent_by_object)
                if parent:
                    access_actions = _parent_access_actions(parent, opened_containers)
                    goto_index = _last_goto_action_index(updated, target)
                    if goto_index is None:
                        updated.extend(access_actions)
                    else:
                        updated[goto_index:goto_index] = access_actions
        updated.append(item)

    return updated


def _put_actions(obj: str, receptacle: str) -> List[Dict[str, Any]]:
    actions = [
        action("GoToObject", obj),
        action("PickupObject", obj),
        action("GoToObject", receptacle),
    ]
    if data_engine._putin_requires_open_close(receptacle):
        actions.append(action("OpenObject", receptacle))
        actions.append(action("PutObject", obj, receptacle))
        actions.append(action("CloseObject", receptacle))
    else:
        actions.append(action("PutObject", obj, receptacle))
    return actions


def select_stove_container(food: str, skill_sets: Dict[str, Any]) -> str:
    for container in sorted(skill_sets.get("stove_burner_placeable_objects", [])):
        if data_engine._can_place_with_skill(food, container, "PutIn", skill_sets):
            return container
    raise ValueError(f"No valid stove container found for {food!r}")


def construct_subtask_name(subtask: Dict[str, Any]) -> str:
    skill = subtask["skill"]
    config = data_engine.SKILL_CONFIGS.get(skill)
    if config is None:
        raise ValueError(f"Unsupported subtask skill: {skill}")
    return config.text_builder(list(subtask.get("objects", [])))


def build_actions_for_subtask(
    subtask: Dict[str, Any],
    skill_sets: Dict[str, Any],
    open_parent_by_object: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    skill = subtask["skill"]
    objects = list(subtask.get("objects", []))

    def finish(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return _insert_parent_open_actions(actions, open_parent_by_object)

    if skill == "Open":
        obj = objects[0]
        return finish([action("GoToObject", obj), action("OpenObject", obj)])
    if skill == "SwitchOn":
        obj = objects[0]
        return finish([action("GoToObject", obj), action("SwitchOn", obj)])
    if skill == "Break":
        obj = objects[0]
        if _is_break_object_blacklisted(obj):
            object_type = _object_type_from_action_arg(obj)
            raise ValueError(f"BreakObject is blacklisted for object type: {object_type}")
        return finish([action("GoToObject", obj), action("BreakObject", obj)])
    if skill == "Wash":
        obj = objects[0]
        return finish([
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", "Sink"),
            action("CleanObject", obj),
        ])
    if skill == "Slice":
        obj = objects[0]
        return finish([
            action("GoToObject", "Knife"),
            action("PickupObject", "Knife"),
            action("GoToObject", obj),
            action("SliceObject", obj),
        ])
    if skill == "PutOn":
        return finish(_put_actions(objects[0], objects[1]))
    if skill == "PutIn":
        return finish(_put_actions(objects[0], objects[1]))
    if skill == "RunMicrowave":
        obj, microwave = objects
        return finish([
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", microwave),
            action("OpenObject", microwave),
            action("PutObject", obj, microwave),
            action("CloseObject", microwave),
            action("RunMicrowave", microwave, obj),
        ])
    if skill == "RunCoffeeMachine":
        mug, coffee_machine = objects
        return finish([
            action("GoToObject", mug),
            action("PickupObject", mug),
            action("GoToObject", coffee_machine),
            action("PutObject", mug, coffee_machine),
            action("RunCoffeeMachine", coffee_machine, mug),
        ])
    if skill == "RunToaster":
        bread, toaster = objects
        return finish([
            action("GoToObject", "Knife"),
            action("PickupObject", "Knife"),
            action("GoToObject", bread),
            action("SliceObject", bread),
            action("PickupObject", bread),
            action("GoToObject", toaster),
            action("RunToaster", toaster, bread),
        ])
    if skill == "CookByStoveBurner":
        food, stove_burner = objects
        container = select_stove_container(food, skill_sets)
        return finish([
            action("GoToObject", food),
            action("PickupObject", food),
            action("GoToObject", container),
            action("PutObject", food, container),
            action("PickupObject", container),
            action("GoToObject", stove_burner),
            action("CookByStoveBurner", stove_burner, container, food),
        ])
    if skill == "PrepareEgg":
        egg, container = objects
        return finish([
            action("GoToObject", egg),
            action("PickupObject", egg),
            action("GoToObject", container),
            action("PutObject", egg, container),
            action("PrepareEgg", egg),
        ])
    if skill == "CookEgg":
        egg, container = objects
        stove_burner = "StoveBurner"
        return finish([
            action("GoToObject", egg),
            action("PickupObject", egg),
            action("GoToObject", container),
            action("PutObject", egg, container),
            action("PrepareEgg", egg),
            action("PickupObject", container),
            action("GoToObject", stove_burner),
            action("CookByStoveBurner", stove_burner, container, egg),
        ])
    if skill == "HeatByStoveBurner":
        obj, stove_burner = objects
        return finish([
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", stove_burner),
            action("HeatByStoveBurner", stove_burner, obj),
        ])
    if skill == "FillWater":
        obj, sink = objects
        return finish([
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", sink),
            action("FillWater", sink, obj),
        ])
    if skill == "ColdObject":
        obj, fridge = objects
        return finish([
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", fridge),
            action("OpenObject", fridge),
            action("PutObject", obj, fridge),
            action("CloseObject", fridge),
            action("ColdObject", fridge, obj),
        ])

    raise ValueError(f"Unsupported subtask skill: {skill}")


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
    if not target_text:
        return False
    return target_text in _object_metadata_aliases(item)


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


def build_pre_task_actions_for_subtask(
    subtask: Dict[str, Any],
    floor_objects: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    objects = list(subtask.get("objects", []))
    if not objects:
        return []

    if subtask.get("skill") == "Wash":
        return [action("DirtyObject", objects[0])]

    if subtask.get("skill") == "FillWater" and _target_has_initial_liquid(
        objects[0],
        floor_objects,
    ):
        return [action("EmptyLiquid", objects[0])]

    return []


def build_bundle_data(
    *,
    task_id: str,
    task_text: str,
    actions: List[Dict[str, Any]],
    pre_task_actions: Optional[List[Dict[str, Any]]] = None,
    plan_file: Optional[Path] = None,
) -> Dict[str, Any]:
    task_plan: Dict[str, Any] = {
        "task_id": task_id,
        "stages": [
            {
                "stage_id": "Phase 1",
                "robot_action_queues": {
                    ROBOT_ID: actions,
                },
            }
        ],
    }
    if pre_task_actions:
        task_plan["pre_task_action_queues"] = {
            ROBOT_ID: pre_task_actions,
        }

    return {
        "task": task_text,
        "task_plan": task_plan,
        "no_trans": len(actions),
        "phases": [[{"subtask_id": 1, "robot_number": 1}]],
        "plan_files": {"1": str(plan_file) if plan_file is not None else ""},
        "object_mappings": {},
        "object_mapping_warnings": [],
    }


def render_plan_text(actions: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for item in actions:
        args = item.get("parameters", {}).get("args", [])
        arg_text = " ".join(str(arg) for arg in args)
        line = f"({item['action_type']} {ROBOT_ID}"
        if arg_text:
            line += f" {arg_text}"
        line += ")"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _python_literal(data: Any) -> str:
    return pformat(data, width=100, sort_dicts=False)


def render_executable(
    task_file: Optional[Path],
    task_index: int,
    bundle_data: Dict[str, Any],
    *,
    task_record: Optional[Dict[str, Any]] = None,
    floor_plan: Optional[int] = None,
) -> str:
    bundle_literal = _python_literal(bundle_data)
    task_file_literal = _python_literal(str(task_file) if task_file is not None else None)
    task_record_literal = _python_literal(task_record)
    floor_plan_literal = _python_literal(str(floor_plan) if floor_plan is not None else None)
    code_repo_root = str(REPO_ROOT)
    forced_robots_literal = _python_literal(
        [
            {
                "name": ROBOT_ID,
                "skills": ALL_GENERATED_EXECUTOR_SKILLS,
                "mass_capacity": 100,
            }
        ]
    )

    return f'''#!/usr/bin/env python3
"""Run a generated single-subtask executor_system plan with forced robot1."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPO_ROOT = Path({code_repo_root!r})
_SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (_SCRIPT_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

if "--runner-mode" in sys.argv[1:]:
    os.environ["renderImage"] = "0"

from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system.action_plan import TaskPlan
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.parallel_runner import run_action_plan_tolerant, write_result_json
from executor_system.runtime import ThorRuntime
from executor_system.task_plan import run_action_plan


BUNDLE_DATA = {bundle_literal}
FORCED_ROBOTS = {forced_robots_literal}

TASK_FILE = {task_file_literal}
TASK_INDEX = {task_index!r}
EMBEDDED_TASK_RECORD = {task_record_literal}
EMBEDDED_FLOOR_PLAN = {floor_plan_literal}
DEFAULT_RUNNER_TIMEOUT_SECONDS = 100.0


runtime = None
robots: List[Dict[str, Any]] = []
floor_no = ""
ground_truth: List[Dict[str, Any]] = []


def load_task_record(task_file: Optional[str], task_index: int) -> Dict[str, Any]:
    if EMBEDDED_TASK_RECORD is not None:
        return copy.deepcopy(EMBEDDED_TASK_RECORD)
    if not task_file:
        raise RuntimeError("TASK_FILE is not set and no embedded task record is available.")
    path = Path(task_file).expanduser()
    if not path.is_file():
        raise RuntimeError(f"TASK_FILE not found: {{path}}")
    if task_index < 0:
        raise RuntimeError("TASK_INDEX must be 0-based and non-negative.")

    with path.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise RuntimeError(f"TASK_FILE line {{task_index}} is empty: {{path}}")
            return json.loads(line)

    raise RuntimeError(f"TASK_INDEX {{task_index}} is out of range for {{path}}")


def floor_plan_from_task_file(task_file: str) -> str:
    match = re.search(r"FloorPlan(\\d+)\\.jsonl$", str(task_file))
    if not match:
        raise RuntimeError(f"Cannot infer floor plan from TASK_FILE: {{task_file}}")
    return match.group(1)


def resolve_floor_plan(task_file: Optional[str]) -> str:
    if EMBEDDED_FLOOR_PLAN is not None:
        return str(EMBEDDED_FLOOR_PLAN)
    if not task_file:
        raise RuntimeError("TASK_FILE is not set and no embedded floor plan is available.")
    return floor_plan_from_task_file(task_file)


def build_forced_robot_team() -> List[Dict[str, Any]]:
    return copy.deepcopy(FORCED_ROBOTS)


def transition_metric(no_trans: int, no_trans_gt: int, max_trans: int) -> float:
    max_trans_value = max_trans + 1
    no_trans_gt_value = no_trans_gt + 1
    if max_trans_value == no_trans_gt_value and no_trans_gt_value == no_trans:
        return 1.0
    if max_trans_value == no_trans_gt_value:
        return 0.0
    return (max_trans_value - no_trans) / (max_trans_value - no_trans_gt_value)


def build_hardcoded_bundle() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        task=BUNDLE_DATA["task"],
        task_plan=TaskPlan.from_dict(BUNDLE_DATA["task_plan"]),
        no_trans=int(BUNDLE_DATA["no_trans"]),
        phases=BUNDLE_DATA["phases"],
        plan_files=BUNDLE_DATA["plan_files"],
        object_mappings=BUNDLE_DATA["object_mappings"],
        object_mapping_warnings=BUNDLE_DATA["object_mapping_warnings"],
    )


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a generated single-subtask executor_system plan."
    )
    parser.add_argument(
        "--runner-mode",
        action="store_true",
        help="Run without rendering and emit machine-readable runner metrics.",
    )
    parser.add_argument(
        "--metrics-output",
        default="",
        help="Path to write runner-mode metrics JSON.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_RUNNER_TIMEOUT_SECONDS,
        help="Runner-mode total timeout in seconds.",
    )
    return parser.parse_args(argv)


def runner_metrics_path(raw_path: str) -> Path:
    if raw_path:
        return Path(raw_path).expanduser()
    return Path(__file__).resolve().parent / "parallel_run_result.json"


def build_runner_result(status: str, start_time: float) -> Dict[str, Any]:
    return {{
        "status": status,
        "timed_out": False,
        "timeout_message": "",
        "run_time_seconds": time.monotonic() - start_time,
        "gcr": None,
        "tc": None,
        "sr": None,
        "ru": None,
        "executed_actions": 0,
        "failed_actions": 0,
        "failure_action_ratio": 0.0,
        "robot_failures": [],
    }}


def run_standalone() -> int:
    global floor_no, ground_truth, robots, runtime

    task_record = load_task_record(TASK_FILE, TASK_INDEX)
    floor_no = resolve_floor_plan(TASK_FILE)
    robots = build_forced_robot_team()
    ground_truth = list(task_record.get("object_states") or [])
    _demo_state.set_ground_truth(ground_truth)

    bundle = build_hardcoded_bundle()

    if bundle.object_mapping_warnings:
        for warning in bundle.object_mapping_warnings:
            print(f"WARNING: {{warning}}")

    runtime = ThorRuntime(robots, floor_no, CLOUD_RENDERING, RENDER_IMAGE)
    _context.runtime = runtime
    try:
        run_action_plan(bundle.task_plan)
        runtime.step({{"action": "Done"}}, check_success=False)

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        ru = transition_metric(bundle.no_trans, no_trans_gt, max_trans)
        sr = 1 if metrics["tc"] == 1.0 and ru == 1.0 else 0
        print(
            "SR:{{sr}}, TC:{{tc}}, GCR:{{gcr}}, Exec:{{exec_rate}}, RU:{{ru}}".format(
                sr=sr,
                tc=int(metrics["tc"]),
                gcr=metrics["gcr"],
                exec_rate=metrics["exec_rate"],
                ru=ru,
            )
        )
        runtime.log_unmet_goals(ground_truth)
        runtime.generate_video()
        runtime.write_final_metadata()
        return 0
    finally:
        runtime.stop()
        runtime = None
        _context.runtime = None


def run_runner_mode(args: argparse.Namespace) -> int:
    global floor_no, ground_truth, robots, runtime

    start_time = time.monotonic()
    metrics_path = runner_metrics_path(args.metrics_output)
    result = build_runner_result("failed", start_time)
    return_code = 1

    try:
        task_record = load_task_record(TASK_FILE, TASK_INDEX)
        floor_no = resolve_floor_plan(TASK_FILE)
        robots = build_forced_robot_team()
        ground_truth = list(task_record.get("object_states") or [])
        _demo_state.set_ground_truth(ground_truth)

        bundle = build_hardcoded_bundle()
        if bundle.object_mapping_warnings:
            result["object_mapping_warnings"] = list(bundle.object_mapping_warnings)

        runtime = ThorRuntime(robots, floor_no, CLOUD_RENDERING, False)
        _context.runtime = runtime

        execution_report = run_action_plan_tolerant(
            runtime,
            bundle.task_plan,
            timeout_seconds=args.timeout_seconds,
        )
        result.update(execution_report)
        try:
            runtime.step({{"action": "Done"}}, check_success=False, save_frame=False)
        except RuntimeError as exc:
            result["done_error"] = str(exc)

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        ru = transition_metric(bundle.no_trans, no_trans_gt, max_trans)
        result.update(
            {{
                "status": "timeout" if execution_report.get("timed_out") else "success",
                "gcr": metrics["gcr"],
                "tc": metrics["tc"],
                "sr": 1 if metrics["tc"] == 1.0 and ru == 1.0 else 0,
                "ru": ru,
                "exec_rate": metrics["exec_rate"],
            }}
        )
        return_code = 124 if result.get("timed_out") else 0
    except Exception as exc:
        result.update(
            {{
                "status": "failed",
                "error": str(exc),
            }}
        )
        return_code = 1
    finally:
        if runtime is not None:
            runtime.stop()
            runtime = None
        _context.runtime = None
        result["run_time_seconds"] = time.monotonic() - start_time
        write_result_json(metrics_path, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))

    return return_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    if args.runner_mode:
        return run_runner_mode(args)
    return run_standalone()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {{exc}}")
        raise SystemExit(1)
'''


def sanitize_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    safe = safe.strip("._")
    return safe[:80] or "item"


def task_dir_name(index: int, subtask: Dict[str, Any]) -> str:
    parts = [f"{index:05d}", subtask["skill"]]
    parts.extend(str(obj) for obj in subtask.get("objects", []))
    return sanitize_path_part("_".join(parts))


def flat_executable_name(item: GeneratedSubtask) -> str:
    return f"{item.floor_plan}_{item.floor_index + 1:05d}_executable_plan.py"


def task_record_for(
    generated: GeneratedSubtask,
    code_path: Optional[Path],
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "task": generated.task_text,
        "object_states": generated.object_states,
        "subtasks": [generated.subtask],
        "trans": len(generated.actions),
        "max_trans": len(generated.actions),
    }
    if code_path is not None:
        record["code_path"] = str(code_path)
    return record


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compile_python(path: Path) -> None:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def prepare_generated_subtasks(
    enumerated: Sequence[EnumeratedSubtask],
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> List[GeneratedSubtask]:
    engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
    floor_counts: Dict[int, int] = {}
    skill_sets_by_floor: Dict[int, Dict[str, Any]] = {}
    floor_objects_by_floor: Dict[int, List[Dict[str, Any]]] = {}
    open_parent_by_floor: Dict[int, Dict[str, str]] = {}
    generated: List[GeneratedSubtask] = []

    for global_index, item in enumerate(enumerated, start=1):
        floor_counts[item.floor_plan] = floor_counts.get(item.floor_plan, 0) + 1
        floor_index = floor_counts[item.floor_plan] - 1
        if item.floor_plan not in skill_sets_by_floor:
            skill_sets_by_floor[item.floor_plan] = data_engine._build_object_skill_sets(
                item.floor_plan,
                object_properties_path,
            )
            floor_objects_by_floor[item.floor_plan] = load_floor_object_metadata(
                item.floor_plan,
                object_properties_path,
            )
            open_parent_by_floor[item.floor_plan] = build_openable_parent_container_map(
                floor_objects_by_floor[item.floor_plan]
            )
        skill_sets = skill_sets_by_floor[item.floor_plan]
        actions = build_actions_for_subtask(
            item.subtask,
            skill_sets,
            open_parent_by_floor[item.floor_plan],
        )
        pre_task_actions = build_pre_task_actions_for_subtask(
            item.subtask,
            floor_objects_by_floor[item.floor_plan],
        )
        generated.append(
            GeneratedSubtask(
                floor_plan=item.floor_plan,
                floor_index=floor_index,
                global_index=global_index,
                subtask=item.subtask,
                task_text=construct_subtask_name(item.subtask),
                object_states=engine.get_task_final_state([item.subtask]),
                actions=actions,
                pre_task_actions=pre_task_actions,
            )
        )

    return generated


def write_flat_outputs(
    generated: Sequence[GeneratedSubtask],
    output_dir: Path,
    overwrite: bool = False,
    validate_code: bool = True,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    skipped_existing = 0
    errors: List[Dict[str, Any]] = []
    floor_plans = sorted({item.floor_plan for item in generated})

    for item in generated:
        executable_path = output_dir / flat_executable_name(item)
        record = task_record_for(item, executable_path)
        bundle_data = build_bundle_data(
            task_id=f"FloorPlan{item.floor_plan}_single_subtask_{item.floor_index}",
            task_text=item.task_text,
            actions=item.actions,
            pre_task_actions=item.pre_task_actions,
        )

        try:
            if executable_path.exists() and not overwrite:
                skipped_existing += 1
                success_count += 1
                continue

            executable_path.write_text(
                render_executable(
                    None,
                    item.floor_index,
                    bundle_data,
                    task_record=record,
                    floor_plan=item.floor_plan,
                ),
                encoding="utf-8",
            )
            if validate_code:
                compile_python(executable_path)
            success_count += 1
        except Exception as exc:  # noqa: BLE001 - report all per-task generation failures.
            errors.append(
                {
                    "floor_plan": item.floor_plan,
                    "task_index": item.floor_index,
                    "subtask": item.subtask,
                    "error": str(exc),
                }
            )

    return {
        "output_layout": "flat",
        "floor_count": len(floor_plans),
        "total_subtasks": len(generated),
        "successful_generations": success_count,
        "failed_generations": len(errors),
        "skipped_existing": skipped_existing,
        "output_dir": str(output_dir),
        "errors": errors,
    }


def write_outputs(
    generated: Sequence[GeneratedSubtask],
    output_dir: Path,
    overwrite: bool = False,
    validate_code: bool = True,
    manifest_only: bool = False,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = output_dir / "dataset"
    manifest_path = output_dir / "manifest.jsonl"
    summary_path = output_dir / "summary.json"

    records_by_floor: Dict[int, List[Dict[str, Any]]] = {}
    manifest_records: List[Dict[str, Any]] = []
    success_count = 0
    skipped_existing = 0
    errors: List[Dict[str, Any]] = []

    for item in generated:
        floor_dir = output_dir / f"FloorPlan{item.floor_plan}"
        dir_name = task_dir_name(item.floor_index + 1, item.subtask)
        task_dir = floor_dir / dir_name
        executable_path = task_dir / "executable_plan.py"
        code_path = None if manifest_only else executable_path
        record = task_record_for(item, code_path)
        records_by_floor.setdefault(item.floor_plan, []).append(record)

        bundle_data = build_bundle_data(
            task_id=f"FloorPlan{item.floor_plan}_single_subtask_{item.floor_index}",
            task_text=item.task_text,
            actions=item.actions,
            pre_task_actions=item.pre_task_actions,
            plan_file=None if manifest_only else task_dir / "subtask_plan.txt",
        )

        manifest_record = {
            "floor_plan": item.floor_plan,
            "task_index": item.floor_index,
            "task": item.task_text,
            "subtask": item.subtask,
            "object_states": item.object_states,
            "no_trans": len(item.actions),
            "code_path": str(code_path) if code_path is not None else None,
        }
        manifest_records.append(manifest_record)

        if manifest_only:
            success_count += 1
            continue

        try:
            if task_dir.exists():
                if overwrite:
                    shutil.rmtree(task_dir)
                else:
                    skipped_existing += 1
                    success_count += 1
                    continue

            task_dir.mkdir(parents=True, exist_ok=True)
            dataset_path = dataset_dir / f"FloorPlan{item.floor_plan}.jsonl"
            write_json(task_dir / "task_record.json", record)
            write_json(task_dir / "plan_bundle.json", bundle_data)
            (task_dir / "subtask_plan.txt").write_text(
                render_plan_text(item.actions),
                encoding="utf-8",
            )
            executable_path.write_text(
                render_executable(dataset_path, item.floor_index, bundle_data),
                encoding="utf-8",
            )
            if validate_code:
                compile_python(executable_path)
            success_count += 1
        except Exception as exc:  # noqa: BLE001 - report all per-task generation failures.
            errors.append(
                {
                    "floor_plan": item.floor_plan,
                    "task_index": item.floor_index,
                    "subtask": item.subtask,
                    "error": str(exc),
                }
            )

    dataset_dir.mkdir(parents=True, exist_ok=True)
    for floor_plan, records in sorted(records_by_floor.items()):
        dataset_path = dataset_dir / f"FloorPlan{floor_plan}.jsonl"
        dataset_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    manifest_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in manifest_records),
        encoding="utf-8",
    )

    summary = {
        "output_layout": "full",
        "floor_count": len(records_by_floor),
        "total_subtasks": len(generated),
        "successful_generations": success_count,
        "failed_generations": len(errors),
        "skipped_existing": skipped_existing,
        "manifest_only": manifest_only,
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "dataset_dir": str(dataset_dir),
        "errors": errors,
    }
    write_json(summary_path, summary)
    return summary


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate executable_plan.py files for all single data_engine subtasks."
    )
    parser.add_argument(
        "--floor-plans",
        nargs="+",
        default=None,
        help="Floor plans to generate, e.g. 1 2 201. Defaults to all cached floor plans.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory for generated single-subtask code.",
    )
    parser.add_argument(
        "--bad-subtasks-config",
        default=None,
        help=(
            "JSON config listing bad single subtasks to exclude. Defaults to "
            f"{DEFAULT_BAD_SUBTASKS_CONFIG} when present."
        ),
    )
    parser.add_argument(
        "--no-valid-positions-config",
        default=None,
        help=(
            "JSON list of floorplan/object/receptacle PutObject failures to exclude. "
            f"Defaults to {DEFAULT_NO_VALID_POSITIONS_PATH} when present."
        ),
    )
    parser.add_argument(
        "--output-layout",
        choices=("flat", "full"),
        default="flat",
        help=(
            "Output layout. 'flat' writes only {floor}_{index}_executable_plan.py files; "
            "'full' writes the legacy dataset, manifest, summary, and per-subtask directories."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the total number of generated subtasks for debugging.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing per-subtask output directories.",
    )
    parser.add_argument(
        "--no-validate-code",
        dest="validate_code",
        action="store_false",
        help="Skip syntax validation for generated executable_plan.py files.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Only write manifest, summary, and dataset JSONL files.",
    )
    args = parser.parse_args(argv)
    if args.manifest_only and args.output_layout != "full":
        parser.error("--manifest-only requires --output-layout full")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    output_dir = Path(args.output_dir).expanduser()
    if args.floor_plans:
        floor_plans = [normalize_floor_plan(value) for value in args.floor_plans]
    else:
        floor_plans = discover_floor_plans()

    started = time.time()
    enumerated = enumerate_single_subtasks(floor_plans)
    bad_subtasks_config = (
        DEFAULT_BAD_SUBTASKS_CONFIG
        if args.bad_subtasks_config is None
        else Path(args.bad_subtasks_config).expanduser()
    )
    bad_subtask_rules = load_bad_subtask_rules(
        bad_subtasks_config,
        missing_ok=args.bad_subtasks_config is None,
    )
    enumerated, excluded_subtasks = filter_bad_subtasks(enumerated, bad_subtask_rules)
    no_valid_positions_config = (
        DEFAULT_NO_VALID_POSITIONS_PATH
        if args.no_valid_positions_config is None
        else Path(args.no_valid_positions_config).expanduser()
    )
    no_valid_position_rules = load_no_valid_position_rules(
        no_valid_positions_config,
        missing_ok=args.no_valid_positions_config is None,
    )
    enumerated, excluded_no_valid_position_subtasks = filter_no_valid_position_subtasks(
        enumerated,
        no_valid_position_rules,
    )
    if args.limit is not None:
        if args.limit < 0:
            raise ValueError("--limit must be non-negative")
        enumerated = enumerated[: args.limit]

    generated = prepare_generated_subtasks(enumerated)
    if args.output_layout == "flat":
        summary = write_flat_outputs(
            generated,
            output_dir,
            overwrite=args.overwrite,
            validate_code=args.validate_code,
        )
    else:
        summary = write_outputs(
            generated,
            output_dir,
            overwrite=args.overwrite,
            validate_code=args.validate_code,
            manifest_only=args.manifest_only,
        )
    summary["excluded_subtasks"] = excluded_subtasks
    summary["bad_subtasks_config"] = str(bad_subtasks_config)
    summary["excluded_no_valid_position_subtasks"] = excluded_no_valid_position_subtasks
    summary["no_valid_positions_config"] = str(no_valid_positions_config)
    summary["elapsed_seconds"] = time.time() - started
    if args.output_layout == "full":
        write_json(output_dir / "summary.json", summary)

    print(
        "Generated {successful_generations}/{total_subtasks} single-subtask code entries "
        "under {output_dir}".format(**summary)
    )
    if summary["failed_generations"]:
        print(f"Failed generations: {summary['failed_generations']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
