#!/usr/bin/env python3
"""Encode parallel-run allocation and PDDL plans into executable AI2-THOR code."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from parsing_utils import ParsingUtils
from run_config import normalize_floor_plan


class PlanEncodingError(Exception):
    """Raised when an allocation or PDDL plan cannot be encoded safely."""


@dataclass
class SubtaskAssignment:
    subtask_id: int
    robot_number: int


@dataclass
class PDDLAction:
    name: str
    args: List[str]
    raw: str


@dataclass
class EncodedAction:
    pddl: PDDLAction
    call: str
    object_tokens: List[str]


ACTION_ALIASES = {
    "gotoobject": "GoToObject",
    "openobject": "OpenObject",
    "closeobject": "CloseObject",
    "breakobject": "BreakObject",
    "sliceobject": "SliceObject",
    "switchon": "SwitchOn",
    "switchoff": "SwitchOff",
    "switchoffobject": "SwitchOff",
    "pickupobject": "PickupObject",
    "putobject": "PutObject",
    "cleanobject": "CleanObject",
}

SUPPORTED_ACTIONS = set(ACTION_ALIASES.values())

def canonical_action_name(action_name: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", str(action_name).lower())
    return ACTION_ALIASES.get(key, str(action_name).strip())


def object_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def strip_trailing_digits(value: str) -> str:
    return re.sub(r"\d+$", "", value)


def pascalize_token(value: str) -> str:
    text = re.sub(r"\d+$", "", str(value).strip())
    parts = re.split(r"[^A-Za-z0-9]+", text)
    if len(parts) > 1:
        return "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not text:
        return str(value)
    return text[:1].upper() + text[1:]


class ObjectNameResolver:
    """Map PDDL object tokens to AI2-THOR object type names."""

    def __init__(self, object_names: Iterable[str]):
        self._by_key: Dict[str, str] = {}
        self.mappings: Dict[str, str] = {}
        self.warnings: List[str] = []

        for name in object_names:
            if not name:
                continue
            key = object_key(name)
            if key and key not in self._by_key:
                self._by_key[key] = str(name)

    def resolve(self, token: str) -> str:
        token = str(token).strip()
        key = object_key(token)
        candidates = [key, strip_trailing_digits(key)]

        for candidate in candidates:
            if candidate in self._by_key:
                resolved = self._by_key[candidate]
                self.mappings[token] = resolved
                return resolved

        fallback = pascalize_token(token)
        warning = f"No AI2-THOR object type match for PDDL token {token!r}; using {fallback!r}."
        if warning not in self.warnings:
            self.warnings.append(warning)
        self.mappings[token] = fallback
        return fallback


def parse_objects_ai(objects_ai: str) -> List[str]:
    if not objects_ai:
        return []

    text = objects_ai.strip()
    if text.startswith("objects"):
        _, _, text = text.partition("=")
        text = text.strip()

    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    names: List[str] = []
    for item in parsed:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def load_object_names(repo_root: Path, floor_plan: str, task_context: Dict[str, Any]) -> List[str]:
    names = parse_objects_ai(str(task_context.get("objects_ai", "")))
    if names:
        return names

    cache_path = repo_root / "data" / "ai2thor_objects_cache" / f"FloorPlan{normalize_floor_plan(floor_plan)}.json"
    if not cache_path.exists():
        return []

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    names = []
    for item in cached:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def parse_allocation_phases(allocated_plan: str) -> List[List[SubtaskAssignment]]:
    sections = ParsingUtils.extract_sequence_sections(allocated_plan)
    if not sections:
        sections = [allocated_plan.strip().splitlines()]

    candidates: List[Tuple[int, int, List[str]]] = []
    for index, section_lines in enumerate(sections):
        normalized_lines, assignments = ParsingUtils.parse_sequence_section(section_lines)
        candidates.append((len(assignments), index, normalized_lines))

    nonempty = [candidate for candidate in candidates if candidate[0] > 0]
    if not nonempty:
        raise PlanEncodingError("No subtask-to-robot assignments found in allocation output.")

    _, _, selected_lines = max(nonempty, key=lambda item: (item[0], item[1]))
    assignment_re = re.compile(r"Subtask\s+(\d+)\s*:\s*Robot\s+(\d+)\s*;", re.IGNORECASE)
    phases: List[List[SubtaskAssignment]] = []

    for line in selected_lines:
        phase = [
            SubtaskAssignment(subtask_id=int(match.group(1)), robot_number=int(match.group(2)))
            for match in assignment_re.finditer(line)
        ]
        if phase:
            phases.append(phase)

    if not phases:
        raise PlanEncodingError("Allocation output did not contain executable phase assignments.")
    return phases


def parse_plan_actions(plan_text: str) -> List[PDDLAction]:
    actions: List[PDDLAction] = []
    action_re = re.compile(r"^\s*\(\s*([^\s()]+)\s*([^()]*)\)\s*$")

    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if not line.startswith("("):
            continue

        match = action_re.match(line)
        if not match:
            raise PlanEncodingError(f"Could not parse PDDL plan line: {raw_line}")

        action_name = canonical_action_name(match.group(1))
        if action_name not in SUPPORTED_ACTIONS:
            raise PlanEncodingError(f"Unsupported PDDL action {match.group(1)!r} in line: {raw_line}")

        args = match.group(2).strip().split()
        actions.append(PDDLAction(name=action_name, args=args, raw=line))

    return actions


def _require_args(action: PDDLAction, count: int) -> None:
    if len(action.args) < count:
        raise PlanEncodingError(
            f"Action {action.raw!r} has {len(action.args)} argument(s), expected at least {count}."
        )


def encode_action(action: PDDLAction, resolver: ObjectNameResolver) -> EncodedAction:
    if action.name == "GoToObject":
        _require_args(action, 2)
        dest = resolver.resolve(action.args[1])
        return EncodedAction(action, f"GoToObject(robot, {dest!r})", [action.args[1]])

    if action.name == "PickupObject":
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return EncodedAction(action, f"PickupObject(robot, {obj!r})", [action.args[1]])

    if action.name == "PutObject":
        _require_args(action, 3)
        obj = resolver.resolve(action.args[1])
        receptacle = resolver.resolve(action.args[2])
        return EncodedAction(
            action,
            f"PutObject(robot, {obj!r}, {receptacle!r})",
            [action.args[1], action.args[2]],
        )

    if action.name in {"OpenObject", "CloseObject", "BreakObject", "SwitchOn", "SwitchOff", "CleanObject"}:
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return EncodedAction(action, f"{action.name}(robot, {obj!r})", [action.args[1]])

    if action.name == "SliceObject":
        _require_args(action, 2)
        obj = resolver.resolve(action.args[1])
        return EncodedAction(action, f"SliceObject(robot, {obj!r})", [action.args[1]])

    raise PlanEncodingError(f"Unsupported PDDL action {action.name!r}.")


def extract_subtask_id(value: str) -> Optional[int]:
    match = re.search(r"subtask[_-]?(\d+)", str(value), re.IGNORECASE)
    return int(match.group(1)) if match else None


def resolve_plan_path(task_run_dir: Path, value: Any) -> Optional[Path]:
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = task_run_dir / path
    return path


def load_plan_paths(task_run_dir: Path) -> Dict[int, Path]:
    manifest_path = task_run_dir / "08_planner" / "planner_manifest.json"
    plan_paths: Dict[int, Path] = {}

    if manifest_path.exists():
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise PlanEncodingError(f"Planner manifest must be a list: {manifest_path}")

        for record in records:
            if not isinstance(record, dict):
                continue
            subtask_id = extract_subtask_id(
                str(record.get("problem_file") or record.get("compatibility_output") or "")
            )
            path = resolve_plan_path(task_run_dir, record.get("compatibility_output"))
            if subtask_id is not None and path is not None:
                plan_paths[subtask_id] = path

    if not plan_paths:
        outputs_dir = task_run_dir / "08_planner" / "outputs"
        for path in sorted(outputs_dir.glob("*_plan.txt")):
            subtask_id = extract_subtask_id(path.name)
            if subtask_id is not None:
                plan_paths[subtask_id] = path

    missing = [str(path) for path in plan_paths.values() if not path.exists()]
    if missing:
        raise PlanEncodingError(f"Planner output file(s) not found: {', '.join(missing)}")
    if not plan_paths:
        raise PlanEncodingError(f"No planner output files found under {task_run_dir}")

    return dict(sorted(plan_paths.items()))


def group_phase_by_robot(phase: Sequence[SubtaskAssignment]) -> "OrderedDict[int, List[int]]":
    grouped: "OrderedDict[int, List[int]]" = OrderedDict()
    for assignment in phase:
        grouped.setdefault(assignment.robot_number, []).append(assignment.subtask_id)
    return grouped


def render_code_plan(
    task: str,
    robots: List[Dict[str, Any]],
    phases: List[List[SubtaskAssignment]],
    encoded_by_subtask: Dict[int, List[EncodedAction]],
) -> str:
    assigned_subtasks = {
        assignment.subtask_id
        for phase in phases
        for assignment in phase
    }
    planned_subtasks = set(encoded_by_subtask)
    missing_plans = sorted(assigned_subtasks - planned_subtasks)
    unassigned_plans = sorted(planned_subtasks - assigned_subtasks)
    if missing_plans:
        raise PlanEncodingError(f"Allocation references subtask(s) without planner output: {missing_plans}")
    if unassigned_plans:
        raise PlanEncodingError(f"Planner output contains unassigned subtask(s): {unassigned_plans}")

    lines: List[str] = [
        "# Encoded from allocation and PDDL planner outputs.",
        f"# Task: {task}",
        "",
    ]

    for subtask_id in sorted(encoded_by_subtask):
        lines.append(f"def run_subtask_{subtask_id:02d}(robot):")
        lines.append(f"    # Subtask {subtask_id}")
        if encoded_by_subtask[subtask_id]:
            for encoded in encoded_by_subtask[subtask_id]:
                lines.append(f"    # {encoded.pddl.raw}")
                lines.append(f"    {encoded.call}")
        else:
            lines.append("    pass")
        lines.append("")

    lines.extend(
        [
            "def run_subtask_sequence(robot, subtask_functions):",
            "    for subtask_function in subtask_functions:",
            "        subtask_function(robot)",
            "",
        ]
    )

    for phase_index, phase in enumerate(phases, start=1):
        grouped = group_phase_by_robot(phase)
        lines.append(f"# Phase {phase_index}")
        lines.append(f"phase_{phase_index}_threads = []")
        for robot_number, subtask_ids in grouped.items():
            if robot_number < 1 or robot_number > len(robots):
                raise PlanEncodingError(
                    f"Allocation references Robot {robot_number}, but only {len(robots)} robot(s) exist."
                )
            functions = ", ".join(f"run_subtask_{subtask_id:02d}" for subtask_id in subtask_ids)
            lines.append(
                f"phase_{phase_index}_robot_{robot_number}_thread = threading.Thread("
                f"target=run_subtask_sequence, args=(robots[{robot_number - 1}], [{functions}]))"
            )
            lines.append(f"phase_{phase_index}_threads.append(phase_{phase_index}_robot_{robot_number}_thread)")
        lines.append(f"for thread in phase_{phase_index}_threads:")
        lines.append("    thread.start()")
        lines.append(f"for thread in phase_{phase_index}_threads:")
        lines.append("    thread.join()")
        lines.append("")

    lines.extend(
        [
            "for _ in range(max(1, len(robots))):",
            "    action_queue.append({'action': 'Done'})",
            "time.sleep(1)",
            "task_over = True",
            "time.sleep(1)",
            "",
        ]
    )
    return "\n".join(lines)


RUNTIME_TEMPLATE = """\
#!/usr/bin/env python3
\"\"\"Standalone AI2-THOR executable generated from a parallel run.\"\"\"

