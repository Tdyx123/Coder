import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_PARALLEL_RUNS_DIR = Path("parallel_runs")
DEFAULT_WITH_ERRORS_NAME = "no_plan_with_errors.jsonl"
ERROR_MARKERS = (
    "error",
    "traceback",
    "exception",
    "failed",
    "unsolvable",
    "no solution",
    "driver aborting",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract PDDL subtasks that did not produce planner plans from a "
            "pddlrun_llmseparate parallel summary."
        )
    )
    parser.add_argument(
        "--summary",
        nargs="+",
        default=None,
        help=(
            "Path(s) to parallel run summary JSON files. "
            f"Default: {DEFAULT_PARALLEL_RUNS_DIR}/*/summary.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for output jsonl files. Default: the summary file directory.",
    )
    parser.add_argument(
        "--with-errors-name",
        default=DEFAULT_WITH_ERRORS_NAME,
        help=f"Filename for no-plan records with errors. Default: {DEFAULT_WITH_ERRORS_NAME}",
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def dedupe_sorted_paths(paths: Iterable[Path]) -> List[Path]:
    return sorted(set(paths), key=lambda path: str(path))


def discover_default_summary_paths(parallel_runs_dir: Path = DEFAULT_PARALLEL_RUNS_DIR) -> List[Path]:
    root = parallel_runs_dir.expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    return dedupe_sorted_paths(path.resolve() for path in root.glob("*/summary.json") if path.is_file())


def resolve_summary_paths(summary_values: Optional[Sequence[str]]) -> Tuple[List[Path], bool]:
    if summary_values is None:
        return discover_default_summary_paths(), True
    return dedupe_sorted_paths(resolve_path(value) for value in summary_values), False


def resolve_output_dir(
    output_dir_value: Optional[str],
    summary_paths: Sequence[Path],
    used_default_summaries: bool,
) -> Path:
    if output_dir_value:
        return resolve_path(output_dir_value)

    if used_default_summaries:
        return resolve_path(str(DEFAULT_PARALLEL_RUNS_DIR))

    if len(summary_paths) == 1:
        return summary_paths[0].parent

    common_parent = os.path.commonpath([str(path.parent) for path in summary_paths])
    return Path(common_parent).resolve()


def read_text_if_exists(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def path_str(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    return str(path)


def iter_task_results(summary: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if isinstance(summary.get("summaries"), list):
        for floor_summary in summary["summaries"]:
            for result in floor_summary.get("results", []):
                yield result
        return

    for result in summary.get("results", []):
        yield result


def parse_subtask(problem_file: str) -> Tuple[Optional[int], str]:
    match = re.search(r"(subtask_(\d+))", problem_file)
    if not match:
        return None, Path(problem_file).stem
    return int(match.group(2)), match.group(1)


def planner_plan_path(task_run_dir: Path, problem_file: str) -> Path:
    return task_run_dir / "08_planner" / "outputs" / f"{Path(problem_file).stem}_plan.txt"


def compatibility_plan_path(task_run_dir: Path, entry: Dict[str, Any]) -> Optional[Path]:
    value = entry.get("compatibility_output")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = task_run_dir / path
    return path


def resolve_pddl_path(task_run_dir: Path, problem_file: str, subtask: str) -> Optional[Path]:
    validated = task_run_dir / "07_validate" / "outputs" / problem_file
    if validated.exists():
        return validated

    fallback = task_run_dir / "05_problem_generation" / "outputs" / f"{subtask}_problem.pddl"
    if fallback.exists():
        return fallback

    return None


def resolve_subtask_path(task_run_dir: Path, subtask: str) -> Path:
    return task_run_dir / "04_problem_files" / "subtasks" / f"{subtask}.txt"


def resolve_plan(
    task_run_dir: Path,
    entry: Dict[str, Any],
) -> Tuple[Optional[Path], Optional[str], bool]:
    problem_file = entry.get("problem_file", "")
    primary_path = planner_plan_path(task_run_dir, problem_file)
    primary_text = read_text_if_exists(primary_path)
    if primary_text and primary_text.strip():
        return primary_path, primary_text, True

    compat_path = compatibility_plan_path(task_run_dir, entry)
    compat_text = read_text_if_exists(compat_path)
    if compat_text and compat_text.strip():
        return compat_path, compat_text, True

    return primary_path, None, False


def find_error_message(stdout_text: Optional[str], stderr_text: Optional[str]) -> Optional[str]:
    combined_lines: List[str] = []
    if stderr_text:
        combined_lines.extend(stderr_text.splitlines())
    if stdout_text:
        combined_lines.extend(stdout_text.splitlines())

    for line in combined_lines:
        lowered = line.lower()
        if any(marker in lowered for marker in ERROR_MARKERS):
            return line
    return None


def summarize_error(
    entry: Dict[str, Any],
    stdout_text: Optional[str],
    stderr_text: Optional[str],
) -> Optional[str]:
    message = find_error_message(stdout_text, stderr_text)
    if message:
        return message

    return_code = entry.get("return_code")
    if isinstance(return_code, int) and return_code != 0:
        return f"planner return_code: {return_code}"
    if stderr_text and stderr_text.strip():
        return stderr_text.splitlines()[0]
    return None


def has_error(entry: Dict[str, Any], stdout_text: Optional[str], stderr_text: Optional[str]) -> bool:
    return_code = entry.get("return_code")
    if isinstance(return_code, int) and return_code != 0:
        return True
    if stderr_text and stderr_text.strip():
        return True
    return find_error_message(stdout_text, stderr_text) is not None


def build_record(
    summary_path: Path,
    result: Dict[str, Any],
    entry: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    task_run_dir = Path(result["task_run_dir"])
    problem_file = entry.get("problem_file", "")
    subtask_index, subtask = parse_subtask(problem_file)

    stdout_path = task_run_dir / entry["stdout_path"] if entry.get("stdout_path") else None
    stderr_path = task_run_dir / entry["stderr_path"] if entry.get("stderr_path") else None
    stdout_text = read_text_if_exists(stdout_path)
    stderr_text = read_text_if_exists(stderr_path)

    plan_path, plan_text, record_has_plan = resolve_plan(task_run_dir, entry)
    pddl_path = resolve_pddl_path(task_run_dir, problem_file, subtask)
    subtask_path = resolve_subtask_path(task_run_dir, subtask)

    record_has_error = has_error(entry, stdout_text, stderr_text)
    floor_plan = str(result.get("floor_plan", ""))
    record = {
        "floorplan": floor_plan,
        "floor_plan": floor_plan,
        "task_index": result.get("task_index"),
        "task": result.get("task", ""),
        "subtask_index": subtask_index,
        "subtask": subtask,
        "subtask_text": read_text_if_exists(subtask_path),
        "pddl_path": path_str(pddl_path),
        "pddl": read_text_if_exists(pddl_path),
        "plan_path": path_str(plan_path),
        "plan": plan_text,
        "return_code": entry.get("return_code"),
        "has_error": record_has_error,
        "error_message": summarize_error(entry, stdout_text, stderr_text),
        "planner_stdout_path": path_str(stdout_path),
        "planner_stderr_path": path_str(stderr_path),
        "planner_stderr": stderr_text,
        "task_run_dir": str(task_run_dir),
        "summary_path": str(summary_path),
    }
    return record, record_has_plan


def extract_no_plan_records(summary_path: Path) -> List[Dict[str, Any]]:
    summary = read_json(summary_path)
    with_errors: List[Dict[str, Any]] = []

    for result in iter_task_results(summary):
        task_run_dir_value = result.get("task_run_dir")
        if not task_run_dir_value:
            continue

        task_run_dir = Path(task_run_dir_value)
        manifest_path = task_run_dir / "08_planner" / "planner_manifest.json"
        if not manifest_path.exists():
            continue

        for entry in read_json(manifest_path):
            record, record_has_plan = build_record(summary_path, result, entry)
            if record_has_plan:
                continue
            if record["has_error"]:
                with_errors.append(record)

    return with_errors


def extract_no_plan_records_from_summaries(summary_paths: Iterable[Path]) -> List[Dict[str, Any]]:
    with_errors: List[Dict[str, Any]] = []
    for summary_path in summary_paths:
        with_errors.extend(extract_no_plan_records(summary_path))
    return with_errors


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    summary_paths, used_default_summaries = resolve_summary_paths(args.summary)
    if not summary_paths:
        print(
            f"No summary files found at {resolve_path(str(DEFAULT_PARALLEL_RUNS_DIR))}/*/summary.json",
            file=sys.stderr,
        )
        return 1

    output_dir = resolve_output_dir(args.output_dir, summary_paths, used_default_summaries)
    with_errors = extract_no_plan_records_from_summaries(summary_paths)
    with_errors_path = output_dir / args.with_errors_name

    with_errors_count = write_jsonl(with_errors_path, with_errors)

    print(
        f"Wrote {with_errors_count} record(s) with errors from "
        f"{len(summary_paths)} summary file(s) to {with_errors_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
