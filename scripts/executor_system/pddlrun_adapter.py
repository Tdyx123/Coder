"""Adapt pddlrun_llmseparate allocation and planner outputs to TaskPlan."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from parsing_utils import ParsingUtils

from .action_plan import Action, StagePlan, TaskPlan
from .utils import robot_name


class PddlRunAdapterError(RuntimeError):
    """Raised when pddlrun outputs cannot be converted to an executor plan."""


@dataclass(frozen=True)
class SubtaskAssignment:
    subtask_id: int
    robot_number: int


@dataclass(frozen=True)
class PddlPlanAction:
    name: str
    args: Tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class EncodedSubtaskAction:
    action: Action
    pddl: PddlPlanAction
    object_tokens: Tuple[str, ...]


@dataclass
class PddlRunPlanBundle:
    task: str
    task_plan: TaskPlan
    no_trans: int
    phases: List[List[SubtaskAssignment]]
    plan_files: Dict[int, Path]
    object_mappings: Dict[str, str]
    object_mapping_warnings: List[str]
    object_id_bindings: List[Dict[str, Any]] = field(default_factory=list)


ACTION_ALIASES = {
    "gotoobject": "GoToObject",
    "pickupobject": "PickupObject",
    "putobject": "PutObject",
    "switchon": "SwitchOn",
    "switchoff": "SwitchOff",
    "switchoffobject": "SwitchOff",
    "openobject": "OpenObject",
    "closeobject": "CloseObject",
    "breakobject": "BreakObject",
    "prepareegg": "PrepareEgg",
    "breakegg": "BreakEgg",
    "sliceobject": "SliceObject",
    "cleanobject": "CleanObject",
    "dirtyobject": "DirtyObject",
    "emptyliquid": "EmptyLiquid",
    "emptyliquidfromobject": "EmptyLiquid",
    "runmicrowave": "RunMicrowave",
    "runcoffeemachine": "RunCoffeeMachine",
    "runtoaster": "RunToaster",
    "cookbystoveburner": "CookByStoveBurner",
    "heatbystoveburner": "HeatByStoveBurner",
    "firebystoveburner": "FireByStoveBurner",
    "fillwater": "FillWater",
    "coldobject": "ColdObject",
    "throwobject": "ThrowObject",
    "waitonetick": "WaitOneTick",
}

SUPPORTED_ACTIONS = set(ACTION_ALIASES.values())
NO_ALLOCATION_ASSIGNMENTS_ERROR = (
    "No subtask-to-robot assignments found in allocation output."
)


def object_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def strip_trailing_digits(value: str) -> str:
    return re.sub(r"\d+$", "", value)


def pascalize_token(value: str) -> str:
    text = re.sub(r"\d+$", "", str(value).strip())
    parts = re.split(r"[^A-Za-z0-9]+", text)
    if len(parts) > 1:
        return "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not text:
        return str(value)
    return text[:1].upper() + text[1:]


class ObjectNameResolver:
    """Map PDDL object tokens to stable executor object references."""

    def __init__(
        self,
        object_names: Optional[Iterable[Any]] = None,
        object_id_bindings: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> None:
        self._by_key: Dict[str, str] = {}
        self._binding_by_key: Dict[str, Dict[str, Any]] = {}
        self.mappings: Dict[str, str] = {}
        self.warnings: List[str] = []

        for raw_name in object_names or ():
            name = self._name_from_any(raw_name)
            if not name:
                continue
            key = object_key(name)
            if key and key not in self._by_key:
                self._by_key[key] = name

        self.register_object_id_bindings(object_id_bindings or ())

    def _name_from_any(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("name") or value.get("objectType") or value.get("objectId")
        return str(value) if value else ""

    def register_object_id_bindings(
        self,
        object_id_bindings: Iterable[Dict[str, Any]],
    ) -> None:
        for raw_binding in object_id_bindings:
            if not isinstance(raw_binding, dict):
                continue
            object_token = raw_binding.get("object")
            object_id = raw_binding.get("object_id")
            if not isinstance(object_token, str) or not object_token:
                continue
            if not isinstance(object_id, str) or not object_id:
                continue

            binding = dict(raw_binding)
            binding["object"] = object_token
            binding["object_id"] = object_id
            for key in self._binding_keys(binding):
                self._binding_by_key[key] = binding

    def _binding_keys(self, binding: Dict[str, Any]) -> List[str]:
        keys = []
        object_token = binding.get("object")
        if isinstance(object_token, str) and object_token:
            keys.append(object_key(object_token))

        object_type = binding.get("object_type")
        number = binding.get("number")
        try:
            number_value = int(number)
        except (TypeError, ValueError):
            number_value = 0
        if isinstance(object_type, str) and object_type:
            if number_value > 0:
                keys.append(object_key(f"{object_type}_{number_value}"))
                keys.append(object_key(f"{object_type}{number_value}"))
            if not bool(binding.get("multiple")):
                keys.append(object_key(object_type))

        return list(dict.fromkeys(key for key in keys if key))

    def resolve(self, token: str) -> str:
        token = str(token).strip()
        key = object_key(token)
        binding = self._binding_by_key.get(key)
        if binding is not None:
            object_token = str(binding.get("object") or token)
            object_id = str(binding.get("object_id") or "")
            if object_id:
                self.mappings[token] = object_id
                self.mappings[object_token] = object_id
            return object_token

        candidates = [key, strip_trailing_digits(key)]

        for candidate in candidates:
            if candidate in self._by_key:
                resolved = self._by_key[candidate]
                self.mappings[token] = resolved
                return resolved

        fallback = pascalize_token(token)
        warning = (
            f"No AI2-THOR object type match for PDDL token {token!r}; "
            f"using {fallback!r}."
        )
        if warning not in self.warnings:
            self.warnings.append(warning)
        self.mappings[token] = fallback
        return fallback


def canonical_action_name(action_name: str) -> str:
    key = object_key(action_name)
    return ACTION_ALIASES.get(key, str(action_name).strip())


def parse_allocation_phases(allocation_text: str) -> List[List[SubtaskAssignment]]:
    sections = ParsingUtils.extract_sequence_sections(allocation_text)
    if not sections:
        sections = [allocation_text.strip().splitlines()]

    candidates: List[Tuple[int, int, List[str]]] = []
    for index, section_lines in enumerate(sections):
        normalized_lines, assignments = ParsingUtils.parse_sequence_section(section_lines)
        candidates.append((len(assignments), index, normalized_lines))

    nonempty = [candidate for candidate in candidates if candidate[0] > 0]
    if not nonempty:
        raise PddlRunAdapterError(NO_ALLOCATION_ASSIGNMENTS_ERROR)

    _, _, selected_lines = max(nonempty, key=lambda item: (item[0], item[1]))
    assignment_re = re.compile(
        r"Subtask\s+(\d+)\s*:\s*Robot\s+(\d+)\s*;",
        re.IGNORECASE,
    )
    phases: List[List[SubtaskAssignment]] = []

    for line in selected_lines:
        phase = [
            SubtaskAssignment(
                subtask_id=int(match.group(1)),
                robot_number=int(match.group(2)),
            )
            for match in assignment_re.finditer(line)
        ]
        if phase:
            phases.append(phase)

    if not phases:
        raise PddlRunAdapterError(
            "Allocation output did not contain executable phase assignments."
        )
    return phases


def parse_plan_actions(plan_text: str) -> List[PddlPlanAction]:
    actions: List[PddlPlanAction] = []
    action_re = re.compile(
        r"^\s*(?:\d+(?:\.\d+)?\s*:\s*)?"
        r"\(\s*([^\s()]+)\s*([^()]*)\)\s*"
        r"(?:\[[^\]]+\])?\s*$"
    )

    for raw_line in plan_text.splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line or not line.startswith("(") and not re.match(r"^\d", line):
            continue

        match = action_re.match(line)
        if not match:
            raise PddlRunAdapterError(f"Could not parse PDDL plan line: {raw_line}")

        action_name = canonical_action_name(match.group(1))
        if action_name not in SUPPORTED_ACTIONS:
            raise PddlRunAdapterError(
                f"Unsupported PDDL action {match.group(1)!r} in line: {raw_line}"
            )

        args = tuple(match.group(2).strip().split())
        actions.append(PddlPlanAction(name=action_name, args=args, raw=line))

    return actions


def _require_args(action: PddlPlanAction, count: int) -> None:
    if len(action.args) < count:
        raise PddlRunAdapterError(
            f"Action {action.raw!r} has {len(action.args)} argument(s), "
            f"expected at least {count}."
        )


def _executor_action(
    action: PddlPlanAction,
    action_type: str,
    args: Sequence[str],
    object_tokens: Sequence[str],
) -> EncodedSubtaskAction:
    return EncodedSubtaskAction(
        action=Action(action_type, {"args": tuple(args)}),
        pddl=action,
        object_tokens=tuple(object_tokens),
    )


def encode_plan_action(
    action: PddlPlanAction,
    resolver: ObjectNameResolver,
) -> EncodedSubtaskAction:
    if action.name == "WaitOneTick":
        return _executor_action(action, action.name, [], [])

    if action.name == "GoToObject":
        _require_args(action, 2)
        dest = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [dest], [action.args[1]])

    if action.name == "PickupObject":
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [obj], [action.args[1]])

    if action.name == "PutObject":
        _require_args(action, 3)
        obj = resolver.resolve(action.args[1])
        receptacle = resolver.resolve(action.args[2])
        return _executor_action(
            action,
            action.name,
            [obj, receptacle],
            [action.args[1], action.args[2]],
        )

    if action.name in {
        "SwitchOn",
        "SwitchOff",
        "OpenObject",
        "CloseObject",
        "BreakObject",
        "DirtyObject",
        "EmptyLiquid",
    }:
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [obj], [action.args[1]])

    if action.name == "BreakEgg":
        _require_args(action, 2)
        egg = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [egg], [action.args[1]])

    if action.name == "PrepareEgg":
        _require_args(action, 3)
        egg = resolver.resolve(action.args[1])
        container = resolver.resolve(action.args[2])
        return _executor_action(
            action,
            action.name,
            [egg, container],
            [action.args[1], action.args[2]],
        )

    if action.name == "SliceObject":
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [obj], [action.args[1]])

    if action.name == "CleanObject":
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [obj], [action.args[1]])

    if action.name in {"RunMicrowave", "RunCoffeeMachine", "RunToaster"}:
        _require_args(action, 3)
        machine = resolver.resolve(action.args[1])
        obj = resolver.resolve(action.args[2])
        return _executor_action(
            action,
            action.name,
            [machine, obj],
            [action.args[1], action.args[2]],
        )

    if action.name == "CookByStoveBurner":
        _require_args(action, 4)
        stove_burner = resolver.resolve(action.args[1])
        container = resolver.resolve(action.args[2])
        food = resolver.resolve(action.args[3])
        return _executor_action(
            action,
            action.name,
            [stove_burner, container, food],
            [action.args[1], action.args[2], action.args[3]],
        )

    if action.name in {"HeatByStoveBurner", "FireByStoveBurner", "FillWater", "ColdObject"}:
        _require_args(action, 3)
        source = resolver.resolve(action.args[1])
        obj = resolver.resolve(action.args[2])
        return _executor_action(
            action,
            action.name,
            [source, obj],
            [action.args[1], action.args[2]],
        )

    if action.name == "ThrowObject":
        object_tokens: List[str] = []
        args: List[str] = []
        if len(action.args) >= 2:
            args.append(resolver.resolve(action.args[1]))
            object_tokens.append(action.args[1])
        return _executor_action(action, action.name, args, object_tokens)

    raise PddlRunAdapterError(f"Unsupported PDDL action {action.name!r}.")


def extract_subtask_id(path: Any) -> Optional[int]:
    match = re.search(r"subtask[_-]?(\d+)", str(path), re.IGNORECASE)
    return int(match.group(1)) if match else None


def resolve_plan_files(
    plan_folder: Optional[Any],
    plan_files: Optional[Sequence[Any]] = None,
) -> List[Path]:
    if plan_files:
        resolved = [Path(str(path)).expanduser() for path in plan_files]
        missing = [str(path) for path in resolved if not path.is_file()]
        if missing:
            raise PddlRunAdapterError(
                f"Planner output file(s) not found: {', '.join(missing)}"
            )
        return resolved

    if plan_folder is None:
        raise PddlRunAdapterError("PLAN_FOLDER is required when PLAN_FILES is empty.")

    folder = Path(str(plan_folder)).expanduser()
    if not folder.is_dir():
        raise PddlRunAdapterError(f"Planner output folder not found: {folder}")

    resolved = sorted(folder.glob("*_plan.txt"))
    if not resolved:
        raise PddlRunAdapterError(f"No planner output files found in: {folder}")
    return resolved


def load_allocation_text(allocate_file: Any) -> str:
    path = Path(str(allocate_file)).expanduser()
    if not path.is_file():
        raise PddlRunAdapterError(f"Allocation output file not found: {path}")
    return path.read_text(encoding="utf-8")


def load_plan_texts(plan_files: Sequence[Any]) -> Dict[int, Tuple[Path, str]]:
    plan_texts: Dict[int, Tuple[Path, str]] = {}
    for raw_path in plan_files:
        path = Path(str(raw_path)).expanduser()
        subtask_id = extract_subtask_id(path.name)
        if subtask_id is None:
            raise PddlRunAdapterError(
                f"Could not infer subtask id from planner output file: {path}"
            )
        if subtask_id in plan_texts:
            raise PddlRunAdapterError(
                f"Duplicate planner output for subtask {subtask_id}: {path}"
            )
        plan_texts[subtask_id] = (path, path.read_text(encoding="utf-8"))

    if not plan_texts:
        raise PddlRunAdapterError("No planner output files were provided.")
    return dict(sorted(plan_texts.items()))


def _coerce_object_id_bindings(value: Any) -> List[Dict[str, Any]]:
    """Normalize pddlrun object-id binding artifacts into a flat list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nested = value.get("object_id_bindings") or value.get("bindings")
        if isinstance(nested, list):
            return [dict(item) for item in nested if isinstance(item, dict)]

        flattened: List[Dict[str, Any]] = []
        for item in value.values():
            flattened.extend(_coerce_object_id_bindings(item))
        return flattened
    return []


