#!/usr/bin/env python3
"""Generate demo-style executor scripts with hardcoded pddlrun bundles."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import py_compile
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, List, Optional, Sequence

from executor_system.action_plan import Action, StagePlan, TaskPlan
from executor_system.pddlrun_adapter import (
    PddlRunAdapterError,
    PddlRunPlanBundle,
    build_task_plan_from_pddlrun_paths,
)
from run_config import normalize_floor_plan

import resources.robots as robot_catalog


REPO_ROOT = Path(__file__).resolve().parent.parent


class PlanToCodeError(RuntimeError):
    """Raised when a pddlrun artifact set cannot be encoded."""


@dataclass(frozen=True)
class RunInputs:
    task_run_dir: Path
    manifest: Dict[str, Any]
    task_context: Dict[str, Any]
    data_repo_root: Path
    task_file: Path
    task_index: int
    task_record: Dict[str, Any]


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def discover_task_runs(logs_dir: str) -> List[Path]:
    """Find pddlrun task directories under logs_dir."""
    root = Path(logs_dir).expanduser()
    if not root.exists():
        raise PlanToCodeError(f"Logs directory not found: {root}")

    task_run_dirs: List[Path] = []
    for manifest_path in sorted(root.rglob("run_manifest.json")):
        task_run_dir = manifest_path.parent
        task_context = task_run_dir / "inputs" / "task_context.json"
        allocate_output = task_run_dir / "02_allocate" / "02_allocate_output.txt"
        planner_manifest = task_run_dir / "08_planner" / "planner_manifest.json"
        planner_outputs = task_run_dir / "08_planner" / "outputs"
        has_plans = planner_manifest.exists() or any(planner_outputs.glob("*_plan.txt"))

        if task_context.exists() and allocate_output.exists() and has_plans:
            task_run_dirs.append(task_run_dir)
        else:
            missing = []
            if not task_context.exists():
                missing.append("inputs/task_context.json")
            if not allocate_output.exists():
                missing.append("02_allocate/02_allocate_output.txt")
            if not has_plans:
                missing.append("08_planner/outputs/*_plan.txt or 08_planner/planner_manifest.json")
            print(f"Skipping incomplete run {task_run_dir}: missing {', '.join(missing)}")

    return task_run_dirs


def parse_objects_ai(objects_ai: str) -> List[str]:
    if not objects_ai:
        return []

    text = objects_ai.strip()
    if text.startswith("objects"):
        _prefix, _sep, text = text.partition("=")
        text = text.strip()

    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    names: List[str] = []
    for item in parsed:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return names


def load_object_names(
    data_repo_root: Path,
    floor_plan: str,
    task_context: Dict[str, Any],
) -> List[str]:
    names = parse_objects_ai(str(task_context.get("objects_ai", "")))
    if names:
        return names

    cache_path = (
        data_repo_root
        / "data"
        / "ai2thor_objects_cache"
        / f"FloorPlan{normalize_floor_plan(floor_plan)}.json"
    )
    if not cache_path.exists():
        return []

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    names = []
    for item in cached:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return names


def load_task_record(task_file: Path, task_index: int) -> Dict[str, Any]:
    if not task_file.is_file():
        raise PlanToCodeError(f"Dataset task file not found: {task_file}")
    if task_index < 0:
        raise PlanToCodeError("task_index must be 0-based and non-negative.")

    with task_file.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise PlanToCodeError(f"Dataset line {task_index} is empty: {task_file}")
            return json.loads(line)

    raise PlanToCodeError(f"task_index {task_index} is out of range for {task_file}")


def load_run_inputs(task_run_dir: Path) -> RunInputs:
    manifest_path = task_run_dir / "run_manifest.json"
    context_path = task_run_dir / "inputs" / "task_context.json"
    manifest = load_json(manifest_path, default={})
    task_context = load_json(context_path, default={})
    if not isinstance(manifest, dict):
        raise PlanToCodeError(f"Invalid run manifest JSON: {manifest_path}")
    if not isinstance(task_context, dict):
        raise PlanToCodeError(f"Invalid task context JSON: {context_path}")

    test_set = manifest.get("test_set")
    floor_plan = manifest.get("floor_plan")
    if not test_set or floor_plan is None:
        raise PlanToCodeError(f"run_manifest.json is missing test_set or floor_plan: {manifest_path}")

    try:
        task_index = int(manifest.get("task_index"))
    except (TypeError, ValueError) as exc:
        raise PlanToCodeError(f"run_manifest.json has invalid task_index: {manifest_path}") from exc

    data_repo_root = Path(str(manifest.get("repo_root") or REPO_ROOT)).expanduser()
    task_file = (
        data_repo_root
        / "data"
        / str(test_set)
        / f"FloorPlan{normalize_floor_plan(str(floor_plan))}.jsonl"
    )
    task_record = load_task_record(task_file, task_index)

    return RunInputs(
        task_run_dir=task_run_dir,
        manifest=manifest,
        task_context=task_context,
        data_repo_root=data_repo_root,
        task_file=task_file,
        task_index=task_index,
        task_record=task_record,
    )


def build_robot_team_from_dataset(robot_ids: Sequence[Any]) -> List[Dict[str, Any]]:
    team: List[Dict[str, Any]] = []
    for index, raw_robot_id in enumerate(robot_ids):
        robot_id = int(raw_robot_id)
        if robot_id < 1 or robot_id > len(robot_catalog.robots):
            raise PlanToCodeError(f"Invalid robot id in task record: {raw_robot_id!r}")
        robot = copy.deepcopy(robot_catalog.robots[robot_id - 1])
        robot["name"] = f"robot{index + 1}"
        team.append(robot)
    if not team:
        raise PlanToCodeError("Task record has no robots in 'robot list'.")
    return team


def robots_for_encoding(run_inputs: RunInputs) -> List[Dict[str, Any]]:
    robots = run_inputs.task_context.get("robots")
    if isinstance(robots, list) and robots:
        return [dict(robot) for robot in robots if isinstance(robot, dict)]
    return build_robot_team_from_dataset(run_inputs.task_record.get("robot list") or [])


def resolve_manifest_plan_files(task_run_dir: Path) -> List[Path]:
    manifest_path = task_run_dir / "08_planner" / "planner_manifest.json"
    records = load_json(manifest_path, default=[])
    if not records:
        return []
    if not isinstance(records, list):
        raise PlanToCodeError(f"Planner manifest must be a list: {manifest_path}")

    plan_files: List[Path] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_path = (
            record.get("compatibility_output")
            or record.get("plan_file")
            or record.get("output")
        )
        if not raw_path:
            continue
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = task_run_dir / path
        plan_files.append(path)
    return plan_files


def build_bundle_for_run(run_inputs: RunInputs) -> PddlRunPlanBundle:
    floor_plan = str(run_inputs.manifest.get("floor_plan"))
    robots = robots_for_encoding(run_inputs)
    object_names = load_object_names(run_inputs.data_repo_root, floor_plan, run_inputs.task_context)
    plan_files = resolve_manifest_plan_files(run_inputs.task_run_dir)
    plan_folder = run_inputs.task_run_dir / "08_planner" / "outputs"

    return build_task_plan_from_pddlrun_paths(
        task=str(run_inputs.task_record.get("task") or run_inputs.task_context.get("task") or ""),
        robots=robots,
        allocate_file=run_inputs.task_run_dir / "02_allocate" / "02_allocate_output.txt",
        plan_folder=plan_folder if plan_folder.exists() else None,
        plan_files=plan_files,
        object_names=object_names,
        task_id=f"FloorPlan{normalize_floor_plan(floor_plan)}_task_{run_inputs.task_index}",
    )


def literalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return tuple(literalize(item) for item in value)
    if isinstance(value, list):
        return [literalize(item) for item in value]
    if isinstance(value, dict):
        return {literalize(key): literalize(item) for key, item in value.items()}
    return value


def serialize_action(action: Action) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "action_type": action.action_type,
        "parameters": literalize(action.parameters),
    }
    if action.robot_id is not None:
        data["robot_id"] = action.robot_id
    if action.action_id is not None:
        data["action_id"] = action.action_id
    return data


def serialize_stage(stage: StagePlan) -> Dict[str, Any]:
    return {
        "stage_id": stage.stage_id,
        "robot_action_queues": {
            robot_id: [serialize_action(action) for action in actions]
            for robot_id, actions in stage.robot_action_queues.items()
        },
    }


def serialize_task_plan(task_plan: TaskPlan) -> Dict[str, Any]:
    return {
        "task_id": task_plan.task_id,
        "stages": [serialize_stage(stage) for stage in task_plan.stages],
    }


def serialize_bundle(bundle: PddlRunPlanBundle) -> Dict[str, Any]:
    return {
        "task": bundle.task,
        "task_plan": serialize_task_plan(bundle.task_plan),
        "no_trans": bundle.no_trans,
        "phases": [
            [
                {
                    "subtask_id": assignment.subtask_id,
                    "robot_number": assignment.robot_number,
                }
                for assignment in phase
            ]
            for phase in bundle.phases
        ],
        "plan_files": {
            subtask_id: str(path)
            for subtask_id, path in bundle.plan_files.items()
        },
        "object_mappings": dict(bundle.object_mappings),
        "object_mapping_warnings": list(bundle.object_mapping_warnings),
    }


def render_bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return pformat(bundle_data, width=100, sort_dicts=False)


def render_demo_executable(run_inputs: RunInputs, bundle: PddlRunPlanBundle) -> str:
    bundle_literal = render_bundle_literal(serialize_bundle(bundle))
    code_repo_root = str(REPO_ROOT)
    task_file = str(run_inputs.task_file)
    task_index = run_inputs.task_index

    return f'''#!/usr/bin/env python3
"""Run a hardcoded pddlrun bundle through executor_system."""

from __future__ import annotations

import copy
import json
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Sequence


REPO_ROOT = Path({code_repo_root!r})
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system import actions as _actions
from executor_system import config as _config
from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system import dependencies as _dependencies
from executor_system import runtime as _runtime_module
from executor_system.action_plan import TaskPlan
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.runtime import ThorRuntime
from executor_system.task_plan import run_action_plan

import resources.robots as robot_catalog


BUNDLE_DATA = {bundle_literal}

TASK_FILE = {task_file!r}
TASK_INDEX = {task_index!r}


runtime = None
robots: List[Dict[str, Any]] = []
floor_no = ""
ground_truth: List[Dict[str, Any]] = []
cv2 = _dependencies.cv2
Controller = _dependencies.Controller
CloudRendering = _dependencies.CloudRendering


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


def build_robot_team(robot_ids: Sequence[Any]) -> List[Dict[str, Any]]:
    team: List[Dict[str, Any]] = []
    for index, raw_robot_id in enumerate(robot_ids):
        robot_id = int(raw_robot_id)
        if robot_id < 1 or robot_id > len(robot_catalog.robots):
            raise RuntimeError(f"Invalid robot id in task record: {{raw_robot_id!r}}")
        robot = copy.deepcopy(robot_catalog.robots[robot_id - 1])
        robot["name"] = f"robot{{index + 1}}"
        team.append(robot)
    if not team:
        raise RuntimeError("Task record has no robots in 'robot list'.")
    return team


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


def main() -> int:
    global floor_no, ground_truth, robots, runtime

    task_record = load_task_record(TASK_FILE, TASK_INDEX)
    floor_no = floor_plan_from_task_file(TASK_FILE)
    robots = build_robot_team(task_record.get("robot list") or [])
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


class _Demo2Facade(types.ModuleType):
    def __getattribute__(self, name):
        if name == "runtime":
            return _context.runtime
        if name == "cv2":
            return _dependencies.cv2
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name == "runtime":
            _context.runtime = value
        elif name == "cv2":
            _dependencies.cv2 = value
            _runtime_module.cv2 = value
        elif name == "ground_truth":
            _demo_state.set_ground_truth(value)
        elif hasattr(_demo_state, name):
            setattr(_demo_state, name, value)
        elif hasattr(_config, name):
            setattr(_config, name, value)
            if hasattr(_actions, name):
                setattr(_actions, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        elif hasattr(_dependencies, name):
            setattr(_dependencies, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Demo2Facade


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {{exc}}")
        raise SystemExit(1)
'''


def compile_python(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def process_task_run(task_run_dir: Path, validate_code: bool) -> Dict[str, Any]:
    start_time = time.time()
    result: Dict[str, Any] = {
        "task_run_dir": str(task_run_dir),
        "status": "failed",
        "success": False,
    }

    try:
        run_inputs = load_run_inputs(task_run_dir)
        bundle = build_bundle_for_run(run_inputs)
        executable_plan = render_demo_executable(run_inputs, bundle)

        compile(executable_plan, "executable_plan.py", "exec")
        output_dir = task_run_dir / "plan_to_code"
        output_dir.mkdir(parents=True, exist_ok=True)
        executable_path = output_dir / "executable_plan.py"
        executable_path.write_text(executable_plan, encoding="utf-8")

        if validate_code:
            compile_python(executable_path)

        result.update(
            {
                "status": "success",
                "success": True,
                "task": bundle.task,
                "floor_plan": normalize_floor_plan(str(run_inputs.manifest.get("floor_plan"))),
                "task_index": run_inputs.task_index,
                "phase_count": len(bundle.task_plan.stages),
                "no_trans": bundle.no_trans,
                "object_mappings": dict(bundle.object_mappings),
                "object_mapping_warnings": list(bundle.object_mapping_warnings),
                "generated": {
                    "executable_plan": str(executable_path),
                },
                "generation_time": time.time() - start_time,
            }
        )
        return result
    except (PlanToCodeError, PddlRunAdapterError, OSError, SyntaxError, py_compile.PyCompileError) as exc:
        result.update(
            {
                "error": str(exc),
                "generation_time": time.time() - start_time,
            }
        )
        return result


def write_summary(processed_results: List[Dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(processed_results)
    successful = sum(1 for result in processed_results if result.get("success"))
    summary = {
        "total_results": total,
        "successful_generations": successful,
        "failed_generations": total - successful,
        "success_rate": successful / total * 100 if total else 0,
        "total_generation_time": sum(float(result.get("generation_time", 0)) for result in processed_results),
    }

    (output_dir / "plan_to_code_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "plan_to_code_results.json").write_text(
        json.dumps(processed_results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n=== PLAN-TO-CODE BUNDLE GENERATION SUMMARY ===")
    print(f"Total runs processed: {total}")
    print(f"Successful generations: {successful} ({summary['success_rate']:.1f}%)")
    print(f"Summary files saved to: {output_dir}")


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate demo-style Python executor scripts from pddlrun allocation "
            "and planner artifacts. The generated script contains a hardcoded bundle."
        )
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        help="Compatibility no-op; no model is called by this deterministic generator.",
    )
    parser.add_argument(
        "--input-source",
        type=str,
        choices=["json", "pddl_logs"],
        default="pddl_logs",
        help="Only pddl_logs is supported for hardcoded bundle generation.",
    )
    parser.add_argument(
        "--input-file",
        type=str,
        default="../model_testing/70b_extracted_actions/70b_extracted_action_sequences.json",
        help="Compatibility no-op; JSON input cannot build hardcoded pddlrun bundles.",
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="./logs",
        help="Path to pddlrun logs containing run_manifest.json files.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./plan_to_code_results",
        help="Directory to save generation summaries.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="Compatibility no-op retained for old invocations.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Compatibility no-op; no text generation is performed.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Compatibility no-op; no text generation is performed.",
    )
    parser.add_argument(
        "--frequency-penalty",
        type=float,
        default=0.0,
        help="Compatibility no-op; no text generation is performed.",
    )
    parser.add_argument(
        "--validate-code",
        action="store_true",
        default=True,
        help="Compile generated executable_plan.py files after writing them (default: True).",
    )
    parser.add_argument(
        "--no-validate-code",
        dest="validate_code",
        action="store_false",
        help="Skip py_compile validation of generated executable_plan.py files.",
    )

    args = parser.parse_args(argv)
    if args.input_source == "json":
        parser.error(
            "--input-source json is not supported: hardcoded bundle generation "
            "requires pddlrun artifacts under --logs-dir."
        )
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)

    try:
        task_run_dirs = discover_task_runs(args.logs_dir)
    except PlanToCodeError as exc:
        print(f"ERROR: {exc}")
        return 1

    if not task_run_dirs:
        print(f"No complete pddlrun task runs found under {args.logs_dir}")
        write_summary([], Path(args.output_dir))
        return 0

    print(f"Found {len(task_run_dirs)} complete pddlrun task run(s)")
    processed_results: List[Dict[str, Any]] = []
    for index, task_run_dir in enumerate(task_run_dirs, start=1):
        print(f"[{index}/{len(task_run_dirs)}] Generating demo bundle script: {task_run_dir}")
        result = process_task_run(task_run_dir, args.validate_code)
        processed_results.append(result)
        if result.get("success"):
            print(f"  ✓ {result['generated']['executable_plan']}")
        else:
            print(f"  ✗ {result.get('error', 'unknown error')}")

    write_summary(processed_results, Path(args.output_dir))
    return 0 if all(result.get("success") for result in processed_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
