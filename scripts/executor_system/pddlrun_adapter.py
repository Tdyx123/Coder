"""Adapt pddlrun_llmseparate allocation and planner outputs to TaskPlan."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
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
    gpu_device: Optional[int] = None


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
    "sliceobject": "SliceObject",
    "cleanobject": "CleanObject",
    "dirtyobject": "DirtyObject",
    "runmicrowave": "RunMicrowave",
    "runcoffeemachine": "RunCoffeeMachine",
    "runtoaster": "RunToaster",
    "cookbystoveburner": "CookByStoveBurner",
    "heatbystoveburner": "HeatByStoveBurner",
    "firebystoveburner": "FireByStoveBurner",
    "fillwater": "FillWater",
    "coldobject": "ColdObject",
    "throwobject": "ThrowObject",
}

SUPPORTED_ACTIONS = set(ACTION_ALIASES.values())


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
    """Map PDDL object tokens to AI2-THOR object type names."""

    def __init__(self, object_names: Optional[Iterable[Any]] = None) -> None:
        self._by_key: Dict[str, str] = {}
        self.mappings: Dict[str, str] = {}
        self.warnings: List[str] = []

        for raw_name in object_names or ():
            name = self._name_from_any(raw_name)
            if not name:
                continue
            key = object_key(name)
            if key and key not in self._by_key:
                self._by_key[key] = name

    def _name_from_any(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("name") or value.get("objectType") or value.get("objectId")
        return str(value) if value else ""

    def resolve(self, token: str) -> str:
        token = str(token).strip()
        key = object_key(token)
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
        raise PddlRunAdapterError(
            "No subtask-to-robot assignments found in allocation output."
        )

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
    }:
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [obj], [action.args[1]])

    if action.name == "PrepareEgg":
        _require_args(action, 2)
        egg = resolver.resolve(action.args[1])
        return _executor_action(action, action.name, [egg], [action.args[1]])

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


def build_task_plan_from_pddlrun_outputs(
    *,
    task: str,
    robots: Sequence[Any],
    allocation_text: str,
    plan_files: Sequence[Any],
    object_names: Optional[Iterable[Any]] = None,
    task_id: str = "pddlrun",
    gpu_device: Optional[int] = None,
) -> PddlRunPlanBundle:
    phases = parse_allocation_phases(allocation_text)
    plan_texts = load_plan_texts(plan_files)
    resolver = ObjectNameResolver(object_names)

    encoded_by_subtask: Dict[int, List[EncodedSubtaskAction]] = {}
    for subtask_id, (_path, plan_text) in plan_texts.items():
        actions = parse_plan_actions(plan_text)
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
    missing_plans = sorted(assigned_subtasks - planned_subtasks)
    unassigned_plans = sorted(planned_subtasks - assigned_subtasks)
    if missing_plans:
        raise PddlRunAdapterError(
            f"Allocation references subtask(s) without planner output: {missing_plans}"
        )
    if unassigned_plans:
        raise PddlRunAdapterError(
            f"Planner output contains unassigned subtask(s): {unassigned_plans}"
        )

    stages: List[StagePlan] = []
    for phase_index, phase in enumerate(phases, start=1):
        queues: Dict[str, List[Action]] = OrderedDict()
        for robot_number, subtask_ids in _group_phase_by_robot(phase).items():
            robot_id = _robot_id_for_number(robots, robot_number)
            actions: List[Action] = []
            for subtask_id in subtask_ids:
                actions.extend(
                    encoded.action.with_robot(robot_id)
                    for encoded in encoded_by_subtask[subtask_id]
                )
            queues[robot_id] = actions
        stages.append(StagePlan(f"Phase {phase_index}", dict(queues)))

    no_trans = sum(
        len(actions)
        for actions in encoded_by_subtask.values()
    )
    return PddlRunPlanBundle(
        task=task,
        task_plan=TaskPlan(task_id, stages),
        no_trans=no_trans,
        phases=phases,
        plan_files={subtask_id: path for subtask_id, (path, _text) in plan_texts.items()},
        object_mappings=dict(resolver.mappings),
        object_mapping_warnings=list(resolver.warnings),
        gpu_device=gpu_device,
    )


def build_task_plan_from_pddlrun_paths(
    *,
    task: str,
    robots: Sequence[Any],
    allocate_file: Any,
    plan_folder: Optional[Any],
    plan_files: Optional[Sequence[Any]] = None,
    object_names: Optional[Iterable[Any]] = None,
    task_id: str = "pddlrun",
    gpu_device: Optional[int] = None,
) -> PddlRunPlanBundle:
    allocation_text = load_allocation_text(allocate_file)
    resolved_plan_files = resolve_plan_files(plan_folder, plan_files)
    return build_task_plan_from_pddlrun_outputs(
        task=task,
        robots=robots,
        allocation_text=allocation_text,
        plan_files=resolved_plan_files,
        object_names=object_names,
        task_id=task_id,
        gpu_device=gpu_device,
    )
