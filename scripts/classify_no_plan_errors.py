import argparse
import ast
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


DEFAULT_INPUT = Path("parallel_runs/no_plan_with_errors.jsonl")
DEFAULT_OUTPUT_JSONL = Path("parallel_runs/no_plan_with_errors_classified.jsonl")
DEFAULT_SUMMARY_CSV = Path("parallel_runs/no_plan_error_summary.csv")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify planner no-plan error records by symptom and detectable root cause."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help=f"Input no-plan JSONL file. Default: {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output-jsonl",
        default=str(DEFAULT_OUTPUT_JSONL),
        help=f"Classified JSONL output file. Default: {DEFAULT_OUTPUT_JSONL}",
    )
    parser.add_argument(
        "--summary-csv",
        default=str(DEFAULT_SUMMARY_CSV),
        help=f"Aggregated CSV summary output file. Default: {DEFAULT_SUMMARY_CSV}",
    )
    parser.add_argument(
        "--no-read-stdout",
        action="store_true",
        help="Do not read planner_stdout_path files while classifying.",
    )
    return parser.parse_args(argv)


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def read_text_if_exists(path_value: Optional[str]) -> str:
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def decode_bytes_literal(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)

    stripped = value.strip()
    if len(stripped) >= 3 and stripped[0] in {"b", "B"} and stripped[1] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return value
        if isinstance(decoded, bytes):
            return decoded.decode("utf-8", errors="replace")
    return value


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Could not parse JSON on line {line_number} of {path}: {exc}") from exc
            records.append(record)
    return records


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def first_line_containing(text: str, needle: str) -> str:
    lowered_needle = needle.lower()
    for line in text.splitlines():
        if lowered_needle in line.lower():
            return line.strip()
    return needle


