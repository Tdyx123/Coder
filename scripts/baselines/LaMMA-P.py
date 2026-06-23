#!/usr/bin/env python3
"""Convert LaMMA-P final matched plans into executor code artifacts."""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import sys
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _path in (SCRIPTS_DIR, REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from parallel_plan_to_code import ObjectNameResolver, load_object_names
from run_config import normalize_floor_plan


DEFAULT_LOGS_DIR = (
    REPO_ROOT
    / "baselines"
    / "LaMMA-P"
    / "logs"
    / "intermediate_runs"
    / "final_test_new_0609_1"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "baselines" / "LaMMA-P" / "final_plan_to_code"

CONVERTIBLE_CATEGORIES = {
    "timed_direct_actions",
    "timed_start_end_actions",
    "flat_direct_actions",
    "domain_with_custom_at_schedule",
}
SKIPPED_CATEGORIES = {
    "domain_or_durative_action_defs",
    "domain_problem_dump",
}


class FinalPlanEncodingError(RuntimeError):
    """Raised when a final plan cannot be converted into code safely."""


@dataclass(frozen=True)
class RawAction:
    time_value: float
    time_label: str
    order: int
    action_type: str
    raw_args: Tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class EncodedAction:
    time_value: float
    time_label: str
    order: int
    robot_id: str
    action_type: str
    args: Tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class ParseResult:
    category: str
    payload: str
    actions: Tuple[RawAction, ...]
    skip_reason: str = ""


ACTION_ALIASES = {
    "goto": "GoToObject",
    "gotoobject": "GoToObject",
    "pickup": "PickupObject",
    "pickupobject": "PickupObject",
    "put": "PutObject",
    "putobject": "PutObject",
    "place": "PutObject",
    "open": "OpenObject",
    "openobject": "OpenObject",
    "close": "CloseObject",
    "closeobject": "CloseObject",
    "break": "BreakObject",
    "breakobject": "BreakObject",
    "slice": "SliceObject",
    "sliceobject": "SliceObject",
    "switchon": "SwitchOn",
    "toggleon": "SwitchOn",
    "switchoff": "SwitchOff",
    "toggleoff": "SwitchOff",
    "clean": "CleanObject",
    "wash": "CleanObject",
    "cleanobject": "CleanObject",
    "dirty": "DirtyObject",
    "dirtyobject": "DirtyObject",
    "emptyliquid": "EmptyLiquid",
    "emptyliquidfromobject": "EmptyLiquid",
    "prepareegg": "PrepareEgg",
    "runmicrowave": "RunMicrowave",
    "microwave": "RunMicrowave",
    "runcoffeemachine": "RunCoffeeMachine",
    "makecoffee": "RunCoffeeMachine",
    "runtoaster": "RunToaster",
    "toast": "RunToaster",
    "cookbystoveburner": "CookByStoveBurner",
    "cook": "CookByStoveBurner",
    "heatbystoveburner": "HeatByStoveBurner",
    "heatby": "HeatByStoveBurner",
    "heat": "HeatByStoveBurner",
    "firebystoveburner": "FireByStoveBurner",
    "fillwater": "FillWater",
    "fill": "FillWater",
    "coldobject": "ColdObject",
    "cold": "ColdObject",
    "cool": "ColdObject",
    "throwobject": "ThrowObject",
    "throw": "ThrowObject",
}

ACTION_PREFIXES = [
    ("gotoobject", "GoToObject"),
    ("goto", "GoToObject"),
    ("pickupobject", "PickupObject"),
    ("pickup", "PickupObject"),
    ("putobject", "PutObject"),
    ("put", "PutObject"),
    ("place", "PutObject"),
    ("openobject", "OpenObject"),
    ("open", "OpenObject"),
    ("closeobject", "CloseObject"),
    ("close", "CloseObject"),
    ("breakobject", "BreakObject"),
    ("break", "BreakObject"),
    ("sliceobject", "SliceObject"),
    ("slice", "SliceObject"),
    ("switchon", "SwitchOn"),
    ("switchoff", "SwitchOff"),
    ("cleanobject", "CleanObject"),
    ("clean", "CleanObject"),
    ("wash", "CleanObject"),
    ("dirtyobject", "DirtyObject"),
    ("dirty", "DirtyObject"),
    ("prepareegg", "PrepareEgg"),
    ("runmicrowave", "RunMicrowave"),
    ("microwave", "RunMicrowave"),
    ("runcoffeemachine", "RunCoffeeMachine"),
    ("makecoffee", "RunCoffeeMachine"),
    ("runtoaster", "RunToaster"),
    ("toast", "RunToaster"),
    ("cookbystoveburner", "CookByStoveBurner"),
    ("cook", "CookByStoveBurner"),
    ("heatbystoveburner", "HeatByStoveBurner"),
    ("heatby", "HeatByStoveBurner"),
    ("heat", "HeatByStoveBurner"),
    ("fillwater", "FillWater"),
    ("fill", "FillWater"),
    ("coldobject", "ColdObject"),
    ("cold", "ColdObject"),
    ("cool", "ColdObject"),
    ("throwobject", "ThrowObject"),
    ("throw", "ThrowObject"),
]


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def canonical_action_name(action_name: str) -> Optional[str]:
    key = normalized_key(action_name)
    if key in ACTION_ALIASES:
        return ACTION_ALIASES[key]
    for prefix, canonical in ACTION_PREFIXES:
        if key.startswith(prefix):
            return canonical
    return None


def sanitize_slug(value: str, max_length: int = 96) -> str:
    slug = re.sub(r'[<>:"/\\|?*\s]+', "_", str(value).strip())
    slug = slug.strip("._")
    if not slug:
        slug = "task"
    return slug[:max_length].rstrip("._") or "task"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_plan_payload(text: str) -> str:
    blocks = re.findall(
        r"```(?:[A-Za-z0-9_-]+)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL,
    )
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def strip_line_comment(line: str) -> str:
    return line.split(";", 1)[0].strip()


def has_domain_markers(payload: str) -> bool:
    lowered = payload.lower()
    return any(
        marker in lowered
        for marker in (
            ":objects",
            ":init",
            ":goal",
            ":durative-action",
            "durative-action ",
            "(:action",
        )
    )


def action_from_inner(
    inner: str,
    time_value: float,
    time_label: str,
    raw: str,
    order: int,
    *,
    allow_start_end: bool = False,
) -> Tuple[Optional[RawAction], bool]:
    parts = inner.strip().split()
    if not parts:
        return None, False

    if normalized_key(parts[0]) in {"start", "end"}:
        if not allow_start_end or len(parts) < 2:
            return None, False
        if normalized_key(parts[0]) == "end":
            return None, True
        action_type = canonical_action_name(parts[1])
        if action_type is None:
            return None, True
        return (
            RawAction(
                time_value=time_value,
                time_label=time_label,
                order=order,
                action_type=action_type,
                raw_args=tuple(parts[2:]),
                raw=raw,
            ),
            True,
        )

    action_type = canonical_action_name(parts[0])
    if action_type is None:
        return None, False
    return (
        RawAction(
            time_value=time_value,
            time_label=time_label,
            order=order,
            action_type=action_type,
            raw_args=tuple(parts[1:]),
            raw=raw,
        ),
        False,
    )


def parse_timed_actions(payload: str) -> Tuple[List[RawAction], bool]:
    actions: List[RawAction] = []
    saw_start_end = False
    timed_re = re.compile(
        r"^\(?\s*(?P<time>\d+(?:\.\d+)?)\s*:\s*"
        r"\((?P<inner>[^()]*)\)\s*"
        r"(?:\[[^\]]+\])?\s*\)?\s*$",
        re.IGNORECASE,
    )
    for order, raw_line in enumerate(payload.splitlines()):
        line = strip_line_comment(raw_line)
        if not line:
            continue
        match = timed_re.match(line)
        if not match:
            continue
        time_label = match.group("time")
        action, is_start_end = action_from_inner(
            match.group("inner"),
            float(time_label),
            time_label,
            line,
            order,
            allow_start_end=True,
        )
        saw_start_end = saw_start_end or is_start_end
        if action is not None:
            actions.append(action)
    return actions, saw_start_end


def parse_flat_actions(payload: str) -> List[RawAction]:
    actions: List[RawAction] = []
    flat_re = re.compile(r"^\(\s*(?P<inner>[^()]*)\)\s*$")
    for order, raw_line in enumerate(payload.splitlines()):
        line = strip_line_comment(raw_line)
        if not line:
            continue
        match = flat_re.match(line)
        if not match:
            continue
        first = match.group("inner").strip().split(maxsplit=1)[0]
        if normalized_key(first) in {"define", "at"}:
            continue
        action, _ = action_from_inner(
            match.group("inner"),
            float(order),
            str(order),
            line,
            order,
        )
        if action is not None:
            actions.append(action)
    return actions


def parse_custom_at_schedule(payload: str) -> List[RawAction]:
    actions: List[RawAction] = []
    at_re = re.compile(
        r"^\(at\s+(?P<time>\d+(?:\.\d+)?)\s+"
        r"\((?P<inner>[^()]*)\)\)\s*$",
        re.IGNORECASE,
    )
    for order, raw_line in enumerate(payload.splitlines()):
        line = strip_line_comment(raw_line)
        if not line:
            continue
        match = at_re.match(line)
        if not match:
            continue
        time_label = match.group("time")
        action, _ = action_from_inner(
            match.group("inner"),
            float(time_label),
            time_label,
            line,
            order,
        )
        if action is not None:
            actions.append(action)
    return actions


def classify_final_plan(text: str) -> ParseResult:
    payload = extract_plan_payload(text)
    timed_actions, saw_start_end = parse_timed_actions(payload)
    direct_timed_actions = [
        action for action in timed_actions
        if not re.match(
            r"^\(?\s*\d+(?:\.\d+)?\s*:\s*\(\s*(?:start|end)\b",
            action.raw,
            re.IGNORECASE,
        )
    ]

    if direct_timed_actions:
        return ParseResult(
            category="timed_direct_actions",
            payload=payload,
            actions=tuple(timed_actions),
        )
    if saw_start_end and timed_actions:
        return ParseResult(
            category="timed_start_end_actions",
            payload=payload,
            actions=tuple(timed_actions),
        )

    domain_markers = has_domain_markers(payload)
    custom_at_actions = parse_custom_at_schedule(payload)
    if domain_markers and custom_at_actions:
        return ParseResult(
            category="domain_with_custom_at_schedule",
            payload=payload,
            actions=tuple(custom_at_actions),
        )

    flat_actions = parse_flat_actions(payload)
    if flat_actions and not domain_markers:
        return ParseResult(
            category="flat_direct_actions",
            payload=payload,
            actions=tuple(flat_actions),
        )

    if domain_markers:
        category = (
            "domain_problem_dump"
            if any(marker in payload.lower() for marker in (":objects", ":init", ":goal"))
            else "domain_or_durative_action_defs"
        )
        return ParseResult(
            category=category,
            payload=payload,
            actions=(),
            skip_reason=f"{category} is not converted by design",
        )

    return ParseResult(
        category="unparsed",
        payload=payload,
        actions=(),
        skip_reason="no supported final-plan action structure found",
    )


def is_robot_token(token: str) -> bool:
    return re.fullmatch(r"robot\d*", normalized_key(token)) is not None


def split_robot_arg(args: Sequence[str]) -> Tuple[str, List[str]]:
    if args and is_robot_token(args[0]):
        key = normalized_key(args[0])
        match = re.search(r"(\d+)$", key)
        robot_id = f"robot{match.group(1)}" if match else "robot1"
        return robot_id, list(args[1:])
    return "robot1", list(args)


def strip_variable_marker(token: str) -> str:
    return str(token).strip().lstrip("?")


def contains_any(token: str, needles: Iterable[str]) -> bool:
    key = normalized_key(token)
    return any(needle in key for needle in needles)


def split_source_object(
    tokens: Sequence[str],
    source_needles: Sequence[str],
    action_name: str,
) -> Tuple[str, str]:
    if len(tokens) < 2:
        raise FinalPlanEncodingError(f"{action_name} requires source and object arguments.")
    if contains_any(tokens[0], source_needles):
        return tokens[0], tokens[1]
    if contains_any(tokens[1], source_needles):
        return tokens[1], tokens[0]
    return tokens[0], tokens[1]


def split_machine_object(
    tokens: Sequence[str],
    machine_needles: Sequence[str],
    action_name: str,
) -> Tuple[str, str]:
    return split_source_object(tokens, machine_needles, action_name)


def split_cook_args(tokens: Sequence[str]) -> Tuple[str, str, str]:
    if len(tokens) < 3:
        raise FinalPlanEncodingError(
            "CookByStoveBurner requires stove burner, container, and food arguments."
        )
    source_index = next(
        (
            index for index, token in enumerate(tokens)
            if contains_any(token, ("stove", "burner"))
        ),
        0,
    )
    source = tokens[source_index]
    remaining = [token for index, token in enumerate(tokens) if index != source_index]
    if source_index == len(tokens) - 1 and len(remaining) >= 2:
        food, container = remaining[0], remaining[1]
    else:
        container, food = remaining[0], remaining[1]
    return source, container, food


def action_object_args(action_type: str, tokens: Sequence[str]) -> Tuple[str, ...]:
    tokens = [strip_variable_marker(token) for token in tokens if strip_variable_marker(token)]
    if action_type == "GoToObject":
        if not tokens:
            raise FinalPlanEncodingError("GoToObject requires one object argument.")
        return (tokens[0],)
    if action_type == "PickupObject":
        if not tokens:
            raise FinalPlanEncodingError("PickupObject requires one object argument.")
        return (tokens[0],)
    if action_type == "PutObject":
        if len(tokens) < 2:
            raise FinalPlanEncodingError("PutObject requires object and receptacle arguments.")
        return (tokens[0], tokens[1])
    if action_type in {
        "OpenObject",
        "CloseObject",
        "BreakObject",
        "SwitchOn",
        "SwitchOff",
        "SliceObject",
        "CleanObject",
        "DirtyObject",
        "EmptyLiquid",
        "PrepareEgg",
    }:
        if not tokens:
            raise FinalPlanEncodingError(f"{action_type} requires one object argument.")
        return (tokens[0],)
    if action_type == "RunMicrowave":
        return split_machine_object(tokens, ("microwave",), action_type)
    if action_type == "RunCoffeeMachine":
        return split_machine_object(tokens, ("coffee", "coffeemachine"), action_type)
    if action_type == "RunToaster":
        return split_machine_object(tokens, ("toaster",), action_type)
    if action_type == "CookByStoveBurner":
        return split_cook_args(tokens)
    if action_type in {"HeatByStoveBurner", "FireByStoveBurner"}:
        return split_source_object(tokens, ("stove", "burner"), action_type)
    if action_type == "FillWater":
        return split_source_object(tokens, ("sink", "faucet"), action_type)
    if action_type == "ColdObject":
        return split_source_object(tokens, ("fridge", "refrigerator"), action_type)
    if action_type == "ThrowObject":
        return ()
    raise FinalPlanEncodingError(f"Unsupported action type: {action_type}")


def encode_actions(
    actions: Sequence[RawAction],
    resolver: ObjectNameResolver,
    robots: Sequence[Dict[str, Any]],
) -> List[EncodedAction]:
    encoded: List[EncodedAction] = []
    robot_names = {
        str(robot.get("name") or f"robot{index + 1}")
        for index, robot in enumerate(robots)
        if isinstance(robot, dict)
    }

    for action in actions:
        robot_id, object_tokens = split_robot_arg(action.raw_args)
        if robot_names and robot_id not in robot_names:
            raise FinalPlanEncodingError(
                f"{action.raw!r} references {robot_id}, but task has robots {sorted(robot_names)}."
            )
        object_args = action_object_args(action.action_type, object_tokens)
        resolved_args = tuple(resolver.resolve(token) for token in object_args)
        encoded.append(
            EncodedAction(
                time_value=action.time_value,
                time_label=action.time_label,
                order=action.order,
                robot_id=robot_id,
                action_type=action.action_type,
                args=resolved_args,
                raw=action.raw,
            )
        )

    return sorted(encoded, key=lambda item: (item.time_value, item.order))


def build_task_plan_data(
    task_id: str,
    encoded_actions: Sequence[EncodedAction],
) -> Dict[str, Any]:
    stages: List[Dict[str, Any]] = []
    current_robot_id: Optional[str] = None
    current_actions: List[Dict[str, Any]] = []

    def flush_stage() -> None:
        nonlocal current_robot_id, current_actions
        if current_robot_id is None or not current_actions:
            return
        stages.append(
            {
                "stage_id": f"Robot Segment {len(stages) + 1}",
                "robot_action_queues": {current_robot_id: current_actions},
            }
        )
        current_robot_id = None
        current_actions = []

    for action in sorted(encoded_actions, key=lambda item: (item.time_value, item.order)):
        if current_robot_id is None:
            current_robot_id = action.robot_id
        elif action.robot_id != current_robot_id:
            flush_stage()
            current_robot_id = action.robot_id

        current_actions.append(
            {
                "action_type": action.action_type,
                "parameters": {"args": list(action.args)},
                "robot_id": action.robot_id,
            }
        )

    flush_stage()

    return {"task_id": task_id, "stages": stages}


def render_code_plan(
    task: str,
    category: str,
    task_plan_data: Dict[str, Any],
    encoded_actions: Sequence[EncodedAction],
) -> str:
    raw_actions = [
        {
            "time": action.time_label,
            "robot_id": action.robot_id,
            "action_type": action.action_type,
            "args": list(action.args),
            "raw": action.raw,
        }
        for action in encoded_actions
    ]
    return f'''#!/usr/bin/env python3
"""TaskPlan data generated from a LaMMA-P final matched plan."""

from __future__ import annotations

import sys
from pathlib import Path
from pprint import pprint


REPO_ROOT = Path({str(REPO_ROOT)!r})
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in (SCRIPTS_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from executor_system.action_plan import TaskPlan


TASK = {task!r}
CATEGORY = {category!r}
RAW_ACTIONS = {pformat(raw_actions, width=100, sort_dicts=False)}
TASK_PLAN_DATA = {pformat(task_plan_data, width=100, sort_dicts=False)}


task_plan = TaskPlan.from_dict(TASK_PLAN_DATA)


if __name__ == "__main__":
    pprint(TASK_PLAN_DATA)
'''


def render_executable_plan(
    *,
    task: str,
    task_plan_data: Dict[str, Any],
    robots: Sequence[Dict[str, Any]],
    floor_plan: str,
    ground_truth: Sequence[Dict[str, Any]],
    gpu_device: Optional[int] = None,
) -> str:
    return f'''#!/usr/bin/env python3
"""Run a LaMMA-P final-plan TaskPlan through executor_system."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional, Sequence


REPO_ROOT = Path({str(REPO_ROOT)!r})
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in (SCRIPTS_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system.action_plan import TaskPlan
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.runtime import ThorRuntime
from executor_system.task_plan import run_action_plan


TASK = {task!r}
TASK_PLAN_DATA = {pformat(task_plan_data, width=100, sort_dicts=False)}
ROBOTS = {pformat(list(robots), width=100, sort_dicts=False)}
FLOOR_NO = {normalize_floor_plan(floor_plan)!r}
GROUND_TRUTH = {pformat(list(ground_truth), width=100, sort_dicts=False)}
GPU_DEVICE = {gpu_device!r}


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generated LaMMA-P final-plan code.")
    parser.add_argument("--gpu-device", type=int, default=GPU_DEVICE)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    _demo_state.set_ground_truth(GROUND_TRUTH)
    runtime = ThorRuntime(
        ROBOTS,
        FLOOR_NO,
        CLOUD_RENDERING,
        RENDER_IMAGE,
        gpu_device=args.gpu_device,
    )
    _context.runtime = runtime
    start_time = time.monotonic()
    try:
        run_action_plan(TaskPlan.from_dict(TASK_PLAN_DATA))
        runtime.step({{"action": "Done"}}, check_success=False)
        metrics = runtime.evaluate(GROUND_TRUTH)
        print(
            "TC:{{tc}}, GCR:{{gcr}}, Exec:{{exec_rate}}, Runtime:{{runtime:.2f}}s".format(
                tc=int(metrics["tc"]),
                gcr=metrics["gcr"],
                exec_rate=metrics["exec_rate"],
                runtime=time.monotonic() - start_time,
            )
        )
        runtime.generate_video()
        runtime.write_final_metadata()
        return 0
    finally:
        runtime.stop()
        _context.runtime = None


if __name__ == "__main__":
    raise SystemExit(main())
'''


def task_run_key(path: Path) -> str:
    parts = path.resolve().parts
    marker = ("logs", "intermediate_runs")
    for index in range(len(parts) - 1):
        if tuple(parts[index:index + 2]) == marker:
            return "/".join(parts[index:])
    return str(path.resolve())


def load_parallel_metadata(baseline_root: Path) -> Dict[str, Dict[str, Any]]:
    metadata: Dict[str, Dict[str, Any]] = {}
    for summary_path in sorted((baseline_root / "parallel_runs").rglob("summary.json")):
        data = read_json(summary_path, default={})
        if not isinstance(data, dict):
            continue
        floor_plan = data.get("floor_plan")
        for result in data.get("results") or []:
            if not isinstance(result, dict):
                continue
            raw_run_dir = result.get("task_run_dir")
            if not raw_run_dir:
                continue
            record = dict(result)
            if floor_plan and not record.get("floor_plan"):
                record["floor_plan"] = floor_plan
            metadata[task_run_key(Path(str(raw_run_dir)))] = record
    return metadata


def infer_test_set(task_run_dir: Path, manifest: Dict[str, Any]) -> Optional[str]:
    if manifest.get("test_set"):
        return str(manifest["test_set"])
    parts = task_run_dir.parts
    for index, part in enumerate(parts):
        if part == "intermediate_runs" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def load_dataset_record(
    repo_root: Path,
    test_set: Optional[str],
    floor_plan: Optional[str],
    task_index: Optional[int],
) -> Dict[str, Any]:
    if not test_set or not floor_plan or task_index is None:
        return {}
    path = repo_root / "data" / test_set / f"FloorPlan{normalize_floor_plan(floor_plan)}.jsonl"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index == task_index and raw_line.strip():
                return json.loads(raw_line)
    return {}


def discover_task_runs(logs_dir: Path) -> List[Path]:
    return sorted(path.parent.parent for path in logs_dir.rglob("08_final_match/02_final_plan.txt"))


def output_dir_for_run(output_root: Path, task_run_dir: Path) -> Path:
    task_slug = sanitize_slug(task_run_dir.parent.name)
    run_id = sanitize_slug(task_run_dir.name)
    return output_root / task_slug / run_id


def process_task_run(
    task_run_dir: Path,
    output_root: Path,
    parallel_metadata: Dict[str, Dict[str, Any]],
    *,
    dry_run: bool = False,
    validate_code: bool = True,
) -> Dict[str, Any]:
    started_at = time.time()
    output_dir = output_dir_for_run(output_root, task_run_dir)
    final_plan_path = task_run_dir / "08_final_match" / "02_final_plan.txt"
    manifest = read_json(task_run_dir / "run_manifest.json", default={}) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    task_context = read_json(task_run_dir / "inputs" / "task_context.json", default={}) or {}
    if not isinstance(task_context, dict):
        task_context = {}
    metadata = parallel_metadata.get(task_run_key(task_run_dir), {})

    task = str(
        metadata.get("task")
        or manifest.get("task")
        or task_context.get("task")
        or task_run_dir.parent.name
    )
    floor_plan = str(
        metadata.get("floor_plan")
        or manifest.get("floor_plan")
        or task_context.get("floor_plan")
        or ""
    )
    raw_task_index = metadata.get("task_index", manifest.get("task_index"))
    try:
        task_index = int(raw_task_index) if raw_task_index is not None else None
    except (TypeError, ValueError):
        task_index = None
    test_set = infer_test_set(task_run_dir, manifest)

    result: Dict[str, Any] = {
        "task": task,
        "task_run_dir": str(task_run_dir),
        "output_dir": str(output_dir),
        "floor_plan": floor_plan or None,
        "task_index": task_index,
        "test_set": test_set,
        "status": "skipped",
        "category": None,
        "skip_reason": "",
        "action_count": 0,
        "stage_count": 0,
        "object_mappings": {},
        "object_mapping_warnings": [],
    }

    try:
        parse_result = classify_final_plan(final_plan_path.read_text(encoding="utf-8"))
        result["category"] = parse_result.category
        if parse_result.category not in CONVERTIBLE_CATEGORIES:
            result["skip_reason"] = parse_result.skip_reason or (
                f"{parse_result.category} is not convertible"
            )
            if not dry_run:
                write_json(output_dir / "encoding_summary.json", result)
            return result

        robots = task_context.get("robots")
        if not isinstance(robots, list) or not robots:
            raise FinalPlanEncodingError("inputs/task_context.json is missing a robot list.")

        object_names = load_object_names(REPO_ROOT, floor_plan or "", task_context)
        resolver = ObjectNameResolver(object_names)
        encoded_actions = encode_actions(parse_result.actions, resolver, robots)
        if not encoded_actions:
            raise FinalPlanEncodingError("final plan category was convertible but no actions were encoded.")

        task_id = f"lammap_{normalize_floor_plan(floor_plan) if floor_plan else 'unknown'}_{task_index if task_index is not None else 'task'}"
        task_plan_data = build_task_plan_data(task_id, encoded_actions)
        ground_truth: List[Dict[str, Any]] = []
        dataset_record = load_dataset_record(REPO_ROOT, test_set, floor_plan or None, task_index)
        if isinstance(dataset_record.get("object_states"), list):
            ground_truth = list(dataset_record["object_states"])

        code_plan = render_code_plan(task, parse_result.category, task_plan_data, encoded_actions)
        executable_plan = render_executable_plan(
            task=task,
            task_plan_data=task_plan_data,
            robots=robots,
            floor_plan=floor_plan,
            ground_truth=ground_truth,
        )

        compile(code_plan, "code_plan.py", "exec")
        compile(executable_plan, "executable_plan.py", "exec")

        generated = {
            "code_plan": str(output_dir / "code_plan.py"),
            "executable_plan": str(output_dir / "executable_plan.py"),
        }
        result.update(
            {
                "status": "success",
                "skip_reason": "",
                "action_count": len(encoded_actions),
                "stage_count": len(task_plan_data["stages"]),
                "object_mappings": dict(resolver.mappings),
                "object_mapping_warnings": list(resolver.warnings),
                "generated": generated,
            }
        )
        if not floor_plan:
            result.setdefault("warnings", []).append("missing_floor_plan")

        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "code_plan.py").write_text(code_plan, encoding="utf-8")
            (output_dir / "executable_plan.py").write_text(executable_plan, encoding="utf-8")
            if validate_code:
                py_compile.compile(str(output_dir / "code_plan.py"), doraise=True)
                py_compile.compile(str(output_dir / "executable_plan.py"), doraise=True)
            write_json(output_dir / "encoding_summary.json", result)
        return result
    except Exception as exc:
        result.update({"status": "skipped", "skip_reason": str(exc)})
        if not dry_run:
            write_json(output_dir / "encoding_summary.json", result)
        return result
    finally:
        result["generation_time"] = time.time() - started_at


def write_global_summary(results: Sequence[Dict[str, Any]], output_root: Path, dry_run: bool) -> None:
    counts = Counter(str(result.get("category") or "unknown") for result in results)
    status_counts = Counter(str(result.get("status") or "unknown") for result in results)
    summary = {
        "total_runs": len(results),
        "successful_generations": status_counts.get("success", 0),
        "skipped_generations": status_counts.get("skipped", 0),
        "category_counts": dict(sorted(counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "results": list(results),
    }
    if not dry_run:
        write_json(output_root / "final_plan_to_code_summary.json", summary)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify and convert LaMMA-P 08_final_match/02_final_plan.txt files."
    )
    parser.add_argument("--logs-dir", default=str(DEFAULT_LOGS_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Only process a category. Can be supplied multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--validate-code",
        action="store_true",
        default=True,
        help="Compile generated code files after writing them (default: true).",
    )
    parser.add_argument(
        "--no-validate-code",
        dest="validate_code",
        action="store_false",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    logs_dir = Path(args.logs_dir).expanduser()
    output_root = Path(args.output_dir).expanduser()
    if not logs_dir.is_dir():
        print(f"ERROR: logs directory not found: {logs_dir}")
        return 1

    baseline_root = output_root.parent if output_root.name == "final_plan_to_code" else REPO_ROOT / "baselines" / "LaMMA-P"
    metadata = load_parallel_metadata(baseline_root)
    task_run_dirs = discover_task_runs(logs_dir)
    results: List[Dict[str, Any]] = []
    selected_categories = set(args.category or [])

    for task_run_dir in task_run_dirs:
        if args.limit is not None and len(results) >= args.limit:
            break
        if selected_categories:
            try:
                category = classify_final_plan(
                    (task_run_dir / "08_final_match" / "02_final_plan.txt").read_text(
                        encoding="utf-8"
                    )
                ).category
            except Exception:
                category = "unparsed"
            if category not in selected_categories:
                continue
        result = process_task_run(
            task_run_dir,
            output_root,
            metadata,
            dry_run=args.dry_run,
            validate_code=args.validate_code,
        )
        results.append(result)
        marker = "OK" if result.get("status") == "success" else "SKIP"
        print(f"[{marker}] {result.get('category')}: {task_run_dir}")

    write_global_summary(results, output_root, args.dry_run)
    success_count = sum(1 for result in results if result.get("status") == "success")
    print(f"Processed {len(results)} run(s); generated {success_count} code bundle(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
