#!/usr/bin/env python3
"""Generate executable scripts for every single feasible data_engine subtask."""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, List, Optional, Sequence


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
ROBOT_ID = "robot1"

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


@dataclass(frozen=True)
class EnumeratedSubtask:
    floor_plan: int
    subtask: Dict[str, Any]


@dataclass(frozen=True)
class GeneratedSubtask:
    floor_plan: int
    floor_index: int
    global_index: int
    subtask: Dict[str, Any]
    task_text: str
    object_states: List[Dict[str, Any]]
    actions: List[Dict[str, Any]]


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


def build_actions_for_subtask(
    subtask: Dict[str, Any],
    skill_sets: Dict[str, Any],
) -> List[Dict[str, Any]]:
    skill = subtask["skill"]
    objects = list(subtask.get("objects", []))

    if skill == "Open":
        obj = objects[0]
        return [action("GoToObject", obj), action("OpenObject", obj)]
    if skill == "SwitchOn":
        obj = objects[0]
        return [action("GoToObject", obj), action("SwitchOn", obj)]
    if skill == "Break":
        obj = objects[0]
        return [action("GoToObject", obj), action("BreakObject", obj)]
    if skill == "Wash":
        obj = objects[0]
        return [
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", "Sink"),
            action("CleanObject", obj),
        ]
    if skill == "Slice":
        obj = objects[0]
        return [
            action("GoToObject", "Knife"),
            action("PickupObject", "Knife"),
            action("GoToObject", obj),
            action("SliceObject", obj),
        ]
    if skill == "PutOn":
        return _put_actions(objects[0], objects[1])
    if skill == "PutIn":
        return _put_actions(objects[0], objects[1])
    if skill == "RunMicrowave":
        obj, microwave = objects
        return [
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", microwave),
            action("OpenObject", microwave),
            action("PutObject", obj, microwave),
            action("CloseObject", microwave),
            action("RunMicrowave", microwave, obj),
        ]
    if skill == "RunCoffeeMachine":
        mug, coffee_machine = objects
        return [
            action("GoToObject", mug),
            action("PickupObject", mug),
            action("GoToObject", coffee_machine),
            action("PutObject", mug, coffee_machine),
            action("RunCoffeeMachine", coffee_machine, mug),
        ]
    if skill == "RunToaster":
        bread, toaster = objects
        return [
            action("GoToObject", "Knife"),
            action("PickupObject", "Knife"),
            action("GoToObject", bread),
            action("SliceObject", bread),
            action("PickupObject", bread),
            action("GoToObject", toaster),
            action("RunToaster", toaster, bread),
        ]
    if skill == "CookByStoveBurner":
        food, stove_burner = objects
        container = select_stove_container(food, skill_sets)
        return [
            action("GoToObject", food),
            action("PickupObject", food),
            action("GoToObject", container),
            action("PutObject", food, container),
            action("PickupObject", container),
            action("GoToObject", stove_burner),
            action("CookByStoveBurner", stove_burner, container, food),
        ]
    if skill == "PrepareEgg":
        egg, container = objects
        return [
            action("GoToObject", egg),
            action("PickupObject", egg),
            action("GoToObject", container),
            action("PickupObject", container),
            action("PutObject", egg, container),
            action("PrepareEgg", egg),
        ]
    if skill == "CookEgg":
        egg, container = objects
        stove_burner = "StoveBurner"
        return [
            action("GoToObject", egg),
            action("PickupObject", egg),
            action("GoToObject", container),
            action("PickupObject", container),
            action("PutObject", egg, container),
            action("PrepareEgg", egg),
            action("GoToObject", stove_burner),
            action("CookByStoveBurner", stove_burner, container, egg),
        ]
    if skill == "HeatByStoveBurner":
        obj, stove_burner = objects
        return [
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", stove_burner),
            action("HeatByStoveBurner", stove_burner, obj),
        ]
    if skill == "FillWater":
        obj, sink = objects
        return [
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", sink),
            action("FillWater", sink, obj),
        ]
    if skill == "ColdObject":
        obj, fridge = objects
        return [
            action("GoToObject", obj),
            action("PickupObject", obj),
            action("GoToObject", fridge),
            action("OpenObject", fridge),
            action("PutObject", obj, fridge),
            action("CloseObject", fridge),
            action("ColdObject", fridge, obj),
        ]

    raise ValueError(f"Unsupported subtask skill: {skill}")


