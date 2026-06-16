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


def extract_records(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []

    for result_index, result in iter_results(summary):
        stdout = result.get("stdout") or ""
        if not isinstance(stdout, str):
            stdout = str(stdout)

        contexts = stdout_failure_contexts(stdout)
        used_context_indexes: Set[int] = set()

        for failure_index, failure in enumerate(result.get("robot_failures") or []):
            error = str(failure.get("error") or "")
            if NO_VALID_POSITIONS not in error:
                continue

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


def write_json(path: Path, records: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
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
        if args.unique:
            records = unique_records(records)
        write_json(output_path, records)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(records)} record(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
