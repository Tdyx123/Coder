"""Task-scoped, immutable goal evaluation state."""

from dataclasses import dataclass
import threading
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .utils import matches_object, object_key


_HISTORICAL_STATES = {"HOT", "COLD", "SLICED", "BROKEN"}


def _normalized_states(goal: Mapping[str, Any]) -> Tuple[str, ...]:
    states = goal.get("states")
    if states is None:
        state = goal.get("state")
        states = () if state in (None, "") else (state,)
    elif isinstance(states, str):
        states = (states,)
    return tuple(str(state).upper() for state in states if state not in (None, ""))


@dataclass(frozen=True)
class GoalSpec:
    name: str
    states: Tuple[str, ...]
    contains: Tuple[str, ...]

    def __post_init__(self) -> None:
        raw_states = self.states or ()
        if isinstance(raw_states, str):
            raw_states = (raw_states,)
        raw_contains = self.contains or ()
        if isinstance(raw_contains, str):
            raw_contains = (raw_contains,)
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(
            self,
            "states",
            tuple(str(state).upper() for state in raw_states if state not in (None, "")),
        )
        object.__setattr__(
            self,
            "contains",
            tuple(str(item) for item in raw_contains if item not in (None, "")),
        )

    @classmethod
    def from_value(cls, value: Any) -> "GoalSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"Goal must be a mapping, got {type(value).__name__}.")
        contains = value.get("contains") or ()
        if isinstance(contains, str):
            contains = (contains,)
        return cls(
            name=str(value.get("name") or ""),
            states=_normalized_states(value),
            contains=tuple(contains),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "states": list(self.states),
            "contains": list(self.contains),
        }


def _object_id(obj: Mapping[str, Any]) -> str:
    return str(obj.get("objectId") or "")


def _object_type_key(obj: Mapping[str, Any]) -> str:
    object_id = _object_id(obj)
    return object_key(obj.get("objectType") or object_id.split("|", 1)[0])


def _state_evidence(obj: Mapping[str, Any], state: str) -> Optional[bool]:
    state = str(state).upper()
    type_key = _object_type_key(obj)
    if state == "SLICED" and type_key.endswith("sliced"):
        return True
    if state == "BROKEN" and type_key == "eggcracked":
        return True
    boolean_fields = {
        "SLICED": ("isSliced", False),
        "BROKEN": ("isBroken", False),
        "OFF": ("isToggled", True),
        "ON": ("isToggled", False),
        "COOKED": ("isCooked", False),
        "OPENED": ("isOpen", False),
        "CLOSED": ("isOpen", True),
        "PICKED": ("isPickedUp", False),
        "CLEANED": ("isDirty", True),
    }
    field_spec = boolean_fields.get(state)
    if field_spec is not None:
        field, invert = field_spec
        if field not in obj or obj.get(field) is None:
            return None
        value = bool(obj.get(field))
        return not value if invert else value

    if state in {"HOT", "COLD"}:
        if "temperature" not in obj or obj.get("temperature") is None:
            return None
        return str(obj.get("temperature")).upper() == state

    if state in {"FILLEDWITHCOFFEE", "FILLEDWITHWATER"}:
        liquid_name = "Coffee" if state == "FILLEDWITHCOFFEE" else "Water"
        direct_field = f"isFilledWith{liquid_name}"
        if direct_field in obj and obj.get(direct_field) is not None:
            return bool(obj.get(direct_field))
        if "isFilledWithLiquid" not in obj or obj.get("isFilledWithLiquid") is None:
            return None
        if not bool(obj.get("isFilledWithLiquid")):
            return False
        liquid = next(
            (
                obj.get(field)
                for field in (
                    "fillLiquid",
                    "filledLiquid",
                    "liquid",
                    "liquidType",
                    "filledWith",
                    "filled_with",
                )
                if obj.get(field) not in (None, "")
            ),
            None,
        )
        if liquid is None:
            return None
        return object_key(liquid) == object_key(liquid_name)

    return None


