#!/usr/bin/env python3
"""Run a hardcoded pddlrun_llmseparate output through executor_system."""

from __future__ import annotations

import copy
import json
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Sequence

from executor_system import actions as _actions
from executor_system import config as _config
from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system import dependencies as _dependencies
from executor_system import runtime as _runtime_module
from executor_system.config import CLOUD_RENDERING, RENDER_IMAGE
from executor_system.pddlrun_adapter import build_task_plan_from_pddlrun_paths
from executor_system.runtime import ThorRuntime
from executor_system.task_plan import run_action_plan

import resources.robots as robot_catalog


REPO_ROOT = Path(__file__).resolve().parent.parent

# Hardcoded pddlrun_llmseparate outputs.
ALLOCATE_FILE = (
    "/home/dwb/thor/LaMMA-P/logs/intermediate_runs/"
    "final_test_new_0528_1___308/"
    "open_the_book,_then_open_the_drawer,_then_open_the_blinds/"
    "20260601_001/02_allocate/02_allocate_output.txt"
)
PLAN_FOLDER = (
    "/home/dwb/thor/LaMMA-P/logs/intermediate_runs/"
    "final_test_new_0528_1___308/"
    "open_the_book,_then_open_the_drawer,_then_open_the_blinds/"
    "20260601_001/08_planner/outputs"
)
PLAN_FILES: List[str] = []
TASK_FILE = "/home/dwb/thor/LaMMA-P/data/final_test_new_0528_1/FloorPlan308.jsonl"
TASK_INDEX = 21


runtime = None
robots: List[Dict[str, Any]] = []
floor_no = ""
ground_truth: List[Dict[str, Any]] = []
cv2 = _dependencies.cv2
Controller = _dependencies.Controller
CloudRendering = _dependencies.CloudRendering


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


def load_object_names(floor_plan: str) -> List[str]:
    cache_path = REPO_ROOT / "data" / "ai2thor_objects_cache" / f"FloorPlan{floor_plan}.json"
    if not cache_path.is_file():
        return []

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    names: List[str] = []
    for item in cached:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return names


def transition_metric(no_trans: int, no_trans_gt: int, max_trans: int) -> float:
    max_trans_value = max_trans + 1
    no_trans_gt_value = no_trans_gt + 1
    if max_trans_value == no_trans_gt_value and no_trans_gt_value == no_trans:
        return 1.0
    if max_trans_value == no_trans_gt_value:
        return 0.0
    return (max_trans_value - no_trans) / (max_trans_value - no_trans_gt_value)


def main() -> int:
    global floor_no, ground_truth, robots, runtime

    task_record = load_task_record(TASK_FILE, TASK_INDEX)
    floor_no = floor_plan_from_task_file(TASK_FILE)
    robots = build_robot_team(task_record.get("robot list") or [])
    ground_truth = list(task_record.get("object_states") or [])
    _demo_state.set_ground_truth(ground_truth)

    bundle = build_task_plan_from_pddlrun_paths(
        task=str(task_record.get("task") or ""),
        robots=robots,
        allocate_file=ALLOCATE_FILE,
        plan_folder=PLAN_FOLDER,
        plan_files=PLAN_FILES,
        object_names=load_object_names(floor_no),
        task_id=f"FloorPlan{floor_no}_task_{TASK_INDEX}",
    )

    if bundle.object_mapping_warnings:
        for warning in bundle.object_mapping_warnings:
            print(f"WARNING: {warning}")

    runtime = ThorRuntime(robots, floor_no, CLOUD_RENDERING, RENDER_IMAGE)
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
        runtime = None
        _context.runtime = None


class _Demo2Facade(types.ModuleType):
    def __getattribute__(self, name):
        if name == "runtime":
            return _context.runtime
        if name == "cv2":
            return _dependencies.cv2
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name == "runtime":
            _context.runtime = value
        elif name == "cv2":
            _dependencies.cv2 = value
            _runtime_module.cv2 = value
        elif name == "ground_truth":
            _demo_state.set_ground_truth(value)
        elif hasattr(_demo_state, name):
            setattr(_demo_state, name, value)
        elif hasattr(_config, name):
            setattr(_config, name, value)
            if hasattr(_actions, name):
                setattr(_actions, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        elif hasattr(_dependencies, name):
            setattr(_dependencies, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Demo2Facade


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
