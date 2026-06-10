"""Mutable demo runtime state shared by goal bookkeeping."""

import threading
from typing import Any, Dict, List, Set, Tuple

GoalSignature = Tuple[str, Tuple[str, ...], Tuple[str, ...]]

ground_truth_lock = threading.Lock()
verified_ground_truth_goal_signatures: Set[GoalSignature] = set()

_active_ground_truth: List[Dict[str, Any]] = []


def set_ground_truth(goals: List[Dict[str, Any]]) -> None:
    global _active_ground_truth
    _active_ground_truth = goals


def get_ground_truth() -> List[Dict[str, Any]]:
    return _active_ground_truth
