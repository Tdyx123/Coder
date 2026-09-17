"""Task-scoped, immutable goal evaluation state."""

from dataclasses import dataclass
import re
import threading
from types import MappingProxyType
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

    def __init__(
        self,
        goals: Sequence[GoalSpec],
        *,
        allow_empty: bool = False,
        object_id_bindings: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.goals = tuple(goals)
        self._allow_empty = bool(allow_empty)
        bindings = {}
        for binding in object_id_bindings:
            token = str(binding.get("object") or "")
            object_id = str(binding.get("object_id") or "")
            if not token or not object_id:
                continue
            keys = {object_key(token)}
            if binding.get("object_type") and binding.get("number"):
                keys.add(object_key(f"{binding['object_type']}_{binding['number']}"))
            for key in keys:
                if key in bindings and bindings[key] != object_id.casefold():
                    bindings[key] = None
                else:
                    bindings[key] = object_id.casefold()
        self._bindings = MappingProxyType(bindings)
        self.subgoals = tuple(
            (index, subgoal)
            for index, goal in enumerate(self.goals)
            for subgoal in (
                [GoalSpec(goal.name, (state,), ()) for state in goal.states]
                + [GoalSpec(goal.name, (), (item,)) for item in goal.contains]
                or [goal]
            )
        )
        self._observations = set()
        self._lock = threading.Lock()

    @classmethod
    def from_goals(
        cls,
        goals: Iterable[Any],
        *,
        allow_empty: bool = False,
        object_id_bindings: Iterable[Mapping[str, Any]] = (),
    ) -> "EvaluationContext":
        return cls(
            tuple(GoalSpec.from_value(goal) for goal in goals),
            allow_empty=allow_empty,
            object_id_bindings=object_id_bindings,
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
                and normalized_id is not None
                and observed_id == normalized_id
                for observed_name, observed_state, observed_id in self._observations
            )

    def _history_satisfies_goal(self, goal: GoalSpec, resolved_name: Any) -> bool:
        """A disappeared goal still needs one identified, alias-compatible object."""
        if not goal.states or goal.contains:
            return False
        names = {object_key(goal.name), object_key(resolved_name)}
        bound_id = (
            str(resolved_name).casefold()
            if str(resolved_name).casefold() != goal.name.casefold()
            else None
        )
        with self._lock:
            object_ids = {
                observed_id
                for observed_name, _, observed_id in self._observations
                if observed_name in names and observed_id is not None
                and (bound_id is None or observed_id == bound_id)
            }
        # Unknown IDs cannot prove that separate observations share an instance.
        return any(
            all(self._observation_satisfies(goal.name, resolved_name, state, object_id)
                for state in goal.states)
            for object_id in object_ids
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
        elif not candidates and self._history_satisfies_goal(goal, resolved_name):
            status = "satisfied"
        else:
            status = "unsatisfied"

        return {
            **goal.to_dict(),
            "status": status,
            "candidates": candidate_results,
        }

    def evaluation_reference(self, name: str) -> Tuple[Optional[str], Optional[str]]:
        """Resolve only explicit references, using the immutable task binding snapshot."""
        if "|" in name:
            return name.casefold(), None
        key = object_key(name)
        # Plain type names remain existential even if the executor bound that type.
        if re.search(r"\d+$", name) or ("_" in name and key in self._bindings):
            if key not in self._bindings:
                return None, f"missing explicit instance binding: {name}"
            if self._bindings[key] is None:
                return None, f"conflicting explicit instance binding: {name}"
            return self._bindings[key], None
        return None, None

    def evaluation_candidates(
        self, name: str, objects: Sequence[Mapping[str, Any]], *,
        include_transformed: bool = False,
    ) -> Tuple[Sequence[Mapping[str, Any]], Optional[str]]:
        object_id, reason = self.evaluation_reference(name)
        if reason:
            return [], reason
        if object_id is not None:
            return [obj for obj in objects if _object_id(obj).casefold() == object_id], None
        candidates = []
        for obj in objects:
            matches = _object_type_key(obj) == object_key(name)
            if include_transformed:
                # Reuse existing sliced-food / broken-egg semantics without
                # letting an object's display name override its type.
                matches = matches or matches_object(name, {
                    "objectId": _object_id(obj), "objectType": obj.get("objectType"),
                })
            if matches:
                candidates.append(obj)
        return candidates, None

    def _atomic_history_ids(
        self, goal: GoalSpec, candidate_id: Optional[str] = None,
    ) -> Tuple[str, ...]:
        if not goal.states or goal.states[0] not in _HISTORICAL_STATES:
            return ()
        bound_id, reason = self.evaluation_reference(goal.name)
        if reason:
            return ()
        with self._lock:
            return tuple(sorted({
                observed_id
                for _, state, observed_id in self._observations
                if state == goal.states[0] and observed_id is not None
                and (candidate_id is None or observed_id == candidate_id.casefold())
                and (observed_id == bound_id if bound_id is not None else
                     matches_object(goal.name, {"objectId": observed_id}))
            }))

    def evaluate_subgoal(
        self, goal: GoalSpec, objects: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Score one independent condition without consulting execution aliases."""
        candidates, reason = self.evaluation_candidates(
            goal.name, objects, include_transformed=bool(goal.states))
        contained_id = None
        if goal.contains:
            contained_id, contained_reason = self.evaluation_reference(goal.contains[0])
            reason = reason or contained_reason
        result = {**goal.to_dict(), "status": "unknown", "reason": reason,
                  "candidates": [], "matched_object_ids": []}
        if reason:
            return result
        for candidate in candidates:
            candidate_id = _object_id(candidate)
            source = "metadata"
            if goal.states:
                if self._atomic_history_ids(goal, candidate_id):
                    satisfied, source = True, "history"
                else:
                    satisfied = _state_evidence(candidate, goal.states[0])
            elif goal.contains:
                contents = candidate.get("receptacleObjectIds")
                if contents is None:
                    satisfied = None
                else:
                    ids = {str(value).casefold() for value in contents if value not in (None, "")}
                    if contained_id is not None:
                        satisfied = contained_id in ids
                    else:
                        matched, _ = self.evaluation_candidates(goal.contains[0], objects)
                        satisfied = any(_object_id(obj).casefold() in ids for obj in matched)
                        # A listed but unavailable object cannot prove its type.
                        if not satisfied and ids - {_object_id(obj).casefold() for obj in objects}:
                            satisfied = None
            else:
                satisfied = True
            status = "satisfied" if satisfied is True else "unsatisfied" if satisfied is False else "unknown"
            result["candidates"].append({"object_id": candidate_id, "status": status, "source": source})
            if satisfied is True:
                result["matched_object_ids"].append(candidate_id)
        if result["matched_object_ids"]:
            result["status"] = "satisfied"
        elif self._atomic_history_ids(goal):
            result["status"] = "satisfied"
            result["matched_object_ids"] = list(self._atomic_history_ids(goal))
            result["reason"] = "identified historical state evidence"
        elif any(item["status"] == "unknown" for item in result["candidates"]):
            result["reason"] = "required object metadata unavailable"
        else:
            result["status"] = "unsatisfied"
            result["reason"] = "no matching instance satisfies the condition"
        return result

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
                subgoal_results=[
                    {**goal.to_dict(), "original_goal_index": index,
                     "subgoal_index": subgoal_index, "status": "unknown",
                     "reason": f"object metadata unavailable: {exc}",
                     "candidates": [], "matched_object_ids": []}
                    for subgoal_index, (index, goal) in enumerate(self.subgoals)
                ],
                satisfied_count=None,
                exec_rate=exec_rate,
            )

        subgoal_results = [
            {**self.evaluate_subgoal(goal, objects), "original_goal_index": index,
             "subgoal_index": subgoal_index}
            for subgoal_index, (index, goal) in enumerate(self.subgoals)
        ]
        goal_results = []
        for index, goal in enumerate(self.goals):
            children = [item for item in subgoal_results if item["original_goal_index"] == index]
            statuses = {item["status"] for item in children}
            status = "unknown" if "unknown" in statuses else "unsatisfied" if "unsatisfied" in statuses else "satisfied"
            goal_results.append({**goal.to_dict(), "status": status,
                                 "subgoal_indices": [item["subgoal_index"] for item in children]})
        if any(result["status"] == "unknown" for result in subgoal_results):
            return self._result(
                status="invalid",
                goal_results=goal_results,
                subgoal_results=subgoal_results,
                satisfied_count=None,
                exec_rate=exec_rate,
            )
        satisfied_count = sum(
            result["status"] == "satisfied" for result in subgoal_results
        )
        return self._result(
            status="valid",
            goal_results=goal_results,
            subgoal_results=subgoal_results,
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
        subgoal_results: Sequence[Mapping[str, Any]] = (),
    ) -> Dict[str, Any]:
        goal_count = len(self.subgoals)
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
            "evaluation_version": "atomic_goals_v3",
            "evaluation_status": status,
            "original_goal_count": len(self.goals),
            "atomic_goal_count": goal_count,
            "subgoal_results": [dict(result) for result in subgoal_results],
            "satisfied_goal_count": satisfied_count,
            "goal_results": [dict(result) for result in goal_results],
            "gcr": gcr,
            "tc": tc,
            "sr": sr,
            "ru": ru,
            "task_success": bool(sr) if valid else None,
            "exec_rate": exec_rate,
        }
