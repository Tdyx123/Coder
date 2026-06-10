"""Position and object resource managers for staged synchronous execution."""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .executor_requests import (
    ConflictDecision,
    MoveToPositionRequest,
    StepRequest,
    TeleportAndFacePositionRequest,
    TeleportToPositionRequest,
)
from .utils import (
    is_broken_egg_object,
    is_egg_query,
    is_sliced_food_object_for_base,
    matches_object,
    object_key,
    object_type_keys,
    operated_object_name_candidate_keys,
    sliced_food_logical_name,
    stable_object_name,
    step_event_failed,
)


class PositionResourceMgr:
    """Infers runtime position blockers for navigation and teleport requests."""

    def __init__(self, runtime: "ThorRuntime") -> None:
        self.runtime = runtime

    def resolve(self, request: Any) -> ConflictDecision:
        if isinstance(request, (TeleportToPositionRequest, TeleportAndFacePositionRequest)):
            blockers = self.runtime.target_position_blockers(
                request.agent_id,
                request.target_position,
            )
            reason = "target position is occupied"
        elif isinstance(request, MoveToPositionRequest):
            try:
                blockers = self.runtime.navigation_blockers(
                    request.agent_id,
                    request.target_position,
                )
            except RuntimeError:
                return ConflictDecision(True)
            reason = "next navigation segment is occupied"
        else:
            return ConflictDecision(True)
        if not blockers:
            return ConflictDecision(True)
        return ConflictDecision(False, reason, blockers=tuple(sorted(blockers)))


OBJECT_STATE_FREE = "free"
OBJECT_STATE_RESERVED = "reserved"
OBJECT_STATE_HELD = "held"


@dataclass
class ObjectTableEntry:
    logical_id: str
    object_id: Optional[str] = None
    owner_robot: Optional[int] = None
    state: str = OBJECT_STATE_FREE
    base_key: str = ""
    aliases: Set[str] = field(default_factory=set)


