#!/usr/bin/env python3
"""Summarize planner and generated-code execution metrics into one CSV-style row."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASELINES = ("LaMMA-P", "SMART-LLM", "Scale-Plan", "KGLAMP")
REPO_ROOT = Path(__file__).resolve().parents[1]
SMART_LLM_RUNTIME_PATTERN = re.compile(
    r"Total Task Runtime:\s*(\d+(?:\.\d+)?)\s+seconds"
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize one parallel_runs summary and one coderun_results JSON as one comma-separated row."
    )
    parser.add_argument("--method", help="Method name to write in the output row.")
    parser.add_argument(
        "--baseline",
        choices=BASELINES,
        help="Baseline name to use as the method and baseline-specific planner source.",
    )
    parser.add_argument(
        "--parallel-run",
        help="Path to a parallel_runs directory/summary.json, or a baseline root with --baseline.",
    )
    parser.add_argument(
        "--coderun-result",
        required=True,
        help="Path to a coderun_results JSON file or a directory containing JSON files.",
    )
    parser.add_argument(
        "--output",
        help="Optional output path. Defaults to printing to stdout.",
    )
    parser.add_argument(
        "--metrics-mode",
        choices=("auto", "legacy", "v2"),
        default="auto",
        help="Use legacy columns, v2 columns, or select v2 for v2 result files.",
    )
    args = parser.parse_args(argv)
    if bool(args.method) == bool(args.baseline):
        parser.error("exactly one of --method or --baseline is required")
    if args.method and not args.parallel_run:
        parser.error("--parallel-run is required with --method")
    return args


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def read_json_list(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON list in {path}")
    return [item for item in data if isinstance(item, dict)]


def resolve_parallel_summary(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_dir():
        path = path / "summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"parallel-run summary not found: {path}")
    return path


def resolve_coderun_result(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_dir():
        candidates = [candidate for candidate in path.glob("*.json") if candidate.is_file()]
        if not candidates:
            raise FileNotFoundError(f"no JSON files found in coderun-result directory: {path}")
        path = max(candidates, key=lambda candidate: (candidate.stat().st_mtime, candidate.name))
    if not path.is_file():
        raise FileNotFoundError(f"coderun-result JSON not found: {path}")
    return path


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def numeric_value(value: Any) -> Optional[float]:
    if not is_number(value):
        return None
    return float(value)


def numeric_values(values: Iterable[Any]) -> List[float]:
    result: List[float] = []
    for value in values:
        number = numeric_value(value)
        if number is not None:
            result.append(number)
    return result


def safe_ratio(numerator: Any, denominator: Any) -> str:
    numerator_number = numeric_value(numerator)
    denominator_number = numeric_value(denominator)
    if numerator_number is None or denominator_number is None or denominator_number == 0:
        return ""
    return format_number(numerator_number / denominator_number)


def mean_and_population_stddev(values: Iterable[Any]) -> str:
    numbers = numeric_values(values)
    if not numbers:
        return ""
    mean = sum(numbers) / len(numbers)
    variance = sum((number - mean) ** 2 for number in numbers) / len(numbers)
    return f"{format_number(mean)} +- {format_number(math.sqrt(variance))}"


def token_count_value(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def planner_token_values(task_results: Sequence[Dict[str, Any]]) -> List[float]:
    tokens: List[float] = []
    for result in task_results:
        llm_token_usage = result.get("llm_token_usage")
        if not isinstance(llm_token_usage, dict) or "total_tokens" not in llm_token_usage:
            continue
        token_count = token_count_value(llm_token_usage.get("total_tokens"))
        if token_count is not None:
            tokens.append(float(token_count))
    return tokens


def planner_token_mean_stddev(task_results: Sequence[Dict[str, Any]]) -> str:
    return mean_and_population_stddev(planner_token_values(task_results))


def format_number(value: float) -> str:
    return f"{value:.4g}"


def task_results_from_summary(
    summary: Dict[str, Any],
    *,
    include_top_level_results: bool = False,
) -> List[Dict[str, Any]]:
    task_results: List[Dict[str, Any]] = []
    if include_top_level_results:
        for result in summary.get("results", []):
            if isinstance(result, dict):
                task_results.append(dict(result))

    for floor_summary in summary.get("summaries", []):
        if not isinstance(floor_summary, dict):
            continue
        results = [
            result
            for result in floor_summary.get("results", [])
            if isinstance(result, dict)
        ]
        inherited_token_usage = floor_summary.get("llm_token_usage")
        for result in results:
            result_data = dict(result)
            if (
                len(results) == 1
                and "llm_token_usage" not in result_data
                and isinstance(inherited_token_usage, dict)
            ):
                result_data["llm_token_usage"] = inherited_token_usage
            task_results.append(result_data)
    return task_results


def parallel_task_results(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return task_results_from_summary(summary, include_top_level_results=False)


def count_plan_actions(plan_file: Path) -> int:
    action_count = 0
    try:
        lines = plan_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(";") and stripped.startswith("("):
            action_count += 1
    return action_count


def plan_length_for_task(task_result: Dict[str, Any]) -> Optional[int]:
    task_run_dir = task_result.get("task_run_dir")
    if not isinstance(task_run_dir, str) or not task_run_dir:
        return None

    output_dir = Path(task_run_dir).expanduser() / "08_planner" / "outputs"
    if not output_dir.is_dir():
        return None

    plan_files = sorted(output_dir.glob("*_plan.txt"))
    if not plan_files:
        return None

    return sum(count_plan_actions(plan_file) for plan_file in plan_files)


def total_results_count(coderun_summary: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> Optional[float]:
    total_results = numeric_value(coderun_summary.get("total_results"))
    if total_results is not None:
        return total_results
    timeout_count = coderun_timeout_count(coderun_summary, results)
    result_timeout_count = sum(1 for result in results if is_timeout_result(result))
    return float(len(results) + max(0, timeout_count - result_timeout_count))


def coderun_results(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [result for result in summary.get("results", []) if isinstance(result, dict)]


def nonnegative_count(value: Any) -> int:
    number = numeric_value(value)
    if number is None:
        return 0
    return max(0, int(number))


def is_timeout_result(result: Dict[str, Any]) -> bool:
    status = result.get("status")
    return bool(result.get("timed_out")) or (
        isinstance(status, str) and status.lower() == "timeout"
    )


def coderun_timeout_count(
    coderun_summary: Dict[str, Any],
    results: Sequence[Dict[str, Any]],
) -> int:
    summary_timeout_count = nonnegative_count(coderun_summary.get("timeout_count"))
    result_timeout_count = sum(1 for result in results if is_timeout_result(result))
    return max(summary_timeout_count, result_timeout_count)


def coderun_metric_values(
    results: Sequence[Dict[str, Any]],
    metric_name: str,
) -> List[float]:
    values: List[float] = []
    for result in results:
        if is_timeout_result(result):
            values.append(0.0)
            continue
        number = numeric_value(result.get(metric_name))
        if number is not None:
            values.append(number)

    return values


def coderun_metric_columns(
    coderun_summary: Dict[str, Any],
    generate_code_denominator: Any,
    *,
    metrics_mode: str = "auto",
) -> List[str]:
    results = coderun_results(coderun_summary)
    versions = {result.get("evaluation_version", "legacy_v1") for result in results}
    if len(versions) > 1:
        raise ValueError("Mixed evaluation versions: split the summary by evaluation_version before aggregating.")
    total_results = total_results_count(coderun_summary, results)
    is_v2 = any(
        result.get("metrics_schema_version") == 2
        and result.get("evaluation_version") in {"fixed_goals_v2", "atomic_goals_v3"}
        for result in results
    )
    if metrics_mode == "v2":
        is_v2 = True
    elif metrics_mode == "legacy":
        is_v2 = False
    if is_v2:
        valid_results = [
            result for result in results if result.get("evaluation_status") == "valid"
        ]
        valid_count = len(valid_results)
        task_success_count = sum(
            1 for result in valid_results if result.get("task_success") is True
        )
        gcr_values = numeric_values(result.get("gcr") for result in valid_results)
        raw_action_values = numeric_values(
            result.get("raw_action_sr", result.get("action_sr"))
            for result in valid_results
        )
        return [
            safe_ratio(total_results, generate_code_denominator),
            format_number(total_results) if total_results is not None else "",
            format_number(float(valid_count)),
            safe_ratio(task_success_count, valid_count),
            mean_and_population_stddev(gcr_values),
            mean_and_population_stddev(raw_action_values),
        ]
    result_count = len(results)
    gcr_values = coderun_metric_values(results, "gcr")
    action_sr_values = coderun_metric_values(results, "action_sr")
    gcr_one_count = sum(1 for value in gcr_values if value == 1.0)

    return [
        safe_ratio(total_results, generate_code_denominator),
        safe_ratio(gcr_one_count, result_count),
        mean_and_population_stddev(gcr_values),
        mean_and_population_stddev(action_sr_values),
    ]


def resolve_lammap_planner_summary(baseline_root: Path) -> Path:
    candidates = sorted((baseline_root / "parallel_runs").glob("*/summary.json"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one LaMMA-P parallel_runs summary under {baseline_root}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def resolve_scale_plan_planner_summary(baseline_root: Path) -> Path:
    summary_root = baseline_root / "logs" / "scale_plan_parallel"
    candidates = sorted(summary_root.glob("pddlrun_*/summary.json"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one Scale-Plan pddlrun summary under {summary_root}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def resolve_kglamp_planner_summary(baseline_root: Path) -> Path:
    summary_root = baseline_root / "parallel_runs"
    candidates = sorted(summary_root.glob("*/summary.json"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one KGLAMP top-level parallel_runs summary under "
            f"{summary_root}, found {len(candidates)}"
        )
    return candidates[0]


def resolve_baseline_results_path(baseline: str, baseline_root: Path) -> Path:
    if baseline in {"LaMMA-P", "Scale-Plan", "KGLAMP"}:
        path = baseline_root / "plan_to_code_results" / "plan_to_code_results.json"
    elif baseline == "SMART-LLM":
        path = baseline_root / "plan_to_code_results.json"
    else:
        raise RuntimeError(f"Unsupported baseline: {baseline}")
    if not path.is_file():
        raise FileNotFoundError(f"baseline plan-to-code results not found: {path}")
    return path


def smart_llm_decomposed_plan_count(baseline_root: Path) -> int:
    logs_dir = baseline_root / "logs"
    if not logs_dir.is_dir():
        return 0
    return sum(1 for path in logs_dir.rglob("decomposed_plan.py") if path.is_file())


def smart_llm_planner_time_values(baseline_root: Path) -> List[float]:
    logs_dir = baseline_root / "logs"
    if not logs_dir.is_dir():
        return []

    values: List[float] = []
    for log_path in sorted(logs_dir.rglob("log.txt")):
        if not log_path.is_file():
            continue
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            match = SMART_LLM_RUNTIME_PATTERN.fullmatch(line.strip())
            if match:
                values.append(float(match.group(1)))
                break
    return values


def default_baseline_root(baseline: str) -> Path:
    if baseline not in BASELINES:
        raise RuntimeError(f"Unsupported baseline: {baseline}")
    return REPO_ROOT / "baselines" / baseline


def planner_summary_denominator(summary: Dict[str, Any]) -> float:
    return (
        (numeric_value(summary.get("success_count")) or 0.0)
        + (numeric_value(summary.get("failure_count")) or 0.0)
    )


def is_successful_plan_to_code_record(result: Dict[str, Any]) -> bool:
    status = result.get("status")
    return result.get("success") is True or (
        isinstance(status, str) and status.lower() == "success"
    )


def baseline_action_count_values(results: Sequence[Dict[str, Any]]) -> List[float]:
    values: List[float] = []
    for result in results:
        if not is_successful_plan_to_code_record(result):
            continue
        action_count = numeric_value(result.get("action_count"))
        if action_count is not None:
            values.append(action_count)
    return values


def baseline_planner_metrics(
    summary_paths: Sequence[Path],
) -> Tuple[List[Dict[str, Any]], float]:
    task_results: List[Dict[str, Any]] = []
    generate_code_denominator = 0.0

    for summary_path in summary_paths:
        planner_summary = read_json(summary_path)
        summary_task_results = task_results_from_summary(
            planner_summary,
            include_top_level_results=True,
        )

        task_results.extend(summary_task_results)
        generate_code_denominator += planner_summary_denominator(planner_summary)

    return task_results, generate_code_denominator


def build_planner_baseline_row(
    baseline: str,
    baseline_root: Path,
    coderun_summary: Dict[str, Any],
    summary_paths: Sequence[Path],
    metrics_mode: str = "auto",
) -> List[str]:
    baseline_results = read_json_list(resolve_baseline_results_path(baseline, baseline_root))
    task_results, generate_code_denominator = baseline_planner_metrics(summary_paths)

    return [
        baseline,
        planner_token_mean_stddev(task_results),
        mean_and_population_stddev(result.get("duration_seconds") for result in task_results),
        "",
        "",
        mean_and_population_stddev(baseline_action_count_values(baseline_results)),
        *coderun_metric_columns(
            coderun_summary, generate_code_denominator, metrics_mode=metrics_mode
        ),
    ]


def build_lammap_row(
    baseline_root: Path,
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    return build_planner_baseline_row(
        "LaMMA-P",
        baseline_root,
        coderun_summary,
        [resolve_lammap_planner_summary(baseline_root)],
        metrics_mode,
    )


def build_smart_llm_row(
    baseline_root: Path,
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    baseline_results = read_json_list(resolve_baseline_results_path("SMART-LLM", baseline_root))
    generate_code_denominator = smart_llm_decomposed_plan_count(baseline_root)

    return [
        "SMART-LLM",
        "",
        mean_and_population_stddev(smart_llm_planner_time_values(baseline_root)),
        "",
        "",
        mean_and_population_stddev(baseline_action_count_values(baseline_results)),
        *coderun_metric_columns(
            coderun_summary, generate_code_denominator, metrics_mode=metrics_mode
        ),
    ]


def build_scale_plan_row(
    baseline_root: Path,
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    return build_planner_baseline_row(
        "Scale-Plan",
        baseline_root,
        coderun_summary,
        [resolve_scale_plan_planner_summary(baseline_root)],
        metrics_mode,
    )


def build_kglamp_row(
    baseline_root: Path,
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    return build_planner_baseline_row(
        "KGLAMP",
        baseline_root,
        coderun_summary,
        [resolve_kglamp_planner_summary(baseline_root)],
        metrics_mode,
    )


def build_baseline_row(
    baseline: str,
    baseline_root_value: Optional[str],
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    baseline_root = (
        Path(baseline_root_value).expanduser()
        if baseline_root_value
        else default_baseline_root(baseline)
    )
    if not baseline_root.is_dir():
        raise FileNotFoundError(f"baseline root not found or not a directory: {baseline_root}")
    if baseline == "LaMMA-P":
        return build_lammap_row(baseline_root, coderun_summary, metrics_mode)
    if baseline == "SMART-LLM":
        return build_smart_llm_row(baseline_root, coderun_summary, metrics_mode)
    if baseline == "Scale-Plan":
        return build_scale_plan_row(baseline_root, coderun_summary, metrics_mode)
    if baseline == "KGLAMP":
        return build_kglamp_row(baseline_root, coderun_summary, metrics_mode)
    raise RuntimeError(f"Unsupported baseline: {baseline}")


def build_row(
    method: str,
    parallel_summary: Dict[str, Any],
    coderun_summary: Dict[str, Any],
    metrics_mode: str = "auto",
) -> List[str]:
    task_results = parallel_task_results(parallel_summary)

    plan_denominator = (
        (numeric_value(parallel_summary.get("success_count")) or 0.0)
        + (numeric_value(parallel_summary.get("failure_count")) or 0.0)
    )
    return [
        method,
        planner_token_mean_stddev(task_results),
        mean_and_population_stddev(result.get("duration_seconds") for result in task_results),
        safe_ratio(parallel_summary.get("all_pass_count"), plan_denominator),
        safe_ratio(parallel_summary.get("pass_one_count"), plan_denominator),
        mean_and_population_stddev(plan_length_for_task(result) for result in task_results),
        *coderun_metric_columns(
            coderun_summary, plan_denominator, metrics_mode=metrics_mode
        ),
    ]


def render_row(row: Sequence[str]) -> str:
    return ",".join(row) + "\n"


def write_or_print(text: str, output: Optional[str]) -> None:
    if output:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        return
    print(text, end="")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    coderun_summary = read_json(resolve_coderun_result(args.coderun_result))
    if args.baseline:
        row = build_baseline_row(
            args.baseline,
            args.parallel_run,
            coderun_summary,
            args.metrics_mode,
        )
    else:
        parallel_summary = read_json(resolve_parallel_summary(args.parallel_run))
        row = build_row(
            args.method,
            parallel_summary,
            coderun_summary,
            args.metrics_mode,
        )
    write_or_print(render_row(row), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
