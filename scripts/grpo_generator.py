#!/usr/bin/env python3
"""Generate GRPO JSONL from two comparable parallel run summaries."""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOCATE_PROMPT_PATH = Path("02_allocate/01_allocate_prompt.txt")
ALLOCATE_OUTPUT_PATH = Path("02_allocate/02_allocate_output.txt")

TaskKey = Tuple[str, str, int]
SummaryResult = Tuple[Dict[str, Any], Dict[str, Any]]


@dataclass
class GenerationStats:
    first_results_seen: int = 0
    second_results_seen: int = 0
    matched_tasks: int = 0
    better_tasks: int = 0
    examples_written: int = 0
    skipped_invalid_task_key: int = 0
    skipped_missing_first_task: int = 0
    skipped_task_text_mismatch: int = 0
    skipped_missing_tc: int = 0
    skipped_not_better: int = 0
    skipped_missing_task_run_dir: int = 0
    skipped_missing_artifact: int = 0
    skipped_unreadable_artifact: int = 0
    skipped_empty_artifact: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "first_results_seen": self.first_results_seen,
            "second_results_seen": self.second_results_seen,
            "matched_tasks": self.matched_tasks,
            "better_tasks": self.better_tasks,
            "examples_written": self.examples_written,
            "skipped_invalid_task_key": self.skipped_invalid_task_key,
            "skipped_missing_first_task": self.skipped_missing_first_task,
            "skipped_task_text_mismatch": self.skipped_task_text_mismatch,
            "skipped_missing_tc": self.skipped_missing_tc,
            "skipped_not_better": self.skipped_not_better,
            "skipped_missing_task_run_dir": self.skipped_missing_task_run_dir,
            "skipped_missing_artifact": self.skipped_missing_artifact,
            "skipped_unreadable_artifact": self.skipped_unreadable_artifact,
            "skipped_empty_artifact": self.skipped_empty_artifact,
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate GRPO JSONL for tasks whose tc is strictly higher in the "
            "second parallel run than in the first."
        )
    )
    parser.add_argument(
        "--first-run",
        required=True,
        help="Baseline run name, run directory, or summary.json path.",
    )
    parser.add_argument(
        "--second-run",
        required=True,
        help="Candidate run name, run directory, or summary.json path.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSONL path. Relative paths are resolved from the repository root.",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def _summary_file(path: Path) -> Optional[Path]:
    candidate = path / "summary.json" if path.is_dir() else path
    if candidate.is_file():
        return candidate.resolve()
    return None


def resolve_summary_path(run_value: str, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve a run name, run directory, or summary file to summary.json."""
    value = str(run_value).strip()
    if not value:
        raise ValueError("parallel run value must be non-empty")

    raw_path = Path(value).expanduser()
    candidates: List[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend([
            repo_root / raw_path,
            repo_root / "parallel_runs" / raw_path,
        ])

    seen = set()
    for candidate in candidates:
        candidate_key = str(candidate)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        summary_path = _summary_file(candidate)
        if summary_path is not None:
            return summary_path

    raise FileNotFoundError(f"parallel run summary not found: {run_value}")


def read_summary(summary_path: Path) -> Dict[str, Any]:
    try:
        with summary_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in summary {summary_path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"unable to read summary {summary_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"summary must contain a JSON object: {summary_path}")
    return data


def _result_list(value: Any, location: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{location} must be a JSON array")

    results: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{location}[{index}] must be a JSON object")
        results.append(item)
    return results


def flatten_summary_results(summary_data: Dict[str, Any]) -> List[SummaryResult]:
    nested_results: List[SummaryResult] = []
    summaries_value = summary_data.get("summaries")
    if summaries_value is not None:
        if not isinstance(summaries_value, list):
            raise ValueError("summaries must be a JSON array")
        for summary_index, floor_summary in enumerate(summaries_value):
            if not isinstance(floor_summary, dict):
                raise ValueError(f"summaries[{summary_index}] must be a JSON object")
            results = _result_list(
                floor_summary.get("results"),
                f"summaries[{summary_index}].results",
            )
            nested_results.extend((floor_summary, result) for result in results)

    if nested_results:
        return nested_results

    top_level_results = _result_list(summary_data.get("results"), "results")
    if top_level_results:
        return [({}, result) for result in top_level_results]

    raise ValueError("summary does not contain any result records")


def normalize_floor_plan(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold().startswith("floorplan"):
        text = text[len("FloorPlan"):].strip()
    return text


def normalize_task_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def normalize_task_index(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        integer = int(value)
        return integer if integer >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\+?\d+", value.strip()):
        return int(value)
    return None


def task_key(
    test_set: str,
    floor_summary: Dict[str, Any],
    result: Dict[str, Any],
) -> Optional[TaskKey]:
    floor_plan = normalize_floor_plan(
        result.get("floor_plan") or floor_summary.get("floor_plan")
    )
    task_index = normalize_task_index(result.get("task_index"))
    if not floor_plan or task_index is None:
        return None
    return (test_set, floor_plan, task_index)


def numeric_tc(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validated_test_sets(
    first_summary: Dict[str, Any],
    second_summary: Dict[str, Any],
) -> Tuple[str, str]:
    first_test_set = str(first_summary.get("test_set") or "").strip()
    second_test_set = str(second_summary.get("test_set") or "").strip()
    if first_test_set and second_test_set and first_test_set != second_test_set:
        raise ValueError(
            "parallel runs use different test_set values: "
            f"{first_test_set!r} != {second_test_set!r}"
        )
    return first_test_set, second_test_set


def _check_duplicate_task_keys(
    summary_results: Iterable[SummaryResult],
    test_set: str,
    summary_path: Path,
) -> None:
    seen = set()
    for floor_summary, result in summary_results:
        key = task_key(test_set, floor_summary, result)
        if key is None:
            continue
        if key in seen:
            raise ValueError(f"duplicate task key {key!r} in summary {summary_path}")
        seen.add(key)


def resolve_task_run_dir(
    value: Any,
    summary_data: Dict[str, Any],
    summary_path: Path,
) -> Optional[Path]:
    if value is None or not str(value).strip():
        return None

    raw_path = Path(str(value)).expanduser()
    if raw_path.is_absolute():
        return raw_path

    candidates: List[Path] = []
    summary_repo_root = summary_data.get("repo_root")
    if summary_repo_root:
        candidates.append(Path(str(summary_repo_root)).expanduser() / raw_path)
    candidates.extend([REPO_ROOT / raw_path, summary_path.parent / raw_path])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0] if candidates else raw_path


def _read_artifact(path: Path) -> Tuple[Optional[str], Optional[str]]:
    if not path.is_file():
        return None, "missing"
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "unreadable"
    if not content.strip():
        return None, "empty"
    return content, None


def generate_grpo_examples(
    first_run: str,
    second_run: str,
    repo_root: Path = REPO_ROOT,
) -> Tuple[List[Dict[str, Any]], GenerationStats]:
    first_summary_path = resolve_summary_path(first_run, repo_root)
    second_summary_path = resolve_summary_path(second_run, repo_root)
    first_summary = read_summary(first_summary_path)
    second_summary = read_summary(second_summary_path)
    first_test_set, second_test_set = _validated_test_sets(first_summary, second_summary)

    first_results = flatten_summary_results(first_summary)
    second_results = flatten_summary_results(second_summary)
    _check_duplicate_task_keys(first_results, first_test_set, first_summary_path)
    _check_duplicate_task_keys(second_results, second_test_set, second_summary_path)

    stats = GenerationStats(
        first_results_seen=len(first_results),
        second_results_seen=len(second_results),
    )
    first_by_key: Dict[TaskKey, Dict[str, Any]] = {}
    for floor_summary, result in first_results:
        key = task_key(first_test_set, floor_summary, result)
        if key is not None:
            first_by_key[key] = result

    examples: List[Dict[str, Any]] = []
    for floor_summary, second_result in second_results:
        second_key = task_key(second_test_set, floor_summary, second_result)
        if second_key is None:
            stats.skipped_invalid_task_key += 1
            continue

        lookup_key = (first_test_set, second_key[1], second_key[2])
        first_result = first_by_key.get(lookup_key)
        if first_result is None:
            stats.skipped_missing_first_task += 1
            continue

        first_task = normalize_task_text(first_result.get("task"))
        second_task = normalize_task_text(second_result.get("task"))
        if not first_task or not second_task or first_task != second_task:
            stats.skipped_task_text_mismatch += 1
            continue

        stats.matched_tasks += 1
        first_tc = numeric_tc(first_result.get("tc"))
        second_tc = numeric_tc(second_result.get("tc"))
        if first_tc is None or second_tc is None:
            stats.skipped_missing_tc += 1
            continue
        if second_tc <= first_tc:
            stats.skipped_not_better += 1
            continue

        stats.better_tasks += 1
        task_run_dir = resolve_task_run_dir(
            second_result.get("task_run_dir"),
            second_summary,
            second_summary_path,
        )
        if task_run_dir is None or not task_run_dir.is_dir():
            stats.skipped_missing_task_run_dir += 1
            continue

        prompt, prompt_error = _read_artifact(task_run_dir / ALLOCATE_PROMPT_PATH)
        solution, solution_error = _read_artifact(task_run_dir / ALLOCATE_OUTPUT_PATH)
        artifact_errors = (prompt_error, solution_error)
        if "missing" in artifact_errors:
            stats.skipped_missing_artifact += 1
            continue
        if "unreadable" in artifact_errors:
            stats.skipped_unreadable_artifact += 1
            continue
        if "empty" in artifact_errors:
            stats.skipped_empty_artifact += 1
            continue

        assert prompt is not None and solution is not None
        examples.append({
            "messages": [{"role": "user", "content": prompt}],
            "solution": solution,
        })
        stats.examples_written += 1

    return examples, stats


def resolve_output_path(output_path: str, repo_root: Path = REPO_ROOT) -> Path:
    path = Path(output_path).expanduser()
    return path if path.is_absolute() else repo_root / path


def write_grpo_jsonl(
    examples: Sequence[Dict[str, Any]],
    output_path: str,
    repo_root: Path = REPO_ROOT,
) -> Path:
    path = resolve_output_path(output_path, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False) + "\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        examples, stats = generate_grpo_examples(args.first_run, args.second_run)
        output_path = write_grpo_jsonl(examples, args.output)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Wrote {len(examples)} GRPO example(s) to {output_path}")
    for key, value in stats.as_dict().items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
