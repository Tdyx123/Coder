#!/usr/bin/env python3
"""Convert KGLAMP final replan artifacts into executor code artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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
    object_names_from_items,
    render_executable_plan as common_render_executable_plan,
    write_plan_to_code_summary,
)
from executor_system.pddlrun_adapter import ObjectNameResolver
from run_config import normalize_floor_plan


DEFAULT_BASELINE_ROOT = REPO_ROOT / "baselines" / "KGLAMP"
DEFAULT_SUMMARY_ROOT = DEFAULT_BASELINE_ROOT / "parallel_runs"
DEFAULT_OUTPUT_DIR = DEFAULT_BASELINE_ROOT / "plan_to_code_results"


class KglampConversionError(RuntimeError):
    """Raised when a KGLAMP artifact set cannot be encoded."""


@dataclass(frozen=True)
class SummaryRun:
    source_summary: Path
    raw_run_dir: str
    task_run_dir: Path
    metadata: Dict[str, Any]


ACTION_LINE_RE = re.compile(r"^\(\s*(?P<inner>[^()]*)\)\s*$")


def read_json_dict(path: Path) -> Dict[str, Any]:
    data = load_json(path, default={})
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise KglampConversionError(f"Expected JSON object: {path}")
    return data


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def value_from_sources(key: str, *sources: Dict[str, Any]) -> Any:
    for source in sources:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def summary_paths(summary_root: Path) -> List[Path]:
    root = summary_root.expanduser()
    if root.is_file():
        return [root]
    if not root.exists():
        raise KglampConversionError(f"Summary root not found: {root}")
    paths = sorted(
        root.rglob("summary.json"),
        key=lambda path: (len(path.parts), str(path)),
    )
    if not paths:
        raise KglampConversionError(f"No summary.json files found under {root}")
    return paths


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


def resolve_run_dir(raw_run_dir: Any, baseline_root: Path, summary_dir: Path) -> Path:
    if raw_run_dir in (None, ""):
        raise KglampConversionError("Summary result is missing run_dir/task_run_dir.")

    raw_path = Path(str(raw_run_dir)).expanduser()
    candidates: List[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(baseline_root / raw_path)
        candidates.append(summary_dir / raw_path)

    relative_logs_path = path_after_marker(raw_run_dir, ("logs", "intermediate_runs"))
    if relative_logs_path is not None:
        candidates.append(
            baseline_root / "logs" / "intermediate_runs" / relative_logs_path
        )

    seen = set()
    unique_candidates: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if candidate.exists():
            return candidate
    return unique_candidates[0]


def add_summary_records(
    runs: List[SummaryRun],
    records: Iterable[Any],
    *,
    defaults: Dict[str, Any],
    source_summary: Path,
    baseline_root: Path,
) -> None:
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        raw_run_dir = raw_record.get("run_dir") or raw_record.get("task_run_dir")
        if not raw_run_dir:
            continue
        metadata = {**defaults, **raw_record, "source_summary": str(source_summary)}
        task_run_dir = resolve_run_dir(raw_run_dir, baseline_root, source_summary.parent)
        runs.append(
            SummaryRun(
                source_summary=source_summary,
                raw_run_dir=str(raw_run_dir),
                task_run_dir=task_run_dir,
                metadata=metadata,
            )
        )


def collect_summary_runs(
    source_summaries: Sequence[Path],
    baseline_root: Path,
) -> List[SummaryRun]:
    runs: List[SummaryRun] = []
    for source_summary in source_summaries:
        summary_data = read_json_dict(source_summary)
        root_defaults = {
            key: summary_data[key]
            for key in (
                "created_at",
                "repo_root",
                "test_set",
                "dataset_file",
                "model",
                "floor_plan",
            )
            if summary_data.get(key) not in (None, "")
        }
        add_summary_records(
            runs,
            summary_data.get("results") or (),
            defaults=root_defaults,
            source_summary=source_summary,
            baseline_root=baseline_root,
        )

        for floor_summary in summary_data.get("summaries") or ():
            if not isinstance(floor_summary, dict):
                continue
            floor_defaults = dict(root_defaults)
            for key in ("floor_plan", "test_set", "dataset_file", "model"):
                if floor_summary.get(key) not in (None, ""):
                    floor_defaults[key] = floor_summary[key]
            add_summary_records(
                runs,
                floor_summary.get("results") or (),
                defaults=floor_defaults,
                source_summary=source_summary,
                baseline_root=baseline_root,
            )

    deduped: List[SummaryRun] = []
    seen = set()
    for run in runs:
        key = str(run.task_run_dir.expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(run)
    return deduped


def infer_test_set(task_run_dir: Path) -> Optional[str]:
    parts = task_run_dir.parts
    for index, part in enumerate(parts):
        if part == "intermediate_runs" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def remap_data_path(raw_path: Any, repo_root: Path) -> Optional[Path]:
    if raw_path in (None, ""):
        return None
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    if path.exists():
        return path

    relative_data_path = path_after_marker(raw_path, ("data",))
    if relative_data_path is not None:
        candidate = repo_root / "data" / relative_data_path
        if candidate.exists():
            return candidate
    return None


def add_dataset_file_candidate(
    candidates: List[Path],
    raw_path: Any,
    repo_root: Path,
) -> None:
    mapped = remap_data_path(raw_path, repo_root)
    if mapped is not None and mapped.is_file():
        candidates.append(mapped)


def dataset_path_for_run(
    *,
    repo_root: Path,
    task_run_dir: Path,
    metadata: Dict[str, Any],
    manifest: Dict[str, Any],
    task_context: Dict[str, Any],
    floor_plan: Optional[str],
    task_index: Optional[int],
) -> Path:
    if not floor_plan:
        raise KglampConversionError("Could not determine floor_plan for generated TASK_FILE.")
    if task_index is None:
        raise KglampConversionError("Could not determine task_index for generated TASK_FILE.")

    candidates: List[Path] = []
    inferred_test_set = infer_test_set(task_run_dir)
    if inferred_test_set:
        candidates.append(
            repo_root
            / "data"
            / inferred_test_set
            / f"FloorPlan{normalize_floor_plan(str(floor_plan))}.jsonl"
        )

    for source in (metadata, manifest, task_context):
        add_dataset_file_candidate(candidates, source.get("dataset_file"), repo_root)

    for source in (manifest, task_context, metadata):
        test_set = source.get("test_set")
        if test_set in (None, ""):
            continue
        candidates.append(
            repo_root
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

    raise KglampConversionError(
        f"Dataset task file not found for floor_plan={floor_plan!r}, task_index={task_index!r}."
    )


def load_task_context(task_run_dir: Path) -> Dict[str, Any]:
    for relative_path in (
        Path("00_inputs") / "task_context.json",
        Path("inputs") / "task_context.json",
    ):
        path = task_run_dir / relative_path
        if path.exists():
            return read_json_dict(path)
    return {}


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


def parse_kglamp_final_plan(text: str) -> List[lammap.RawAction]:
    actions: List[lammap.RawAction] = []
    for order, raw_line in enumerate(text.splitlines()):
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        match = ACTION_LINE_RE.match(line)
        if not match:
            raise KglampConversionError(
                f"Unsupported KGLAMP final_plan line {order + 1}: {raw_line!r}"
            )
        action, _ = lammap.action_from_inner(
            match.group("inner"),
            float(order),
            str(order),
            line,
            order,
        )
        if action is None:
            raise KglampConversionError(
                f"Unsupported KGLAMP action line {order + 1}: {raw_line!r}"
            )
        if not action.raw_args or not lammap.is_robot_token(action.raw_args[0]):
            raise KglampConversionError(
                f"KGLAMP action line {order + 1} is missing robotN argument: {raw_line!r}"
            )
        actions.append(action)
    return actions


def encoded_action_data(action: lammap.EncodedAction) -> Dict[str, Any]:
    return {
        "action_type": action.action_type,
        "parameters": {"args": list(action.args)},
        "robot_id": action.robot_id,
    }


def build_task_plan_data(
    task_id: str,
    encoded_actions: Sequence[lammap.EncodedAction],
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
                "stage_id": f"KGLAMP Stage {len(stages) + 1}",
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
        current_actions.append(encoded_action_data(action))

    flush_stage()
    return {"task_id": task_id, "stages": stages}


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
        description="Run a KGLAMP final-plan bundle through executor_system.",
        repo_root=REPO_ROOT,
    )


def run_matches_floor_plan(summary_run: SummaryRun, floor_plan: Optional[str]) -> bool:
    if not floor_plan:
        return True
    normalized = normalize_floor_plan(str(floor_plan))
    raw_floor_plan = summary_run.metadata.get("floor_plan")
    if raw_floor_plan in (None, ""):
        manifest = load_json(summary_run.task_run_dir / "run_manifest.json", default={}) or {}
        task_context = load_task_context(summary_run.task_run_dir)
        if not isinstance(manifest, dict):
            manifest = {}
        raw_floor_plan = manifest.get("floor_plan") or task_context.get("floor_plan")
    return raw_floor_plan not in (None, "") and normalize_floor_plan(str(raw_floor_plan)) == normalized


def process_task_run(
    summary_run: SummaryRun,
    *,
    dry_run: bool = False,
    validate_code: bool = True,
) -> Dict[str, Any]:
    started_at = time.time()
    task_run_dir = summary_run.task_run_dir.expanduser()
    metadata = summary_run.metadata
    manifest: Dict[str, Any] = {}
    task_context: Dict[str, Any] = {}
    output_dir = task_run_dir / "plan_to_code"
    executable_path = output_dir / "executable_plan.py"

    result: Dict[str, Any] = {
        "task": metadata.get("task"),
        "task_run_dir": str(task_run_dir),
        "raw_run_dir": summary_run.raw_run_dir,
        "source_summary": str(summary_run.source_summary),
        "floor_plan": metadata.get("floor_plan"),
        "task_index": parse_int(metadata.get("task_index")),
        "test_set": None,
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
        if not task_run_dir.is_dir():
            raise KglampConversionError(f"Task run directory not found: {task_run_dir}")

        manifest = read_json_dict(task_run_dir / "run_manifest.json")
        task_context = load_task_context(task_run_dir)

        task = str(
            value_from_sources("task", metadata, manifest, task_context)
            or task_run_dir.parent.name
        )
        floor_plan = value_from_sources("floor_plan", metadata, manifest, task_context)
        task_index = parse_int(value_from_sources("task_index", metadata, manifest, task_context))
        test_set = infer_test_set(task_run_dir) or value_from_sources(
            "test_set",
            manifest,
            task_context,
            metadata,
        )
        model = value_from_sources("model", metadata, manifest, task_context)

        result.update(
            {
                "task": task,
                "floor_plan": str(floor_plan) if floor_plan not in (None, "") else None,
                "task_index": task_index,
                "test_set": str(test_set) if test_set not in (None, "") else None,
                "model": str(model) if model not in (None, "") else None,
            }
        )

        final_plan_path = task_run_dir / "09_replan" / "final_plan.txt"
        if not final_plan_path.is_file():
            result.update(
                {
                    "status": "skipped",
                    "skip_reason": f"missing final plan: {final_plan_path}",
                }
            )
            return result

        final_plan_text = final_plan_path.read_text(encoding="utf-8")
        raw_actions = parse_kglamp_final_plan(final_plan_text)
        if not raw_actions:
            result.update({"status": "skipped", "skip_reason": "empty final_plan.txt"})
            return result

        robots = task_context.get("robots")
        if not isinstance(robots, list) or not robots:
            raise KglampConversionError("00_inputs/task_context.json is missing a robot list.")

        task_file = dataset_path_for_run(
            repo_root=REPO_ROOT,
            task_run_dir=task_run_dir,
            metadata=metadata,
            manifest=manifest,
            task_context=task_context,
            floor_plan=str(floor_plan) if floor_plan not in (None, "") else None,
            task_index=task_index,
        )
        gcr = lammap.load_task_record_gcr(task_file, int(task_index))

        object_names = load_object_names(REPO_ROOT, str(floor_plan or ""), task_context)
        object_names.extend(object_names_from_items(task_context.get("objects") or ()))
        object_names.extend(object_names_from_gcr(gcr))
        resolver = ObjectNameResolver(dict.fromkeys(object_names).keys())
        encoded_actions = lammap.encode_actions(raw_actions, resolver, robots)
        if not encoded_actions:
            raise KglampConversionError("final_plan.txt did not encode any executable actions.")

        task_id = (
            f"kglamp_{normalize_floor_plan(str(floor_plan)) if floor_plan else 'unknown'}_"
            f"{task_index if task_index is not None else 'task'}"
        )
        task_plan_data = build_task_plan_data(task_id, encoded_actions)
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
            task_index=int(task_index),
        )
        compile(executable_plan, "executable_plan.py", "exec")

        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            executable_path.write_text(executable_plan, encoding="utf-8")
            if validate_code:
                compile_python(executable_path)

        result.update(
            {
                "status": "success",
                "success": True,
                "skip_reason": "",
                "action_count": len(encoded_actions),
                "phase_count": len(task_plan_data["stages"]),
                "stage_count": len(task_plan_data["stages"]),
                "no_trans": len(encoded_actions),
                "object_mappings": dict(resolver.mappings),
                "object_mapping_warnings": list(resolver.warnings),
                "generated": {"executable_plan": str(executable_path)},
            }
        )
        return result
    except Exception as exc:
        result.update({"status": "failed", "success": False, "error": str(exc)})
        return result
    finally:
        result["generation_time"] = time.time() - started_at


def convert(
    *,
    baseline_root: Path = DEFAULT_BASELINE_ROOT,
    summary_root: Path = DEFAULT_SUMMARY_ROOT,
    output_root: Path = DEFAULT_OUTPUT_DIR,
    floor_plan: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    validate_code: bool = True,
) -> int:
    try:
        resolved_baseline_root = baseline_root.expanduser()
        source_summaries = summary_paths(summary_root.expanduser())
        summary_runs = collect_summary_runs(source_summaries, resolved_baseline_root)
    except (OSError, json.JSONDecodeError, KglampConversionError) as exc:
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
            dry_run=dry_run,
            validate_code=validate_code,
        )
        results.append(result)
        status = str(result.get("status") or "failed")
        marker = "OK" if status == "success" else "SKIP" if status == "skipped" else "FAIL"
        print(f"[{marker}] [{index}/{len(summary_runs)}] {summary_run.task_run_dir}")

    failed_count = sum(1 for result in results if result.get("status") == "failed")
    skipped_count = sum(1 for result in results if result.get("status") == "skipped")
    summary = write_plan_to_code_summary(
        results,
        output_root.expanduser(),
        dry_run=dry_run,
        include_dry_run=True,
        extra={
            "source_summaries": [str(path) for path in source_summaries],
            "skipped_generations": skipped_count,
            "error_generations": failed_count,
        },
    )
    print(
        f"Processed {summary['total_results']} KGLAMP summary run(s); "
        f"generated {summary['successful_generations']} code bundle(s); "
        f"skipped {skipped_count}; failed {failed_count}."
    )
    return 1 if failed_count else 0


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert KGLAMP 09_replan/final_plan.txt artifacts from summary.json records."
    )
    parser.add_argument("--root", default=str(DEFAULT_BASELINE_ROOT))
    parser.add_argument("--summary-root", default=str(DEFAULT_SUMMARY_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--floor-plan", default="")
    parser.add_argument("--limit", type=int, default=None)
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
    baseline_root = Path(args.root).expanduser()
    return convert(
        baseline_root=baseline_root,
        summary_root=Path(args.summary_root).expanduser(),
        output_root=Path(args.output_dir).expanduser(),
        floor_plan=args.floor_plan or None,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        validate_code=bool(args.validate_code),
    )


if __name__ == "__main__":
    raise SystemExit(main())
