import argparse
import contextlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from run_config import RunConfig, load_run_config, normalize_floor_plan


@dataclass(frozen=True)
class TaskJob:
    floor_plan: str
    task_index: int
    record: Dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parallel wrapper for pddlrun_llmseparate.py. "
            "Runs multiple floor plans and multiple task indices in parallel."
        )
    )
    parser.add_argument("--floor-plans", nargs="+", required=True, help="Floor plans to execute")
    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--test-set", type=str, default="final_test")
    parser.add_argument("--max-floor-plan-workers", type=int, default=2)
    parser.add_argument("--max-task-workers", type=int, default=2)
    parser.add_argument("--output-root", type=str, default=None)
    parser.add_argument("--prompt-decompse-set", type=str, default="pddl_train_task_decomposesep")
    parser.add_argument("--prompt-allocation-set", type=str, default="pddl_train_task_allocationsep")
    parser.add_argument(
        "--disable-log-results",
        action="store_true",
        help="Accepted for compatibility; this parallel runner does not write legacy log_results output.",
    )
    return parser.parse_args()


def floor_plan_sort_key(value: str) -> tuple:
    prefix_digits = []
    suffix = []
    digit_phase = True
    for ch in value:
        if digit_phase and ch.isdigit():
            prefix_digits.append(ch)
        else:
            digit_phase = False
            suffix.append(ch)
    number = int("".join(prefix_digits)) if prefix_digits else -1
    return (number, "".join(suffix))


def safe_count(value: Any) -> int:
    return value if isinstance(value, int) else 0


def all_subtasks_passed(result: Dict[str, Any]) -> bool:
    total = safe_count(result.get("total"))
    tc = safe_count(result.get("tc"))
    return total > 0 and tc == total


def load_jobs(
    config: RunConfig,
    test_set: str,
    floor_plan: str,
) -> List[TaskJob]:
    normalized = normalize_floor_plan(floor_plan)
    dataset_file = config.dataset_file(test_set, normalized)
    if not dataset_file.exists():
        raise FileNotFoundError(f"Dataset file not found: {dataset_file}")

    jobs: List[TaskJob] = []
    with dataset_file.open("r", encoding="utf-8") as handle:
        for idx, raw_line in enumerate(handle):
            line = raw_line.strip()
            if not line:
                continue
            jobs.append(TaskJob(floor_plan=normalized, task_index=idx, record=json.loads(line)))
    return jobs


def run_single_job(
    repo_root: Path,
    config: RunConfig,
    args: argparse.Namespace,
    job: TaskJob,
    objects_ai: str,
) -> Dict[str, Any]:
    started_at = time.time()
    status = "success"
    error_message = None

    try:
        from pddlrun_llmseparate import run_single_floor_plan_task

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_single_floor_plan_task(
                base_path=str(repo_root),
                model=args.model,
                floor_plan=job.floor_plan,
                task_record=job.record,
                prompt_decompse_set=args.prompt_decompse_set,
                prompt_allocation_set=args.prompt_allocation_set,
                objects_ai=objects_ai,
                config=config,
                test_set=args.test_set,
            )
    except Exception as exc:
        status = "error"
        result = {}
        error_message = str(exc)

    summary: Dict[str, Any] = {
        "floor_plan": job.floor_plan,
        "model": args.model,
        "task_index": job.task_index,
        "task": job.record.get("task", ""),
        "status": status,
        "duration_seconds": round(time.time() - started_at, 3),
    }
    if error_message:
        summary["error"] = error_message
    if result:
        summary["task_run_dir"] = result.get("task_run_dir")
        summary["tc"] = result.get("tc")
        summary["total"] = result.get("total")
    return summary


def run_floor_plan_jobs(
    repo_root: Path,
    output_root: Path,
    config: RunConfig,
    args: argparse.Namespace,
    floor_plan: str,
) -> Dict[str, Any]:
    from pddlrun_llmseparate import PDDLUtils

    jobs = load_jobs(config, args.test_set, floor_plan)
    floor_plan_key = normalize_floor_plan(floor_plan)
    objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(int(PDDLUtils.extract_floor_plan_number(floor_plan_key)), config)}"
    results: List[Dict[str, Any]] = []

    print(f"[FloorPlan{floor_plan_key}] loaded {len(jobs)} task(s)")

    with ThreadPoolExecutor(max_workers=args.max_task_workers) as executor:
        future_map = {
            executor.submit(run_single_job, repo_root, config, args, job, objects_ai): job
            for job in jobs
        }
        for future in as_completed(future_map):
            job = future_map[future]
            result = future.result()
            results.append(result)
            print(
                f"[FloorPlan{floor_plan_key}] task {job.task_index + 1}/{len(jobs)} "
                f"{result['status']} in {result['duration_seconds']}s"
            )

    results.sort(key=lambda item: item["task_index"])
    summary = {
        "floor_plan": floor_plan_key,
        "task_count": len(results),
        "success_count": sum(1 for item in results if item["status"] == "success"),
        "failure_count": sum(1 for item in results if item["status"] != "success"),
        "all_pass_count": sum(1 for item in results if all_subtasks_passed(item)),
        "pass_one_count": sum(1 for item in results if safe_count(item.get("tc")) > 0),
        "results": results,
    }
    floor_plan_summary = output_root / f"FloorPlan{floor_plan_key}" / "summary.json"
    floor_plan_summary.parent.mkdir(parents=True, exist_ok=True)
    floor_plan_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    config = load_run_config(repo_root)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_root = (
        config.resolve_path(args.output_root)
        if args.output_root
        else config.path("storage", "parallel_output_root") / f"pddlrun_llmseparate_{timestamp}"
    )
    output_root.mkdir(parents=True, exist_ok=True)

    floor_plans = [normalize_floor_plan(value) for value in args.floor_plans]
    summaries: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=args.max_floor_plan_workers) as executor:
        future_map = {
            executor.submit(run_floor_plan_jobs, repo_root, output_root, config, args, floor_plan): floor_plan
            for floor_plan in floor_plans
        }
        for future in as_completed(future_map):
            floor_plan = future_map[future]
            summary = future.result()
            summaries.append(summary)
            print(
                f"[FloorPlan{floor_plan}] completed: "
                f"{summary['success_count']}/{summary['task_count']} success"
            )

    summaries.sort(key=lambda item: floor_plan_sort_key(item["floor_plan"]))
    final_summary = {
        "created_at": timestamp,
        "repo_root": str(repo_root),
        "output_root": str(output_root),
        "test_set": args.test_set,
        "floor_plan_count": len(summaries),
        "success_count": sum([summary["success_count"] for summary in summaries]),
        "failure_count": sum([summary["failure_count"] for summary in summaries]),
        "all_pass_count": sum([summary["all_pass_count"] for summary in summaries]),
        "pass_one_count": sum([summary["pass_one_count"] for summary in summaries]),
        "summaries": summaries,
    }
    summary_file = output_root / "summary.json"
    summary_file.write_text(json.dumps(final_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    total_tasks = sum(item["task_count"] for item in summaries)
    total_success = sum(item["success_count"] for item in summaries)
    print(f"All jobs finished: {total_success}/{total_tasks} tasks succeeded")
    print(f"Summary written to: {summary_file}")


if __name__ == "__main__":
    main()
