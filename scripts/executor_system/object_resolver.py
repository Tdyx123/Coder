"""Deterministic object selection, aliases and per-runtime operation history."""
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .utils import distance_pts, is_broken_egg_object, is_egg_query, is_sliced_food_object_for_base, matches_object, object_center, object_distance, object_key, object_mass, operated_object_name, operated_object_name_candidate_keys, operated_sliced_food_query_rank, position_to_tuple, sliceable_food_query_key, log


class ObjectResolver:
    def __init__(self, runtime):
        self.runtime = runtime

    def _ensure_object_alias_state(self) -> None:
        runtime = self.runtime
        if not hasattr(runtime, "object_alias_bindings"):
            runtime.object_alias_bindings = {}
        if not hasattr(runtime, "object_alias_key_to_token"):
            runtime.object_alias_key_to_token = {}
        if not hasattr(runtime, "object_alias_by_object_id"):
            runtime.object_alias_by_object_id = {}
        if not hasattr(runtime, "object_alias_warnings"):
            runtime.object_alias_warnings = set()
        if not hasattr(runtime, "object_alias_lock"):
            runtime.object_alias_lock = threading.Lock()

    def _iter_object_id_bindings(self, bindings: Any) -> List[Dict[str, Any]]:
        runtime = self.runtime
        if bindings is None:
            return []
        if isinstance(bindings, list):
            return [dict(item) for item in bindings if isinstance(item, dict)]
        if isinstance(bindings, dict):
            nested = bindings.get("object_id_bindings") or bindings.get("bindings")
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, dict)]
            flattened: List[Dict[str, Any]] = []
            for item in bindings.values():
                flattened.extend(runtime._iter_object_id_bindings(item))
            return flattened
        return []

    def object_alias_keys_for_binding(self, binding: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        object_token = binding.get("object")
        if isinstance(object_token, str) and object_token:
            keys.append(object_key(object_token))

        object_type = binding.get("object_type")
        try:
            number = int(binding.get("number") or 0)
        except (TypeError, ValueError):
            number = 0
        if isinstance(object_type, str) and object_type:
            if number > 0:
                keys.append(object_key(f"{object_type}_{number}"))
                keys.append(object_key(f"{object_type}{number}"))
            if not bool(binding.get("multiple")):
                keys.append(object_key(object_type))
        return list(dict.fromkeys(key for key in keys if key))

    def register_object_id_bindings(self, bindings: Any) -> None:
        runtime = self.runtime
        runtime._ensure_object_alias_state()
        for raw_binding in runtime._iter_object_id_bindings(bindings):
            object_token = raw_binding.get("object")
            object_id = raw_binding.get("object_id")
            if not isinstance(object_token, str) or not object_token:
                continue
            if not isinstance(object_id, str) or not object_id:
                continue

            binding = dict(raw_binding)
            binding["object"] = object_token
            binding["object_id"] = object_id
            current_obj = runtime._current_object_by_id_optional(None, object_id)
            if current_obj is not None:
                runtime._refresh_binding_from_object(binding, current_obj)
            elif "last_position" not in binding:
                position = object_center({"objectId": object_id})
                if position:
                    binding["last_position"] = dict(position)

            with runtime.object_alias_lock:
                old_binding = runtime.object_alias_bindings.get(object_token)
                if old_binding:
                    old_id = str(old_binding.get("object_id") or "")
                    if old_id:
                        runtime.object_alias_by_object_id.get(old_id, set()).discard(object_token)
                runtime.object_alias_bindings[object_token] = binding
                runtime.object_alias_by_object_id.setdefault(object_id, set()).add(object_token)
                for key in runtime.object_alias_keys_for_binding(binding):
                    runtime.object_alias_key_to_token[key] = object_token

    def _refresh_binding_from_object(
        self,
        binding: Dict[str, Any],
        obj: Dict[str, Any],
    ) -> None:
        object_id = str(obj.get("objectId") or "")
        if object_id:
            binding["object_id"] = object_id
        object_type = obj.get("objectType") or object_id.split("|", 1)[0]
        if object_type:
            binding["object_type"] = str(object_type)
        position = object_center(obj)
        if position:
            binding["last_position"] = dict(position)

    def _current_object_by_id_optional(
        self,
        agent_id: Optional[int],
        object_id: Any,
        objects: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        runtime = self.runtime
        object_id_text = str(object_id or "")
        if not object_id_text:
            return None
        try:
            candidates = list(objects) if objects is not None else runtime.current_objects(agent_id)
        except Exception as exc:
            raise_if_execution_aborted(runtime, exc)
            return None
        for obj in candidates:
            if str(obj.get("objectId") or "") == object_id_text:
                return dict(obj)
        return None

    def object_alias_token_for_pattern(self, pattern: Any) -> Optional[str]:
        runtime = self.runtime
        runtime._ensure_object_alias_state()
        key = object_key(pattern)
        if not key:
            return None
        with runtime.object_alias_lock:
            return runtime.object_alias_key_to_token.get(key)

    def object_alias_current_id(self, pattern: Any) -> Optional[str]:
        runtime = self.runtime
        token = runtime.object_alias_token_for_pattern(pattern)
        if token is None:
            return None
        with runtime.object_alias_lock:
            binding = runtime.object_alias_bindings.get(token)
            object_id = str((binding or {}).get("object_id") or "")
        return object_id or None

    def _object_alias_binding_snapshot(self, token: str) -> Optional[Dict[str, Any]]:
        runtime = self.runtime
        runtime._ensure_object_alias_state()
        with runtime.object_alias_lock:
            binding = runtime.object_alias_bindings.get(token)
            return dict(binding) if binding else None

    def _warn_object_alias_once(self, message: str) -> None:
        runtime = self.runtime
        runtime._ensure_object_alias_state()
        with runtime.object_alias_lock:
            if message in runtime.object_alias_warnings:
                return
            runtime.object_alias_warnings.add(message)
        log(f"WARNING: {message}")

    def resolve_object_alias(self, pattern: Any, agent_id: Optional[int] = None) -> Any:
        runtime = self.runtime
        token = runtime.object_alias_token_for_pattern(pattern)
        if token is None:
            return pattern

        binding = runtime._object_alias_binding_snapshot(token)
        if not binding:
            return pattern

        object_id = str(binding.get("object_id") or "")
        if object_id and runtime._current_object_by_id_optional(agent_id, object_id) is not None:
            return object_id

        repaired_id = runtime.repair_object_alias(token, agent_id=agent_id)
        if repaired_id:
            return repaired_id
        if object_id:
            runtime._warn_object_alias_once(
                f"Could not refresh object alias {token!r}; using stale objectId {object_id!r}."
            )
            return object_id
        return pattern

    def _set_object_alias_current_object(
        self,
        token: str,
        obj: Dict[str, Any],
    ) -> Optional[str]:
        runtime = self.runtime
        object_id = str(obj.get("objectId") or "")
        if not object_id:
            return None
        runtime._ensure_object_alias_state()
        with runtime.object_alias_lock:
            binding = runtime.object_alias_bindings.get(token)
            if binding is None:
                return None
            old_id = str(binding.get("object_id") or "")
            if old_id and old_id != object_id:
                old_tokens = runtime.object_alias_by_object_id.get(old_id)
                if old_tokens is not None:
                    old_tokens.discard(token)
                    if not old_tokens:
                        runtime.object_alias_by_object_id.pop(old_id, None)
            runtime._refresh_binding_from_object(binding, obj)
            runtime.object_alias_by_object_id.setdefault(object_id, set()).add(token)
            for key in runtime.object_alias_keys_for_binding(binding):
                runtime.object_alias_key_to_token[key] = token
        return object_id

    def _record_object_alias_match(
        self,
        pattern: Any,
        obj: Dict[str, Any],
        match_count: int,
    ) -> Optional[str]:
        runtime = self.runtime
        object_id = str(obj.get("objectId") or "")
        token = runtime.object_alias_token_for_pattern(pattern)
        if token is not None:
            return runtime._set_object_alias_current_object(token, obj)
        token = str(pattern)
        if not token or not object_id:
            return None

        binding: Dict[str, Any] = {
            "object": token,
            "object_id": object_id,
            "count": int(match_count),
            "multiple": match_count > 1,
            "inferred": True,
        }
        runtime._refresh_binding_from_object(binding, obj)
        runtime._ensure_object_alias_state()
        with runtime.object_alias_lock:
            old_binding = runtime.object_alias_bindings.get(token)
            if old_binding:
                old_id = str(old_binding.get("object_id") or "")
                if old_id and old_id != object_id:
                    old_tokens = runtime.object_alias_by_object_id.get(old_id)
                    if old_tokens is not None:
                        old_tokens.discard(token)
                        if not old_tokens:
                            runtime.object_alias_by_object_id.pop(old_id, None)
            runtime.object_alias_bindings[token] = binding
            runtime.object_alias_by_object_id.setdefault(object_id, set()).add(token)
            for key in runtime.object_alias_keys_for_binding(binding):
                runtime.object_alias_key_to_token[key] = token
        return object_id

    def _event_objects(self, event: Any) -> List[Dict[str, Any]]:
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        return [dict(obj) for obj in objects if isinstance(obj, dict)]

    def repair_object_alias(
        self,
        token: str,
        *,
        agent_id: Optional[int] = None,
        objects: Optional[Sequence[Dict[str, Any]]] = None,
        preferred_parent_id: Optional[str] = None,
        transform: Optional[str] = None,
        exclude_object_ids: Sequence[str] = (),
    ) -> Optional[str]:
        runtime = self.runtime
        binding = runtime._object_alias_binding_snapshot(token)
        if not binding:
            return None

        old_id = str(binding.get("object_id") or "")
        object_type = str(binding.get("object_type") or old_id.split("|", 1)[0] or "")
        object_type_key = object_key(object_type)
        candidates = list(objects) if objects is not None else None
        if candidates is None:
            try:
                candidates = runtime.current_objects(agent_id)
            except Exception as exc:
                raise_if_execution_aborted(runtime, exc)
                candidates = []

        if transform is None:
            current = runtime._current_object_by_id_optional(agent_id, old_id, candidates)
            if current is not None:
                return runtime._set_object_alias_current_object(token, current)

        excluded = {str(object_id) for object_id in exclude_object_ids if object_id}
        matched: List[Dict[str, Any]] = []
        for obj in candidates:
            object_id = str(obj.get("objectId") or "")
            if not object_id or object_id in excluded:
                continue
            if transform == "sliced":
                if is_sliced_food_object_for_base(old_id or object_type or token, obj):
                    matched.append(dict(obj))
                continue
            if transform == "broken":
                if is_broken_egg_object(obj):
                    matched.append(dict(obj))
                continue
            obj_type = obj.get("objectType") or object_id.split("|", 1)[0]
            if object_type_key and object_key(obj_type) != object_type_key:
                continue
            matched.append(dict(obj))

        if not matched:
            runtime._warn_object_alias_once(
                f"Could not find a current object for alias {token!r}."
            )
            return None

        last_position = binding.get("last_position")
        if not isinstance(last_position, dict):
            last_position = object_center({"objectId": old_id})
        mapped_ids = set(runtime.object_alias_by_object_id)
        if old_id:
            mapped_ids.discard(old_id)
        preferred_parent_id = str(preferred_parent_id or "")

        def candidate_score(obj: Dict[str, Any]) -> Tuple[int, int, float, float, str]:
            object_id = str(obj.get("objectId") or "")
            parents = [str(parent) for parent in obj.get("parentReceptacles") or [] if parent]
            parent_rank = 0 if preferred_parent_id and preferred_parent_id in parents else 1
            mapped_rank = 1 if object_id in mapped_ids else 0
            center = object_center(obj)
            if center and isinstance(last_position, dict):
                try:
                    distance = distance_pts(
                        position_to_tuple(center),
                        position_to_tuple(last_position),
                    )
                except (KeyError, TypeError, ValueError):
                    distance = 999999.0
            else:
                distance = 999999.0
            return (
                parent_rank,
                mapped_rank,
                distance,
                object_distance(obj),
                object_id,
            )

        selected = min(matched, key=candidate_score)
        return runtime._set_object_alias_current_object(token, selected)

    def update_object_alias_for_pattern(
        self,
        pattern: Any,
        obj_or_object_id: Any,
        *,
        agent_id: Optional[int] = None,
        objects: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        runtime = self.runtime
        token = runtime.object_alias_token_for_pattern(pattern)
        if token is None:
            return None
        if isinstance(obj_or_object_id, dict):
            obj = obj_or_object_id
        else:
            obj = runtime._current_object_by_id_optional(agent_id, obj_or_object_id, objects)
            if obj is None:
                obj = {"objectId": str(obj_or_object_id or "")}
        return runtime._set_object_alias_current_object(token, obj)

    def update_object_aliases_for_object_ids(
        self,
        object_ids: Sequence[Any],
        *,
        agent_id: Optional[int] = None,
        event: Any = None,
        preferred_parent_id: Optional[str] = None,
        transform: Optional[str] = None,
        exclude_object_ids: Sequence[str] = (),
    ) -> None:
        runtime = self.runtime
        runtime._ensure_object_alias_state()
        event_objects = runtime._event_objects(event) if event is not None else None
        for raw_object_id in object_ids:
            object_id = str(raw_object_id or "")
            if not object_id:
                continue
            with runtime.object_alias_lock:
                tokens = list(runtime.object_alias_by_object_id.get(object_id, set()))
            for token in tokens:
                runtime.repair_object_alias(
                    token,
                    agent_id=agent_id,
                    objects=event_objects,
                    preferred_parent_id=preferred_parent_id,
                    transform=transform,
                    exclude_object_ids=exclude_object_ids,
                )

    def update_object_alias_after_action(
        self,
        action: str,
        agent_id: int,
        obj: Dict[str, Any],
        *,
        event: Any = None,
        goal_object_name: Any = None,
        extra_object_resources: Sequence[str] = (),
        known_object_ids: Sequence[str] = (),
    ) -> None:
        runtime = self.runtime
        event_objects = runtime._event_objects(event) if event is not None else None
        token = runtime.object_alias_token_for_pattern(goal_object_name)
        if token is not None:
            if action == "SliceObject":
                runtime.repair_object_alias(
                    token,
                    agent_id=agent_id,
                    objects=event_objects,
                    transform="sliced",
                    exclude_object_ids=known_object_ids,
                )
            elif action == "BreakObject" and is_egg_query(goal_object_name or obj.get("objectId")):
                runtime.repair_object_alias(
                    token,
                    agent_id=agent_id,
                    objects=event_objects,
                    transform="broken",
                    exclude_object_ids=known_object_ids,
                )
            else:
                current = runtime._current_object_by_id_optional(
                    agent_id,
                    obj.get("objectId"),
                    event_objects,
                )
                runtime._set_object_alias_current_object(token, current or obj)

        if action == "PutObject":
            runtime.update_object_aliases_for_object_ids(
                extra_object_resources,
                agent_id=agent_id,
                event=event,
                preferred_parent_id=str(obj.get("objectId") or ""),
            )

    def _operated_object_name_state(self) -> Tuple[Set[str], threading.Lock]:
        runtime = self.runtime
        names = getattr(runtime, "operated_object_names", None)
        if names is None:
            names = set()
            runtime.operated_object_names = names
        lock = getattr(runtime, "operated_object_names_lock", None)
        if lock is None:
            lock = threading.Lock()
            runtime.operated_object_names_lock = lock
        return names, lock

    def operated_object_names_snapshot(self) -> Set[str]:
        runtime = self.runtime
        names, lock = runtime._operated_object_name_state()
        with lock:
            return set(names)

    def record_operated_object_name(self, obj: Dict[str, Any]) -> None:
        runtime = self.runtime
        name = operated_object_name(obj)
        name_key = object_key(name)
        if not name_key:
            return
        log(f"Operated object name: {name}")
        names, lock = runtime._operated_object_name_state()
        with lock:
            names.add(name_key)

    def record_created_slice_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        runtime = self.runtime
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        created_objects: List[Dict[str, Any]] = []
        resource = (
            source_obj.get("objectId")
            or source_obj.get("objectType")
            or source_obj.get("name")
        )
        source_id = str(source_obj.get("objectId") or "")
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id:
                continue
            if object_id in known_object_ids and object_id != source_id:
                continue
            if is_sliced_food_object_for_base(resource, obj):
                runtime.record_operated_object_name(obj)
                created_objects.append(dict(obj))
        return created_objects

    def record_created_broken_egg_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        runtime = self.runtime
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        created_objects: List[Dict[str, Any]] = []
        resource = (
            source_obj.get("objectId")
            or source_obj.get("objectType")
            or source_obj.get("name")
        )
        if not is_egg_query(resource):
            return created_objects
        source_id = str(source_obj.get("objectId") or "")
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id:
                continue
            if object_id in known_object_ids and object_id != source_id:
                continue
            if is_broken_egg_object(obj):
                runtime.record_operated_object_name(obj)
                created_objects.append(dict(obj))
        return created_objects

    def object_name_was_operated(
        self,
        obj: Dict[str, Any],
        operated_object_names: Optional[Set[str]] = None,
    ) -> bool:
        runtime = self.runtime
        names = (
            runtime.operated_object_names_snapshot()
            if operated_object_names is None
            else operated_object_names
        )
        return bool(operated_object_name_candidate_keys(obj) & names)

    def stove_burner_occupied(
        self,
        burner: Dict[str, Any],
        objects: Sequence[Dict[str, Any]],
    ) -> bool:
        if burner.get("receptacleObjectIds"):
            return True

        burner_id = str(burner.get("objectId") or "")
        if not burner_id:
            return False
        for obj in objects:
            if str(obj.get("objectId") or "") == burner_id:
                continue
            parent_receptacles = obj.get("parentReceptacles") or []
            if any(burner_id == str(parent) for parent in parent_receptacles if parent):
                return True
        return False

    def find_objects(self, pattern: Any, agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
        runtime = self.runtime
        objects = list(runtime.current_objects(agent_id))
        from .action_resources import active_resources
        admitted = active_resources(runtime)
        if admitted is not None:
            bound = admitted.bound_objects(pattern, objects)
            if bound is not None:
                runtime._record_object_alias_match(pattern, bound[0], len(bound))
                return bound
        resolved_pattern = runtime.resolve_object_alias(pattern, agent_id=agent_id)
        matches = [obj for obj in objects if matches_object(resolved_pattern, obj)]
        if not matches and resolved_pattern != pattern:
            matches = [obj for obj in objects if matches_object(pattern, obj)]
        operated_object_names = runtime.operated_object_names_snapshot()
        sliceable_key = sliceable_food_query_key(resolved_pattern)
        egg_query = is_egg_query(resolved_pattern)
        prefer_empty_stove_burner = object_key(resolved_pattern) == "stoveburner"

        def stove_burner_occupancy_rank(obj: Dict[str, Any]) -> int:
            if not prefer_empty_stove_burner:
                return 0
            return int(runtime.stove_burner_occupied(obj, objects))

        if agent_id is not None:
            if sliceable_key is not None:
                matches.sort(
                    key=lambda obj: (
                        operated_sliced_food_query_rank(
                            resolved_pattern,
                            obj,
                            operated_object_names,
                        ),
                        not bool(obj.get("visible", False)),
                        object_distance(obj),
                        obj.get("objectId", ""),
                    )
                )
            elif egg_query:
                matches.sort(
                    key=lambda obj: (
                        stove_burner_occupancy_rank(obj),
                        not is_broken_egg_object(obj),
                        not runtime.object_name_was_operated(obj, operated_object_names),
                        not bool(obj.get("visible", False)),
                        object_distance(obj),
                        obj.get("objectId", ""),
                    )
                )
            else:
                matches.sort(
                    key=lambda obj: (
                        stove_burner_occupancy_rank(obj),
                        not runtime.object_name_was_operated(obj, operated_object_names),
                        not bool(obj.get("visible", False)),
                        object_distance(obj),
                        obj.get("objectId", ""),
                    )
                )
        elif sliceable_key is not None:
            def global_sliceable_sort_key(obj: Dict[str, Any]) -> Tuple[int, float, str]:
                rank = operated_sliced_food_query_rank(
                    resolved_pattern,
                    obj,
                    operated_object_names,
                )
                other_sliced_mass = -object_mass(obj) if rank == 1 else 0.0
                return (rank, other_sliced_mass, obj.get("objectId", ""))

            matches.sort(
                key=global_sliceable_sort_key
            )
        elif egg_query:
            matches.sort(
                key=lambda obj: (
                    stove_burner_occupancy_rank(obj),
                    not is_broken_egg_object(obj),
                    not runtime.object_name_was_operated(obj, operated_object_names),
                    not bool(obj.get("visible", False)),
                    object_distance(obj),
                    obj.get("objectId", ""),
                )
            )
        else:
            matches.sort(
                key=lambda obj: (
                    stove_burner_occupancy_rank(obj),
                    not runtime.object_name_was_operated(obj, operated_object_names),
                )
            )
        if matches:
            runtime._record_object_alias_match(pattern, matches[0], len(matches))
        return matches

    def find_object(
        self,
        pattern: Any,
        *,
        agent_id: Optional[int] = None,
        require_center: bool = False,
    ) -> Dict[str, Any]:
        runtime = self.runtime
        matches = runtime.find_objects(pattern, agent_id)
        if not matches:
            raise RuntimeError(f"Could not find AI2-THOR object matching {pattern!r}")
        if require_center:
            for obj in matches:
                if object_center(obj):
                    return obj
            raise RuntimeError(f"Object {pattern!r} has no usable center.")
        return matches[0]