class ObjectResourceMgr:
    """Tracks object reservations, held objects, handoff ownership, and logical ids."""

    def __init__(
        self,
        agent_ids: Iterable[int],
        runtime: Optional["ThorRuntime"] = None,
    ) -> None:
        self.object_owners: Dict[str, int] = {}
        self.agent_object_intents: Dict[int, Set[str]] = {}
        self.agent_held_objects: Dict[int, Set[str]] = {}
        self.object_table: Dict[str, ObjectTableEntry] = {}
        self.object_id_to_logical: Dict[str, str] = {}
        self.logical_key_to_id: Dict[str, str] = {}
        self.base_key_to_logicals: Dict[str, List[str]] = {}
        for agent_id in agent_ids:
            self.ensure_agent(agent_id)
        if runtime is not None:
            self.initialize_from_runtime(runtime)

    def initialize_from_runtime(self, runtime: "ThorRuntime") -> None:
        try:
            objects = runtime.current_objects()
        except BaseException:
            return

        grouped: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {}
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id:
                continue
            base_name = stable_object_name(obj) or object_id.split("|", 1)[0]
            base_key = object_key(base_name)
            if not base_key:
                continue
            grouped.setdefault(base_key, []).append((base_name, object_id, obj))

        for base_key, items in grouped.items():
            items.sort(key=lambda item: item[1])
            multiple = len(items) > 1
            for index, (base_name, object_id, obj) in enumerate(items, start=1):
                logical_id = base_name if not multiple else f"{base_name}{index}"
                stored_object_id = object_id if multiple else None
                entry = self.ensure_entry(
                    logical_id,
                    object_id=stored_object_id,
                    base_key=base_key,
                )
                entry.aliases.update(operated_object_name_candidate_keys(obj))
                self.object_id_to_logical[object_id] = logical_id

    def ensure_agent(self, agent_id: int) -> None:
        self.agent_object_intents.setdefault(agent_id, set())
        self.agent_held_objects.setdefault(agent_id, set())

    def ensure_entry(
        self,
        logical_id: str,
        *,
        object_id: Optional[str] = None,
        base_key: Optional[str] = None,
    ) -> ObjectTableEntry:
        logical_id = str(logical_id)
        entry = self.object_table.get(logical_id)
        if entry is None:
            entry = ObjectTableEntry(
                logical_id=logical_id,
                object_id=object_id,
                base_key=base_key or self.logical_base_key(logical_id),
            )
            self.object_table[logical_id] = entry
            self.logical_key_to_id[object_key(logical_id)] = logical_id
            self.base_key_to_logicals.setdefault(entry.base_key, []).append(logical_id)
            self.base_key_to_logicals[entry.base_key].sort(key=self.logical_sort_key)
        elif object_id:
            entry.object_id = object_id
        if object_id:
            entry.aliases.add(object_key(object_id.split("|", 1)[0]))
            self.object_id_to_logical[object_id] = logical_id
        return entry

    def logical_sort_key(self, logical_id: str) -> Tuple[str, int, str]:
        key = object_key(logical_id)
        digits = ""
        while key and key[-1].isdigit():
            digits = key[-1] + digits
            key = key[:-1]
        return (key, int(digits or 0), logical_id)

    def logical_base_key(self, logical_id: str) -> str:
        key = object_key(logical_id)
        while key and key[-1].isdigit():
            key = key[:-1]
        return key or object_key(logical_id)

    def bind_object_id(self, logical_id: str, object_id: Optional[str]) -> None:
        if not object_id:
            return
        entry = self.ensure_entry(logical_id, object_id=object_id)
        entry.object_id = object_id
        self.object_id_to_logical[object_id] = logical_id

    def logical_resource(self, resource: Any, agent_id: Optional[int] = None) -> str:
        resource_text = str(resource)
        if resource_text in self.object_table:
            return resource_text
        mapped = self.object_id_to_logical.get(resource_text)
        if mapped is not None:
            return mapped

        resource_key = object_key(resource_text)
        mapped = self.logical_key_to_id.get(resource_key)
        if mapped is not None:
            return mapped

        if "|" in resource_text:
            base_key = object_key(resource_text.split("|", 1)[0])
        else:
            base_key = self.logical_base_key(resource_text)
        logicals = self.base_key_to_logicals.get(base_key)
        if logicals:
            return self.select_logical_for_base(logicals, agent_id)

        return resource_text

    def select_logical_for_base(
        self,
        logicals: Sequence[str],
        agent_id: Optional[int],
    ) -> str:
        if agent_id is not None:
            for logical_id in logicals:
                if self.object_owners.get(logical_id) == agent_id:
                    return logical_id
                entry = self.object_table.get(logical_id)
                if entry is not None and entry.owner_robot == agent_id:
                    return logical_id
        for logical_id in logicals:
            entry = self.object_table.get(logical_id)
            owner = self.object_owners.get(logical_id)
            if owner is None and (entry is None or entry.owner_robot is None):
                return logical_id
        return logicals[0]

    def object_resources_for_request(self, request: Any) -> Tuple[str, ...]:
        resources = tuple(getattr(request, "object_resources", ()) or ())
        if resources:
            return resources
        object_resource = getattr(request, "object_resource", None)
        return (str(object_resource),) if object_resource else ()

    def resource_ids(self, resource: Any, agent_id: Optional[int] = None) -> Tuple[str, ...]:
        raw = str(resource)
        logical = self.logical_resource(raw, agent_id)
        ids = [logical]
        entry = self.object_table.get(logical)
        if entry is not None and entry.object_id:
            ids.append(entry.object_id)
        ids.extend(
            object_id
            for object_id, mapped in self.object_id_to_logical.items()
            if mapped == logical
        )
        if raw not in ids:
            ids.append(raw)
        return tuple(dict.fromkeys(ids))

    def owner_record(
        self,
        resource: Any,
        agent_id: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[int]]:
        for resource_id in self.resource_ids(resource, agent_id):
            owner = self.object_owners.get(resource_id)
            if owner is not None:
                return resource_id, owner
        logical = self.logical_resource(resource, agent_id)
        entry = self.object_table.get(logical)
        if entry is not None and entry.owner_robot is not None:
            return logical, entry.owner_robot
        return None, None

    def resource_held_by(
        self,
        agent_id: int,
        resource: Any,
    ) -> bool:
        ids = set(self.resource_ids(resource, agent_id))
        if ids & self.agent_held_objects.get(agent_id, set()):
            return True
        logical = self.logical_resource(resource, agent_id)
        entry = self.object_table.get(logical)
        return bool(
            entry is not None
            and entry.state == OBJECT_STATE_HELD
            and entry.owner_robot == agent_id
        )

    def physical_object_id(self, resource: Any) -> Optional[str]:
        logical = self.logical_resource(resource)
        entry = self.object_table.get(logical)
        if entry is not None and entry.object_id:
            return entry.object_id
        for object_id, mapped in self.object_id_to_logical.items():
            if mapped == logical:
                return object_id
        resource_text = str(resource)
        return resource_text if "|" in resource_text else None

    def handoff_object_resource(self, resource: Any) -> str:
        return self.physical_object_id(resource) or str(resource)

    def agent_holds_object(self, agent_id: int, object_resource: str) -> bool:
        return self.resource_held_by(agent_id, object_resource)

    def agent_held_objects_snapshot(self, agent_id: int) -> Set[str]:
        held = set(self.agent_held_objects.get(agent_id, set()))
        for entry in self.object_table.values():
            if entry.owner_robot == agent_id and entry.state == OBJECT_STATE_HELD:
                physical_id = self.physical_object_id(entry.logical_id)
                if physical_id is not None:
                    held.add(physical_id)
        return held

    def agent_held_object_matching(self, agent_id: int, pattern: Any) -> Optional[str]:
        pattern_logical = self.logical_resource(pattern, agent_id)
        for object_resource in self.agent_held_objects_snapshot(agent_id):
            if object_resource == pattern_logical:
                return object_resource
            if self.logical_resource(object_resource, agent_id) == pattern_logical:
                return object_resource
            stub = {
                "objectId": object_resource,
                "objectType": object_resource.split("|", 1)[0],
            }
            if matches_object(pattern, stub):
                return object_resource
        return None

    def resolve(
        self,
        request: Any,
        completed_agent_ids: Set[int],
    ) -> ConflictDecision:
        for object_resource in self.object_resources_for_request(request):
            owner_key, owner = self.owner_record(object_resource, request.agent_id)
            if owner is None or owner == request.agent_id:
                continue

            if self.resource_held_by(owner, object_resource):
                if owner in completed_agent_ids:
                    return ConflictDecision(
                        False,
                        f"{object_resource} is held by completed agent {owner}",
                        blockers=(owner,),
                        handoff_owner=owner,
                        resource=object_resource,
                    )
                return ConflictDecision(
                    False,
                    f"{object_resource} is held by active agent {owner}",
                    blockers=(owner,),
                    resource=object_resource,
                )

            if owner in completed_agent_ids:
                self.release_agent_intent(owner, object_resource)
                if owner_key is not None:
                    self.release_object_owner(owner, owner_key)
                continue

            return ConflictDecision(
                False,
                f"{object_resource} is reserved by active agent {owner}",
                blockers=(owner,),
                resource=object_resource,
            )

        return ConflictDecision(True)

    def acquire(self, request: Any) -> None:
        resources = self.object_resources_for_request(request)
        if not resources:
            return
        self.ensure_agent(request.agent_id)
        is_navigation = isinstance(
            request,
            (MoveToPositionRequest, TeleportToPositionRequest, TeleportAndFacePositionRequest),
        )
        for object_resource in resources:
            logical = self.logical_resource(object_resource, request.agent_id)
            entry = self.ensure_entry(logical)
            if "|" in str(object_resource):
                self.object_id_to_logical.setdefault(str(object_resource), logical)
            self.object_owners[logical] = request.agent_id
            entry.owner_robot = request.agent_id
            if entry.state != OBJECT_STATE_HELD:
                entry.state = OBJECT_STATE_RESERVED
            if is_navigation:
                self.agent_object_intents[request.agent_id].add(logical)

    def release_after_step(self, request: StepRequest) -> None:
        action = request.payload.get("action")
        resources = self.object_resources_for_request(request)
        primary_resource = resources[0] if resources else request.object_resource
        succeeded = request.exception is None
        if request.event is not None:
            succeeded = succeeded and not step_event_failed(request.event)

        self.ensure_agent(request.agent_id)
        if action == "PickupObject" and primary_resource and succeeded:
            physical_id = str(request.payload.get("objectId") or primary_resource)
            logical = self.logical_resource(primary_resource, request.agent_id)
            self.mark_held(request.agent_id, logical, physical_id)
            return

        if succeeded and action == "SliceObject":
            for object_resource in resources:
                self.register_created_slice_objects(
                    object_resource,
                    request.event,
                    request.agent_id,
                )
        elif succeeded and action == "BreakObject":
            for object_resource in resources:
                self.register_broken_egg_objects(
                    object_resource,
                    request.event,
                    request.agent_id,
                )
        elif succeeded:
            for object_resource in resources:
                self.refresh_binding_from_event(object_resource, request.event)

        if action in {"PutObject", "ThrowObject"} and succeeded:
            self.release_held_objects(request.agent_id)

        for object_resource in resources:
            self.release_agent_intent(request.agent_id, object_resource)
            if not self.resource_held_by(request.agent_id, object_resource):
                self.release_object_owner(request.agent_id, object_resource)

    def mark_held(self, agent_id: int, logical: str, physical_id: str) -> None:
        entry = self.ensure_entry(logical, object_id=physical_id)
        self.agent_object_intents[agent_id].discard(logical)
        self.agent_held_objects[agent_id].add(physical_id)
        self.object_owners[logical] = agent_id
        entry.owner_robot = agent_id
        entry.state = OBJECT_STATE_HELD
        self.bind_object_id(logical, physical_id)

    def refresh_binding_from_event(self, resource: Any, event: Any) -> None:
        if event is None:
            return
        logical = self.logical_resource(resource)
        entry = self.object_table.get(logical)
        if entry is None:
            return
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        if not objects:
            return

        expected_ids = {
            resource_id
            for resource_id in self.resource_ids(resource)
            if "|" in resource_id
        }
        alias_keys = {entry.base_key, object_key(entry.logical_id), *entry.aliases}
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id:
                continue
            if object_id in expected_ids:
                self.bind_object_id(logical, object_id)
                return
            candidate_keys = object_type_keys(obj) | operated_object_name_candidate_keys(obj)
            if candidate_keys & alias_keys:
                self.bind_object_id(logical, object_id)
                return

    def register_created_slice_objects(
        self,
        resource: Any,
        event: Any,
        agent_id: int,
    ) -> List[str]:
        if event is None:
            return []
        logical_base = sliced_food_logical_name(resource)
        if logical_base is None:
            return []

        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        registered = []
        base_key = object_key(logical_base)
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id or object_id in self.object_id_to_logical:
                continue
            if not is_sliced_food_object_for_base(resource, obj):
                continue
            logical_id = self.next_available_logical_id(logical_base)
            entry = self.ensure_entry(
                logical_id,
                object_id=object_id,
                base_key=base_key,
            )
            entry.owner_robot = None
            entry.state = OBJECT_STATE_FREE
            entry.aliases.update(operated_object_name_candidate_keys(obj))
            self.release_agent_intent(agent_id, logical_id)
            registered.append(logical_id)
        return registered

    def register_broken_egg_objects(
        self,
        resource: Any,
        event: Any,
        agent_id: int,
    ) -> List[str]:
        if event is None or not is_egg_query(resource):
            return []

        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        registered = []
        logical_base = "EggCracked"
        base_key = object_key(logical_base)
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id or not is_broken_egg_object(obj):
                continue

            mapped_logical = self.object_id_to_logical.get(object_id)
            if (
                mapped_logical is not None
                and self.logical_base_key(mapped_logical) == base_key
            ):
                entry = self.ensure_entry(
                    mapped_logical,
                    object_id=object_id,
                    base_key=base_key,
                )
                entry.owner_robot = None
                entry.state = OBJECT_STATE_FREE
                entry.aliases.update(operated_object_name_candidate_keys(obj))
                self.object_owners.pop(mapped_logical, None)
                self.object_owners.pop(object_id, None)
                self.release_agent_intent(agent_id, mapped_logical)
                continue

            logical_id = self.next_available_logical_id(logical_base)
            if mapped_logical is not None:
                self.release_agent_intent(agent_id, mapped_logical)
                self.release_object_owner(agent_id, mapped_logical)
                old_entry = self.object_table.get(mapped_logical)
                if old_entry is not None:
                    if old_entry.object_id == object_id:
                        old_entry.object_id = None
                    old_entry.owner_robot = None
                    old_entry.state = OBJECT_STATE_FREE
                for held_objects in self.agent_held_objects.values():
                    held_objects.discard(object_id)
                self.object_id_to_logical.pop(object_id, None)

            entry = self.ensure_entry(
                logical_id,
                object_id=object_id,
                base_key=base_key,
            )
            entry.owner_robot = None
            entry.state = OBJECT_STATE_FREE
            entry.aliases.update(operated_object_name_candidate_keys(obj))
            self.object_owners.pop(logical_id, None)
            self.object_owners.pop(object_id, None)
            self.release_agent_intent(agent_id, logical_id)
            registered.append(logical_id)
        return registered

    def next_available_logical_id(self, logical_base: str) -> str:
        if logical_base not in self.object_table:
            return logical_base
        index = 2
        while f"{logical_base}{index}" in self.object_table:
            index += 1
        return f"{logical_base}{index}"

    def release_after_move_failure(self, request: Any) -> None:
        for object_resource in self.object_resources_for_request(request):
            self.release_agent_intent(request.agent_id, object_resource)
            if not self.resource_held_by(request.agent_id, object_resource):
                self.release_object_owner(request.agent_id, object_resource)

    def complete_handoff(self, handoff: "ObjectHandoffRequest") -> None:
        self.ensure_agent(handoff.from_agent_id)
        self.ensure_agent(handoff.to_agent_id)
        logical = self.logical_resource(handoff.object_resource, handoff.to_agent_id)
        physical_id = self.physical_object_id(handoff.object_resource) or handoff.object_resource
        self.agent_held_objects[handoff.from_agent_id].discard(physical_id)
        self.release_agent_intent(handoff.from_agent_id, logical)
        self.agent_held_objects[handoff.to_agent_id].add(physical_id)
        self.release_agent_intent(handoff.to_agent_id, logical)
        self.object_owners[logical] = handoff.to_agent_id
        entry = self.ensure_entry(logical, object_id=physical_id)
        entry.owner_robot = handoff.to_agent_id
        entry.state = OBJECT_STATE_HELD

    def release_agent_intent(self, agent_id: int, object_resource: Any) -> None:
        for resource_id in self.resource_ids(object_resource, agent_id):
            self.agent_object_intents.setdefault(agent_id, set()).discard(resource_id)

    def release_object_owner(self, agent_id: int, object_resource: str) -> None:
        logical = self.logical_resource(object_resource, agent_id)
        for resource_id in self.resource_ids(object_resource, agent_id):
            if self.object_owners.get(resource_id) == agent_id:
                self.object_owners.pop(resource_id, None)
        entry = self.object_table.get(logical)
        if entry is not None and entry.owner_robot == agent_id:
            entry.owner_robot = None
            if entry.state != OBJECT_STATE_HELD:
                entry.state = OBJECT_STATE_FREE

    def release_held_objects(self, agent_id: int) -> None:
        held_objects = list(self.agent_held_objects.get(agent_id, set()))
        released_logicals = {
            self.logical_resource(object_resource, agent_id)
            for object_resource in held_objects
        }
        for object_resource in held_objects:
            self.release_object_owner(agent_id, object_resource)
        self.agent_held_objects.setdefault(agent_id, set()).clear()
        for entry in self.object_table.values():
            if entry.owner_robot != agent_id or entry.state != OBJECT_STATE_HELD:
                continue
            released_logicals.add(entry.logical_id)
            self.release_object_owner(agent_id, entry.logical_id)
        for logical_id in released_logicals:
            entry = self.object_table.get(logical_id)
            if entry is None:
                continue
            entry.owner_robot = None
            entry.state = OBJECT_STATE_FREE

    def clear(self) -> None:
        self.object_owners.clear()
        for entry in self.object_table.values():
            entry.owner_robot = None
            entry.state = OBJECT_STATE_FREE
        for intents in self.agent_object_intents.values():
            intents.clear()
        for held_objects in self.agent_held_objects.values():
            held_objects.clear()