import math
import os
import random
import re
import shutil
import subprocess
import time
import threading
from glob import glob
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np
from ai2thor.controller import Controller
from ai2thor.platform import CloudRendering
from scipy.spatial import distance


robots = {robots_repr}
floor_no = {floor_no_repr}
ground_truth = {ground_truth_repr}
no_trans_gt = {no_trans_gt_repr}
max_trans = {max_trans_repr}
no_trans = {no_trans_repr}
HEADLESS = {headless_default_repr}
if os.environ.get("LAMMAP_HEADLESS", "").lower() in {{"1", "true", "yes"}}:
    HEADLESS = True

total_exec = 0
success_exec = 0
task_over = False
action_queue = []
recp_id = None


def closest_node(node, nodes, no_robot, clost_node_location):
    crps = []
    distances = distance.cdist([node], nodes)[0]
    dist_indices = np.argsort(np.array(distances))
    for i in range(no_robot):
        pos_index = dist_indices[min((i * 5) + clost_node_location[i], len(dist_indices) - 1)]
        crps.append(nodes[pos_index])
    return crps


def distance_pts(p1: Tuple[float, float, float], p2: Tuple[float, float, float]):
    return ((p1[0] - p2[0]) ** 2 + (p1[2] - p2[2]) ** 2) ** 0.5


