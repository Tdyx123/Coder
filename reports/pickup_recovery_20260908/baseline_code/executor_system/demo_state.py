"""Mutable demo runtime state shared by goal bookkeeping."""

import copy
import threading
from typing import Any, Dict, List, Set, Tuple

GoalSignature = Tuple[str, Tuple[str, ...], Tuple[str, ...]]

ground_truth_lock = threading.Lock()
verified_ground_truth_goal_signatures: Set[GoalSignature] = set()

_active_ground_truth: List[Dict[str, Any]] = []


class _RuntimeGroundTruth:
    def __init__(self):
        self.goals = []
        self.verified = set()
        self.lock = threading.Lock()


def _bound_state(runtime=None):
    from .context import get_bound_runtime
    if runtime is None:
        runtime = get_bound_runtime()
    if runtime is None:
        return None
    with ground_truth_lock:
        state = getattr(runtime, '_compat_ground_truth', None)
        if state is None:
            state = runtime._compat_ground_truth = _RuntimeGroundTruth()
        return state


def goal_bookkeeping(runtime=None):
    state = _bound_state(runtime)
    return ((state.lock, state.verified) if state is not None
            else (ground_truth_lock, verified_ground_truth_goal_signatures))


def set_ground_truth(goals: List[Dict[str, Any]]) -> None:
    global _active_ground_truth
    state = _bound_state()
    if state is not None:
        with state.lock:
            state.goals = copy.deepcopy(list(goals))
            state.verified.clear()
    else:
        with ground_truth_lock:
            _active_ground_truth = copy.deepcopy(list(goals))
            verified_ground_truth_goal_signatures.clear()


def get_ground_truth() -> List[Dict[str, Any]]:
    state = _bound_state()
    if state is not None:
        with state.lock:
            return copy.deepcopy(state.goals)
    with ground_truth_lock:
        return copy.deepcopy(_active_ground_truth)
