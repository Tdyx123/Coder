#!/usr/bin/env python3
"""Convert Scale-Plan final-plan JSON artifacts into executor code artifacts."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent
for _path in (SCRIPTS_DIR, REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from baseline_converters import lammap
from baseline_converters.common import (
    build_bundle_data as common_build_bundle_data,
    compile_python,
    load_json,
    load_object_names,
    render_executable_plan as common_render_executable_plan,
    write_plan_to_code_summary,
)
from executor_system.pddlrun_adapter import ObjectNameResolver
from run_config import normalize_floor_plan


DEFAULT_BASELINE_ROOT = REPO_ROOT / "baselines" / "Scale-Plan"
DEFAULT_LOGS_DIR = DEFAULT_BASELINE_ROOT / "logs" / "intermediate_runs"
DEFAULT_SUMMARY_ROOT = DEFAULT_BASELINE_ROOT / "logs" / "scale_plan_parallel"
DEFAULT_OUTPUT_DIR = DEFAULT_BASELINE_ROOT / "plan_to_code_results"


class ScalePlanConversionError(RuntimeError):
    """Raised when a Scale-Plan artifact set cannot be encoded."""


@dataclass(frozen=True)
class IndexedRun:
    metadata: Dict[str, Any]
    raw_task_run_dir: str
    task_run_dir: Path


def read_json_dict(path: Path) -> Dict[str, Any]:
    data = load_json(path, default={})
    if not isinstance(data, dict):
        raise ScalePlanConversionError(f"Expected JSON object: {path}")
    return data


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summary_path(summary_root: Path) -> Path:
    root = summary_root.expanduser()
    if root.is_file():
        return root

    matches = sorted(root.glob("pddlrun_*/summary.json"))
    if not matches:
        raise ScalePlanConversionError(f"No pddlrun summary.json found under {root}")
    if len(matches) != 1:
        joined = ", ".join(str(path) for path in matches)
        raise ScalePlanConversionError(
            f"Expected exactly one pddlrun summary.json under {root}; found {len(matches)}: {joined}"
        )
    return matches[0]


def path_after_marker(raw_path: Any, marker: Tuple[str, ...]) -> Optional[Path]:
    if raw_path in (None, ""):
        return None
    parts = Path(str(raw_path)).parts
    marker_len = len(marker)
    for index in range(len(parts) - marker_len + 1):
        if tuple(parts[index:index + marker_len]) == marker:
            tail = parts[index + marker_len :]
            return Path(*tail) if tail else Path()
    return None


def local_task_run_dir(raw_task_run_dir: Any, logs_dir: Path) -> Path:
    relative = path_after_marker(raw_task_run_dir, ("logs", "intermediate_runs"))
    if relative is not None:
        return logs_dir / relative

    raw_path = Path(str(raw_task_run_dir)).expanduser()
    if raw_path.is_absolute() and raw_path.exists():
        return raw_path
    if raw_path.is_absolute():
        return logs_dir / raw_path.name
    return logs_dir / raw_path


def collect_summary_runs(summary_data: Dict[str, Any], logs_dir: Path) -> List[IndexedRun]:
    root_defaults = {
        key: summary_data[key]
        for key in ("repo_root", "test_set", "test_set_path", "dataset_file", "model")
        if summary_data.get(key) not in (None, "")
    }

    indexed_runs: List[IndexedRun] = []
    for floor_summary in summary_data.get("summaries") or ():
        if not isinstance(floor_summary, dict):
            continue
        floor_defaults = dict(root_defaults)
        for key in (
            "floor_plan",
            "dataset_file",
            "test_set",
            "test_set_path",
            "model",
            "llm_token_usage",
        ):
            if floor_summary.get(key) not in (None, ""):
                floor_defaults[key] = floor_summary[key]

        for result in floor_summary.get("results") or ():
            if not isinstance(result, dict):
                continue
            raw_task_run_dir = result.get("task_run_dir")
            if not raw_task_run_dir:
                continue
            metadata = {**floor_defaults, **result}
            indexed_runs.append(
                IndexedRun(
                    metadata=metadata,
                    raw_task_run_dir=str(raw_task_run_dir),
                    task_run_dir=local_task_run_dir(raw_task_run_dir, logs_dir),
                )
            )

    return indexed_runs


def remap_data_path(raw_path: Any) -> Optional[Path]:
    if raw_path in (None, ""):
        return None
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.exists():
        return path

    relative = path_after_marker(raw_path, ("data",))
    if relative is not None:
        candidate = REPO_ROOT / "data" / relative
        if candidate.exists():
            return candidate
    return None


def add_dataset_file_candidates(
    candidates: List[Path],
    raw_path: Any,
) -> None:
    mapped = remap_data_path(raw_path)
    if mapped is not None and mapped.is_file():
        candidates.append(mapped)


def add_test_set_path_candidates(
    candidates: List[Path],
    raw_path: Any,
    floor_plan: Optional[str],
) -> None:
    if not floor_plan:
        return
    mapped = remap_data_path(raw_path)
    if mapped is not None and mapped.is_dir():
        candidates.append(mapped / f"FloorPlan{normalize_floor_plan(str(floor_plan))}.jsonl")


def dataset_path_for_run(
    metadata: Dict[str, Any],
    manifest: Dict[str, Any],
    task_context: Dict[str, Any],
    floor_plan: Optional[str],
    test_set: Optional[str],
) -> Path:
    candidates: List[Path] = []
    for source in (metadata, manifest, task_context):
        add_dataset_file_candidates(candidates, source.get("dataset_file"))

    for source in (metadata, manifest, task_context):
        add_test_set_path_candidates(candidates, source.get("test_set_path"), floor_plan)

    if test_set and floor_plan:
        candidates.append(
            REPO_ROOT
            / "data"
            / str(test_set)
            / f"FloorPlan{normalize_floor_plan(str(floor_plan))}.jsonl"
        )

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate

    raise ScalePlanConversionError("Dataset task file not found for Scale-Plan run.")


def load_task_record_gcr(task_file: Path, task_index: int) -> List[Any]:
    if task_index < 0:
        raise ScalePlanConversionError("task_index must be 0-based and non-negative.")
    with task_file.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise ScalePlanConversionError(f"Dataset line {task_index} is empty: {task_file}")
            record = json.loads(line)
            gcr = record.get("object_states")
            if not isinstance(gcr, list):
                raise ScalePlanConversionError(
                    "Dataset task record is missing list object_states for BUNDLE_DATA['gcr']."
                )
            return gcr
    raise ScalePlanConversionError(f"task_index {task_index} is out of range for {task_file}")


def object_names_from_gcr(gcr: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for item in gcr:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name:
            names.append(str(name))
        contains = item.get("contains")
        if isinstance(contains, list):
            names.extend(str(value) for value in contains if value)
    return names


def encoded_action_data(action: lammap.EncodedAction) -> Dict[str, Any]:
    return {
        "action_type": action.action_type,
        "parameters": {"args": list(action.args)},
        "robot_id": action.robot_id,
    }


def encode_plan_text(
    plan_text: str,
    resolver: ObjectNameResolver,
    robots: Sequence[Dict[str, Any]],
) -> List[lammap.EncodedAction]:
    raw_actions = lammap.parse_flat_actions(plan_text)
    if not raw_actions and plan_text.strip():
        raise ScalePlanConversionError(f"No supported actions found in plan text: {plan_text!r}")
    return lammap.encode_actions(raw_actions, resolver, robots)


def build_task_plan_data(
    *,
    task_id: str,
    final_plan: Dict[str, Any],
    resolver: ObjectNameResolver,
    robots: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Any], int]:
    raw_stages = final_plan.get("stages")
    if not isinstance(raw_stages, list):
        raise ScalePlanConversionError("05_plan/03_final_plan.json is missing a list 'stages'.")

    stages: List[Dict[str, Any]] = []
    action_count = 0
    for stage_index, stage in enumerate(raw_stages, start=1):
        if not isinstance(stage, dict):
            raise ScalePlanConversionError("Scale-Plan stage entries must be JSON objects.")
        plans = stage.get("plans")
        if not isinstance(plans, list):
            raise ScalePlanConversionError("Scale-Plan stage is missing a list 'plans'.")

        queues: Dict[str, List[Dict[str, Any]]] = {}
        for plan in plans:
            if not isinstance(plan, dict):
                raise ScalePlanConversionError("Scale-Plan plan entries must be JSON objects.")
            plan_text = str(plan.get("plan") or "")
            if not plan_text.strip():
                raise ScalePlanConversionError("Scale-Plan plan entry is missing non-empty 'plan'.")
            declared_robot = str(plan.get("robot") or "").strip()
            encoded_actions = encode_plan_text(plan_text, resolver, robots)
            for action in encoded_actions:
                if declared_robot and action.robot_id != declared_robot:
                    raise ScalePlanConversionError(
                        f"Plan declared {declared_robot}, but action uses {action.robot_id}: {action.raw}"
                    )
                queues.setdefault(action.robot_id, []).append(encoded_action_data(action))
                action_count += 1

        if not queues:
            raise ScalePlanConversionError("Scale-Plan stage did not produce any robot action queues.")

        stage_id = str(
            stage.get("stage_id")
            or stage.get("parallel_group_id")
            or f"Scale-Plan Stage {stage_index}"
        )
        stages.append(
            {
                "stage_id": stage_id,
                "robot_action_queues": queues,
            }
        )

    return {"task_id": task_id, "stages": stages}, action_count


def render_executable_plan(
    *,
    bundle_data: Dict[str, Any],
    task_file: Path,
    task_index: int,
) -> str:
    return common_render_executable_plan(
        bundle_data=bundle_data,
        task_file=task_file,
        task_index=task_index,
        description="Run a Scale-Plan final-plan bundle through executor_system.",
        repo_root=REPO_ROOT,
    )


def value_from_sources(
    key: str,
    *sources: Dict[str, Any],
) -> Any:
    for source in sources:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def run_matches_floor_plan(indexed_run: IndexedRun, floor_plan: Optional[str]) -> bool:
    if not floor_plan:
        return True
    normalized = normalize_floor_plan(str(floor_plan))
    manifest = load_json(indexed_run.task_run_dir / "run_manifest.json", default={}) or {}
    task_context = load_json(indexed_run.task_run_dir / "inputs" / "task_context.json", default={}) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    if not isinstance(task_context, dict):
        task_context = {}
    raw_floor_plan = value_from_sources("floor_plan", indexed_run.metadata, manifest, task_context)
    return raw_floor_plan not in (None, "") and normalize_floor_plan(str(raw_floor_plan)) == normalized


def process_indexed_run(
    indexed_run: IndexedRun,
    *,
    dry_run: bool = False,
    validate_code: bool = True,
) -> Dict[str, Any]:
    started_at = time.time()
    task_run_dir = indexed_run.task_run_dir
    metadata = indexed_run.metadata
    result: Dict[str, Any] = {
        "task": metadata.get("task"),
        "task_run_dir": str(task_run_dir),
        "floor_plan": metadata.get("floor_plan"),
        "task_index": parse_int(metadata.get("task_index")),
        "status": "failed",
        "success": False,
        "action_count": 0,
        "stage_count": 0,
        "no_trans": 0,
        "object_mappings": {},
        "object_mapping_warnings": [],
    }

    try:
        if not task_run_dir.is_dir():
            raise ScalePlanConversionError(f"Local task run directory not found: {task_run_dir}")

        manifest = read_json_dict(task_run_dir / "run_manifest.json")
        task_context = read_json_dict(task_run_dir / "inputs" / "task_context.json")
        final_plan_path = task_run_dir / "05_plan" / "03_final_plan.json"
        final_plan = read_json_dict(final_plan_path)

        task = str(value_from_sources("task", metadata, manifest, task_context) or task_run_dir.parent.name)
        floor_plan = value_from_sources("floor_plan", metadata, manifest, task_context)
        task_index = parse_int(value_from_sources("task_index", metadata, manifest, task_context))
        test_set = value_from_sources("test_set", metadata, manifest, task_context)
        if task_index is None:
            raise ScalePlanConversionError("Could not determine task_index for Scale-Plan run.")

        robots = task_context.get("robots")
        if not isinstance(robots, list) or not robots:
            raise ScalePlanConversionError("inputs/task_context.json is missing a robot list.")

        task_file = dataset_path_for_run(
            metadata,
            manifest,
            task_context,
            str(floor_plan) if floor_plan is not None else None,
            str(test_set) if test_set is not None else None,
        )
        gcr = load_task_record_gcr(task_file, task_index)

        object_names = load_object_names(REPO_ROOT, str(floor_plan or ""), task_context)
        object_names.extend(object_names_from_gcr(gcr))
        resolver = ObjectNameResolver(dict.fromkeys(object_names).keys())

        task_id = (
            f"scale_plan_{normalize_floor_plan(str(floor_plan)) if floor_plan else 'unknown'}_"
            f"{task_index}"
        )
        task_plan_data, action_count = build_task_plan_data(
            task_id=task_id,
            final_plan=final_plan,
            resolver=resolver,
            robots=robots,
        )

        bundle_data = common_build_bundle_data(
            task=task,
            task_plan_data=task_plan_data,
            gcr=gcr,
            no_trans=action_count,
            object_mappings=dict(resolver.mappings),
            object_mapping_warnings=list(resolver.warnings),
        )
        executable_plan = render_executable_plan(
            bundle_data=bundle_data,
            task_file=task_file,
            task_index=task_index,
        )
        compile(executable_plan, "executable_plan.py", "exec")

        output_dir = task_run_dir / "plan_to_code"
        executable_path = output_dir / "executable_plan.py"
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            executable_path.write_text(executable_plan, encoding="utf-8")
            if validate_code:
                compile_python(executable_path)

        result.update(
            {
                "task": task,
                "floor_plan": str(floor_plan) if floor_plan is not None else None,
                "task_index": task_index,
                "test_set": str(test_set) if test_set is not None else None,
                "status": "success",
                "success": True,
                "action_count": action_count,
                "stage_count": len(task_plan_data["stages"]),
                "phase_count": len(task_plan_data["stages"]),
                "no_trans": action_count,
                "object_mappings": dict(resolver.mappings),
                "object_mapping_warnings": list(resolver.warnings),
                "generated": {
                    "executable_plan": str(executable_path),
                },
            }
        )
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        result["generation_time"] = time.time() - started_at


def convert(
    *,
    summary_root: Path = DEFAULT_SUMMARY_ROOT,
    logs_dir: Path = DEFAULT_LOGS_DIR,
    output_root: Path = DEFAULT_OUTPUT_DIR,
    floor_plan: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    validate_code: bool = True,
) -> int:
    try:
        source_summary_path = summary_path(summary_root)
        summary_data = read_json_dict(source_summary_path)
    except (OSError, json.JSONDecodeError, ScalePlanConversionError) as exc:
        print(f"ERROR: {exc}")
        return 1

    indexed_runs = collect_summary_runs(summary_data, logs_dir.expanduser())
    if floor_plan:
        indexed_runs = [
            indexed_run
            for indexed_run in indexed_runs
            if run_matches_floor_plan(indexed_run, floor_plan)
        ]
    if limit is not None:
        indexed_runs = indexed_runs[:limit]

    results: List[Dict[str, Any]] = []
    for index, indexed_run in enumerate(indexed_runs, start=1):
        result = process_indexed_run(
            indexed_run,
            dry_run=dry_run,
            validate_code=validate_code,
        )
        results.append(result)
        marker = "OK" if result.get("success") else "FAIL"
        print(f"[{marker}] [{index}/{len(indexed_runs)}] {indexed_run.task_run_dir}")

    summary = write_plan_to_code_summary(
        results,
        output_root.expanduser(),
        dry_run=dry_run,
        include_dry_run=True,
        extra={"source_summary": str(source_summary_path)},
    )
    print(
        f"Processed {summary['total_results']} Scale-Plan run(s); "
        f"generated {summary['successful_generations']} code bundle(s)."
    )
    return 0 if all(result.get("success") for result in results) else 1


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Scale-Plan 05_plan/03_final_plan.json artifacts."
    )
    parser.add_argument("--summary-root", default=str(DEFAULT_SUMMARY_ROOT))
    parser.add_argument("--logs-dir", default=str(DEFAULT_LOGS_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--floor-plan", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--validate-code",
        action="store_true",
        default=True,
        help="Compile generated executable_plan.py files after writing them (default: true).",
    )
    parser.add_argument(
        "--no-validate-code",
        dest="validate_code",
        action="store_false",
        help="Skip py_compile validation of generated executable_plan.py files.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    return convert(
        summary_root=Path(args.summary_root).expanduser(),
        logs_dir=Path(args.logs_dir).expanduser(),
        output_root=Path(args.output_dir).expanduser(),
        floor_plan=args.floor_plan or None,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        validate_code=bool(args.validate_code),
    )


if __name__ == "__main__":
    raise SystemExit(main())
