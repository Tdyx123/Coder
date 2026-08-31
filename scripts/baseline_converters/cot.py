#!/usr/bin/env python3
"""Convert COT direct-planner artifacts into executor code bundles."""

from __future__ import annotations

import json
import py_compile
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from baseline_converters import lammap
from baseline_converters.common import (
    build_bundle_data as common_build_bundle_data,
    compile_python,
    load_object_names,
    normalize_floor_plan,
    render_executable_plan as common_render_executable_plan,
    write_plan_to_code_summary,
)
from executor_system.pddlrun_adapter import ObjectNameResolver


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent
DEFAULT_BASELINE_ROOT = REPO_ROOT / "baselines" / "COT"
DEFAULT_SUMMARY_ROOT = DEFAULT_BASELINE_ROOT / "parallel_runs"
DEFAULT_OUTPUT_DIR = DEFAULT_BASELINE_ROOT / "plan_to_code_results"


class CotConversionError(RuntimeError):
    """Raised when a COT artifact set cannot be encoded safely."""


@dataclass(frozen=True)
class SummaryRun:
    source_summary: Path
    floor_summary: Path
    parallel_run_root: Path
    raw_run_dir: str
    task_run_dir: Path
    metadata: Dict[str, Any]


def read_json_dict(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CotConversionError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CotConversionError(f"Invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CotConversionError(f"Expected a JSON object: {path}")
    return payload


def parse_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise CotConversionError(f"{field_name} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CotConversionError(f"{field_name} must be a non-negative integer.") from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise CotConversionError(f"{field_name} must be a non-negative integer.")
    return parsed


def floor_plan_sort_key(value: Any) -> Tuple[int, str]:
    normalized = normalize_floor_plan(str(value or ""))
    try:
        return int(normalized), normalized
    except ValueError:
        return 10**9, normalized


def is_top_level_summary(payload: Dict[str, Any]) -> bool:
    summaries = payload.get("summaries")
    return isinstance(summaries, list) and any(
        isinstance(item, dict) and bool(item.get("summary"))
        for item in summaries
    )


def discover_top_level_summaries(summary_root: Path) -> List[Path]:
    root = summary_root.expanduser()
    if root.is_file():
        payload = read_json_dict(root)
        if not is_top_level_summary(payload):
            raise CotConversionError(f"Not a COT top-level summary: {root}")
        return [root]
    if not root.is_dir():
        raise CotConversionError(f"COT summary root not found: {root}")

    candidates: List[Path] = []
    for path in sorted(root.rglob("summary.json")):
        try:
            payload = read_json_dict(path)
        except CotConversionError:
            continue
        if is_top_level_summary(payload):
            candidates.append(path)
    if not candidates:
        raise CotConversionError(f"No COT top-level summary.json files found under {root}")
    return candidates


def counter_from_mapping(value: Any, field_name: str) -> Counter:
    if not isinstance(value, dict):
        raise CotConversionError(f"{field_name} must be a JSON object.")
    counts: Counter = Counter()
    for raw_key, raw_count in value.items():
        counts[str(raw_key)] = parse_non_negative_int(raw_count, f"{field_name}.{raw_key}")
    return counts


def resolve_floor_summary_path(parallel_run_root: Path, raw_path: Any) -> Path:
    raw_text = str(raw_path or "").strip()
    if not raw_text:
        raise CotConversionError("COT top-level summary entry is missing 'summary'.")
    path = Path(raw_text).expanduser()
    return resolve_local_artifact_path(parallel_run_root, path, "floor summary")


def resolve_task_run_dir(parallel_run_root: Path, raw_run_dir: Any) -> Path:
    raw_text = str(raw_run_dir or "").strip()
    if not raw_text:
        raise CotConversionError("COT task summary entry is missing 'run_dir'.")
    path = Path(raw_text).expanduser()
    return resolve_local_artifact_path(parallel_run_root, path, "task run_dir")


def resolve_local_artifact_path(root: Path, path: Path, label: str) -> Path:
    local_root = root.expanduser().resolve()
    if not path.is_absolute():
        candidate = local_root / path
    else:
        candidate = path
        try:
            candidate.resolve().relative_to(local_root)
        except ValueError:
            for index, part in enumerate(path.parts):
                if part.startswith("FloorPlan"):
                    candidate = local_root.joinpath(*path.parts[index:])
                    break

    resolved = candidate.resolve()
    try:
        resolved.relative_to(local_root)
    except ValueError as exc:
        raise CotConversionError(
            f"COT {label} must resolve under the parallel-run root {local_root}: {path}"
        ) from exc
    return resolved


def collect_summary_runs(source_summaries: Sequence[Path]) -> List[SummaryRun]:
    runs: List[SummaryRun] = []
    for source_summary in source_summaries:
        top = read_json_dict(source_summary)
        parallel_run_root = source_summary.parent
        raw_floor_summaries = top.get("summaries")
        if not isinstance(raw_floor_summaries, list) or not raw_floor_summaries:
            raise CotConversionError(f"COT summary has no floor summaries: {source_summary}")

        aggregate_statuses: Counter = Counter()
        aggregate_total = 0
        seen_task_keys = set()
        for raw_floor_entry in raw_floor_summaries:
            if not isinstance(raw_floor_entry, dict):
                raise CotConversionError(f"Invalid floor summary entry in {source_summary}")
            floor_summary_path = resolve_floor_summary_path(
                parallel_run_root,
                raw_floor_entry.get("summary"),
            )
            floor = read_json_dict(floor_summary_path)
            tasks = floor.get("tasks")
            if not isinstance(tasks, list):
                raise CotConversionError(
                    f"Floor summary is missing list 'tasks': {floor_summary_path}"
                )
            floor_plan = floor.get("floor_plan") or raw_floor_entry.get("floor_plan")
            if not floor_plan:
                raise CotConversionError(
                    f"Floor summary is missing floor_plan: {floor_summary_path}"
                )
            expected_floor = raw_floor_entry.get("floor_plan")
            if expected_floor and normalize_floor_plan(str(expected_floor)) != normalize_floor_plan(
                str(floor_plan)
            ):
                raise CotConversionError(
                    f"Floor mismatch between {source_summary} and {floor_summary_path}"
                )

            floor_total = parse_non_negative_int(
                floor.get("total_tasks"),
                f"{floor_summary_path} total_tasks",
            )
            if floor_total != len(tasks):
                raise CotConversionError(
                    f"Floor summary task count mismatch in {floor_summary_path}: "
                    f"total_tasks={floor_total}, tasks={len(tasks)}"
                )
            floor_statuses = counter_from_mapping(
                floor.get("status_counts"),
                f"{floor_summary_path} status_counts",
            )
            actual_floor_statuses = Counter(
                str(task.get("status"))
                for task in tasks
                if isinstance(task, dict)
            )
            if floor_statuses != actual_floor_statuses:
                raise CotConversionError(f"Floor status counts mismatch in {floor_summary_path}")

            aggregate_total += floor_total
            aggregate_statuses.update(floor_statuses)
            defaults = {
                key: value
                for key, value in {**top, **floor}.items()
                if key in {"test_set", "model", "floor_plan"} and value not in (None, "")
            }
            for task in tasks:
                if not isinstance(task, dict):
                    raise CotConversionError(f"Invalid task entry in {floor_summary_path}")
                task_index = parse_non_negative_int(
                    task.get("task_index"),
                    f"{floor_summary_path} task_index",
                )
                task_floor = task.get("floor_plan") or floor_plan
                if normalize_floor_plan(str(task_floor)) != normalize_floor_plan(
                    str(floor_plan)
                ):
                    raise CotConversionError(
                        f"Task floor mismatch in {floor_summary_path}: "
                        f"{task_floor!r} != {floor_plan!r}"
                    )
                task_key = (normalize_floor_plan(str(task_floor)), task_index)
                if task_key in seen_task_keys:
                    raise CotConversionError(
                        f"Duplicate COT task key {task_key!r} in {source_summary}"
                    )
                seen_task_keys.add(task_key)
                raw_run_dir = str(task.get("run_dir") or task.get("task_run_dir") or "")
                metadata = {**defaults, **task, "task_index": task_index, "floor_plan": task_floor}
                embedded_manifest = task.get("manifest")
                if not isinstance(embedded_manifest, dict):
                    raise CotConversionError(
                        f"COT task entry is missing manifest metadata in {floor_summary_path}"
                    )
                for field in ("task", "floor_plan", "task_index", "status", "test_set", "model"):
                    if field not in embedded_manifest or not values_match(
                        field,
                        metadata.get(field),
                        embedded_manifest.get(field),
                    ):
                        raise CotConversionError(
                            f"COT summary/embedded manifest mismatch for {field} in "
                            f"{floor_summary_path}"
                        )
                runs.append(
                    SummaryRun(
                        source_summary=source_summary,
                        floor_summary=floor_summary_path,
                        parallel_run_root=parallel_run_root,
                        raw_run_dir=raw_run_dir,
                        task_run_dir=resolve_task_run_dir(parallel_run_root, raw_run_dir),
                        metadata=metadata,
                    )
                )

        top_total = parse_non_negative_int(top.get("total_tasks"), f"{source_summary} total_tasks")
        if top_total != aggregate_total:
            raise CotConversionError(
                f"Top-level task count mismatch in {source_summary}: "
                f"total_tasks={top_total}, floor_total={aggregate_total}"
            )
        top_statuses = counter_from_mapping(
            top.get("status_counts"),
            f"{source_summary} status_counts",
        )
        if top_statuses != aggregate_statuses:
            raise CotConversionError(f"Top-level status counts mismatch in {source_summary}")

    return sorted(
        runs,
        key=lambda run: (
            floor_plan_sort_key(run.metadata.get("floor_plan")),
            int(run.metadata.get("task_index", 0)),
            str(run.source_summary),
            str(run.task_run_dir),
        ),
    )


def load_task_record(task_file: Path, task_index: int) -> Dict[str, Any]:
    if not task_file.is_file():
        raise CotConversionError(f"Dataset task file not found: {task_file}")
    with task_file.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise CotConversionError(f"Dataset line {task_index} is empty: {task_file}")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise CotConversionError(
                    f"Dataset line {task_index} must be a JSON object: {task_file}"
                )
            return payload
    raise CotConversionError(f"task_index {task_index} is out of range for {task_file}")


def dataset_path_for_run(
    repo_root: Path,
    floor_plan: str,
    test_set: str,
) -> Path:
    test_set_component = str(test_set or "").strip()
    normalized_floor = normalize_floor_plan(str(floor_plan or "")).strip()
    if (
        not test_set_component
        or Path(test_set_component).name != test_set_component
        or test_set_component in {".", ".."}
    ):
        raise CotConversionError(f"Invalid COT test_set path component: {test_set!r}")
    if not re.fullmatch(r"\d+", normalized_floor):
        raise CotConversionError(f"Invalid COT floor_plan: {floor_plan!r}")

    data_root = (repo_root.expanduser().resolve() / "data").resolve()
    task_file = (
        data_root / test_set_component / f"FloorPlan{normalized_floor}.jsonl"
    ).resolve()
    try:
        task_file.relative_to(data_root)
    except ValueError as exc:
        raise CotConversionError(
            f"COT dataset path must resolve under {data_root}: {task_file}"
        ) from exc
    return task_file


def object_labels(task_context: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    objects = task_context.get("objects")
    if not isinstance(objects, list):
        return labels
    for item in objects:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("name") or item.get("objectType")
        if label:
            labels.append(str(label))
    return labels


def normalized_robots(task_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_robots = task_context.get("robots")
    if not isinstance(raw_robots, list) or not raw_robots:
        raise CotConversionError("00_inputs/task_context.json is missing a robot list.")
    robots: List[Dict[str, Any]] = []
    seen = set()
    for index, raw_robot in enumerate(raw_robots):
        if not isinstance(raw_robot, dict):
            raise CotConversionError("COT robot entries must be JSON objects.")
        name = str(raw_robot.get("symbol") or raw_robot.get("name") or f"robot{index + 1}")
        if not name or name in seen:
            raise CotConversionError(f"Invalid or duplicate COT robot symbol: {name!r}")
        skills = raw_robot.get("skills")
        if (
            not isinstance(skills, list)
            or not skills
            or not all(isinstance(skill, str) and skill.strip() for skill in skills)
        ):
            raise CotConversionError(
                f"COT robot {name!r} must have a non-empty string list 'skills'."
            )
        seen.add(name)
        robot = dict(raw_robot)
        robot["name"] = name
        robot["skills"] = [skill.strip() for skill in skills]
        robots.append(robot)
    return robots


def parse_and_encode_plan(
    final_plan: Dict[str, Any],
    resolver: ObjectNameResolver,
    robots: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[lammap.EncodedAction]]:
    raw_entries = final_plan.get("plan")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CotConversionError("02_plan/01_final_plan.json is missing a non-empty list 'plan'.")

    robot_skills = {
        str(robot["name"]): {str(skill) for skill in robot.get("skills") or ()}
        for robot in robots
    }
    normalized_entries: List[Dict[str, Any]] = []
    raw_actions: List[lammap.RawAction] = []
    for order, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise CotConversionError(f"COT plan action {order} must be a JSON object.")
        raw_action_name = raw_entry.get("action")
        if not isinstance(raw_action_name, str) or not raw_action_name.strip():
            raise CotConversionError(f"COT plan action {order} is missing 'action'.")
        action_type = lammap.canonical_action_name(raw_action_name)
        if action_type is None:
            raise CotConversionError(
                f"Unsupported COT action {raw_action_name!r} at index {order}."
            )
        arguments = raw_entry.get("arguments")
        if (
            not isinstance(arguments, list)
            or not arguments
            or not all(isinstance(value, str) and value.strip() for value in arguments)
        ):
            raise CotConversionError(
                f"COT plan action {order} must have a non-empty string list 'arguments'."
            )
        reasoning_step = parse_non_negative_int(
            raw_entry.get("reasoning_step"),
            f"COT plan action {order} reasoning_step",
        )
        if reasoning_step < 1:
            raise CotConversionError(f"COT plan action {order} reasoning_step must be positive.")
        robot_id = arguments[0]
        if robot_id not in robot_skills:
            raise CotConversionError(
                f"COT plan action {order} references unknown robot {robot_id!r}."
            )
        if action_type not in robot_skills[robot_id]:
            raise CotConversionError(
                f"COT plan action {order} assigns {action_type} to {robot_id}, "
                "but the robot does not have that skill."
            )

        raw_text = f"({raw_action_name} {' '.join(arguments)})"
        raw_actions.append(
            lammap.RawAction(
                time_value=float(order),
                time_label=str(reasoning_step),
                order=order,
                action_type=action_type,
                raw_args=tuple(arguments),
                raw=raw_text,
            )
        )
        normalized_entries.append(
            {
                "reasoning_step": reasoning_step,
                "robot_id": robot_id,
            }
        )

    try:
        encoded_actions = lammap.encode_actions(raw_actions, resolver, robots)
    except lammap.FinalPlanEncodingError as exc:
        raise CotConversionError(str(exc)) from exc
    if len(encoded_actions) != len(normalized_entries):
        raise CotConversionError("COT action encoding changed the plan action count.")
    return normalized_entries, encoded_actions


def encoded_action_data(action: lammap.EncodedAction) -> Dict[str, Any]:
    return {
        "action_type": action.action_type,
        "parameters": {"args": list(action.args)},
        "robot_id": action.robot_id,
    }


def build_task_plan_data(
    task_id: str,
    entries: Sequence[Dict[str, Any]],
    encoded_actions: Sequence[lammap.EncodedAction],
) -> Dict[str, Any]:
    stages: List[Dict[str, Any]] = []
    current_key: Optional[Tuple[int, str]] = None
    current_actions: List[Dict[str, Any]] = []

    def flush_stage() -> None:
        nonlocal current_key, current_actions
        if current_key is None or not current_actions:
            return
        reasoning_step, robot_id = current_key
        stages.append(
            {
                "stage_id": f"COT Step {reasoning_step} Segment {len(stages) + 1}",
                "robot_action_queues": {robot_id: current_actions},
            }
        )
        current_key = None
        current_actions = []

    for entry, action in zip(entries, encoded_actions):
        key = (int(entry["reasoning_step"]), str(entry["robot_id"]))
        if current_key is None:
            current_key = key
        elif key != current_key:
            flush_stage()
            current_key = key
        current_actions.append(encoded_action_data(action))
    flush_stage()
    return {"task_id": task_id, "stages": stages}


def render_executable_plan(
    *,
    bundle_data: Dict[str, Any],
    task_file: Path,
    task_index: int,
    repo_root: Path,
) -> str:
    return common_render_executable_plan(
        bundle_data=bundle_data,
        task_file=task_file,
        task_index=task_index,
        description="Run a COT direct-plan bundle through executor_system.",
        repo_root=repo_root,
    )


def values_match(field: str, summary_value: Any, manifest_value: Any) -> bool:
    if field == "floor_plan":
        return normalize_floor_plan(str(summary_value)) == normalize_floor_plan(str(manifest_value))
    if field == "task_index":
        try:
            return int(summary_value) == int(manifest_value)
        except (TypeError, ValueError):
            return False
    return summary_value == manifest_value


def process_task_run(
    summary_run: SummaryRun,
    *,
    repo_root: Path,
    dry_run: bool = False,
    validate_code: bool = True,
) -> Dict[str, Any]:
    started_at = time.time()
    metadata = summary_run.metadata
    task_run_dir = summary_run.task_run_dir
    output_dir = task_run_dir / "plan_to_code"
    executable_path = output_dir / "executable_plan.py"
    result: Dict[str, Any] = {
        "task": metadata.get("task"),
        "task_run_dir": str(task_run_dir),
        "raw_run_dir": summary_run.raw_run_dir,
        "source_summary": str(summary_run.source_summary),
        "floor_plan": metadata.get("floor_plan"),
        "task_index": metadata.get("task_index"),
        "test_set": metadata.get("test_set"),
        "model": metadata.get("model"),
        "summary_status": metadata.get("status"),
        "status": "failed",
        "success": False,
        "skip_reason": "",
        "action_count": 0,
        "stage_count": 0,
        "no_trans": 0,
        "object_mappings": {},
        "object_mapping_warnings": [],
    }

    try:
        if metadata.get("status") != "success":
            result.update(
                {
                    "status": "skipped",
                    "skip_reason": f"source status: {metadata.get('status')}",
                }
            )
            return result
        if not task_run_dir.is_dir():
            raise CotConversionError(f"COT task run directory not found: {task_run_dir}")

        manifest = read_json_dict(task_run_dir / "run_manifest.json")
        task_context = read_json_dict(task_run_dir / "00_inputs" / "task_context.json")
        validation = read_json_dict(task_run_dir / "02_plan" / "03_validation.json")
        if validation.get("valid") is not True:
            raise CotConversionError(
                "COT plan validation is not valid: "
                f"{task_run_dir / '02_plan' / '03_validation.json'}"
            )
        for field in ("task", "floor_plan", "task_index", "status", "test_set", "model"):
            if not values_match(field, metadata.get(field), manifest.get(field)):
                raise CotConversionError(
                    f"COT summary/manifest mismatch for {field}: "
                    f"{metadata.get(field)!r} != {manifest.get(field)!r}"
                )

        task = str(manifest.get("task") or task_context.get("task") or "")
        floor_plan = str(manifest.get("floor_plan") or task_context.get("floor_plan") or "")
        task_index = parse_non_negative_int(
            manifest.get("task_index"),
            "run_manifest.json task_index",
        )
        test_set = str(manifest.get("test_set") or task_context.get("test_set") or "")
        if not task or not floor_plan or not test_set:
            raise CotConversionError("COT manifest is missing task, floor_plan, or test_set.")

        task_file = dataset_path_for_run(repo_root, floor_plan, test_set)
        task_record = load_task_record(task_file, task_index)
        if task_record.get("task") != task:
            raise CotConversionError(
                f"Dataset/manifest task mismatch at {task_file}:{task_index}."
            )
        gcr = task_record.get("object_states")
        if not isinstance(gcr, list):
            raise CotConversionError("Dataset task record is missing list object_states.")
        robots = normalized_robots(task_context)
        dataset_robot_ids = task_record.get("robot list")
        context_robot_ids = [robot.get("source_id") for robot in robots]
        if not isinstance(dataset_robot_ids, list) or dataset_robot_ids != context_robot_ids:
            raise CotConversionError(
                "Dataset robot list does not match 00_inputs/task_context.json source_id order."
            )
        object_names = load_object_names(repo_root, floor_plan, task_context)
        object_names.extend(object_labels(task_context))
        resolver = ObjectNameResolver(dict.fromkeys(object_names).keys())
        final_plan = read_json_dict(task_run_dir / "02_plan" / "01_final_plan.json")
        entries, encoded_actions = parse_and_encode_plan(final_plan, resolver, robots)
        task_id = f"cot_{normalize_floor_plan(floor_plan)}_{task_index}"
        task_plan_data = build_task_plan_data(task_id, entries, encoded_actions)
        bundle_data = common_build_bundle_data(
            task=task,
            task_plan_data=task_plan_data,
            gcr=gcr,
            no_trans=len(encoded_actions),
            object_mappings=dict(resolver.mappings),
            object_mapping_warnings=list(resolver.warnings),
        )
        executable_plan = render_executable_plan(
            bundle_data=bundle_data,
            task_file=task_file,
            task_index=task_index,
            repo_root=repo_root,
        )
        compile(executable_plan, "executable_plan.py", "exec")
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            executable_path.write_text(executable_plan, encoding="utf-8")
            if validate_code:
                compile_python(executable_path)

        result.update(
            {
                "task": task,
                "floor_plan": floor_plan,
                "task_index": task_index,
                "test_set": test_set,
                "status": "success",
                "success": True,
                "action_count": len(encoded_actions),
                "stage_count": len(task_plan_data["stages"]),
                "phase_count": len(task_plan_data["stages"]),
                "no_trans": len(encoded_actions),
                "object_mappings": dict(resolver.mappings),
                "object_mapping_warnings": list(resolver.warnings),
                "generated": {"executable_plan": str(executable_path)},
            }
        )
        return result
    except (
        CotConversionError,
        OSError,
        SyntaxError,
        json.JSONDecodeError,
        py_compile.PyCompileError,
    ) as exc:
        result.update({"status": "failed", "success": False, "error": str(exc)})
        return result
    finally:
        result["generation_time"] = time.time() - started_at


def run_matches_floor_plan(summary_run: SummaryRun, floor_plan: Optional[str]) -> bool:
    if not floor_plan:
        return True
    actual = normalize_floor_plan(str(summary_run.metadata.get("floor_plan") or ""))
    return actual == normalize_floor_plan(str(floor_plan))


def convert(
    *,
    repo_root: Path = REPO_ROOT,
    baseline_root: Path = DEFAULT_BASELINE_ROOT,
    summary_root: Path = DEFAULT_SUMMARY_ROOT,
    output_root: Path = DEFAULT_OUTPUT_DIR,
    floor_plan: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    validate_code: bool = True,
) -> int:
    repo_root = repo_root.expanduser().resolve()
    baseline_root = baseline_root.expanduser().resolve()
    summary_root = summary_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    try:
        source_summaries = discover_top_level_summaries(summary_root)
        summary_runs = collect_summary_runs(source_summaries)
    except (CotConversionError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if floor_plan:
        summary_runs = [
            summary_run
            for summary_run in summary_runs
            if run_matches_floor_plan(summary_run, floor_plan)
        ]
    if limit is not None:
        summary_runs = summary_runs[:limit]

    results: List[Dict[str, Any]] = []
    for index, summary_run in enumerate(summary_runs, start=1):
        result = process_task_run(
            summary_run,
            repo_root=repo_root,
            dry_run=dry_run,
            validate_code=validate_code,
        )
        results.append(result)
        status = str(result.get("status") or "failed")
        marker = "OK" if status == "success" else "SKIP" if status == "skipped" else "FAIL"
        print(f"[{marker}] [{index}/{len(summary_runs)}] {summary_run.task_run_dir}")

    skipped_count = sum(1 for result in results if result.get("status") == "skipped")
    error_count = sum(1 for result in results if result.get("status") == "failed")
    summary = write_plan_to_code_summary(
        results,
        output_root,
        dry_run=dry_run,
        include_dry_run=True,
        extra={
            "source_summaries": [str(path) for path in source_summaries],
            "skipped_generations": skipped_count,
            "error_generations": error_count,
        },
    )
    print(
        f"Processed {summary['total_results']} COT task(s); "
        f"generated {summary['successful_generations']} code bundle(s); "
        f"skipped {skipped_count}; failed {error_count}."
    )
    return 1 if error_count else 0
