#!/usr/bin/env python3
"""Extract PutObject no-valid-position failures from parallel runner output."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_INPUT = Path("parallel_runner_results/parallel_runner_summary.json")
DEFAULT_OUTPUT = Path("data/no_valid_positions.json")
NO_VALID_POSITIONS = "No valid positions to place object found"

FLOORPLAN_RE = re.compile(r"\b(FloorPlan\d+)\s+initialized\b")
HELD_OBJECT_RE = re.compile(r"currently holding:\s*([^|\n.]+)", re.IGNORECASE)
FAILED_PUTOBJECT_RE = re.compile(
    r"failed PutObject:\s*(.+?"
    + re.escape(NO_VALID_POSITIONS)
    + r")\s*$",
    re.IGNORECASE,
)
RECEPTACLE_TARGET_RE = re.compile(
    r"\bon\s+([^:\n]+?):\s*" + re.escape(NO_VALID_POSITIONS),
    re.IGNORECASE,
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract floorplan/object/receptacle triples for PutObject failures "
            "where AI2-THOR reports no valid positions to place the object."
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
        help=f"Output JSON array path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--unique",
        action="store_true",
        help="Deduplicate floorplan/object/receptacle triples while preserving order.",
    )
    return parser.parse_args(argv)


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def iter_results(summary: Dict[str, Any]) -> Iterable[Tuple[int, Dict[str, Any]]]:
    index = 0
    if isinstance(summary.get("summaries"), list):
        for floor_summary in summary["summaries"]:
            for result in floor_summary.get("results", []):
                yield index, result
                index += 1
        return

    for result in summary.get("results", []):
        yield index, result
        index += 1


def object_type_from_object_id(value: str) -> Optional[str]:
    object_type = value.strip().split("|", 1)[0].strip()
    return object_type or None


def parse_floorplan(stdout: str) -> Optional[str]:
    match = FLOORPLAN_RE.search(stdout)
    if not match:
        return None
    return match.group(1)


def parse_held_object(line: str) -> Optional[str]:
    match = HELD_OBJECT_RE.search(line)
    if not match:
        return None
    return object_type_from_object_id(match.group(1))


def parse_receptacle(error: str) -> Optional[str]:
    match = RECEPTACLE_TARGET_RE.search(error)
    if not match:
        return None
    return object_type_from_object_id(match.group(1))


def stdout_failure_contexts(stdout: str) -> List[Dict[str, Optional[str]]]:
    contexts: List[Dict[str, Optional[str]]] = []
    held_object: Optional[str] = None

    for line in stdout.splitlines():
        parsed_held_object = parse_held_object(line)
        if parsed_held_object:
            held_object = parsed_held_object

        match = FAILED_PUTOBJECT_RE.search(line)
        if match:
            contexts.append(
                {
                    "error": match.group(1).strip(),
                    "object": held_object,
                }
            )

    return contexts


def find_context_object(
    error: str,
    contexts: List[Dict[str, Optional[str]]],
    used_context_indexes: Set[int],
) -> Optional[str]:
    for index, context in enumerate(contexts):
        if index in used_context_indexes:
            continue
        if context["error"] == error:
            used_context_indexes.add(index)
            return context["object"]
    return None


def find_nearest_held_object_before_error(stdout: str, error: str) -> Optional[str]:
    position = stdout.find(error)
    if position < 0:
        position = stdout.lower().find(NO_VALID_POSITIONS.lower())
    prefix = stdout[:position] if position >= 0 else stdout

    held_object: Optional[str] = None
    for match in HELD_OBJECT_RE.finditer(prefix):
        held_object = object_type_from_object_id(match.group(1)) or held_object
    return held_object


def is_no_valid_put_failure(failure: Any) -> bool:
    if not isinstance(failure, dict):
        return False

    error = str(failure.get("error") or "")
    if NO_VALID_POSITIONS not in error:
        return False

    action_type = str(failure.get("action_type") or "")
    return action_type == "PutObject"


def result_has_no_valid_put_failure(result: Dict[str, Any]) -> bool:
    return any(
        is_no_valid_put_failure(failure)
        for failure in (result.get("robot_failures") or [])
    )


def success_count(results: Iterable[Dict[str, Any]]) -> int:
    return sum(
        1
        for result in results
        if result.get("returncode") == 0 and not result.get("timed_out")
    )


def failure_count(results: Iterable[Dict[str, Any]]) -> int:
    return sum(
        1
        for result in results
        if result.get("returncode") not in (0, None) and not result.get("timed_out")
    )


def timeout_count(results: Iterable[Dict[str, Any]]) -> int:
    return sum(1 for result in results if result.get("timed_out"))


def safe_count(value: Any) -> int:
    return value if isinstance(value, int) else 0


def all_subtasks_passed(result: Dict[str, Any]) -> bool:
    total = safe_count(result.get("total"))
    tc = safe_count(result.get("tc"))
    return total > 0 and tc == total


def refresh_flat_summary_counts(summary: Dict[str, Any]) -> None:
    results = summary.get("results")
    if not isinstance(results, list):
        return

    summary["total_results"] = len(results)
    summary["success_count"] = success_count(results)
    summary["failure_count"] = failure_count(results)
    summary["timeout_count"] = timeout_count(results)


def refresh_floor_summary_counts(floor_summary: Dict[str, Any]) -> None:
    results = floor_summary.get("results")
    if not isinstance(results, list):
        return

    floor_summary["task_count"] = len(results)
    floor_summary["success_count"] = sum(
        1 for result in results if result.get("status") == "success"
    )
    floor_summary["failure_count"] = sum(
        1 for result in results if result.get("status") != "success"
    )
    floor_summary["all_pass_count"] = sum(
        1 for result in results if all_subtasks_passed(result)
    )
    floor_summary["pass_one_count"] = sum(
        1 for result in results if safe_count(result.get("tc")) > 0
    )


def refresh_nested_summary_counts(summary: Dict[str, Any]) -> None:
    summaries = summary.get("summaries")
    if not isinstance(summaries, list):
        return

    floor_summaries = [
        floor_summary
        for floor_summary in summaries
        if isinstance(floor_summary, dict)
    ]
    for floor_summary in floor_summaries:
        refresh_floor_summary_counts(floor_summary)

    summary["success_count"] = sum(
        safe_count(floor_summary.get("success_count"))
        for floor_summary in floor_summaries
    )
    summary["failure_count"] = sum(
        safe_count(floor_summary.get("failure_count"))
        for floor_summary in floor_summaries
    )
    summary["all_pass_count"] = sum(
        safe_count(floor_summary.get("all_pass_count"))
        for floor_summary in floor_summaries
    )
    summary["pass_one_count"] = sum(
        safe_count(floor_summary.get("pass_one_count"))
        for floor_summary in floor_summaries
    )


def prune_no_valid_put_results(summary: Dict[str, Any]) -> int:
    removed = 0

    summaries = summary.get("summaries")
    if isinstance(summaries, list):
        for floor_summary in summaries:
            if not isinstance(floor_summary, dict):
                continue
            results = floor_summary.get("results")
            if not isinstance(results, list):
                continue

            kept_results = [
                result
                for result in results
                if not (
                    isinstance(result, dict)
                    and result_has_no_valid_put_failure(result)
                )
            ]
            removed += len(results) - len(kept_results)
            floor_summary["results"] = kept_results

        refresh_nested_summary_counts(summary)
        return removed

    results = summary.get("results")
    if not isinstance(results, list):
        return 0

    kept_results = [
        result
        for result in results
        if not (
            isinstance(result, dict)
            and result_has_no_valid_put_failure(result)
        )
    ]
    removed = len(results) - len(kept_results)
    summary["results"] = kept_results
    refresh_flat_summary_counts(summary)
    return removed


def extract_records(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []

    for result_index, result in iter_results(summary):
        stdout = result.get("stdout") or ""
        if not isinstance(stdout, str):
            stdout = str(stdout)

        contexts = stdout_failure_contexts(stdout)
        used_context_indexes: Set[int] = set()

        for failure_index, failure in enumerate(result.get("robot_failures") or []):
            if not is_no_valid_put_failure(failure):
                continue
            error = str(failure.get("error") or "")

            floorplan = parse_floorplan(stdout)
            placed_object = find_context_object(error, contexts, used_context_indexes)
            if placed_object is None:
                placed_object = find_nearest_held_object_before_error(stdout, error)
            receptacle = parse_receptacle(error)

            missing = [
                name
                for name, value in (
                    ("floorplan", floorplan),
                    ("object", placed_object),
                    ("receptacle", receptacle),
                )
                if not value
            ]
            if missing:
                missing_fields = ", ".join(missing)
                raise ValueError(
                    "Could not parse "
                    f"{missing_fields} for result {result_index}, "
                    f"failure {failure_index}: {error}"
                )

            records.append(
                {
                    "floorplan": str(floorplan),
                    "object": str(placed_object),
                    "receptacle": str(receptacle),
                }
            )

    return records


def unique_records(records: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    unique: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()

    for record in records:
        key = (record["floorplan"], record["object"], record["receptacle"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)

    return unique


def read_existing_output_records(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []

    existing_records = read_json(path)
    if not isinstance(existing_records, list):
        raise ValueError(f"Expected output JSON array in {path}")
    return existing_records


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = resolve_path(args.input)
    output_path = resolve_path(args.output)

    try:
        summary = read_json(input_path)
        if not isinstance(summary, dict):
            raise ValueError(f"Expected summary JSON object in {input_path}")

        records = extract_records(summary)
        existing_records = read_existing_output_records(output_path)
        output_records = existing_records + records
        if args.unique:
            output_records = unique_records(output_records)
        write_json(output_path, output_records)
        removed_count = prune_no_valid_put_results(summary)
        write_json(input_path, summary)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {len(output_records)} record(s) to {output_path} "
        f"({len(records)} extracted, {len(existing_records)} existing)"
    )
    print(f"Removed {removed_count} result(s) from {input_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