def object_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def robot_agent_id(robot):
    match = re.search(r"(\\d+)$", str(robot.get("name", "")))
    if not match:
        raise RuntimeError(f"Robot name does not end with an agent id: {{robot}}")
    return int(match.group(1)) - 1


def current_objects(agent_id=None):
    if agent_id is not None and hasattr(c.last_event, "events") and c.last_event.events:
        return c.last_event.events[agent_id].metadata["objects"]
    return c.last_event.metadata["objects"]


def matches_object(pattern, obj):
    object_id = obj.get("objectId", "")
    object_type = obj.get("objectType", object_id.split("|", 1)[0])
    pattern_key = object_key(pattern)
    return (
        re.match(re.escape(str(pattern)), object_id, re.IGNORECASE) is not None
        or object_key(object_type) == pattern_key
        or object_key(object_id.split("|", 1)[0]) == pattern_key
    )


def object_center(obj):
    bbox = obj.get("axisAlignedBoundingBox") or {}
    center = bbox.get("center")
    if center and center != {{"x": 0.0, "y": 0.0, "z": 0.0}}:
        return center
    object_id = obj.get("objectId", "")
    if "|" in object_id:
        parts = object_id.split("|")
        if len(parts) >= 4:
            try:
                return {{"x": float(parts[1]), "y": float(parts[2]), "z": float(parts[3])}}
            except ValueError:
                pass
    return center


