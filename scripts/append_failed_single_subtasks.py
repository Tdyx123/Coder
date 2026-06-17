#!/usr/bin/env python3
"""Append failed generated single-subtask runs to the bad-subtasks config."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_INPUT = REPO_ROOT / "parallel_runner_results" / "parallel_runner_summary.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "bad_single_subtasks.json"
DEFAULT_REASON = "Failed in parallel runner"


def _config_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid bad subtask config {path}: {message}")


def _summary_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid parallel runner summary {path}: {message}")


def normalize_floor_plan(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid floor plan: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"(?:FloorPlan)?(\d+)", value.strip())
        if match:
            return int(match.group(1))
    raise ValueError(f"invalid floor plan: {value!r}")


def normalize_subtask(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("subtask must be an object")

    skill = value.get("skill")
    if not isinstance(skill, str) or not skill:
        raise ValueError("subtask.skill must be a non-empty string")

    objects = value.get("objects")
    if not isinstance(objects, list) or not all(isinstance(item, str) for item in objects):
        raise ValueError("subtask.objects must be a list of strings")

    return {"skill": skill, "objects": list(objects)}


def subtask_key(subtask: Dict[str, Any]) -> str:
    return json.dumps(subtask, ensure_ascii=False, sort_keys=True)


def is_failed_result(result: Dict[str, Any]) -> bool:
    robot_failures = result.get("robot_failures")
    return isinstance(robot_failures, list) and bool(robot_failures)


def load_summary(path: Path) -> List[Dict[str, Any]]:
    try:
        raw_summary = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _summary_error(path, str(exc)) from exc

    if not isinstance(raw_summary, dict):
        raise _summary_error(path, "top-level value must be an object")

    results = raw_summary.get("results")
    if not isinstance(results, list):
        raise _summary_error(path, "results must be a list")

    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise _summary_error(path, f"results[{index}] must be an object")
    return results


def load_or_create_bad_subtasks_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "bad_subtasks": []}

    try:
        raw_config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _config_error(path, str(exc)) from exc

    if not isinstance(raw_config, dict):
        raise _config_error(path, "top-level value must be an object")
    if raw_config.get("version") != 1:
        raise _config_error(path, "version must be 1")
    if not isinstance(raw_config.get("bad_subtasks"), list):
        raise _config_error(path, "bad_subtasks must be a list")
    return raw_config


def literal_assignments(path: Path) -> Dict[str, Any]:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"cannot parse generated script: {exc}") from exc

    assignments: Dict[str, Any] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"EMBEDDED_TASK_RECORD", "EMBEDDED_FLOOR_PLAN"}:
                continue
            try:
                assignments[target.id] = ast.literal_eval(node.value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{target.id} must be a literal value") from exc
    return assignments


def extract_single_subtask(path: Path) -> Tuple[int, Dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"generated script not found: {path}")

    assignments = literal_assignments(path)
    if "EMBEDDED_FLOOR_PLAN" not in assignments:
        raise ValueError("EMBEDDED_FLOOR_PLAN is missing")
    if "EMBEDDED_TASK_RECORD" not in assignments:
        raise ValueError("EMBEDDED_TASK_RECORD is missing")

    floor_plan = normalize_floor_plan(assignments["EMBEDDED_FLOOR_PLAN"])
    task_record = assignments["EMBEDDED_TASK_RECORD"]
    if not isinstance(task_record, dict):
        raise ValueError("EMBEDDED_TASK_RECORD must be an object")

    subtasks = task_record.get("subtasks")
    if not isinstance(subtasks, list):
        raise ValueError('EMBEDDED_TASK_RECORD["subtasks"] must be a list')
    if len(subtasks) != 1:
        raise ValueError(
            f'EMBEDDED_TASK_RECORD["subtasks"] must contain exactly 1 item, got {len(subtasks)}'
        )
    return floor_plan, normalize_subtask(subtasks[0])


def existing_bad_subtask_keys(entries: Sequence[Any]) -> set[Tuple[int, str]]:
    keys: set[Tuple[int, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("floor_plan") is None:
            continue
        try:
            floor_plan = normalize_floor_plan(entry.get("floor_plan"))
            subtask = normalize_subtask(entry.get("subtask"))
        except ValueError:
            continue
        keys.add((floor_plan, subtask_key(subtask)))
    return keys


def append_failed_subtasks(summary_path: Path, output_path: Path, reason: str) -> Dict[str, Any]:
    results = load_summary(summary_path)
    output_existed = output_path.exists()
    config = load_or_create_bad_subtasks_config(output_path)
    bad_subtasks = config["bad_subtasks"]
    known_keys = existing_bad_subtask_keys(bad_subtasks)

    appended = 0
    duplicates = 0
    skipped = 0
    errors: List[str] = []

    for index, result in enumerate(results):
        if not is_failed_result(result):
            continue

        executable_path = result.get("executable_path")
        if not isinstance(executable_path, str) or not executable_path:
            skipped += 1
            errors.append(f"results[{index}] missing executable_path")
            continue

        try:
            floor_plan, subtask = extract_single_subtask(Path(executable_path).expanduser())
        except (OSError, ValueError) as exc:
            skipped += 1
            errors.append(f"results[{index}] {executable_path}: {exc}")
            continue

        key = (floor_plan, subtask_key(subtask))
        if key in known_keys:
            duplicates += 1
            continue

        bad_subtasks.append(
            {
                "floor_plan": floor_plan,
                "subtask": subtask,
                "reason": reason,
            }
        )
        known_keys.add(key)
        appended += 1

    if appended or not output_existed:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return {
        "total_results": len(results),
        "failed_results": sum(1 for result in results if is_failed_result(result)),
        "appended": appended,
        "duplicates": duplicates,
        "skipped": skipped,
        "errors": errors,
        "output": str(output_path),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Append failed generated single-subtask runs from "
            "parallel_runner_summary.json to bad_single_subtasks.json."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help=f"Path to parallel_runner_summary.json. Default: {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Path to bad_single_subtasks.json. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--reason",
        default=DEFAULT_REASON,
        help=f"Reason to store on appended bad subtasks. Default: {DEFAULT_REASON!r}",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    summary_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()

    try:
        stats = append_failed_subtasks(summary_path, output_path, args.reason)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        "Scanned {total_results} result(s); found {failed_results} result(s) "
        "with robot_failures.".format(
            **stats
        )
    )
    print(
        "Appended {appended} bad subtask(s) to {output}; skipped {duplicates} "
        "duplicate(s) and {skipped} invalid failed result(s).".format(**stats)
    )
    if stats["errors"]:
        print("Skipped failed results:")
        for error in stats["errors"]:
            print(f"- {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