class EvaluationContext:
    """Fixed goals and historical evidence owned by one task execution."""

    def __init__(self, goals: Sequence[GoalSpec], *, allow_empty: bool = False) -> None:
        self.goals = tuple(goals)
        self._allow_empty = bool(allow_empty)
        self._observations = set()
        self._lock = threading.Lock()

    @classmethod
    def from_goals(
        cls,
        goals: Iterable[Any],
        *,
        allow_empty: bool = False,
    ) -> "EvaluationContext":
        return cls(
            tuple(GoalSpec.from_value(goal) for goal in goals),
            allow_empty=allow_empty,
        )

    def matches_goals(self, goals: Iterable[Any]) -> bool:
        return self.goals == tuple(GoalSpec.from_value(goal) for goal in goals)

    def record_observation(
        self,
        name: str,
        state: str,
        object_id: Optional[str],
    ) -> None:
        name_key = object_key(name)
        state_key = str(state).upper()
        if not name_key or not state_key:
            return
        normalized_id = None if object_id in (None, "") else str(object_id).casefold()
        with self._lock:
            self._observations.add((name_key, state_key, normalized_id))

    def has_observation(self, name: str, state: str) -> bool:
        name_key = object_key(name)
        state_key = str(state).upper()
        with self._lock:
            return any(
                observed_name == name_key and observed_state == state_key
                for observed_name, observed_state, _ in self._observations
            )

    def _observation_satisfies(
        self,
        goal_name: str,
        resolved_name: Any,
        state: str,
        object_id: Optional[str],
    ) -> bool:
        if state not in _HISTORICAL_STATES:
            return False
        names = {object_key(goal_name), object_key(resolved_name)}
        normalized_id = None if object_id in (None, "") else str(object_id).casefold()
        with self._lock:
            return any(
                observed_name in names
                and observed_state == state
                and (observed_id is None or normalized_id is None or observed_id == normalized_id)
                for observed_name, observed_state, observed_id in self._observations
            )

    @staticmethod
    def _resolve(runtime: Any, name: str) -> Any:
        resolver = getattr(runtime, "resolve_object_alias", None)
        if not callable(resolver):
            return name
        return resolver(name)

    def _goal_candidates(
        self,
        runtime: Any,
        goal: GoalSpec,
        objects: Sequence[Mapping[str, Any]],
    ) -> Tuple[Any, Sequence[Mapping[str, Any]]]:
        resolved_name = self._resolve(runtime, goal.name)
        if str(resolved_name).casefold() != goal.name.casefold():
            resolved_id = str(resolved_name).casefold()
            return resolved_name, [
                obj for obj in objects if _object_id(obj).casefold() == resolved_id
            ]
        return resolved_name, [obj for obj in objects if matches_object(goal.name, dict(obj))]

    def _contains_evidence(
        self,
        runtime: Any,
        obj: Mapping[str, Any],
        contains: Sequence[str],
        objects: Sequence[Mapping[str, Any]],
    ) -> Optional[bool]:
        if not contains:
            return True
        if "receptacleObjectIds" not in obj or obj.get("receptacleObjectIds") is None:
            return None
        receptacle_ids = {
            str(value).casefold()
            for value in obj.get("receptacleObjectIds") or ()
            if value not in (None, "")
        }
        for requested in contains:
            if requested.casefold() in receptacle_ids:
                continue
            resolved = self._resolve(runtime, requested)
            if str(resolved).casefold() != requested.casefold():
                if str(resolved).casefold() not in receptacle_ids:
                    return False
                continue

            requested_type = object_key(requested)
            matching_ids = {
                _object_id(candidate).casefold()
                for candidate in objects
                if _object_type_key(candidate) == requested_type and _object_id(candidate)
            }
            if not (matching_ids & receptacle_ids):
                return False
        return True

    def evaluate_goal(
        self,
        runtime: Any,
        goal: GoalSpec,
        *,
        objects: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        all_objects = list(runtime.current_objects() if objects is None else objects)
        resolved_name, candidates = self._goal_candidates(runtime, goal, all_objects)
        candidate_results = []
        any_unknown = False

        for candidate in candidates:
            candidate_id = _object_id(candidate) or None
            state_results = []
            for state in goal.states:
                if self._observation_satisfies(
                    goal.name, resolved_name, state, candidate_id
                ):
                    value = True
                    source = "history"
                else:
                    value = _state_evidence(candidate, state)
                    source = "metadata"
                state_results.append({"state": state, "satisfied": value, "source": source})

            contains_value = self._contains_evidence(
                runtime, candidate, goal.contains, all_objects
            )
            values = [item["satisfied"] for item in state_results]
            if goal.contains:
                values.append(contains_value)
            if all(value is True for value in values):
                candidate_status = "satisfied"
            elif any(value is False for value in values):
                candidate_status = "unsatisfied"
            else:
                candidate_status = "unknown"
                any_unknown = True
            candidate_results.append(
                {
                    "object_id": candidate_id,
                    "status": candidate_status,
                    "states": state_results,
                    "contains_satisfied": contains_value,
                }
            )

        if any(item["status"] == "satisfied" for item in candidate_results):
            status = "satisfied"
        elif candidates and any_unknown:
            status = "unknown"
        elif not candidates and goal.states and not goal.contains and all(
            self._observation_satisfies(goal.name, resolved_name, state, None)
            for state in goal.states
        ):
            status = "satisfied"
        else:
            status = "unsatisfied"

        return {
            **goal.to_dict(),
            "status": status,
            "candidates": candidate_results,
        }

    def evaluate(self, runtime: Any) -> Dict[str, Any]:
        goal_count = len(self.goals)
        with getattr(runtime, "stats_lock", threading.Lock()):
            total_exec = int(getattr(runtime, "total_exec", 0))
            success_exec = int(getattr(runtime, "success_exec", 0))
        exec_rate = 1.0 if total_exec == 0 else success_exec / total_exec

        if not self.goals and not self._allow_empty:
            return self._result(
                status="invalid",
                goal_results=[],
                satisfied_count=None,
                exec_rate=exec_rate,
            )
        if not self.goals:
            return self._result(
                status="valid",
                goal_results=[],
                satisfied_count=0,
                exec_rate=exec_rate,
            )

        try:
            objects = list(runtime.current_objects())
        except Exception as exc:
            return self._result(
                status="invalid",
                goal_results=[
                    {
                        **goal.to_dict(),
                        "status": "unknown",
                        "reason": f"object metadata unavailable: {exc}",
                        "candidates": [],
                    }
                    for goal in self.goals
                ],
                satisfied_count=None,
                exec_rate=exec_rate,
            )

        goal_results = [
            self.evaluate_goal(runtime, goal, objects=objects) for goal in self.goals
        ]
        if any(result["status"] == "unknown" for result in goal_results):
            return self._result(
                status="invalid",
                goal_results=goal_results,
                satisfied_count=None,
                exec_rate=exec_rate,
            )
        satisfied_count = sum(
            result["status"] == "satisfied" for result in goal_results
        )
        return self._result(
            status="valid",
            goal_results=goal_results,
            satisfied_count=satisfied_count,
            exec_rate=exec_rate,
        )

    def _result(
        self,
        *,
        status: str,
        goal_results: Sequence[Mapping[str, Any]],
        satisfied_count: Optional[int],
        exec_rate: float,
    ) -> Dict[str, Any]:
        goal_count = len(self.goals)
        valid = status == "valid"
        gcr = (
            (1.0 if goal_count == 0 else float(satisfied_count) / goal_count)
            if valid and satisfied_count is not None
            else None
        )
        tc = 1.0 if valid and satisfied_count == goal_count else (0.0 if valid else None)
        ru = 1.0 if valid else None
        sr = 1.0 if valid and tc == 1.0 and ru == 1.0 else (0.0 if valid else None)
        return {
            "evaluation_version": "fixed_goals_v2",
            "evaluation_status": status,
            "original_goal_count": goal_count,
            "satisfied_goal_count": satisfied_count,
            "goal_results": [dict(result) for result in goal_results],
            "gcr": gcr,
            "tc": tc,
            "sr": sr,
            "ru": ru,
            "task_success": bool(sr) if valid else None,
            "exec_rate": exec_rate,
        }
