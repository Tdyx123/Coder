#!/usr/bin/env python3
"""Convert pddlrun allocation/planner artifacts into executor bundles."""

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent
for path in (SCRIPTS_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from executor_system.action_plan import Action, StagePlan, TaskPlan
from executor_system.pddlrun_adapter import (
    PddlRunAdapterError,
    PddlRunPlanBundle,
    build_task_plan_from_pddlrun_paths,
    object_key,
)
from baseline_converters.common import (
    render_bundle_literal as common_render_bundle_literal,
    render_executable_plan as common_render_executable_plan,
)
from run_config import normalize_floor_plan

import resources.robots as robot_catalog


class PlanToCodeError(RuntimeError):
    """Raised when a pddlrun artifact set cannot be encoded."""


BASE_LINE_CHOICES = ("LaMMA-P", "SMART-LLM")


@dataclass(frozen=True)
class RunInputs:
    task_run_dir: Path
    manifest: Dict[str, Any]
    task_context: Dict[str, Any]
    data_repo_root: Path
    task_file: Path
    task_index: int
    task_record: Dict[str, Any]


@dataclass(frozen=True)
class GenerationPaths:
    logs_dir: Path
    output_dir: Path


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def default_baseline_root(base_line: str) -> Path:
    return REPO_ROOT / "baselines" / base_line


def baseline_logs_dir(base_line: str, root: Path) -> Path:
    if base_line == "LaMMA-P":
        return root / "logs" / "intermediate_runs"
    if base_line == "SMART-LLM":
        return root / "logs"
    raise PlanToCodeError(f"Unsupported base-line: {base_line}")


def baseline_output_dir(base_line: str, root: Path) -> Path:
    if base_line == "LaMMA-P":
        return root / "plan_to_code_results"
    if base_line == "SMART-LLM":
        return root
    raise PlanToCodeError(f"Unsupported base-line: {base_line}")


def resolve_generation_paths(args: argparse.Namespace) -> GenerationPaths:
    if args.root and not args.base_line:
        raise PlanToCodeError("--root requires --base-line.")

    if args.base_line:
        baseline_root = (
            Path(args.root).expanduser()
            if args.root
            else default_baseline_root(args.base_line)
        )
        logs_dir = (
            Path(args.logs_dir).expanduser()
            if args.logs_dir
            else baseline_logs_dir(args.base_line, baseline_root)
        )
        output_dir = (
            Path(args.output_dir).expanduser()
            if args.output_dir
            else baseline_output_dir(args.base_line, baseline_root)
        )
        return GenerationPaths(logs_dir=logs_dir, output_dir=output_dir)

    return GenerationPaths(
        logs_dir=Path(args.logs_dir or "./logs").expanduser(),
        output_dir=Path(args.output_dir or "./plan_to_code_results").expanduser(),
    )


def missing_task_run_artifacts(task_run_dir: Path) -> List[str]:
    task_context = task_run_dir / "inputs" / "task_context.json"
    allocate_output = task_run_dir / "02_allocate" / "02_allocate_output.txt"
    planner_manifest = task_run_dir / "08_planner" / "planner_manifest.json"
    planner_outputs = task_run_dir / "08_planner" / "outputs"
    has_plans = planner_manifest.exists() or any(planner_outputs.glob("*_plan.txt"))

    missing = []
    if not task_context.exists():
        missing.append("inputs/task_context.json")
    if not allocate_output.exists():
        missing.append("02_allocate/02_allocate_output.txt")
    if not has_plans:
        missing.append("08_planner/outputs/*_plan.txt or 08_planner/planner_manifest.json")
    return missing


def discover_task_runs(logs_dir: str) -> List[Path]:
    """Find pddlrun task directories under logs_dir."""
    root = Path(logs_dir).expanduser()
    if not root.exists():
        raise PlanToCodeError(f"Logs directory not found: {root}")

    task_run_dirs: List[Path] = []
    for manifest_path in sorted(root.rglob("run_manifest.json")):
        task_run_dir = manifest_path.parent
        missing = missing_task_run_artifacts(task_run_dir)

        if not missing:
            task_run_dirs.append(task_run_dir)
        else:
            print(f"Skipping incomplete run {task_run_dir}: missing {', '.join(missing)}")

    return task_run_dirs


def task_run_matches_floor_plan(task_run_dir: Path, normalized_floor_plan: Optional[str]) -> bool:
    if normalized_floor_plan is None:
        return True

    manifest = load_json(task_run_dir / "run_manifest.json", default={})
    if not isinstance(manifest, dict):
        return False
    floor_plan = manifest.get("floor_plan")
    if floor_plan is None:
        return False
    return normalize_floor_plan(str(floor_plan)) == normalized_floor_plan


def filter_task_runs_by_floor_plan(
    task_run_dirs: Sequence[Path],
    floor_plan: Optional[str],
) -> List[Path]:
    if not floor_plan:
        return list(task_run_dirs)
    normalized_floor_plan = normalize_floor_plan(str(floor_plan))
    return [
        task_run_dir
        for task_run_dir in task_run_dirs
        if task_run_matches_floor_plan(task_run_dir, normalized_floor_plan)
    ]


def summary_path_from_parallel_run(parallel_run: Path) -> Path:
    if parallel_run.is_file():
        return parallel_run
    return parallel_run / "summary.json"


def collect_parallel_summary_results(summary_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(summary_data.get("summaries"), list):
        results: List[Dict[str, Any]] = []
        for floor_summary in summary_data["summaries"]:
            if not isinstance(floor_summary, dict) or not isinstance(floor_summary.get("results"), list):
                continue
            floor_plan = floor_summary.get("floor_plan")
            for result in floor_summary["results"]:
                if not isinstance(result, dict):
                    continue
                item = dict(result)
                if floor_plan is not None and item.get("floor_plan") is None:
                    item["floor_plan"] = floor_plan
                results.append(item)
        return results

    if isinstance(summary_data.get("results"), list):
        return [result for result in summary_data["results"] if isinstance(result, dict)]

    return []


def resolve_summary_task_run_dir(
    raw_task_run_dir: Any,
    repo_root: Path,
    summary_dir: Path,
) -> Path:
    path = Path(str(raw_task_run_dir)).expanduser()
    if path.is_absolute():
        return path

    repo_candidate = repo_root / path
    if repo_candidate.exists():
        return repo_candidate
    summary_candidate = summary_dir / path
    if summary_candidate.exists():
        return summary_candidate
    return repo_candidate


def discover_parallel_run_task_runs(
    parallel_run: str,
    floor_plan: Optional[str] = None,
) -> List[Path]:
    root = Path(parallel_run).expanduser()
    if not root.exists():
        raise PlanToCodeError(f"Parallel run path not found: {root}")

    normalized_floor_plan = normalize_floor_plan(str(floor_plan)) if floor_plan else None
    summary_path = summary_path_from_parallel_run(root)
    if not summary_path.exists():
        return filter_task_runs_by_floor_plan(discover_task_runs(str(root)), floor_plan)

    summary_data = load_json(summary_path, default={})
    if not isinstance(summary_data, dict):
        raise PlanToCodeError(f"Invalid parallel run summary JSON: {summary_path}")

    repo_root = Path(str(summary_data.get("repo_root") or REPO_ROOT)).expanduser()
    task_run_dirs: List[Path] = []
    seen = set()
    for result in collect_parallel_summary_results(summary_data):
        if result.get("status") not in (None, "success"):
            continue
        raw_floor_plan = result.get("floor_plan")
        if (
            normalized_floor_plan is not None
            and raw_floor_plan is not None
            and normalize_floor_plan(str(raw_floor_plan)) != normalized_floor_plan
        ):
            continue

        raw_task_run_dir = result.get("task_run_dir")
        if not raw_task_run_dir:
            continue
        task_run_dir = resolve_summary_task_run_dir(raw_task_run_dir, repo_root, summary_path.parent)
        if normalized_floor_plan is not None and raw_floor_plan is None:
            if not task_run_matches_floor_plan(task_run_dir, normalized_floor_plan):
                continue

        missing = missing_task_run_artifacts(task_run_dir)
        if missing:
            print(f"Skipping incomplete run {task_run_dir}: missing {', '.join(missing)}")
            continue

        key = str(task_run_dir)
        if key in seen:
            continue
        seen.add(key)
        task_run_dirs.append(task_run_dir)

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
        if not path.is_file():
            print(f"Skipping missing planner output listed in manifest: {path}")
            continue
        plan_files.append(path)
    return plan_files


def build_bundle_for_run(
    run_inputs: RunInputs,
) -> PddlRunPlanBundle:
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


def gcr_from_task_record(task_record: Dict[str, Any]) -> List[Any]:
    gcr = task_record.get("object_states")
    if not isinstance(gcr, list):
        raise PlanToCodeError("Dataset task record is missing list object_states for BUNDLE_DATA['gcr'].")
    return gcr


def binding_type_key(binding: Dict[str, Any]) -> str:
    object_type = binding.get("object_type")
    if isinstance(object_type, str) and object_type:
        return object_key(object_type)

    object_token = str(binding.get("object") or "")
    object_token = re.sub(r"[_-]?\d+$", "", object_token)
    return object_key(object_token)


def binding_is_multi_instance(binding: Dict[str, Any], group_size: int) -> bool:
    if group_size > 1:
        return True
    if bool(binding.get("multiple")):
        return True
    try:
        return int(binding.get("count") or 0) > 1
    except (TypeError, ValueError):
        return False


def multi_instance_binding_groups(
    object_id_bindings: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for raw_binding in object_id_bindings:
        if not isinstance(raw_binding, dict):
            continue
        object_token = raw_binding.get("object")
        object_id = raw_binding.get("object_id")
        if not isinstance(object_token, str) or not object_token:
            continue
        if not isinstance(object_id, str) or not object_id:
            continue
        type_key = binding_type_key(raw_binding)
        if not type_key:
            continue
        grouped.setdefault(type_key, []).append(dict(raw_binding))

    return {
        type_key: bindings
        for type_key, bindings in grouped.items()
        if any(binding_is_multi_instance(binding, len(bindings)) for binding in bindings)
    }


def binding_lookup_tables(
    grouped_bindings: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, Tuple[str, str]]]:
    by_token_key: Dict[str, Tuple[str, str]] = {}
    by_object_id: Dict[str, Tuple[str, str]] = {}
    for type_key, bindings in grouped_bindings.items():
        for binding in bindings:
            token = str(binding.get("object") or "")
            object_id = str(binding.get("object_id") or "")
            if token:
                by_token_key[object_key(token)] = (type_key, token)
            if object_id:
                by_object_id[object_id] = (type_key, token)
    return by_token_key, by_object_id


def record_plan_token(
    raw_value: Any,
    plan_tokens_by_type: Dict[str, List[str]],
    binding_by_token_key: Dict[str, Tuple[str, str]],
    binding_by_object_id: Dict[str, Tuple[str, str]],
) -> None:
    value = str(raw_value or "")
    if not value:
        return

    binding_ref = binding_by_object_id.get(value) or binding_by_token_key.get(object_key(value))
    if binding_ref is None:
        return

    type_key, token = binding_ref
    tokens = plan_tokens_by_type.setdefault(type_key, [])
    if token not in tokens:
        tokens.append(token)


def plan_tokens_by_multi_instance_type(
    bundle: PddlRunPlanBundle,
    grouped_bindings: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[str]]:
    binding_by_token_key, binding_by_object_id = binding_lookup_tables(grouped_bindings)
    plan_tokens_by_type: Dict[str, List[str]] = {}

    for stage in bundle.task_plan.stages:
        for actions in stage.robot_action_queues.values():
            for action in actions:
                for arg in action.args():
                    record_plan_token(arg, plan_tokens_by_type, binding_by_token_key, binding_by_object_id)

    for source, target in bundle.object_mappings.items():
        record_plan_token(source, plan_tokens_by_type, binding_by_token_key, binding_by_object_id)
        record_plan_token(target, plan_tokens_by_type, binding_by_token_key, binding_by_object_id)

    return plan_tokens_by_type


def selected_gcr_token(
    value: Any,
    grouped_bindings: Dict[str, List[Dict[str, Any]]],
    plan_tokens_by_type: Dict[str, List[str]],
    binding_by_token_key: Dict[str, Tuple[str, str]],
    binding_by_object_id: Dict[str, Tuple[str, str]],
) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if value in binding_by_object_id or object_key(value) in binding_by_token_key:
        return value

    type_key = object_key(value)
    bindings = grouped_bindings.get(type_key)
    if not bindings:
        return value

    plan_tokens = plan_tokens_by_type.get(type_key) or []
    if plan_tokens:
        return plan_tokens[0]
    return str(bindings[0].get("object") or value)


def gcr_for_bundle(task_record: Dict[str, Any], bundle: PddlRunPlanBundle) -> List[Any]:
    gcr = gcr_from_task_record(task_record)
    grouped_bindings = multi_instance_binding_groups(bundle.object_id_bindings)
    if not grouped_bindings:
        return list(gcr)

    binding_by_token_key, binding_by_object_id = binding_lookup_tables(grouped_bindings)
    plan_tokens_by_type = plan_tokens_by_multi_instance_type(bundle, grouped_bindings)
    normalized_gcr: List[Any] = []

    for raw_goal in gcr:
        if not isinstance(raw_goal, dict):
            normalized_gcr.append(raw_goal)
            continue

        goal = dict(raw_goal)
        if "name" in goal:
            goal["name"] = selected_gcr_token(
                goal["name"],
                grouped_bindings,
                plan_tokens_by_type,
                binding_by_token_key,
                binding_by_object_id,
            )

        contains = goal.get("contains")
        if isinstance(contains, list):
            goal["contains"] = [
                selected_gcr_token(
                    item,
                    grouped_bindings,
                    plan_tokens_by_type,
                    binding_by_token_key,
                    binding_by_object_id,
                )
                for item in contains
            ]

        normalized_gcr.append(goal)

    return normalized_gcr


def serialize_bundle(bundle: PddlRunPlanBundle, gcr: Sequence[Any]) -> Dict[str, Any]:
    return {
        "task": bundle.task,
        "task_plan": serialize_task_plan(bundle.task_plan),
        "gcr": list(gcr),
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
        "object_mappings": dict(bundle.object_mappings),
        "object_mapping_warnings": list(bundle.object_mapping_warnings),
        "object_id_bindings": list(bundle.object_id_bindings),
    }


def render_bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return common_render_bundle_literal(bundle_data)


def render_demo_executable(run_inputs: RunInputs, bundle: PddlRunPlanBundle) -> str:
    return common_render_executable_plan(
        bundle_data=serialize_bundle(bundle, gcr_for_bundle(run_inputs.task_record, bundle)),
        task_file=run_inputs.task_file,
        task_index=run_inputs.task_index,
        description="Run a hardcoded pddlrun bundle through executor_system.",
        repo_root=REPO_ROOT,
    )


def compile_python(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def process_task_run(
    task_run_dir: Path,
    validate_code: bool,
) -> Dict[str, Any]:
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


def discover_source_task_runs(
    *,
    logs_dir: Path,
    parallel_run: str = "",
    floor_plan: Optional[str] = None,
) -> List[Path]:
    if parallel_run:
        return discover_parallel_run_task_runs(
            parallel_run,
            floor_plan=floor_plan,
        )
    return filter_task_runs_by_floor_plan(
        discover_task_runs(str(logs_dir)),
        floor_plan,
    )


def convert(
    *,
    logs_dir: Path,
    output_dir: Path,
    validate_code: bool = True,
    parallel_run: str = "",
    floor_plan: Optional[str] = None,
) -> int:
    try:
        task_run_dirs = discover_source_task_runs(
            logs_dir=logs_dir,
            parallel_run=parallel_run,
            floor_plan=floor_plan,
        )
    except PlanToCodeError as exc:
        print(f"ERROR: {exc}")
        return 1

    source_label = parallel_run or str(logs_dir)
    if not task_run_dirs:
        print(f"No complete pddlrun task runs found under {source_label}")
        write_summary([], output_dir)
        return 0

    print(f"Found {len(task_run_dirs)} complete pddlrun task run(s)")
    processed_results: List[Dict[str, Any]] = []
    for index, task_run_dir in enumerate(task_run_dirs, start=1):
        print(f"[{index}/{len(task_run_dirs)}] Generating demo bundle script: {task_run_dir}")
        result = process_task_run(
            task_run_dir,
            validate_code,
        )
        processed_results.append(result)
        if result.get("success"):
            print(f"  ✓ {result['generated']['executable_plan']}")
        else:
            print(f"  ✗ {result.get('error', 'unknown error')}")

    write_summary(processed_results, output_dir)
    return 0 if all(result.get("success") for result in processed_results) else 1


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate demo-style Python executor scripts from pddlrun allocation "
            "and planner artifacts. The generated script contains a hardcoded bundle."
        )
    )
    parser.add_argument(
        "--base-line",
        choices=BASE_LINE_CHOICES,
        help=(
            "Use baseline-compatible defaults for generated summaries. "
            "Supported values: LaMMA-P, SMART-LLM."
        ),
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help=(
            "Baseline root used with --base-line. "
            "Defaults to baselines/<base-line>."
        ),
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="",
        help=(
            "Path to pddlrun logs containing run_manifest.json files. "
            "Defaults to ./logs, or the baseline logs directory with --base-line."
        ),
    )
    parser.add_argument(
        "--parallel-run",
        type=str,
        default="",
        help=(
            "Path to a parallel_runs output directory or summary.json. "
            "When set, this is used instead of --logs-dir."
        ),
    )
    parser.add_argument(
        "--floor-plan",
        type=str,
        default="",
        help="Optional FloorPlan filter, e.g. 6 or FloorPlan6.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help=(
            "Directory to save generation summaries. Defaults to "
            "./plan_to_code_results, or the baseline summary directory with "
            "--base-line."
        ),
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

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)

    try:
        paths = resolve_generation_paths(args)
    except PlanToCodeError as exc:
        print(f"ERROR: {exc}")
        return 1

    return convert(
        logs_dir=paths.logs_dir,
        output_dir=paths.output_dir,
        validate_code=args.validate_code,
        parallel_run=args.parallel_run,
        floor_plan=args.floor_plan or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
