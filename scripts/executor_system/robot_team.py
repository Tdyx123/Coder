"""Build catalog-named teams with independent physical-agent positions."""

import copy
import re
from typing import Any, Dict, List

import resources.robots as robot_catalog


def build_real_robot_team(robot_ids: Any) -> List[Dict[str, Any]]:
    if not isinstance(robot_ids, list) or not robot_ids:
        raise RuntimeError("Task record must have a non-empty 'robot list'.")
    team = []
    seen = set()
    for index, raw_id in enumerate(robot_ids):
        if not (
            type(raw_id) is int
            or isinstance(raw_id, str) and re.fullmatch(r"[1-9]\d*", raw_id)
        ):
            raise RuntimeError(f"Invalid robot id in task record: {raw_id!r}")
        robot_id = int(raw_id)
        if not 1 <= robot_id <= len(robot_catalog.robots):
            raise RuntimeError(f"Invalid robot id in task record: {raw_id!r}")
        if robot_id in seen:
            raise RuntimeError(f"Duplicate robot id in task record: {robot_id}")
        seen.add(robot_id)
        robot = copy.deepcopy(robot_catalog.robots[robot_id - 1])
        robot['name'] = f'robot{robot_id}'
        robot['agent_id'] = index
        team.append(robot)
    return team