def extract_first(pattern: str, text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    return match.group(1).strip()


def extract_parse_reason(text: str) -> Optional[str]:
    return extract_first(r"Reason:\s*([^\n\r]+)", text)


def has_unbalanced_parentheses(pddl: str) -> bool:
    return pddl.count("(") != pddl.count(")")


def has_natural_language_leak(pddl: str) -> bool:
    lowered = pddl.lower()
    indicators = (
        "wait no",
        "let me",
        "thought process",
        "memory",
        "correct line should",
        "copy paste error",
        "original actually said",
        "ugh",
        "glitching",
        "here is",
    )
    return any(indicator in lowered for indicator in indicators)


def is_parse_error(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "could not parse task file",
        "expected ",
        "tokens remaining after parsing",
        "error in initial state specification",
        "duplicate object",
        "translate exit code: 31",
    )
    return any(marker in lowered for marker in markers)


def classify_parse_root_cause(text: str, pddl: str) -> Dict[str, Any]:
    parse_reason = extract_parse_reason(text)
    details: Dict[str, Any] = {}
    if parse_reason:
        details["parse_reason"] = parse_reason

    if pddl.lstrip().startswith("```") or (parse_reason and "```" in parse_reason):
        return {
            "root_cause_category": "markdown_fence_in_pddl",
            "classification_reason": parse_reason or "PDDL starts with a markdown code fence.",
            "classification_details": details,
        }

    if has_natural_language_leak(pddl):
        return {
            "root_cause_category": "natural_language_leak",
            "classification_reason": "PDDL contains natural-language model output.",
            "classification_details": details,
        }

    if (
        (parse_reason and ("expected" in parse_reason.lower() or "tokens remaining" in parse_reason.lower()))
        or has_unbalanced_parentheses(pddl)
        or "tokens remaining after parsing" in text.lower()
    ):
        return {
            "root_cause_category": "malformed_pddl_syntax",
            "classification_reason": parse_reason or "PDDL has malformed syntax.",
            "classification_details": details,
        }

    return {
        "root_cause_category": "malformed_pddl_syntax",
        "classification_reason": parse_reason or "Planner reported a PDDL parse error.",
        "classification_details": details,
    }


def classify_record(record: Dict[str, Any], stdout_text: str = "") -> Dict[str, Any]:
    error_message = decode_bytes_literal(record.get("error_message"))
    planner_stderr = decode_bytes_literal(record.get("planner_stderr"))
    stdout_text = decode_bytes_literal(stdout_text)
    pddl = record.get("pddl") or ""
    combined_text = "\n".join(part for part in (error_message, planner_stderr, stdout_text) if part)
    lowered = combined_text.lower()

    base = {
        "error_phase": "unknown",
        "symptom_category": "unknown_error",
        "root_cause_category": "unknown_error",
        "classification_reason": "No classification rule matched.",
        "classification_confidence": "low",
        "classification_details": {},
    }

    if "this configuration does not support axioms" in lowered:
        return {
            **base,
            "error_phase": "search",
            "symptom_category": "planner_unsupported_feature",
            "root_cause_category": "unsupported_axioms_or_disjunction",
            "classification_reason": first_line_containing(combined_text, "This configuration does not support axioms"),
            "classification_confidence": "high",
        }

    if "failed to match magic word 'begin_version'" in lowered:
        return {
            **base,
            "error_phase": "driver",
            "symptom_category": "driver_sas_io_error",
            "root_cause_category": "missing_or_invalid_sas_output",
            "classification_reason": first_line_containing(combined_text, "Failed to match magic word"),
            "classification_confidence": "high",
            "classification_details": {"sas_error": "invalid_or_empty_sas_output"},
        }

    if "filenotfounderror" in lowered and "output.sas" in lowered:
        return {
            **base,
            "error_phase": "driver",
            "symptom_category": "driver_sas_io_error",
            "root_cause_category": "missing_or_invalid_sas_output",
            "classification_reason": first_line_containing(combined_text, "FileNotFoundError"),
            "classification_confidence": "high",
            "classification_details": {"missing_file": "output.sas"},
        }

    missing_type = extract_first(r"KeyError:\s*['\"]([^'\"]+)['\"]", combined_text, flags=0)
    if missing_type:
        return {
            **base,
            "error_phase": "translate",
            "symptom_category": "translator_unknown_type",
            "root_cause_category": "pddl_unknown_type",
            "classification_reason": first_line_containing(combined_text, "KeyError"),
            "classification_confidence": "high",
            "classification_details": {"missing_type": missing_type},
        }

    duplicate_object = extract_first(r"duplicate object ['\"]([^'\"]+)['\"]", combined_text)
    if duplicate_object:
        return {
            **base,
            "error_phase": "parse",
            "symptom_category": "pddl_duplicate_object",
            "root_cause_category": "duplicate_object_definition",
            "classification_reason": first_line_containing(combined_text, "duplicate object"),
            "classification_confidence": "high",
            "classification_details": {"duplicate_object": duplicate_object},
        }

    if "error in initial state specification" in lowered:
        conflicting_atom = extract_first(r"(Atom [^\n\r]+? is true and false\.)", combined_text)
        details = {}
        if conflicting_atom:
            details["conflicting_atom"] = conflicting_atom
        return {
            **base,
            "error_phase": "parse",
            "symptom_category": "pddl_initial_state_error",
            "root_cause_category": "initial_state_contradiction",
            "classification_reason": conflicting_atom or first_line_containing(combined_text, "Error in initial state specification"),
            "classification_confidence": "high",
            "classification_details": details,
        }

    if is_parse_error(combined_text):
        parse_classification = classify_parse_root_cause(combined_text, pddl)
        return {
            **base,
            "error_phase": "parse",
            "symptom_category": "pddl_parse_error",
            "classification_confidence": "high",
            **parse_classification,
        }

    if "trivially false goal" in lowered:
        return {
            **base,
            "error_phase": "search",
            "symptom_category": "planner_unsolvable",
            "root_cause_category": "trivially_false_goal",
            "classification_reason": first_line_containing(combined_text, "trivially false goal"),
            "classification_confidence": "high",
        }

    if "no relaxed solution" in lowered:
        return {
            **base,
            "error_phase": "search",
            "symptom_category": "planner_unsolvable",
            "root_cause_category": "no_relaxed_solution",
            "classification_reason": first_line_containing(combined_text, "No relaxed solution"),
            "classification_confidence": "high",
        }

    if "completely explored state space -- no solution" in lowered:
        return {
            **base,
            "error_phase": "search",
            "symptom_category": "planner_unsolvable",
            "root_cause_category": "state_space_no_solution",
            "classification_reason": first_line_containing(combined_text, "Completely explored state space"),
            "classification_confidence": "high",
        }

    if "driver aborting after translate" in lowered or record.get("return_code") in {30, 31}:
        return {
            **base,
            "error_phase": "translate",
            "symptom_category": "translator_abort_unknown",
            "root_cause_category": "translator_abort_unknown",
            "classification_reason": first_line_containing(combined_text, "Driver aborting after translate"),
            "classification_confidence": "medium",
        }

    if "traceback" in lowered:
        return {
            **base,
            "error_phase": "unknown",
            "symptom_category": "other_traceback",
            "root_cause_category": "other_traceback",
            "classification_reason": first_line_containing(combined_text, "Traceback"),
            "classification_confidence": "medium",
        }

    return base


def classify_records(records: Iterable[Dict[str, Any]], read_stdout: bool = True) -> List[Dict[str, Any]]:
    classified: List[Dict[str, Any]] = []
    for record in records:
        stdout_text = ""
        if read_stdout:
            stdout_text = read_text_if_exists(record.get("planner_stdout_path"))
        enriched = dict(record)
        enriched.update(classify_record(record, stdout_text=stdout_text))
        classified.append(enriched)
    return classified


def make_summary_rows(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Counter = Counter()
    samples: Dict[Any, Dict[str, Any]] = {}

    for record in records:
        key = (
            record.get("error_phase", "unknown"),
            record.get("symptom_category", "unknown_error"),
            record.get("root_cause_category", "unknown_error"),
        )
        counts[key] += 1
        samples.setdefault(key, record)

    rows: List[Dict[str, Any]] = []
    for key, count in counts.most_common():
        sample = samples[key]
        rows.append(
            {
                "error_phase": key[0],
                "symptom_category": key[1],
                "root_cause_category": key[2],
                "count": count,
                "sample_floorplan": sample.get("floorplan") or sample.get("floor_plan") or "",
                "sample_task_index": sample.get("task_index"),
                "sample_task": sample.get("task", ""),
                "sample_subtask": sample.get("subtask", ""),
                "sample_error_message": sample.get("error_message", ""),
            }
        )
    return rows


def write_summary_csv(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    rows = make_summary_rows(records)
    fieldnames = [
        "error_phase",
        "symptom_category",
        "root_cause_category",
        "count",
        "sample_floorplan",
        "sample_task_index",
        "sample_task",
        "sample_subtask",
        "sample_error_message",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = resolve_path(args.input)
    output_jsonl_path = resolve_path(args.output_jsonl)
    summary_csv_path = resolve_path(args.summary_csv)

    records = read_jsonl(input_path)
    classified = classify_records(records, read_stdout=not args.no_read_stdout)
    classified_count = write_jsonl(output_jsonl_path, classified)
    summary_count = write_summary_csv(summary_csv_path, classified)

    print(f"Wrote {classified_count} classified record(s) to {output_jsonl_path}")
    print(f"Wrote {summary_count} summary row(s) to {summary_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