def build_bundle_data(
    *,
    task_id: str,
    task_text: str,
    actions: List[Dict[str, Any]],
    plan_file: Optional[Path] = None,
) -> Dict[str, Any]:
    return {
        "task": task_text,
        "task_plan": {
            "task_id": task_id,
            "stages": [
                {
                    "stage_id": "Phase 1",
                    "robot_action_queues": {
                        ROBOT_ID: actions,
                    },
                }
            ],
        },
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


def _bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return pformat(bundle_data, width=100, sort_dicts=False)


def render_executable(task_file: Path, task_index: int, bundle_data: Dict[str, Any]) -> str:
    bundle_literal = _bundle_literal(bundle_data)
    code_repo_root = str(REPO_ROOT)
    forced_robots_literal = pformat(
        [
            {
                "name": ROBOT_ID,
                "skills": ALL_GENERATED_EXECUTOR_SKILLS,
                "mass_capacity": 100,
            }
        ],
        width=100,
        sort_dicts=False,
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

TASK_FILE = {str(task_file)!r}
TASK_INDEX = {task_index!r}
DEFAULT_RUNNER_TIMEOUT_SECONDS = 100.0


runtime = None
robots: List[Dict[str, Any]] = []
floor_no = ""
ground_truth: List[Dict[str, Any]] = []


def load_task_record(task_file: str, task_index: int) -> Dict[str, Any]:
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
    floor_no = floor_plan_from_task_file(TASK_FILE)
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
        floor_no = floor_plan_from_task_file(TASK_FILE)
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
    py_compile.compile(str(path), doraise=True)


def prepare_generated_subtasks(
    enumerated: Sequence[EnumeratedSubtask],
    object_properties_path: Path = DEFAULT_OBJECT_PROPERTIES_PATH,
) -> List[GeneratedSubtask]:
    engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
    floor_counts: Dict[int, int] = {}
    skill_sets_by_floor: Dict[int, Dict[str, Any]] = {}
    generated: List[GeneratedSubtask] = []

    for global_index, item in enumerate(enumerated, start=1):
        floor_counts[item.floor_plan] = floor_counts.get(item.floor_plan, 0) + 1
        floor_index = floor_counts[item.floor_plan] - 1
        if item.floor_plan not in skill_sets_by_floor:
            skill_sets_by_floor[item.floor_plan] = data_engine._build_object_skill_sets(
                item.floor_plan,
                object_properties_path,
            )
        skill_sets = skill_sets_by_floor[item.floor_plan]
        actions = build_actions_for_subtask(item.subtask, skill_sets)
        generated.append(
            GeneratedSubtask(
                floor_plan=item.floor_plan,
                floor_index=floor_index,
                global_index=global_index,
                subtask=item.subtask,
                task_text=engine.subtask_to_str(item.subtask),
                object_states=engine.get_task_final_state([item.subtask]),
                actions=actions,
            )
        )

    return generated


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
        help="Skip py_compile validation for generated executable_plan.py files.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Only write manifest, summary, and dataset JSONL files.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    output_dir = Path(args.output_dir).expanduser()
    if args.floor_plans:
        floor_plans = [normalize_floor_plan(value) for value in args.floor_plans]
    else:
        floor_plans = discover_floor_plans()

    started = time.time()
    enumerated = enumerate_single_subtasks(floor_plans)
    if args.limit is not None:
        if args.limit < 0:
            raise ValueError("--limit must be non-negative")
        enumerated = enumerated[: args.limit]

    generated = prepare_generated_subtasks(enumerated)
    summary = write_outputs(
        generated,
        output_dir,
        overwrite=args.overwrite,
        validate_code=args.validate_code,
        manifest_only=args.manifest_only,
    )
    summary["elapsed_seconds"] = time.time() - started
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