def find_object(pattern, agent_id=None, require_center=False):
    matches = [obj for obj in current_objects(agent_id) if matches_object(pattern, obj)]
    if not matches:
        raise RuntimeError(f"Could not find AI2-THOR object matching {{pattern!r}}")
    if agent_id is not None:
        matches.sort(key=lambda obj: obj.get("distance", 999999.0))
    if require_center:
        for obj in matches:
            if object_center(obj):
                return obj
    return matches[0]


def generate_video():
    frame_rate = 5
    cur_path = str(Path(__file__).resolve().parent / "*/")
    for imgs_folder in glob(cur_path, recursive=False):
        view = Path(imgs_folder).name
        if not os.path.isdir(imgs_folder):
            continue
        command_set = [
            "ffmpeg",
            "-i",
            f"{{imgs_folder}}/img_%05d.png",
            "-framerate",
            str(frame_rate),
            "-pix_fmt",
            "yuv420p",
            str(Path(__file__).resolve().parent / f"video_{{view}}.mp4"),
        ]
        subprocess.call(command_set)


if HEADLESS:
    c = Controller(height=1000, width=1000, platform=CloudRendering)
else:
    c = Controller(height=1000, width=1000)
c.reset("FloorPlan" + str(floor_no))
no_robot = len(robots)

multi_agent_event = c.step(dict(
    action="Initialize",
    agentMode="default",
    snapGrid=False,
    gridSize=0.5,
    rotateStepDegrees=20,
    visibilityDistance=100,
    fieldOfView=90,
    agentCount=no_robot,
))

event = c.step(action="GetMapViewCameraProperties")
event = c.step(action="AddThirdPartyCamera", **event.metadata["actionReturn"])

reachable_positions_ = c.step(action="GetReachablePositions").metadata["actionReturn"]
reachable_positions = [(p["x"], p["y"], p["z"]) for p in reachable_positions_]

for i in range(no_robot):
    init_pos = random.choice(reachable_positions_)
    c.step(dict(action="Teleport", position=init_pos, agentId=i))

for i in range(no_robot):
    c.step(action="LookDown", degrees=35, agentId=i)


def exec_actions():
    global total_exec, success_exec
    output_root = Path(__file__).resolve().parent
    for folder in glob(str(output_root / "*/"), recursive=True):
        shutil.rmtree(folder)

    for i in range(no_robot):
        (output_root / f"agent_{{i + 1}}").mkdir(parents=True, exist_ok=True)
    (output_root / "top_view").mkdir(parents=True, exist_ok=True)

    img_counter = 0
    multi_agent_event = c.last_event

    while not task_over:
        if not action_queue:
            time.sleep(0.05)
            continue

        try:
            act = action_queue[0]
            if act["action"] == "ObjectNavExpertAction":
                multi_agent_event = c.step(dict(
                    action=act["action"],
                    position=act["position"],
                    agentId=act["agent_id"],
                ))
                next_action = multi_agent_event.metadata["actionReturn"]
                if next_action is not None:
                    multi_agent_event = c.step(action=next_action, agentId=act["agent_id"], forceAction=True)

            elif act["action"] == "MoveAhead":
                multi_agent_event = c.step(action="MoveAhead", agentId=act["agent_id"])
            elif act["action"] == "MoveBack":
                multi_agent_event = c.step(action="MoveBack", agentId=act["agent_id"])
            elif act["action"] == "RotateLeft":
                multi_agent_event = c.step(action="RotateLeft", degrees=act["degrees"], agentId=act["agent_id"])
            elif act["action"] == "RotateRight":
                multi_agent_event = c.step(action="RotateRight", degrees=act["degrees"], agentId=act["agent_id"])

            elif act["action"] in {{
                "PickupObject",
                "PutObject",
                "ToggleObjectOn",
                "ToggleObjectOff",
                "OpenObject",
                "CloseObject",
                "SliceObject",
                "ThrowObject",
                "BreakObject",
                "CleanObject",
            }}:
                total_exec += 1
                if act["action"] == "ThrowObject":
                    multi_agent_event = c.step(
                        action="ThrowObject",
                        moveMagnitude=7,
                        agentId=act["agent_id"],
                        forceAction=True,
                    )
                else:
                    multi_agent_event = c.step(
                        action=act["action"],
                        objectId=act["objectId"],
                        agentId=act["agent_id"],
                        forceAction=True,
                    )
                if multi_agent_event.metadata["errorMessage"]:
                    print(multi_agent_event.metadata["errorMessage"])
                else:
                    success_exec += 1

            elif act["action"] == "Done":
                multi_agent_event = c.step(action="Done")

        except Exception as exc:
            print(exc)

        try:
            for i, event in enumerate(multi_agent_event.events):
                frame_path = output_root / f"agent_{{i + 1}}" / f"img_{{img_counter:05d}}.png"
                cv2.imwrite(str(frame_path), event.cv2img)
                if not HEADLESS:
                    cv2.imshow(f"agent{{i}}", event.cv2img)

            top_view_rgb = cv2.cvtColor(c.last_event.events[0].third_party_camera_frames[-1], cv2.COLOR_BGR2RGB)
            cv2.imwrite(str(output_root / "top_view" / f"img_{{img_counter:05d}}.png"), top_view_rgb)
            if not HEADLESS:
                cv2.imshow("Top View", top_view_rgb)
                if cv2.waitKey(25) & 0xFF == ord("q"):
                    break
        except Exception as exc:
            print(exc)

        img_counter += 1
        action_queue.pop(0)


