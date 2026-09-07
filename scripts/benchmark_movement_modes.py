#!/usr/bin/env python3
"""Select and compare generated plans in teleport and step movement modes."""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _path in (_THIS_DIR, _REPO_ROOT):
    _path_text = str(_path)
    if _path_text not in sys.path:
        sys.path.insert(0, _path_text)

from executor_system.parallel_runner import (  # noqa: E402
    effective_timeout_seconds,
    run_generated_executable,
)


CATEGORY_RANGES = (
    ("kitchen", 1, 30),
    ("living_room", 201, 230),
    ("bedroom", 301, 330),
    ("bathroom", 401, 430),
)
STRATA = ("single_navigation", "concurrent_goto", "multiple_waves")
MOVEMENT_MODES = ("teleport", "step")


class CandidateRejected(RuntimeError):
    """Raised when a generated plan is unsafe to include in the benchmark."""


def _literal_assignments(tree: ast.AST) -> Dict[str, Any]:
    wanted = {"BUNDLE_DATA", "TASK_FILE", "TASK_INDEX"}
    values = {}
    for node in getattr(tree, "body", ()):  # pragma: no branch - module body
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in wanted:
                continue
            try:
                values[target.id] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError) as exc:
                raise CandidateRejected(
                    f"{target.id} is not a literal assignment"
                ) from exc
    missing = wanted - set(values)
    if missing:
        raise CandidateRejected(
            "missing literal assignment(s): " + ", ".join(sorted(missing))
        )
    return values


def _imports_generated_runtime(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "executor_system.generated_plan_runtime"
        and any(alias.name == "main" for alias in node.names)
        for node in ast.walk(tree)
    )


def _resolve_task_file(raw_path: Any, repo_root: Path) -> Path:
    supplied = Path(str(raw_path)).expanduser()
    candidates = [supplied]
    if not supplied.is_absolute():
        candidates.append(repo_root / supplied)
    candidates.append(repo_root / "data" / supplied.name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CandidateRejected(f"TASK_FILE does not exist: {supplied}")


def _load_task_record(task_file: Path, task_index: int) -> Dict[str, Any]:
    if task_index < 0:
        raise CandidateRejected("TASK_INDEX must be non-negative")
    with task_file.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index == task_index:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CandidateRejected(
                        f"invalid task JSON at line {task_index}"
                    ) from exc
                if not isinstance(record, dict):
                    raise CandidateRejected("task record must be a JSON object")
                return record
    raise CandidateRejected(f"TASK_INDEX {task_index} is out of range")


def _action_type(raw_action: Any) -> str:
    if isinstance(raw_action, Mapping):
        return str(raw_action.get("action_type") or raw_action.get("name") or "")
    return str(getattr(raw_action, "action_type", ""))


def classify_stratum(task_plan: Mapping[str, Any]) -> str:
    navigation_waves = []
    for stage in task_plan.get("stages") or ():
        queues = (stage or {}).get("robot_action_queues") or {}
        maximum_cursor = max((len(queue or ()) for queue in queues.values()), default=0)
        for cursor in range(maximum_cursor):
            goto_count = sum(
                1
                for queue in queues.values()
                if cursor < len(queue or ())
                and _action_type(queue[cursor]) == "GoToObject"
            )
            if goto_count:
                navigation_waves.append(goto_count)
    total_navigation = sum(navigation_waves)
    if total_navigation == 1:
        return "single_navigation"
    if any(count >= 2 for count in navigation_waves):
        return "concurrent_goto"
    if len(navigation_waves) >= 2:
        return "multiple_waves"
    raise CandidateRejected("task plan does not match a navigation stratum")


def floor_category(floor_plan: int) -> str:
    for category, minimum, maximum in CATEGORY_RANGES:
        if minimum <= floor_plan <= maximum:
            return category
    raise CandidateRejected(f"FloorPlan{floor_plan} is outside benchmark ranges")


def inspect_candidate(path: Path, repo_root: Path) -> Dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        source = resolved.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(resolved))
    except (OSError, SyntaxError) as exc:
        raise CandidateRejected(f"cannot parse generated plan: {exc}") from exc
    if not _imports_generated_runtime(tree):
        raise CandidateRejected("does not import executor_system.generated_plan_runtime")

    values = _literal_assignments(tree)
    bundle = values["BUNDLE_DATA"]
    if not isinstance(bundle, dict):
        raise CandidateRejected("BUNDLE_DATA must be a dictionary")
    task_plan = bundle.get("task_plan")
    if not isinstance(task_plan, dict):
        raise CandidateRejected("BUNDLE_DATA.task_plan must be a dictionary")

    task_file = _resolve_task_file(values["TASK_FILE"], repo_root)
    try:
        task_index = int(values["TASK_INDEX"])
    except (TypeError, ValueError) as exc:
        raise CandidateRejected("TASK_INDEX must be an integer") from exc
    task_record = _load_task_record(task_file, task_index)
    robots = task_record.get("robot list") or []
    if not isinstance(robots, list) or not 2 <= len(robots) <= 4:
        raise CandidateRejected("physical robot count must be between 2 and 4")

    floor_match = re.search(r"FloorPlan(\d+)", task_file.name)
    if floor_match is None:
        raise CandidateRejected(f"cannot infer FloorPlan from {task_file.name}")
    floor_plan = int(floor_match.group(1))
    task_id = str(task_plan.get("task_id") or task_record.get("task_id") or "")
    if not task_id:
        raise CandidateRejected("task plan is missing task_id")
    try:
        relative_path = resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        relative_path = resolved.as_posix()
    return {
        "category": floor_category(floor_plan),
        "stratum": classify_stratum(task_plan),
        "path": relative_path,
        "task_id": task_id,
        "robot_count": len(robots),
        "floor_plan": floor_plan,
        "resolved_path": resolved.as_posix(),
    }


