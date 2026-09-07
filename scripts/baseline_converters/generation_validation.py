"""Offline capability checks shared by every plan-to-code converter."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from special_task_skills import canonical_skill_key
from executor_system.capability_checks import (
    normalize_skill_name as _skill_key, finite_nonnegative_number, capability_failure,
)


class GenerationValidationError(RuntimeError):
    def __init__(self, reason: str, message: str, details: Dict[str, Any]) -> None:
        self.reason = reason
        self.details = dict(details)
        super().__init__(message)


def generation_failure_counts(results: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {"mass_failed_generations": 0, "skill_failed_generations": 0}
    fields = {"mass_exceeded": "mass_failed_generations", "missing_skill": "skill_failed_generations"}
    for result in results:
        field = fields.get(result.get("failure_reason"))
        if field and result.get("status") == "failed" and not result.get("success"):
            counts[field] += 1
    return counts


def generation_failure_result(
    exc: GenerationValidationError, executable_path: Path, *, dry_run: bool = False,
) -> Dict[str, Any]:
    result = {
        "status": "failed",
        "success": False,
        "failure_reason": exc.reason,
        "validation_error": exc.details,
        "error": str(exc),
    }
    if not dry_run:
        try:
            executable_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            result["cleanup_error"] = str(cleanup_exc)
    return result


def _object_key(value: Any) -> str:
    text = str(value).strip()
    # Coordinates (including their signs) identify an instance, not a type alias.
    return text.lower() if "|" in text else canonical_skill_key(text)


def _catalog_robot(source: Any) -> Dict[str, Any]:
    import resources.robots as robot_catalog

    match = re.fullmatch(r"(?:robot)?([1-9]\d*)", str(source), re.IGNORECASE)
    if match and int(match[1]) <= len(robot_catalog.robots):
        return robot_catalog.robots[int(match[1]) - 1]
    return {}


def prepare_generation_robots(
    robots: Any, task_record: Mapping[str, Any], *, use_source_names: bool = False,
) -> List[Dict[str, Any]]:
    """Recover an absent team from explicit dataset IDs before plan encoding."""
    details = {"field": "robots"}
    if robots is None or robots == []:
        robot_ids = task_record.get("robot list")
        if not isinstance(robot_ids, list) or not robot_ids:
            raise GenerationValidationError(
                "validation_data_missing", "Missing robots and dataset robot list.", details,
            )
        team = []
        for index, source in enumerate(robot_ids):
            robot = dict(_catalog_robot(source))
            if not robot:
                raise GenerationValidationError(
                    "validation_data_missing", f"Cannot resolve original robot {source!r}.", details,
                )
            robot["source_name"] = robot["name"]
            robot["source_id"] = int(robot["name"][len("robot"):])
            if not use_source_names:
                robot["name"] = f"robot{index + 1}"
            team.append(robot)
        return team
    if not isinstance(robots, list) or any(
        not isinstance(robot, (dict, str)) for robot in robots
    ):
        raise GenerationValidationError(
            "validation_data_missing", "Robot metadata must be a list of robot configurations.", details,
        )
    return [dict(robot) if isinstance(robot, dict) else {"name": robot} for robot in robots]


def _robot_config(
    robot_id: str, robots: Sequence[Any], task_record: Mapping[str, Any],
    robot_id_map: Optional[Mapping[str, str]], details: Dict[str, Any],
) -> Dict[str, Any]:
    matches = []
    for index, raw in enumerate(robots):
        if isinstance(raw, dict):
            name = str(raw.get("symbol") or raw.get("name") or f"robot{index + 1}")
        else:
            name = str(raw)
        local_name = robot_id_map.get(name) if robot_id_map is not None else name
        if local_name == robot_id:
            matches.append((index, name, raw))
    if len(matches) != 1:
        raise GenerationValidationError(
            "validation_data_missing", f"Cannot uniquely resolve robot {robot_id!r}.", details,
        )
    index, name, raw = matches[0]
    config = dict(raw) if isinstance(raw, dict) else {}
    source = config.get("source_id") or config.get("source_name")
    if source is None and robot_id_map is not None:
        source = name
    if source is None:
        robot_ids = task_record.get("robot list")
        if isinstance(robot_ids, list) and index < len(robot_ids):
            source = robot_ids[index]
    fallback = _catalog_robot(source)
    for field in ("skills", "mass_capacity"):
        if field not in config and field in fallback:
            config[field] = fallback[field]
    return config


class _ObjectMassLookup:
    def __init__(
        self, *, objects: Sequence[Any], task_context: Mapping[str, Any], repo_root: Path,
        floor_plan: Any, object_mappings: Mapping[str, str], object_id_bindings: Sequence[Any],
    ) -> None:
        self.sources = [objects]
        self.sources.extend(task_context.get(key) or [] for key in ("objects", "scene_objects"))
        self.objects_ai = task_context.get("objects_ai")
        floor = re.sub(r"^floorplan", "", str(floor_plan or "").strip(), flags=re.IGNORECASE)
        self.cache_path = repo_root / "data/ai2thor_objects_cache" / f"FloorPlan{floor}.json"
        self.mappings = object_mappings
        self.bindings = object_id_bindings
        self.loaded = False
        self.cached: Optional[List[Any]] = None

    def _load(self) -> None:
        if self.loaded:
            return
        if self.objects_ai:
            text = str(self.objects_ai).strip()
            if text.startswith("objects"):
                text = text.partition("=")[2].strip()
            self.sources.append(ast.literal_eval(text))
        # The cache is a fallback; defer reading it until run metadata is insufficient.
        self.loaded = True

    @staticmethod
    def _keys(item: Mapping[str, Any]) -> set:
        return {
            _object_key(item[key])
            for key in ("name", "label", "symbol", "objectId", "object_id", "objectType")
            if item.get(key)
        }

    def _reference_keys(self, reference: str) -> tuple:
        aliases = {str(reference)}
        for binding in self.bindings:
            if not isinstance(binding, dict):
                continue
            tokens = [binding.get("object")]
            if binding.get("object_type") and binding.get("number"):
                tokens.append(f"{binding['object_type']}_{binding['number']}")
            if _object_key(reference) in {_object_key(t) for t in tokens if t}:
                aliases.update(
                    str(binding[k]) for k in ("object_id", "object_type") if binding.get(k)
                )
        # Follow mappings forward only: reverse traversal could join separate instances.
        for _ in range(len(self.mappings)):
            alias_keys = {_object_key(a) for a in aliases}
            added = {str(value) for key, value in self.mappings.items() if _object_key(key) in alias_keys}
            if added.issubset(aliases):
                break
            aliases.update(added)
        ids = {_object_key(a) for a in aliases if "|" in a}
        exact = {_object_key(a) for a in aliases}
        types = {_object_key(a.split("|", 1)[0]) for a in aliases if "|" in a}
        types.update(
            _object_key(re.sub(r"(?:_\d+|\d+|_[0-9a-f]{8})$", "", a))
            for a in aliases if "|" not in a
        )
        return ids, exact, types

    def _find(
        self, sources: Sequence[Any], keys: set, *, instance_ids: Optional[set] = None,
    ) -> Optional[float]:
        for source in sources:
            if not isinstance(source, (list, tuple)):
                raise ValueError("Object metadata must be a list.")
            matches = [item for item in source if isinstance(item, dict) and self._keys(item) & keys]
            if instance_ids:
                matches = [
                    item for item in matches
                    if not (item.get("objectId") or item.get("object_id"))
                    or _object_key(item.get("objectId") or item.get("object_id")) in instance_ids
                ]
            if not matches:
                continue
            masses = [finite_nonnegative_number(item["mass"]) for item in matches if "mass" in item]
            if any(mass is None for mass in masses):
                raise ValueError("Object mass must be finite and non-negative.")
            if len(set(masses)) > 1:
                raise ValueError("Object reference matches multiple different masses.")
            if masses and len(masses) != len(matches):
                raise ValueError("Object reference has ambiguous, partially missing masses.")
            if masses and len(masses) == len(matches):
                return masses[0]
        return None

    def mass(self, reference: str) -> float:
        self._load()
        key_groups = self._reference_keys(reference)
        ids = key_groups[0]
        if len(ids) > 1:
            raise ValueError(f"Conflicting object bindings for {reference!r}.")
        if ids:
            value = self._find(self.sources, ids)
            if value is None:
                value = self._find([self._cache()], ids)
            if value is not None:
                return value
        for keys in key_groups:
            value = self._find(self.sources, keys, instance_ids=ids)
            if value is not None:
                return value
        for keys in key_groups:
            value = self._find([self._cache()], keys, instance_ids=ids)
            if value is not None:
                return value
        raise ValueError(f"Cannot determine mass for object {reference!r}.")

    def _cache(self) -> List[Any]:
        if self.cached is None:
            self.cached = (
                json.loads(self.cache_path.read_text(encoding="utf-8"))
                if self.cache_path.is_file() else []
            )
        return self.cached


def validate_generation_plan(
    task_plan_data: Mapping[str, Any], *, robots: Sequence[Any], repo_root: Path,
    floor_plan: Any, task_record: Optional[Mapping[str, Any]] = None,
    task_context: Optional[Mapping[str, Any]] = None, objects: Sequence[Any] = (),
    object_mappings: Optional[Mapping[str, str]] = None, object_id_bindings: Sequence[Any] = (),
    robot_id_map: Optional[Mapping[str, str]] = None,
) -> None:
    """Fail on the first violation in stage/queue/action order, checking skills first."""
    lookup = _ObjectMassLookup(
        objects=objects, task_context=task_context or {}, repo_root=repo_root,
        floor_plan=floor_plan, object_mappings=object_mappings or {}, object_id_bindings=object_id_bindings,
    )
    for stage_index, stage in enumerate(task_plan_data.get("stages", [])):
        for robot_id, actions in stage["robot_action_queues"].items():
            for action_index, action in enumerate(actions):
                action_type = action["action_type"]
                details = {
                    "stage_id": stage["stage_id"],
                    "stage_index": stage_index,
                    "robot_id": robot_id,
                    "action_index": action_index,
                    "action_type": action_type,
                }
                required_skill = _skill_key(action_type)
                if required_skill in {"wait", "waitonetick", "waituntil", "pass", "done"}:
                    continue
                robot = _robot_config(robot_id, robots, task_record or {}, robot_id_map, details)
                parameters = action.get("parameters") or {}
                args = parameters.get("args") or []
                target = args[0] if args else parameters.get("objectId")
                # A provisional finite mass checks skill and capacity before lookup.
                failure = capability_failure(robot, action_type, object_name=target, object_mass=0)
                if failure:
                    raise GenerationValidationError(failure['reason'], failure['message'],
                                                    dict(details, **failure['details']))
                if required_skill != "pickupobject":
                    continue
                details.update(object=target, mass_capacity=finite_nonnegative_number(robot.get('mass_capacity')))
                try:
                    mass = lookup.mass(target)
                except (ValueError, SyntaxError, TypeError, OSError) as exc:
                    raise GenerationValidationError("validation_data_missing", str(exc), details) from exc
                failure = capability_failure(robot, action_type, object_name=target, object_mass=mass)
                if failure:
                    raise GenerationValidationError(failure['reason'], failure['message'],
                                                    dict(details, **failure['details']))