actions_thread = threading.Thread(target=exec_actions)
actions_thread.start()


def GoToObject(robot, dest_obj):
    agent_id = robot_agent_id(robot)
    dest = find_object(dest_obj, agent_id=agent_id, require_center=True)
    dest_center = object_center(dest)
    if not dest_center:
        raise RuntimeError(f"Object {{dest_obj!r}} has no usable center.")

    print("Going to", dest_obj, dest.get("objectId"))
    dest_obj_pos = [dest_center["x"], dest_center["y"], dest_center["z"]]
    dist_goal = 10.0
    prev_dist_goal = 10.0
    count_since_update = 0
    clost_node_location = [0]
    goal_thresh = 0.25
    step_count = 0
    max_steps = 120

    while dist_goal > goal_thresh and step_count < max_steps:
        crp = closest_node(dest_obj_pos, reachable_positions, 1, clost_node_location)[0]
        metadata = c.last_event.events[agent_id].metadata
        location = metadata["agent"]["position"]
        prev_dist_goal = dist_goal
        dist_goal = distance_pts([location["x"], location["y"], location["z"]], crp)
        dist_del = abs(dist_goal - prev_dist_goal)

        if dist_del < 0.2:
            count_since_update += 1
        else:
            count_since_update = 0

        if count_since_update < 8:
            action_queue.append({
                "action": "ObjectNavExpertAction",
                "position": dict(x=crp[0], y=crp[1], z=crp[2]),
                "agent_id": agent_id,
            })
        else:
            clost_node_location[0] += 1
            count_since_update = 0
        step_count += 1
        time.sleep(0.3)

    metadata = c.last_event.events[agent_id].metadata
    robot_location = metadata["agent"]["position"]
    robot_rotation = metadata["agent"]["rotation"]["y"]
    robot_object_vec = [dest_obj_pos[0] - robot_location["x"], dest_obj_pos[2] - robot_location["z"]]
    norm = np.linalg.norm(robot_object_vec)
    if norm > 0:
        unit_vector = robot_object_vec / norm
        unit_y = np.array([0, 1])
        angle = math.atan2(np.linalg.det([unit_vector, unit_y]), np.dot(unit_vector, unit_y))
        angle = (360 * angle / (2 * np.pi) + 360) % 360
        rot_angle = angle - robot_rotation
        if rot_angle > 0:
            action_queue.append({"action": "RotateRight", "degrees": abs(rot_angle), "agent_id": agent_id})
        else:
            action_queue.append({"action": "RotateLeft", "degrees": abs(rot_angle), "agent_id": agent_id})

    print("Reached:", dest_obj)


def PickupObject(robot, pick_obj):
    agent_id = robot_agent_id(robot)
    pick = find_object(pick_obj, agent_id=agent_id)
    print("Picking up", pick_obj, pick.get("objectId"))
    action_queue.append({"action": "PickupObject", "objectId": pick["objectId"], "agent_id": agent_id})
    time.sleep(1)