def _candidate_paths(selection_root: Path) -> Tuple[Path, ...]:
    """Scan the known generated-run layout without descending into artifacts."""

    pending = [(selection_root, 0)]
    found = []
    while pending:
        directory, depth = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == "plan_to_code":
                executable = Path(entry.path) / "executable_plan.py"
                if executable.is_file():
                    found.append(executable)
                continue
            if depth < 3:
                pending.append((Path(entry.path), depth + 1))
    return tuple(sorted(found))


def select_manifest(root: Path, *, repo_root: Path = _REPO_ROOT) -> Dict[str, Any]:
    selection_root = root.expanduser().resolve()
    if not selection_root.is_dir():
        raise RuntimeError(f"selection root does not exist: {selection_root}")
    candidates = []
    exclusions = []
    for path in _candidate_paths(selection_root):
        try:
            candidates.append(inspect_candidate(path, repo_root.resolve()))
        except CandidateRejected as exc:
            try:
                excluded_path = path.resolve().relative_to(
                    repo_root.resolve()
                ).as_posix()
            except ValueError:
                excluded_path = path.resolve().as_posix()
            exclusions.append({"path": excluded_path, "reason": str(exc)})

    candidates.sort(
        key=lambda item: (
            int(item["floor_plan"]),
            str(item["task_id"]),
            str(item["resolved_path"]),
        )
    )
    selected = {}
    for candidate in candidates:
        selected.setdefault(
            (candidate["category"], candidate["stratum"]),
            candidate,
        )

    missing = [
        f"{category}/{stratum}"
        for category, _minimum, _maximum in CATEGORY_RANGES
        for stratum in STRATA
        if (category, stratum) not in selected
    ]
    if missing:
        raise RuntimeError(
            "benchmark selection is missing required bucket(s): "
            + ", ".join(missing)
        )

    cases = []
    for category, _minimum, _maximum in CATEGORY_RANGES:
        for stratum in STRATA:
            item = dict(selected[(category, stratum)])
            item.pop("floor_plan", None)
            item.pop("resolved_path", None)
            cases.append(item)
    try:
        recorded_root = selection_root.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        recorded_root = str(selection_root)
    return {
        "version": 1,
        "cases": cases,
        "selection": {
            "root": recorded_root,
            "eligible_candidates": len(candidates),
            "excluded": exclusions,
        },
    }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _nearest_rank_percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[rank - 1]