def _coerce_object_id_bindings_by_subtask(value: Any) -> Dict[int, List[Dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}

    result: Dict[int, List[Dict[str, Any]]] = {}
    for raw_subtask_id, raw_bindings in value.items():
        try:
            subtask_id = int(raw_subtask_id)
        except (TypeError, ValueError):
            continue
        bindings = _coerce_object_id_bindings(raw_bindings)
        if bindings:
            result[subtask_id] = bindings
    return result


def _read_json_if_exists(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PddlRunAdapterError(f"Invalid JSON artifact: {path}") from exc


def infer_task_run_dir_from_allocate_file(allocate_file: Any) -> Optional[Path]:
    path = Path(str(allocate_file)).expanduser()
    if path.name == "02_allocate_output.txt" and path.parent.name == "02_allocate":
        return path.parent.parent
    if path.parent.name == "02_allocate":
        return path.parent.parent
    return None


def discover_object_id_binding_artifacts(
    allocate_file: Any,
) -> Tuple[List[Dict[str, Any]], Dict[int, List[Dict[str, Any]]]]:
    task_run_dir = infer_task_run_dir_from_allocate_file(allocate_file)
    if task_run_dir is None:
        return [], {}

    problem_dir = task_run_dir / "05_problem_generation"
    bindings = _coerce_object_id_bindings(
        _read_json_if_exists(problem_dir / "key_object_id_bindings.json")
    )
    bindings_by_subtask = _coerce_object_id_bindings_by_subtask(
        _read_json_if_exists(problem_dir / "key_object_id_bindings_by_subtask.json")
    )
    return bindings, bindings_by_subtask


def merge_object_id_bindings(
    object_id_bindings: Sequence[Dict[str, Any]],
    object_id_bindings_by_subtask: Optional[Dict[int, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    merged: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    for binding in object_id_bindings:
        object_token = str(binding.get("object") or "")
        object_id = str(binding.get("object_id") or "")
        if not object_token or not object_id:
            continue
        merged[(object_token, object_id)] = dict(binding)

    for bindings in (object_id_bindings_by_subtask or {}).values():
        for binding in bindings:
            object_token = str(binding.get("object") or "")
            object_id = str(binding.get("object_id") or "")
            if not object_token or not object_id:
                continue
            merged[(object_token, object_id)] = dict(binding)
    return list(merged.values())


def _robot_id_for_number(robots: Sequence[Any], robot_number: int) -> str:
    if robot_number < 1 or robot_number > len(robots):
        raise PddlRunAdapterError(
            f"Allocation references Robot {robot_number}, "
            f"but only {len(robots)} robot(s) exist."
        )
    robot_id = robot_name(robots[robot_number - 1])
    if not robot_id:
        raise PddlRunAdapterError(f"Cannot infer robot name for Robot {robot_number}.")
    return robot_id


def _group_phase_by_robot(
    phase: Sequence[SubtaskAssignment],
) -> "OrderedDict[int, List[int]]":
    grouped: "OrderedDict[int, List[int]]" = OrderedDict()
    for assignment in phase:
        grouped.setdefault(assignment.robot_number, []).append(assignment.subtask_id)
    return grouped


def _normalize_robot_number(robots: Sequence[Any], robot_number: int) -> int:
    if 1 <= robot_number <= len(robots):
        return robot_number
    if robots:
        return 1
    return robot_number


def _normalize_phase_robot_numbers(
    robots: Sequence[Any],
    phases: Sequence[Sequence[SubtaskAssignment]],
) -> List[List[SubtaskAssignment]]:
    return [
        [
            SubtaskAssignment(
                subtask_id=assignment.subtask_id,
                robot_number=_normalize_robot_number(robots, assignment.robot_number),
            )
            for assignment in phase
        ]
        for phase in phases
    ]


def _infer_robot_number_from_plan_actions(
    actions: Sequence[EncodedSubtaskAction],
) -> int:
    for encoded in actions:
        if not encoded.pddl.args:
            continue
        match = re.fullmatch(r"robot[_-]?(\d+)", encoded.pddl.args[0], re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def build_task_plan_from_pddlrun_outputs(
    *,
    task: str,
    robots: Sequence[Any],
    allocation_text: str,
    plan_files: Sequence[Any],
    object_names: Optional[Iterable[Any]] = None,
    object_id_bindings: Optional[Any] = None,
    object_id_bindings_by_subtask: Optional[Any] = None,
    task_id: str = "pddlrun",
) -> PddlRunPlanBundle:
    plan_texts = load_plan_texts(plan_files)
    try:
        phases = parse_allocation_phases(allocation_text)
    except PddlRunAdapterError as exc:
        if str(exc) != NO_ALLOCATION_ASSIGNMENTS_ERROR:
            raise
        phases = [
            [
                SubtaskAssignment(subtask_id=subtask_id, robot_number=1)
                for subtask_id in plan_texts
            ]
        ]
    object_names_list = list(object_names or [])
    global_object_id_bindings = _coerce_object_id_bindings(object_id_bindings)
    subtask_object_id_bindings = _coerce_object_id_bindings_by_subtask(
        object_id_bindings_by_subtask
    )
    bundled_object_id_bindings = merge_object_id_bindings(
        global_object_id_bindings,
        subtask_object_id_bindings,
    )
    resolver_by_subtask: Dict[int, ObjectNameResolver] = {}

    def resolver_for_subtask(subtask_id: int) -> ObjectNameResolver:
        resolver = resolver_by_subtask.get(subtask_id)
        if resolver is None:
            resolver = ObjectNameResolver(
                object_names_list,
                [
                    *global_object_id_bindings,
                    *subtask_object_id_bindings.get(subtask_id, []),
                ],
            )
            resolver_by_subtask[subtask_id] = resolver
        return resolver

    encoded_by_subtask: Dict[int, List[EncodedSubtaskAction]] = {}
    for subtask_id, (_path, plan_text) in plan_texts.items():
        actions = parse_plan_actions(plan_text)
        resolver = resolver_for_subtask(subtask_id)
        encoded_by_subtask[subtask_id] = [
            encode_plan_action(action, resolver)
            for action in actions
        ]

    assigned_subtasks = {
        assignment.subtask_id
        for phase in phases
        for assignment in phase
    }
    planned_subtasks = set(encoded_by_subtask)
    executable_subtasks = {
        subtask_id
        for subtask_id, actions in encoded_by_subtask.items()
        if actions
    }
    missing_plans = sorted(assigned_subtasks - planned_subtasks)
    unassigned_plans = sorted(executable_subtasks - assigned_subtasks)
    filtered_phases: List[List[SubtaskAssignment]] = []
    for phase in phases:
        filtered_phase = [
            assignment
            for assignment in phase
            if assignment.subtask_id in executable_subtasks
        ]
        if filtered_phase:
            filtered_phases.append(filtered_phase)

    if not filtered_phases and missing_plans:
        raise PddlRunAdapterError(
            "Allocation references no subtask(s) with planner output; "
            f"missing planner output for allocated subtask(s): {missing_plans}"
        )
    if not executable_subtasks:
        raise PddlRunAdapterError("No executable actions found in planner outputs.")
    if unassigned_plans:
        filtered_phases.append(
            [
                SubtaskAssignment(
                    subtask_id=subtask_id,
                    robot_number=_infer_robot_number_from_plan_actions(
                        encoded_by_subtask[subtask_id]
                    ),
                )
                for subtask_id in unassigned_plans
            ]
        )

    filtered_phases = _normalize_phase_robot_numbers(robots, filtered_phases)

    stages: List[StagePlan] = []
    for phase_index, phase in enumerate(filtered_phases, start=1):
        queues: Dict[str, List[Action]] = OrderedDict()
        for robot_number, subtask_ids in _group_phase_by_robot(phase).items():
            robot_id = _robot_id_for_number(robots, robot_number)
            actions: List[Action] = []
            for subtask_id in subtask_ids:
                actions.extend(
                    encoded.action.with_robot(robot_id)
                    for encoded in encoded_by_subtask[subtask_id]
                )
            if actions:
                queues[robot_id] = actions
        if queues:
            stages.append(StagePlan(f"Phase {phase_index}", dict(queues)))

    if not stages:
        raise PddlRunAdapterError("No executable actions found in planner outputs.")

    no_trans = sum(
        len(actions)
        for actions in encoded_by_subtask.values()
    )
    object_mappings: Dict[str, str] = {}
    object_mapping_warnings: List[str] = []
    seen_warnings = set()
    for resolver in resolver_by_subtask.values():
        object_mappings.update(resolver.mappings)
        for warning in resolver.warnings:
            if warning in seen_warnings:
                continue
            seen_warnings.add(warning)
            object_mapping_warnings.append(warning)

    return PddlRunPlanBundle(
        task=task,
        task_plan=TaskPlan(task_id, stages),
        no_trans=no_trans,
        phases=filtered_phases,
        plan_files={subtask_id: path for subtask_id, (path, _text) in plan_texts.items()},
        object_mappings=object_mappings,
        object_mapping_warnings=object_mapping_warnings,
        object_id_bindings=bundled_object_id_bindings,
    )


def build_task_plan_from_pddlrun_paths(
    *,
    task: str,
    robots: Sequence[Any],
    allocate_file: Any,
    plan_folder: Optional[Any],
    plan_files: Optional[Sequence[Any]] = None,
    object_names: Optional[Iterable[Any]] = None,
    object_id_bindings: Optional[Any] = None,
    object_id_bindings_by_subtask: Optional[Any] = None,
    task_id: str = "pddlrun",
) -> PddlRunPlanBundle:
    allocation_text = load_allocation_text(allocate_file)
    resolved_plan_files = resolve_plan_files(plan_folder, plan_files)
    if object_id_bindings is None and object_id_bindings_by_subtask is None:
        discovered_bindings, discovered_bindings_by_subtask = (
            discover_object_id_binding_artifacts(allocate_file)
        )
        object_id_bindings = discovered_bindings
        object_id_bindings_by_subtask = discovered_bindings_by_subtask
    return build_task_plan_from_pddlrun_outputs(
        task=task,
        robots=robots,
        allocation_text=allocation_text,
        plan_files=resolved_plan_files,
        object_names=object_names,
        object_id_bindings=object_id_bindings,
        object_id_bindings_by_subtask=object_id_bindings_by_subtask,
        task_id=task_id,
    )
