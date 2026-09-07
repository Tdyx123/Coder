"""Shared runtime for generated plan-to-code executables."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import tempfile
import time
import types
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system.action_plan import TaskPlan
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.evaluation import EvaluationContext
from executor_system.execution_control import (
    ExecutionCancelled, ExecutionShutdownTimeout, PlanExecutionTimeout,
    close_runtime, error_record,
)
from executor_system.movement import MovementConfig
from executor_system.parallel_runner import (
    TolerantRunStats,
    effective_timeout_seconds,
    run_action_plan_tolerant,
    write_result_json,
)
from executor_system.run_results import task_key_for_executable
from executor_system.runtime import ThorRuntime
from executor_system.task_plan import run_action_plan

import resources.robots as robot_catalog


DEFAULT_RUNNER_TIMEOUT_SECONDS = 30.0


def load_task_record(task_file: str, task_index: int) -> Dict[str, Any]:
    path = Path(task_file).expanduser()
    if not path.is_file():
        raise RuntimeError(f"TASK_FILE not found: {path}")
    if task_index < 0:
        raise RuntimeError("TASK_INDEX must be 0-based and non-negative.")

    with path.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise RuntimeError(f"TASK_FILE line {task_index} is empty: {path}")
            return json.loads(line)

    raise RuntimeError(f"TASK_INDEX {task_index} is out of range for {path}")


def floor_plan_from_task_file(task_file: str) -> str:
    match = re.search(r"FloorPlan(\d+)\.jsonl$", str(task_file))
    if not match:
        raise RuntimeError(f"Cannot infer floor plan from TASK_FILE: {task_file}")
    return match.group(1)


def build_robot_team(robot_ids: Sequence[Any]) -> List[Dict[str, Any]]:
    team: List[Dict[str, Any]] = []
    for index, raw_robot_id in enumerate(robot_ids):
        robot_id = int(raw_robot_id)
        if robot_id < 1 or robot_id > len(robot_catalog.robots):
            raise RuntimeError(f"Invalid robot id in task record: {raw_robot_id!r}")
        robot = copy.deepcopy(robot_catalog.robots[robot_id - 1])
        robot["name"] = f"robot{index + 1}"
        team.append(robot)
    if not team:
        raise RuntimeError("Task record has no robots in 'robot list'.")
    return team


def transition_metric(no_trans: int, no_trans_gt: int, max_trans: int) -> float:
    max_trans_value = max_trans + 1
    no_trans_gt_value = no_trans_gt + 1
    if max_trans_value == no_trans_gt_value and no_trans_gt_value == no_trans:
        return 1.0
    if max_trans_value == no_trans_gt_value:
        return 0.0
    return (max_trans_value - no_trans) / (max_trans_value - no_trans_gt_value)


def build_hardcoded_bundle(bundle_data: Dict[str, Any]) -> types.SimpleNamespace:
    if "gcr" not in bundle_data:
        raise RuntimeError("BUNDLE_DATA is missing required 'gcr' target object_states.")
    if not isinstance(bundle_data["gcr"], list):
        raise RuntimeError("BUNDLE_DATA['gcr'] must be a list of target object_states.")

    noop_subtasks = bundle_data.get("noop_subtasks", [])
    if not isinstance(noop_subtasks, list):
        raise RuntimeError("BUNDLE_DATA['noop_subtasks'] must be a list when provided.")
    if any(
        not isinstance(item, dict) or not bool(item.get("verified"))
        for item in noop_subtasks
    ):
        raise RuntimeError("BUNDLE_DATA contains an unverified no-op subtask.")

    return types.SimpleNamespace(
        task=bundle_data["task"],
        task_plan=TaskPlan.from_dict(bundle_data["task_plan"]),
        gcr=list(bundle_data["gcr"]),
        no_trans=int(bundle_data["no_trans"]),
        phases=bundle_data["phases"],
        object_mappings=bundle_data["object_mappings"],
        object_mapping_warnings=bundle_data["object_mapping_warnings"],
        object_id_bindings=bundle_data.get("object_id_bindings", []),
        noop_subtasks=list(noop_subtasks),
    )


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a hardcoded pddlrun bundle through executor_system."
    )
    parser.add_argument(
        "--runner-mode",
        action="store_true",
        help="Run without rendering and emit machine-readable runner metrics.",
    )
    parser.add_argument(
        "--metrics-output",
        default="",
        help="Path to write runner-mode metrics JSON.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=finite_positive_seconds,
        default=None,
        help="Runner timeout; defaults to 30s for teleport and 120s for step.",
    )
    parser.add_argument(
        "--movement-mode",
        choices=("teleport", "step"),
        default=None,
        help="Robot movement mode; otherwise LAMMAP_MOVEMENT_MODE or step.",
    )
    parser.add_argument("--execution-policy", choices=("legacy", "strict"), default="legacy")
    return parser.parse_args(argv)


def finite_positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return seconds


def runner_metrics_path(raw_path: str, script_file: str) -> Path:
    if raw_path:
        return Path(raw_path).expanduser()
    return Path(script_file).resolve().parent / "parallel_run_result.json"


def build_runner_result(status: str, start_time: float) -> Dict[str, Any]:
    return {
        "status": status,
        "timed_out": False,
        "timeout_message": "",
        "run_time_seconds": time.monotonic() - start_time,
        "gcr": None,
        "tc": None,
        "sr": None,
        "ru": None,
        "executed_actions": 0,
        "failed_actions": 0,
        "failure_action_ratio": 0.0,
        "robot_failures": [],
        "metrics_schema_version": 2,
        "evaluation_version": "fixed_goals_v2",
        "execution_policy": "legacy",
        "process_status": "failed",
        "execution_status": "failed",
        "evaluation_status": "incomplete",
        "task_success": None,
        "original_goal_count": None,
        "satisfied_goal_count": None,
        "worker_errors": [],
        "cleanup_errors": [],
        "phase_durations_seconds": {name: 0.0 for name in
                                    ("startup", "execution", "evaluation", "cleanup")},
    }


def record_execution_error(result, exc, runtime=None):
    result.setdefault('cleanup_errors', []).extend(
        getattr(exc, 'execution_cleanup_errors', [])
    )
    report = getattr(runtime, 'execution_report', {}) if runtime is not None else {}
    result.update(report)
    cause = exc
    timed_out = bool(report.get('timed_out'))
    while cause is not None:
        timed_out = timed_out or isinstance(cause, PlanExecutionTimeout)
        cause = cause.__cause__
    status = 'timeout' if timed_out else ('cancelled' if isinstance(exc, (ExecutionCancelled, KeyboardInterrupt, SystemExit)) else 'failed')
    result.update(status=status, process_status=status, execution_status=status,
                  evaluation_status='incomplete', task_success=None,
                  error=str(exc), timed_out=timed_out, action_sr=None)
    for key in ('gcr', 'tc', 'sr', 'ru', 'satisfied_goal_count'):
        result[key] = None
    return 124 if timed_out else 1


def finalize_runner_result(runtime, result, start_time, metrics_path):
    cleanup_start = time.monotonic()
    try:
        if runtime is not None:
            result['worker_errors'] = list(getattr(runtime, 'worker_errors', []))
            result.update(getattr(runtime, 'action_metrics', {}))
            context = getattr(runtime, 'evaluation_context', None)
            if context is not None:
                result['original_goal_count'] = len(context.goals)
            try:
                result['movement_mode'] = runtime.movement_config.mode.value
                result['navigation_metrics'] = runtime.navigation_metrics.to_dict()
            except Exception as exc:
                result['cleanup_errors'].append(error_record(exc, phase='cleanup'))
            close_runtime(runtime, result['cleanup_errors'])
    finally:
        _context.runtime = None
        result['phase_durations_seconds']['cleanup'] = time.monotonic() - cleanup_start
        result['run_time_seconds'] = time.monotonic() - start_time
        write_result_json(metrics_path, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


def close_standalone_runtime(runtime, start_time, script_file, task_index, *, identity=None):
    cleanup_start = time.monotonic()
    errors = []
    try:
        close_runtime(runtime, errors)
    finally:
        _context.runtime = None
    if errors:
        # Ordinary entrypoints must also leave an artifact when closing fails.
        result = build_runner_result('failed', start_time)
        result.update(identity or runner_identity(script_file, task_index))
        failure = RuntimeError(errors[0]['message'])
        record_execution_error(result, failure, runtime)
        result.update(getattr(runtime, 'action_metrics', {}))
        result['worker_errors'] = list(getattr(runtime, 'worker_errors', []))
        result['cleanup_errors'] = errors
        result['phase_durations_seconds']['cleanup'] = time.monotonic() - cleanup_start
        result['run_time_seconds'] = time.monotonic() - start_time
        context = getattr(runtime, 'evaluation_context', None)
        if context is not None:
            result['original_goal_count'] = len(context.goals)
        write_result_json(runner_metrics_path('', script_file), result)
        raise failure


def runner_identity(script_file: str, task_index: int) -> Dict[str, Any]:
    """Use the parent identity when present, otherwise identify standalone runs."""

    raw_attempt = os.environ.get("LAMMAP_ATTEMPT", "1")
    try:
        attempt = int(raw_attempt)
    except ValueError as exc:
        raise RuntimeError("LAMMAP_ATTEMPT must be an integer") from exc
    return {
        "run_id": os.environ.get("LAMMAP_RUN_ID", uuid.uuid4().hex),
        "task_key": os.environ.get(
            "LAMMAP_TASK_KEY", task_key_for_executable(Path(script_file))
        ),
        "attempt": attempt,
    }


def runtime_output_root(
    metrics_path: Optional[Path],
    identity: Mapping[str, Any],
) -> Path:
    """Choose one owned output directory for this runtime attempt.

    Parent-supervised executions already reserve an ``attempt_N`` directory;
    media belongs beside that attempt's metrics and logs.  Standalone runs use
    an identity-addressed directory below the system temporary area.
    """

    if metrics_path is not None:
        return Path(metrics_path).expanduser().resolve().parent

    run_id = str(identity["run_id"])
    task_key = str(identity["task_key"])
    attempt = identity["attempt"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise RuntimeError("run_id must be a safe output path component")
    if not re.fullmatch(r"[0-9a-f]{64}", task_key):
        raise RuntimeError("task_key must be a SHA-256 executable path digest")
    if type(attempt) is not int or attempt < 1:
        raise RuntimeError("attempt must be a positive integer")
    return (
        Path(tempfile.gettempdir())
        / "lammap-runtime"
        / run_id
        / task_key
        / f"attempt_{attempt}"
    ).resolve()


def _runtime_inputs(
    bundle_data: Dict[str, Any],
    task_file: str,
    task_index: int,
) -> tuple[Dict[str, Any], str, List[Dict[str, Any]], List[Dict[str, Any]], types.SimpleNamespace]:
    task_record = load_task_record(task_file, task_index)
    floor_no = floor_plan_from_task_file(task_file)
    robots = build_robot_team(task_record.get("robot list") or [])
    bundle = build_hardcoded_bundle(bundle_data)
    ground_truth = list(bundle.gcr)
    _demo_state.set_ground_truth(ground_truth)
    return task_record, floor_no, robots, ground_truth, bundle


def run_standalone(
    bundle_data: Dict[str, Any],
    task_file: str,
    task_index: int,
    movement_mode: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    script_file: Optional[str] = None,
    *,
    execution_policy: str = "legacy",
) -> int:
    start_time = time.monotonic()
    failure_result = None
    script_file = script_file or __file__
    identity = runner_identity(script_file, task_index)
    task_record, floor_no, robots, ground_truth, bundle = _runtime_inputs(
        bundle_data,
        task_file,
        task_index,
    )

    if bundle.object_mapping_warnings:
        for warning in bundle.object_mapping_warnings:
            print(f"WARNING: {warning}")

    runtime = ThorRuntime(
        robots,
        floor_no,
        CLOUD_RENDERING,
        RENDER_IMAGE,
        movement_mode=movement_mode,
        output_root=runtime_output_root(None, identity),
    )
    runtime.evaluation_context = EvaluationContext.from_goals(
        bundle.gcr,
        allow_empty=bool(bundle.noop_subtasks) and not bundle.task_plan.stages,
    )
    runtime.register_object_id_bindings(bundle.object_id_bindings)
    _context.runtime = runtime
    try:
        if bundle.task_plan.stages:
            run_action_plan(bundle.task_plan, execution_policy=execution_policy, timeout_seconds=effective_timeout_seconds(
                runtime.movement_config.mode.value, timeout_seconds))
        elif not bundle.noop_subtasks:
            run_action_plan(bundle.task_plan, execution_policy=execution_policy, timeout_seconds=effective_timeout_seconds(
                runtime.movement_config.mode.value, timeout_seconds))
        runtime.step({"action": "Done"}, check_success=False)

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        ru = transition_metric(bundle.no_trans, no_trans_gt, max_trans)
        evaluation_valid = metrics["evaluation_status"] == "valid"
        if not evaluation_valid:
            ru = None
        sr = (
            1 if metrics["tc"] == 1.0 and ru == 1.0 else 0
        ) if evaluation_valid else None
        tc_display = int(metrics["tc"]) if metrics["tc"] is not None else None
        print(
            "SR:{sr}, TC:{tc}, GCR:{gcr}, Exec:{exec_rate}, RU:{ru}".format(
                sr=sr,
                tc=tc_display,
                gcr=metrics["gcr"],
                exec_rate=metrics["exec_rate"],
                ru=ru,
            )
        )
        runtime.log_unmet_goals(ground_truth)
        runtime.generate_video()
        runtime.write_final_metadata()
        return 0
    except BaseException as exc:
        failure_result = build_runner_result('failed', start_time)
        failure_result.update(identity)
        failure_result['execution_policy'] = execution_policy
        record_execution_error(failure_result, exc, runtime)
        raise
    finally:
        if failure_result is not None:
            finalize_runner_result(runtime, failure_result, start_time,
                                   runner_metrics_path('', script_file))
        else:
            close_standalone_runtime(
                runtime, start_time, script_file, task_index, identity=identity
            )


def run_runner_mode(
    args: argparse.Namespace,
    bundle_data: Dict[str, Any],
    task_file: str,
    task_index: int,
    script_file: str,
) -> int:
    start_time = time.monotonic()
    metrics_path = runner_metrics_path(args.metrics_output, script_file)
    result = build_runner_result("failed", start_time)
    result.update(runner_identity(script_file, task_index))
    result["execution_policy"] = getattr(args, "execution_policy", "legacy")
    return_code = 1
    runtime = None
    interrupt = None
    phase = "startup"
    phase_start = start_time

    try:
        resolved_movement = MovementConfig.resolve(args.movement_mode)
        result["movement_mode"] = resolved_movement.mode.value
        result["navigation_metrics"] = {}
        task_record, floor_no, robots, ground_truth, bundle = _runtime_inputs(
            bundle_data,
            task_file,
            task_index,
        )
        if bundle.object_mapping_warnings:
            result["object_mapping_warnings"] = list(bundle.object_mapping_warnings)

        runtime = ThorRuntime(
            robots,
            floor_no,
            CLOUD_RENDERING,
            False,
            movement_mode=args.movement_mode,
            output_root=runtime_output_root(metrics_path, result),
        )
        runtime.evaluation_context = EvaluationContext.from_goals(
            bundle.gcr,
            allow_empty=bool(bundle.noop_subtasks) and not bundle.task_plan.stages,
        )
        result["movement_mode"] = runtime.movement_config.mode.value
        result["navigation_metrics"] = runtime.navigation_metrics.to_dict()
        runtime.register_object_id_bindings(bundle.object_id_bindings)
        _context.runtime = runtime
        result['phase_durations_seconds'][phase] = time.monotonic() - phase_start
        phase, phase_start = 'execution', time.monotonic()

        if bundle.task_plan.stages or not bundle.noop_subtasks:
            execution_report = run_action_plan_tolerant(
                runtime,
                bundle.task_plan,
                execution_policy=result["execution_policy"],
                timeout_seconds=effective_timeout_seconds(
                    runtime.movement_config.mode.value,
                    args.timeout_seconds,
                ),
            )
        else:
            execution_report = TolerantRunStats(execution_policy=result["execution_policy"]).to_dict()
            execution_report.update(execution_status="completed", execution_quiescent=True, scheduler_version=2)
        result.update(execution_report)
        if execution_report.get('timed_out'):
            raise PlanExecutionTimeout(execution_report.get('timeout_message', 'execution timed out'))
        if (execution_report.get('execution_quiescent') is False
                or (execution_report.get('scheduler_version') == 2
                    and execution_report.get('execution_quiescent') is not True)):
            raise RuntimeError('execution did not become quiescent; final evaluation is unavailable')
        if execution_report.get('execution_status') == 'cancelled':
            raise ExecutionCancelled('execution cancelled')
        result['phase_durations_seconds'][phase] = time.monotonic() - phase_start
        phase, phase_start = 'evaluation', time.monotonic()
        try:
            runtime.step({"action": "Done"}, check_success=False, save_frame=False)
        except RuntimeError as exc:
            result["done_error"] = str(exc)
            raise

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        result["ru_inputs"] = {
            "no_trans": bundle.no_trans,
            "no_trans_gt": no_trans_gt,
            "max_trans": max_trans,
        }
        evaluation_valid = (
            metrics["evaluation_status"] == "valid"
            and not bool(execution_report.get("timed_out"))
        )
        ru = (
            transition_metric(bundle.no_trans, no_trans_gt, max_trans)
            if evaluation_valid
            else None
        )
        result.update(
            {
                "status": "timeout" if execution_report.get("timed_out") else "success",
                "process_status": "timeout" if execution_report.get("timed_out") else "completed",
                "execution_status": execution_report.get("execution_status", (
                    "partial" if execution_report.get("action_counts", {}).get("failed", 0)
                    else "completed")),
                "execution_quiescent": execution_report.get("execution_quiescent", True),
                "gcr": metrics["gcr"] if evaluation_valid else None,
                "tc": metrics["tc"] if evaluation_valid else None,
                "sr": (
                    1 if metrics["tc"] == 1.0 and ru == 1.0 else 0
                ) if evaluation_valid else None,
                "ru": ru,
                "exec_rate": metrics["exec_rate"],
                "evaluation_version": metrics["evaluation_version"],
                "evaluation_status": metrics["evaluation_status"],
                "original_goal_count": metrics["original_goal_count"],
                "satisfied_goal_count": (
                    metrics["satisfied_goal_count"] if evaluation_valid else None
                ),
                "goal_results": metrics["goal_results"],
                "task_success": (
                    bool(metrics["tc"] == 1.0 and ru == 1.0)
                    if evaluation_valid
                    else None
                ),
            }
        )
        return_code = 124 if result.get("timed_out") else 0
    except BaseException as exc:
        return_code = record_execution_error(result, exc, runtime)
        if not isinstance(exc, Exception):
            interrupt = (exc, exc.__traceback__)
    finally:
        result['phase_durations_seconds'][phase] = time.monotonic() - phase_start
        finalize_runner_result(runtime, result, start_time, metrics_path)

    if interrupt is not None:
        raise interrupt[0].with_traceback(interrupt[1])

    return return_code


def main(
    bundle_data: Dict[str, Any],
    task_file: str,
    task_index: int,
    script_file: str,
    argv: Optional[Sequence[str]] = None,
) -> int:
    args = parse_arguments(argv)
    if args.runner_mode:
        return run_runner_mode(args, bundle_data, task_file, task_index, script_file)
    return run_standalone(
        bundle_data,
        task_file,
        task_index,
        movement_mode=args.movement_mode,
        timeout_seconds=args.timeout_seconds,
        script_file=script_file,
        execution_policy=args.execution_policy,
    )