def _aggregate_mode(mode: str, results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    items = [item for item in results if item.get("mode") == mode]
    navigation_requests = 0
    navigation_successes = 0
    navigation_failures = 0
    successful_navigation_cases = 0
    navigation_teleports = 0
    replans = 0
    planning_durations = []
    for item in items:
        metrics = item.get("navigation_metrics") or {}
        requests = int(metrics.get("requests", 0) or 0)
        successes = int(metrics.get("successes", 0) or 0)
        failures = int(metrics.get("failures", 0) or 0)
        navigation_requests += requests
        navigation_successes += successes
        navigation_failures += failures
        if (
            requests > 0
            and failures == 0
            and successes >= requests
            and not item.get("timed_out")
        ):
            successful_navigation_cases += 1
        action_counts = metrics.get("action_counts") or {}
        navigation_teleports += int(action_counts.get("Teleport", 0) or 0)
        replans += int(metrics.get("replans", 0) or 0)
        planning_durations.extend(
            _number(value)
            for value in metrics.get("planning_durations_seconds") or ()
        )

    case_count = len(items)
    v2_items = [
        item
        for item in items
        if item.get("metrics_schema_version") == 2
        and item.get("evaluation_version") == "fixed_goals_v2"
    ]
    metric_items = (
        [item for item in v2_items if item.get("evaluation_status") == "valid"]
        if v2_items and len(v2_items) == len(items)
        else items
    )
    gcr_values = [_number(item.get("gcr")) for item in metric_items]
    tc_values = [_number(item.get("tc")) for item in metric_items]
    sr_values = [_number(item.get("sr")) for item in metric_items]
    return {
        "mode": mode,
        "case_count": case_count,
        "process_successes": sum(
            1
            for item in items
            if item.get("status") == "success" and not item.get("timed_out")
        ),
        "navigation_requests": navigation_requests,
        "navigation_successes": navigation_successes,
        "navigation_failures": navigation_failures,
        "navigation_success_rate": (
            navigation_successes / navigation_requests
            if navigation_requests
            else 0.0
        ),
        "cases_without_navigation_failure": successful_navigation_cases,
        "goto_navigation_teleports": navigation_teleports,
        "replans": replans,
        "success_gcr": sum(gcr_values) / len(gcr_values) if gcr_values else 0.0,
        "mean_tc": sum(tc_values) / len(tc_values) if tc_values else 0.0,
        "mean_sr": sum(sr_values) / len(sr_values) if sr_values else 0.0,
        "total_run_time_seconds": sum(
            _number(item.get("run_time_seconds")) for item in items
        ),
        "planner_fixture_p95_seconds": _nearest_rank_percentile(
            planning_durations,
            0.95,
        ),
        "all_subprocesses_within_timeout": all(
            not bool(item.get("timed_out")) for item in items
        ),
    }


def _result_groups(results: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for result in results:
        key = (
            result.get("metrics_schema_version", 1),
            result.get("evaluation_version", "legacy_v1"),
            result.get("execution_policy", "legacy"),
            result.get("mode", result.get("movement_mode", "step")),
            result.get("scheduler_version", 1),
        )
        group = grouped.setdefault(
            key,
            {
                "metrics_schema_version": key[0],
                "evaluation_version": key[1],
                "execution_policy": key[2],
                "movement_mode": key[3],
                "scheduler_version": key[4],
                "total_task_count": 0,
                "valid_evaluation_count": 0,
                "task_success_count": 0,
            },
        )
        group["total_task_count"] += 1
        if result.get("evaluation_status") == "valid":
            group["valid_evaluation_count"] += 1
            if result.get("task_success") is True:
                group["task_success_count"] += 1
    return [grouped[key] for key in sorted(grouped, key=repr)]


def build_benchmark_report(
    manifest: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    copied_results = [dict(item) for item in results]
    groups = _result_groups(copied_results)
    for group in groups:
        members = [
            result
            for result in copied_results
            if result.get("metrics_schema_version", 1)
            == group["metrics_schema_version"]
            and result.get("evaluation_version", "legacy_v1")
            == group["evaluation_version"]
            and result.get("execution_policy", "legacy")
            == group["execution_policy"]
            and result.get("scheduler_version", 1) == group["scheduler_version"]
            and result.get("mode", result.get("movement_mode", "step"))
            == group["movement_mode"]
        ]
        group["aggregate"] = _aggregate_mode(group["movement_mode"], members)
    return {
        "version": 1,
        "manifest_version": manifest.get("version"),
        "case_count": len(manifest.get("cases") or ()),
        "results": copied_results,
        "result_groups": groups,
        "aggregates": {
            mode: _aggregate_mode(mode, copied_results)
            for mode in MOVEMENT_MODES
        },
    }


def acceptance_failures(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    aggregates = report.get("aggregates") or {}
    step = aggregates.get("step") or {}
    teleport = aggregates.get("teleport") or {}
    failures = []

    def reject(code: str, actual: Any, expected: str) -> None:
        failures.append({"code": code, "actual": actual, "expected": expected})

    if _number(step.get("navigation_success_rate")) < 0.90:
        reject(
            "step_navigation_success_rate",
            step.get("navigation_success_rate"),
            ">= 0.90",
        )
    if int(step.get("cases_without_navigation_failure", 0) or 0) < 10:
        reject(
            "step_successful_navigation_cases",
            step.get("cases_without_navigation_failure"),
            ">= 10",
        )
    if int(step.get("goto_navigation_teleports", 0) or 0) != 0:
        reject(
            "step_navigation_teleports",
            step.get("goto_navigation_teleports"),
            "== 0",
        )
    gcr_gap = _number(teleport.get("success_gcr")) - _number(
        step.get("success_gcr")
    )
    if gcr_gap > 0.05 + 1e-12:
        reject("success_gcr_gap", gcr_gap, "<= 0.05")
    if _number(step.get("planner_fixture_p95_seconds")) > 0.250 + 1e-12:
        reject(
            "planner_fixture_p95",
            step.get("planner_fixture_p95_seconds"),
            "<= 0.250",
        )
    if not bool(step.get("all_subprocesses_within_timeout")):
        reject("step_subprocess_timeout", False, "all completed within timeout")
    return failures


def _failure_category(result: Mapping[str, Any]) -> str:
    if result.get("timed_out"):
        return "timeout"
    if result.get("status") != "success":
        return "process_failure"
    if result.get("execution_status") in {"failed", "partial", "cancelled"}:
        return "execution_failure"
    if result.get("evaluation_status") == "valid" and result.get("task_success") is False:
        return "goal_failure"
    metrics = result.get("navigation_metrics") or {}
    if int(metrics.get("failures", 0) or 0):
        return "navigation_failure"
    return ""


def run_benchmark(
    manifest: Mapping[str, Any],
    *,
    repo_root: Path = _REPO_ROOT,
    explicit_timeout: Optional[float] = None,
    execution_policy: str = "legacy",
) -> Dict[str, Any]:
    cases = manifest.get("cases") or []
    results = []
    with tempfile.TemporaryDirectory(prefix="movement_benchmark_") as temp_dir:
        metrics_root = Path(temp_dir) / execution_policy
        metrics_root.mkdir()
        for case_index, case in enumerate(cases):
            executable_path = (repo_root / str(case["path"])).resolve()
            if not executable_path.is_file():
                raise RuntimeError(f"benchmark executable does not exist: {executable_path}")
            for mode in MOVEMENT_MODES:
                timeout_seconds = effective_timeout_seconds(mode, explicit_timeout)
                metrics_path = metrics_root / f"{case_index:02d}_{mode}.json"
                raw_result = run_generated_executable(
                    executable_path,
                    metrics_output=metrics_path,
                    timeout_seconds=timeout_seconds,
                    movement_mode=mode,
                    execution_policy=execution_policy,
                    save_all_stdout=False,
                )
                result = {
                    **dict(case),
                    **raw_result,
                    "mode": mode,
                    "effective_timeout_seconds": timeout_seconds,
                }
                result["failure_category"] = _failure_category(result)
                results.append(result)
    report = build_benchmark_report(manifest, results)
    report["acceptance_failures"] = acceptance_failures(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Robot Movement Mode Benchmark",
        "",
        "| Case | Mode | Status | Navigation | Teleport | Replans | GCR | TC | SR | Runtime (s) | Failure |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in report.get("results") or ():
        metrics = result.get("navigation_metrics") or {}
        actions = metrics.get("action_counts") or {}
        lines.append(
            "| {case} | {mode} | {status} | {successes}/{requests} | "
            "{teleports} | {replans} | {gcr:.3f} | {tc:.3f} | {sr:.3f} | "
            "{runtime:.3f} | {failure} |".format(
                case=result.get("task_id") or result.get("path"),
                mode=result.get("mode", ""),
                status=result.get("status", ""),
                successes=int(metrics.get("successes", 0) or 0),
                requests=int(metrics.get("requests", 0) or 0),
                teleports=int(actions.get("Teleport", 0) or 0),
                replans=int(metrics.get("replans", 0) or 0),
                gcr=_number(result.get("gcr")),
                tc=_number(result.get("tc")),
                sr=_number(result.get("sr")),
                runtime=_number(result.get("run_time_seconds")),
                failure=result.get("failure_category", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Aggregates",
            "",
            "| Mode | Navigation success | Successful cases | Navigation Teleport | GCR | Planner P95 (s) | Within timeout |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for mode in MOVEMENT_MODES:
        aggregate = (report.get("aggregates") or {}).get(mode) or {}
        lines.append(
            "| {mode} | {nav:.3f} | {cases} | {teleports} | {gcr:.3f} | "
            "{p95:.3f} | {timeout} |".format(
                mode=mode,
                nav=_number(aggregate.get("navigation_success_rate")),
                cases=int(aggregate.get("cases_without_navigation_failure", 0) or 0),
                teleports=int(aggregate.get("goto_navigation_teleports", 0) or 0),
                gcr=_number(aggregate.get("success_gcr")),
                p95=_number(aggregate.get("planner_fixture_p95_seconds")),
                timeout="yes" if aggregate.get("all_subprocesses_within_timeout") else "no",
            )
        )
    failures = report.get("acceptance_failures") or acceptance_failures(report)
    lines.extend(["", "## Acceptance", ""])
    if not failures:
        lines.append("PASS")
    else:
        lines.extend(
            f"- {item['code']}: actual={item['actual']!r}, expected {item['expected']}"
            for item in failures
        )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-manifest", action="store_true")
    parser.add_argument("--root", default="logs/intermediate_runs")
    parser.add_argument(
        "--manifest",
        default="tests/fixtures/movement_benchmark_plans.json",
    )
    parser.add_argument("--output-json", default="reports/movement_modes_benchmark.json")
    parser.add_argument("--output-md", default="reports/movement_modes_benchmark.md")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--execution-policy", choices=("legacy", "strict"), default="legacy")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    manifest_path = Path(args.manifest).expanduser()
    try:
        if args.select_manifest:
            manifest = select_manifest(
                Path(args.root),
                repo_root=_REPO_ROOT,
            )
            _write_json(manifest_path, manifest)
            print(f"Movement benchmark manifest saved to: {manifest_path}")
            return 0

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = run_benchmark(
            manifest,
            repo_root=_REPO_ROOT,
            explicit_timeout=args.timeout_seconds,
            execution_policy=args.execution_policy,
        )
        output_json = Path(args.output_json).expanduser()
        output_json = output_json.parent / args.execution_policy / output_json.name
        output_md = Path(args.output_md).expanduser()
        output_md = output_md.parent / args.execution_policy / output_md.name
        _write_json(output_json, report)
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(render_markdown(report), encoding="utf-8")
        print(f"Movement benchmark JSON saved to: {output_json}")
        print(f"Movement benchmark Markdown saved to: {output_md}")
        if args.check and report.get("acceptance_failures"):
            return 1
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