def PutObject(robot, put_obj, recp):
    agent_id = robot_agent_id(robot)
    recp_obj = find_object(recp, agent_id=agent_id)
    print("Putting", put_obj, "on/in", recp, recp_obj.get("objectId"))
    action_queue.append({"action": "PutObject", "objectId": recp_obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


def SwitchOn(robot, sw_obj):
    agent_id = robot_agent_id(robot)
    matches = [obj for obj in current_objects(agent_id) if matches_object(sw_obj, obj)]
    if not matches:
        raise RuntimeError(f"Could not find switchable object {{sw_obj!r}}")
    for obj in matches if object_key(sw_obj) == "stoveknob" else matches[:1]:
        action_queue.append({"action": "ToggleObjectOn", "objectId": obj["objectId"], "agent_id": agent_id})
        time.sleep(0.5)


def SwitchOff(robot, sw_obj):
    agent_id = robot_agent_id(robot)
    matches = [obj for obj in current_objects(agent_id) if matches_object(sw_obj, obj)]
    if not matches:
        raise RuntimeError(f"Could not find switchable object {{sw_obj!r}}")
    for obj in matches if object_key(sw_obj) == "stoveknob" else matches[:1]:
        action_queue.append({"action": "ToggleObjectOff", "objectId": obj["objectId"], "agent_id": agent_id})
        time.sleep(0.5)


def OpenObject(robot, obj_name):
    agent_id = robot_agent_id(robot)
    obj = find_object(obj_name, agent_id=agent_id)
    action_queue.append({"action": "OpenObject", "objectId": obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


def CloseObject(robot, obj_name):
    agent_id = robot_agent_id(robot)
    obj = find_object(obj_name, agent_id=agent_id)
    action_queue.append({"action": "CloseObject", "objectId": obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


def BreakObject(robot, obj_name):
    agent_id = robot_agent_id(robot)
    obj = find_object(obj_name, agent_id=agent_id)
    action_queue.append({"action": "BreakObject", "objectId": obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


def SliceObject(robot, obj_name):
    agent_id = robot_agent_id(robot)
    obj = find_object(obj_name, agent_id=agent_id)
    action_queue.append({"action": "SliceObject", "objectId": obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


def CleanObject(robot, obj_name):
    agent_id = robot_agent_id(robot)
    obj = find_object(obj_name, agent_id=agent_id)
    action_queue.append({"action": "CleanObject", "objectId": obj["objectId"], "agent_id": agent_id})
    time.sleep(1)


"""


FINAL_TEMPLATE = """\

actions_thread.join(timeout=5)

exec_rate = float(success_exec) / float(total_exec) if total_exec else 1.0
objs = list([obj for obj in c.last_event.metadata["objects"]])

gcr_tasks = 0.0
gcr_complete = 0.0
for obj_gt in ground_truth:
    obj_name = obj_gt.get("name")
    state = obj_gt.get("state")
    contains = obj_gt.get("contains") or []
    gcr_tasks += 1
    for obj in objs:
        if state == "SLICED" and obj_name in obj["name"] and obj.get("isSliced"):
            gcr_complete += 1
        if state == "BROKEN" and obj_name in obj["name"] and obj.get("isBroken"):
            gcr_complete += 1
        if state == "OFF" and obj_name in obj["name"] and not obj.get("isToggled"):
            gcr_complete += 1
        if state == "ON" and obj_name in obj["name"] and obj.get("isToggled"):
            gcr_complete += 1
        if state == "HOT" and obj_name in obj["name"] and obj.get("temperature") == "Hot":
            gcr_complete += 1
        if state == "COOKED" and obj_name in obj["name"] and obj.get("isCooked"):
            gcr_complete += 1
        if state == "OPENED" and obj_name in obj["name"] and obj.get("isOpen"):
            gcr_complete += 1
        if state == "CLOSED" and obj_name in obj["name"] and not obj.get("isOpen"):
            gcr_complete += 1
        if state == "PICKED" and obj_name in obj["name"] and obj.get("isPickedUp"):
            gcr_complete += 1
        if contains and obj_name in obj["name"] and obj.get("receptacleObjectIds"):
            for rec in contains:
                if any(rec in receptacle for receptacle in obj["receptacleObjectIds"]):
                    gcr_complete += 1

gcr = 1 if gcr_tasks == 0 else gcr_complete / gcr_tasks
tc = 1 if gcr == 1.0 else 0

max_trans_value = max_trans + 1
no_trans_gt_value = no_trans_gt + 1
if max_trans_value == no_trans_gt_value and no_trans_gt_value == no_trans:
    ru = 1
elif max_trans_value == no_trans_gt_value:
    ru = 0
else:
    ru = (max_trans_value - no_trans) / (max_trans_value - no_trans_gt_value)

sr = 1 if tc == 1 and ru == 1 else 0
print(f"SR:{sr}, TC:{tc}, GCR:{gcr}, Exec:{exec_rate}, RU:{ru}")

try:
    generate_video()
finally:
    c.stop()
"""


def render_executable_plan(
    code_plan: str,
    robots: List[Dict[str, Any]],
    floor_no: str,
    ground_truth: List[Dict[str, Any]],
    no_trans_gt: int,
    max_trans: int,
    no_trans: int,
    headless_default: bool,
) -> str:
    runtime = RUNTIME_TEMPLATE
    replacements = {
        "robots_repr": repr(robots),
        "floor_no_repr": repr(normalize_floor_plan(floor_no)),
        "ground_truth_repr": repr(ground_truth),
        "no_trans_gt_repr": repr(no_trans_gt),
        "max_trans_repr": repr(max_trans),
        "no_trans_repr": repr(no_trans),
        "headless_default_repr": repr(headless_default),
    }
    for key, value in replacements.items():
        runtime = runtime.replace("{" + key + "}", value)
    runtime = runtime.replace("{{", "{").replace("}}", "}")
    return runtime + "\n# Encoded plan\n" + code_plan + FINAL_TEMPLATE


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_task_context(task_run_dir: Path) -> Dict[str, Any]:
    context = load_json(task_run_dir / "inputs" / "task_context.json", default={})
    if not isinstance(context, dict):
        raise PlanEncodingError(f"Invalid task context JSON under {task_run_dir}")
    return context


def load_dataset_record(
    repo_root: Path,
    test_set: Optional[str],
    floor_plan: str,
    task_index: Optional[int],
) -> Dict[str, Any]:
    if not test_set or task_index is None:
        return {}

    dataset_path = repo_root / "data" / test_set / f"FloorPlan{normalize_floor_plan(floor_plan)}.jsonl"
    if not dataset_path.exists():
        return {}

    with dataset_path.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index == task_index and raw_line.strip():
                return json.loads(raw_line)
    return {}


def collect_results(summary_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(summary_data.get("summaries"), list):
        results: List[Dict[str, Any]] = []
        for floor_summary in summary_data["summaries"]:
            if isinstance(floor_summary, dict) and isinstance(floor_summary.get("results"), list):
                results.extend(result for result in floor_summary["results"] if isinstance(result, dict))
        return results

    if isinstance(summary_data.get("results"), list):
        return [result for result in summary_data["results"] if isinstance(result, dict)]

    return []


def selected_results(
    results: Iterable[Dict[str, Any]],
    floor_plan: Optional[str],
    task_index: Optional[int],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    normalized_floor = normalize_floor_plan(floor_plan) if floor_plan else None

    for result in results:
        if result.get("status") != "success":
            continue
        if not result.get("task_run_dir"):
            continue
        if normalized_floor and normalize_floor_plan(str(result.get("floor_plan", ""))) != normalized_floor:
            continue
        if task_index is not None and int(result.get("task_index", -1)) != task_index:
            continue
        selected.append(result)
        if limit is not None and len(selected) >= limit:
            break

    return selected


def write_error_summary(task_run_dir: Path, result: Dict[str, Any], error: Exception) -> None:
    output_dir = task_run_dir / "plan_to_code"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "status": "error",
        "task": result.get("task"),
        "floor_plan": result.get("floor_plan"),
        "task_index": result.get("task_index"),
        "error": str(error),
    }
    (output_dir / "encoding_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def encode_task(
    result: Dict[str, Any],
    repo_root: Path,
    test_set: Optional[str],
    headless_default: bool,
) -> Dict[str, Any]:
    task_run_dir = Path(str(result["task_run_dir"]))
    if not task_run_dir.is_absolute():
        task_run_dir = repo_root / task_run_dir
    task_run_dir = task_run_dir.resolve()

    try:
        task_context = load_task_context(task_run_dir)
        manifest = load_json(task_run_dir / "run_manifest.json", default={}) or {}
        if not isinstance(manifest, dict):
            manifest = {}

        task = str(result.get("task") or task_context.get("task") or manifest.get("task") or "")
        floor_plan = str(
            result.get("floor_plan")
            or manifest.get("floor_plan")
            or task_context.get("floor_plan")
            or ""
        )
        if not floor_plan:
            raise PlanEncodingError(f"Could not determine floor plan for {task_run_dir}")

        robots = task_context.get("robots")
        if not isinstance(robots, list) or not robots:
            raise PlanEncodingError(f"No robot list found in task context: {task_run_dir}")

        raw_task_index = result.get("task_index")
        try:
            task_index = int(raw_task_index) if raw_task_index is not None else None
        except (TypeError, ValueError):
            task_index = None

        dataset_record = load_dataset_record(
            repo_root,
            test_set or manifest.get("test_set"),
            floor_plan,
            task_index,
        )
        ground_truth = dataset_record.get("object_states", [])
        if not isinstance(ground_truth, list):
            ground_truth = []
        no_trans_gt = int(dataset_record.get("trans", 0) or 0)
        max_trans = int(dataset_record.get("min_trans", dataset_record.get("max_trans", 0)) or 0)

        allocate_path = task_run_dir / "02_allocate" / "02_allocate_output.txt"
        if not allocate_path.exists():
            raise PlanEncodingError(f"Allocation output not found: {allocate_path}")
        phases = parse_allocation_phases(allocate_path.read_text(encoding="utf-8"))

        plan_paths = load_plan_paths(task_run_dir)
        resolver = ObjectNameResolver(load_object_names(repo_root, floor_plan, task_context))
        encoded_by_subtask: Dict[int, List[EncodedAction]] = {}
        action_counts: Dict[int, int] = {}

        for subtask_id, plan_path in plan_paths.items():
            actions = parse_plan_actions(plan_path.read_text(encoding="utf-8"))
            encoded_actions = [encode_action(action, resolver) for action in actions]
            encoded_by_subtask[subtask_id] = encoded_actions
            action_counts[subtask_id] = len(encoded_actions)

        no_trans = sum(action_counts.values())
        code_plan = render_code_plan(task, robots, phases, encoded_by_subtask)
        executable_plan = render_executable_plan(
            code_plan=code_plan,
            robots=robots,
            floor_no=floor_plan,
            ground_truth=ground_truth,
            no_trans_gt=no_trans_gt,
            max_trans=max_trans,
            no_trans=no_trans,
            headless_default=headless_default,
        )

        compile(code_plan, "code_plan.py", "exec")
        compile(executable_plan, "executable_plan.py", "exec")

        output_dir = task_run_dir / "plan_to_code"
        output_dir.mkdir(parents=True, exist_ok=True)
        code_plan_path = output_dir / "code_plan.py"
        executable_path = output_dir / "executable_plan.py"
        summary_path = output_dir / "encoding_summary.json"
        code_plan_path.write_text(code_plan, encoding="utf-8")
        executable_path.write_text(executable_plan, encoding="utf-8")

        assignments = [
            [
                {"subtask_id": assignment.subtask_id, "robot_number": assignment.robot_number}
                for assignment in phase
            ]
            for phase in phases
        ]
        subtasks = []
        for subtask_id, encoded_actions in sorted(encoded_by_subtask.items()):
            robot_number = next(
                assignment.robot_number
                for phase in phases
                for assignment in phase
                if assignment.subtask_id == subtask_id
            )
            subtasks.append(
                {
                    "subtask_id": subtask_id,
                    "robot_number": robot_number,
                    "plan_path": str(plan_paths[subtask_id]),
                    "action_count": len(encoded_actions),
                    "actions": [encoded.pddl.raw for encoded in encoded_actions],
                }
            )

        summary = {
            "status": "success",
            "task": task,
            "floor_plan": normalize_floor_plan(floor_plan),
            "task_index": result.get("task_index"),
            "phase_count": len(phases),
            "assignments": assignments,
            "subtasks": subtasks,
            "no_trans": no_trans,
            "object_mappings": resolver.mappings,
            "object_mapping_warnings": resolver.warnings,
            "generated": {
                "code_plan": str(code_plan_path),
                "executable_plan": str(executable_path),
            },
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary

    except Exception as exc:
        write_error_summary(task_run_dir, result, exc)
        raise


def summary_path_from_parallel_run(parallel_run: Path) -> Path:
    if parallel_run.is_file():
        return parallel_run
    return parallel_run / "summary.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encode a specified parallel_runs output into standalone AI2-THOR Python scripts."
    )
    parser.add_argument("--parallel-run", required=True, help="Path to a parallel run directory or summary.json.")
    parser.add_argument("--floor-plan", help="Optional FloorPlan filter, e.g. 6 or FloorPlan6.")
    parser.add_argument("--task-index", type=int, help="Optional task index filter.")
    parser.add_argument("--limit", type=int, help="Maximum number of matching tasks to encode.")
    parser.add_argument("--execute", action="store_true", help="Execute each generated standalone script.")
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
        help="Generate/execute scripts without cv2 windows and use AI2-THOR CloudRendering (default).",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Generate/execute scripts with local display rendering for visual debugging.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    summary_path = summary_path_from_parallel_run(Path(args.parallel_run).resolve())
    if not summary_path.exists():
        parser.error(f"parallel run summary not found: {summary_path}")

    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    repo_root = Path(summary_data.get("repo_root") or Path.cwd()).resolve()
    test_set = summary_data.get("test_set")

    results = selected_results(
        collect_results(summary_data),
        floor_plan=args.floor_plan,
        task_index=args.task_index,
        limit=args.limit,
    )
    if not results:
        print("No matching successful task results found.")
        return 1

    generated: List[Dict[str, Any]] = []
    started_at = time.time()
    for result in results:
        encoded = encode_task(
            result=result,
            repo_root=repo_root,
            test_set=str(test_set) if test_set else None,
            headless_default=bool(args.headless),
        )
        generated.append(encoded)
        executable_path = encoded["generated"]["executable_plan"]
        print(f"Generated task_index={encoded.get('task_index')} FloorPlan{encoded['floor_plan']}: {executable_path}")

        if args.execute:
            env = os.environ.copy()
            if args.headless:
                env["LAMMAP_HEADLESS"] = "1"
            python_exe = sys.executable or "python3"
            subprocess.run([python_exe, executable_path], check=True, env=env)

    duration = time.time() - started_at
    print(f"Encoded {len(generated)} task(s) in {duration:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
