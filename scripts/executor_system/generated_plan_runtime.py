"""Shared runtime for generated plan-to-code executables."""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system.action_plan import TaskPlan
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.parallel_runner import run_action_plan_tolerant, write_result_json
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

    return types.SimpleNamespace(
        task=bundle_data["task"],
        task_plan=TaskPlan.from_dict(bundle_data["task_plan"]),
        gcr=list(bundle_data["gcr"]),
        no_trans=int(bundle_data["no_trans"]),
        phases=bundle_data["phases"],
        object_mappings=bundle_data["object_mappings"],
        object_mapping_warnings=bundle_data["object_mapping_warnings"],
        object_id_bindings=bundle_data.get("object_id_bindings", []),
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
        type=float,
        default=DEFAULT_RUNNER_TIMEOUT_SECONDS,
        help="Runner-mode total timeout in seconds.",
    )
    return parser.parse_args(argv)


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
    }


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
) -> int:
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
    )
    runtime.register_object_id_bindings(bundle.object_id_bindings)
    _context.runtime = runtime
    try:
        run_action_plan(bundle.task_plan)
        runtime.step({"action": "Done"}, check_success=False)

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        ru = transition_metric(bundle.no_trans, no_trans_gt, max_trans)
        sr = 1 if metrics["tc"] == 1.0 and ru == 1.0 else 0
        print(
            "SR:{sr}, TC:{tc}, GCR:{gcr}, Exec:{exec_rate}, RU:{ru}".format(
                sr=sr,
                tc=int(metrics["tc"]),
                gcr=metrics["gcr"],
                exec_rate=metrics["exec_rate"],
                ru=ru,
            )
        )
        runtime.log_unmet_goals(ground_truth)
        runtime.generate_video()
        runtime.write_final_metadata()
        return 0
    finally:
        runtime.stop()
        _context.runtime = None


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
    return_code = 1
    runtime = None

    try:
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
        )
        runtime.register_object_id_bindings(bundle.object_id_bindings)
        _context.runtime = runtime

        execution_report = run_action_plan_tolerant(
            runtime,
            bundle.task_plan,
            timeout_seconds=args.timeout_seconds,
        )
        result.update(execution_report)
        try:
            runtime.step({"action": "Done"}, check_success=False, save_frame=False)
        except RuntimeError as exc:
            result["done_error"] = str(exc)

        metrics = runtime.evaluate(ground_truth)
        no_trans_gt = int(task_record.get("trans", 0) or 0)
        max_trans = int(task_record.get("min_trans", task_record.get("max_trans", 0)) or 0)
        ru = transition_metric(bundle.no_trans, no_trans_gt, max_trans)
        result.update(
            {
                "status": "timeout" if execution_report.get("timed_out") else "success",
                "gcr": metrics["gcr"],
                "tc": metrics["tc"],
                "sr": 1 if metrics["tc"] == 1.0 and ru == 1.0 else 0,
                "ru": ru,
                "exec_rate": metrics["exec_rate"],
            }
        )
        return_code = 124 if result.get("timed_out") else 0
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "error": str(exc),
            }
        )
        return_code = 1
    finally:
        if runtime is not None:
            runtime.stop()
        _context.runtime = None
        result["run_time_seconds"] = time.monotonic() - start_time
        write_result_json(metrics_path, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))

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
    return run_standalone(bundle_data, task_file, task_index)
