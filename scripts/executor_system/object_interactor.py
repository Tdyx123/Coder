"""Explicit object interaction, compound helpers and bounded recovery."""
import math
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import PLACEMENT_RESTRICTIONS
from .utils import RobotRef, event_error_message, is_egg_query, matches_object, object_center, object_distance, object_key, operated_object_name, stable_object_name, step_event_failed, require_break_egg_target, log
from .execution_control import raise_if_execution_aborted
from .goals import record_verified_goal_state

from .plan_types import PlannedAction
from .config import INTERACTION_MAX_PASS_STEPS
from .goals import object_filled_with_coffee, object_filled_with_water, record_groundtruth_state, state_satisfied


def is_pickup_object_clip_error(value: Any) -> bool:
    return 'Picking up object would cause it to collide and clip into something!'.casefold() in str(value or "").casefold()


def is_object_action_target_visibility_error(value: Any) -> bool:
    return 'Target object not found within the specified visibility'.casefold() in str(value or "").casefold()


def is_pickup_object_target_visibility_error(value: Any) -> bool:
    return is_object_action_target_visibility_error(value)


class ObjectInteractor:
    def __init__(self, runtime, *, helper_log=log, max_pass_steps=None, planned_action_consumer=None):
        self.runtime = runtime
        self.planned_action_consumer = planned_action_consumer
        self.helper_log = helper_log
        self.max_pass_steps = INTERACTION_MAX_PASS_STEPS if max_pass_steps is None else max_pass_steps

    def execute(self, robot_id, prepared, action_context, *, registry=None):
        """Dispatch the prepared action inside the caller's admission/lease scope."""
        from .action_registry import ActionRegistry, _direct
        registry = registry or ActionRegistry()
        normalized = prepared.normalized
        executor = (_direct if normalized.form == 'thor'
                    else registry.get(normalized.action.action_type).executor)
        return executor(self.runtime, robot_id, normalized, action_context)

    def held_objects_description_for_log(self, agent_id: Any) -> str:
        runtime = self.runtime
        try:
            held_objects = sorted(runtime.agent_held_objects_for(int(agent_id)))
        except (RuntimeError, TypeError, ValueError):
            held_objects = []
        return ", ".join(held_objects) if held_objects else "nothing"

    def object_name_for_log(self, obj: Dict[str, Any]) -> str:
        return stable_object_name(obj) or str(obj.get("objectId") or "")

    def object_id_name_for_log(self, agent_id: int, object_id: Any) -> str:
        runtime = self.runtime
        object_id_text = str(object_id or "")
        for obj in runtime.current_objects(agent_id):
            if str(obj.get("objectId") or "") == object_id_text:
                return runtime.object_name_for_log(obj)
        return object_id_text.split("|", 1)[0] if object_id_text else ""

    def current_object_by_id(
        self,
        agent_id: int,
        object_id: Any,
    ) -> Optional[Dict[str, Any]]:
        runtime = self.runtime
        object_id_text = str(object_id or "")
        for obj in runtime.current_objects(agent_id):
            if str(obj.get("objectId") or "") == object_id_text:
                return obj
        return None

    def toggle_action_target_state(self, action: str) -> Optional[bool]:
        if action == "ToggleObjectOn":
            return True
        if action == "ToggleObjectOff":
            return False
        return None

    def object_toggle_state(self, obj: Dict[str, Any]) -> Optional[bool]:
        if "isToggled" in obj and obj.get("isToggled") is not None:
            return bool(obj.get("isToggled"))
        if "isOn" in obj and obj.get("isOn") is not None:
            return bool(obj.get("isOn"))
        return None

    def toggle_state_matches(self, action: str, obj: Dict[str, Any]) -> bool:
        runtime = self.runtime
        desired_state = runtime.toggle_action_target_state(action)
        current_state = runtime.object_toggle_state(obj)
        return desired_state is not None and current_state is not None and desired_state == current_state

    def toggle_error_matches_desired_state(self, action: str, error: str) -> bool:
        error_text = str(error or "").lower()
        if action == "ToggleObjectOn":
            return "already on" in error_text
        if action == "ToggleObjectOff":
            return "already off" in error_text
        return False

    def held_object_names_for_log(self, agent_id: int) -> List[str]:
        runtime = self.runtime
        return [
            runtime.object_id_name_for_log(agent_id, object_id)
            for object_id in sorted(runtime.agent_held_objects_for(agent_id))
        ]

    def log_put_object_failure_held_items(self, agent_id: Any) -> None:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        held_description = runtime.held_objects_description_for_log(agent_id)
        settings['log'](
            f"PutObject failed for agent {agent_id}; "
            f"currently holding: {held_description}."
        )

    def retry_move_past_open_object_blocker(
        self,
        agent_id: int,
        move_payload: Dict[str, Any],
        failed_event: Any,
    ) -> Optional[bool]:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        match = settings['MOVE_BLOCKER_PATTERN'].match(event_error_message(failed_event))
        if match is None:
            return None
        try:
            blocker = runtime.find_object(match.group("object_name"), agent_id=agent_id)
        except RuntimeError as exc:
            raise_if_execution_aborted(runtime, exc)
            return None
        if not blocker.get("openable") or not blocker.get("isOpen"):
            return None

        object_id = str(blocker.get("objectId") or "")
        if not object_id:
            return None
        from .action_registry import validate_recovery_capabilities
        # A robot that can close but cannot restore must leave the blocker open.
        validate_recovery_capabilities(runtime, agent_id, 'CloseObject', 'OpenObject')
        close_event = runtime._step_direct(
            {
                "action": "CloseObject",
                "objectId": object_id,
                "agentId": agent_id,
                "forceAction": settings['CLOSE_OBJECT_FORCE_ACTION'],
            },
            check_success=False,
        )
        if step_event_failed(close_event):
            return None

        retry_event = None
        try:
            retry_event = runtime._step_direct(move_payload, check_success=False)
        finally:
            reopen_event = runtime._step_direct(
                {
                    "action": "OpenObject",
                    "objectId": object_id,
                    "agentId": agent_id,
                    "forceAction": settings['OPEN_OBJECT_FORCE_ACTION'],
                },
                check_success=False,
            )
            if step_event_failed(reopen_event):
                raise RuntimeError(
                    f"Could not restore open navigation blocker {object_id!r}: "
                    f"{event_error_message(reopen_event) or 'no error message returned'}"
                )
        return retry_event is not None and not step_event_failed(retry_event)

    def handoff_held_object_direct(
        self,
        from_agent_id: int,
        to_agent_id: int,
        object_resource: str,
    ):
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        settings['log'](
            "Transferring held object "
            f"{object_resource} from agent {from_agent_id} to agent {to_agent_id}."
        )
        drop_event = runtime._step_direct(
            {
                "action": "DropHandObject",
                "agentId": from_agent_id,
                "forceAction": False,
            },
            check_success=False,
            save_frame=False,
        )
        if step_event_failed(drop_event):
            metadata = getattr(drop_event, "metadata", {}) or {}
            error = metadata.get("errorMessage") or "no error message returned"
            raise RuntimeError(f"DropHandObject failed for agent {from_agent_id}: {error}")
        runtime.update_object_aliases_for_object_ids(
            [object_resource],
            agent_id=to_agent_id,
            event=drop_event,
        )

        dropped = runtime.find_object(object_resource, agent_id=to_agent_id, require_center=True)
        center = object_center(dropped)
        if not center:
            raise RuntimeError(f"Dropped object {object_resource!r} has no usable center.")
        target_position = runtime.closest_reachable(center, agent_id=to_agent_id)
        try:
            if from_agent_id in runtime.navigation_blockers(to_agent_id, target_position):
                runtime.teleport_completed_agent_to_free_position(
                    from_agent_id,
                    to_agent_id,
                    target_position,
                )
        except RuntimeError as exc:
            raise_if_execution_aborted(runtime, exc)
            pass
        runtime.move_to_position_direct(
            to_agent_id,
            target_position,
        )
        runtime.face_position_direct(to_agent_id, center)
        pickup_event = runtime._step_direct(
            {
                "action": "PickupObject",
                "objectId": dropped["objectId"],
                "agentId": to_agent_id,
                "forceAction": False,
            },
            check_success=False,
        )
        if step_event_failed(pickup_event):
            metadata = getattr(pickup_event, "metadata", {}) or {}
            error = metadata.get("errorMessage") or "no error message returned"
            raise RuntimeError(f"PickupObject handoff failed for agent {to_agent_id}: {error}")
        return pickup_event

    def agent_holds_object(self, agent_id: int, object_resource: str) -> bool:
        runtime = self.runtime
        return object_resource in runtime.agent_held_objects_for(agent_id)

    def agent_held_object_matching(self, agent_id: int, pattern: Any) -> Optional[str]:
        runtime = self.runtime
        resolved_pattern = runtime.object_alias_current_id(pattern) or pattern
        for object_resource in runtime.agent_held_objects_for(agent_id):
            stub = {
                "objectId": object_resource,
                "objectType": object_resource.split("|", 1)[0],
            }
            if matches_object(resolved_pattern, stub) or (
                resolved_pattern != pattern and matches_object(pattern, stub)
            ):
                return object_resource
        return None

    def agent_held_objects_for(self, agent_id: int) -> Set[str]:
        runtime = self.runtime
        held_objects = runtime.agent_held_object_overrides_snapshot(agent_id)
        held_objects.update(runtime.metadata_held_objects(agent_id))
        return held_objects

    def _held_object_override_state(self) -> Tuple[Dict[int, Set[str]], threading.Lock]:
        runtime = self.runtime
        overrides = getattr(runtime, "agent_held_object_overrides", None)
        if overrides is None:
            overrides = {}
            runtime.agent_held_object_overrides = overrides
        lock = getattr(runtime, "agent_held_object_overrides_lock", None)
        if lock is None:
            lock = threading.Lock()
            runtime.agent_held_object_overrides_lock = lock
        return overrides, lock

    def agent_held_object_overrides_snapshot(self, agent_id: int) -> Set[str]:
        runtime = self.runtime
        overrides, lock = runtime._held_object_override_state()
        with lock:
            return set(overrides.get(agent_id, set()))

    def record_agent_held_object(self, agent_id: int, object_id: str) -> None:
        runtime = self.runtime
        if not object_id:
            return
        overrides, lock = runtime._held_object_override_state()
        with lock:
            overrides.setdefault(agent_id, set()).add(str(object_id))

    def release_agent_held_objects(self, agent_id: int) -> None:
        runtime = self.runtime
        overrides, lock = runtime._held_object_override_state()
        with lock:
            overrides.setdefault(agent_id, set()).clear()

    def metadata_held_objects(self, agent_id: int) -> Set[str]:
        runtime = self.runtime
        held_objects = set()
        with runtime.controller_lock:
            event = runtime._agent_event_unlocked(agent_id)
            metadata = getattr(event, "metadata", {}) or {}
            inventory_objects = metadata.get("inventoryObjects") or []
        for obj in inventory_objects:
            if not isinstance(obj, dict):
                continue
            object_id = obj.get("objectId")
            if object_id:
                held_objects.add(str(object_id))
        return held_objects

    def prepare_hand_for_goto_if_needed(
        self,
        robot: RobotRef,
        next_action: Optional[PlannedAction],
    ) -> None:
        runtime = self.runtime
        from .action_resources import active_resources
        if active_resources(runtime) is not None:
            return
        if next_action is None or next_action.name != "PickupObject":
            return
        pickup_target = next_action.args[0] if next_action.args else None
        agent_id = runtime.physical_agent_id(robot)
        if not runtime.agent_held_objects_for(agent_id):
            return
        if pickup_target is not None and runtime.agent_held_object_matching(
            agent_id,
            pickup_target,
        ):
            return
        runtime.place_held_objects_for_pickup(robot, pickup_target)

    def prepare_hand_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
        target_obj: Dict[str, Any],
    ) -> Dict[str, Any]:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        agent_id = runtime.physical_agent_id(robot)
        if not runtime.agent_held_objects_for(agent_id):
            return target_obj

        original_position = dict(runtime.current_agent_position(agent_id))
        target_object_id = str(target_obj.get("objectId") or "")
        can_return_to_target = bool(target_object_id and object_center(target_obj))

        runtime.place_held_objects_for_pickup(robot, pickup_target)

        if can_return_to_target:
            try:
                return runtime.navigate_to_object(
                    robot,
                    target_object_id,
                    allow_hand_preparation=False,
                )
            except RuntimeError as exc:
                raise_if_execution_aborted(runtime, exc)
                settings['log'](
                    "Could not return directly to pickup target "
                    f"{target_object_id}: {exc}; returning to prior position."
                )

        runtime.teleport_to_position(agent_id, original_position)
        return runtime.find_object(pickup_target, agent_id=agent_id)

    def place_held_objects_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
    ) -> None:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        agent_id = runtime.physical_agent_id(robot)
        held_objects = sorted(runtime.agent_held_objects_for(agent_id))
        if not held_objects:
            return

        from .action_registry import validate_recovery_capabilities
        validate_recovery_capabilities(runtime, agent_id, 'GoToObject', 'PutObject')
        held_object = held_objects[0]
        held_object_type = runtime.held_object_type(agent_id, held_object)
        candidates = runtime.compatible_receptacle_candidates(
            agent_id,
            held_object,
            held_object_type,
        )
        if not candidates:
            raise RuntimeError(
                "No compatible receptacle found for held object "
                f"{held_object} ({held_object_type}) before PickupObject "
                f"{pickup_target!r} for agent {agent_id}."
            )

        from .action_resources import active_resources
        admitted = active_resources(runtime)
        if admitted is not None:
            bound_id = admitted.resolved.bindings.get('@hand_receptacle')
            if bound_id is None:
                admitted.invalid('unbound automatic hand placement')
            candidates = [runtime.find_object(bound_id, agent_id=agent_id)]

        last_error = "no candidate was attempted"
        for receptacle in candidates:
            receptacle_id = str(receptacle.get("objectId"))
            try:
                runtime.navigate_to_object(
                    robot,
                    receptacle_id,
                    allow_hand_preparation=False,
                )
                event = runtime.put_held_object_in_receptacle(agent_id, receptacle)
            except RuntimeError as exc:
                raise_if_execution_aborted(runtime, exc)
                last_error = str(exc)
                settings['log'](
                    f"Could not place held object {held_object} into "
                    f"{receptacle_id}: {last_error}"
                )
                continue

            if not step_event_failed(event):
                settings['log'](f"Placed held object {held_object} into {receptacle_id}.")
                return

            metadata = getattr(event, "metadata", {}) or {}
            last_error = metadata.get("errorMessage") or "PutObject failed"
            settings['log'](
                f"Could not place held object {held_object} into "
                f"{receptacle_id}: {last_error}"
            )

        raise RuntimeError(
            "Could not place held object "
            f"{held_object} ({held_object_type}) before PickupObject "
            f"{pickup_target!r} for agent {agent_id}: {last_error}"
        )

    def held_object_type(self, agent_id: int, held_object: str) -> str:
        runtime = self.runtime
        for obj in runtime.current_objects(agent_id):
            if obj.get("objectId") != held_object:
                continue
            object_type = obj.get("objectType") or obj.get("name")
            if object_type:
                return str(object_type)
        return held_object.split("|", 1)[0]

    def allowed_receptacle_type_keys(self, held_object_type: str) -> Set[str]:
        held_key = object_key(held_object_type)
        for object_type, receptacle_types in PLACEMENT_RESTRICTIONS.items():
            if object_key(object_type) == held_key:
                return {object_key(receptacle_type) for receptacle_type in receptacle_types}
        return set()

    def compatible_receptacle_candidates(
        self,
        agent_id: int,
        held_object: str,
        held_object_type: str,
    ) -> List[Dict[str, Any]]:
        runtime = self.runtime
        allowed_type_keys = runtime.allowed_receptacle_type_keys(held_object_type)
        if not allowed_type_keys:
            return []

        candidates = []
        for obj in runtime.current_objects(agent_id):
            object_id = obj.get("objectId")
            if not object_id or str(object_id) == held_object:
                continue
            object_type = obj.get("objectType") or str(object_id).split("|", 1)[0]
            if object_key(object_type) not in allowed_type_keys:
                continue
            if object_center(obj) is None:
                continue
            candidates.append(obj)

        candidates.sort(
            key=lambda obj: (
                not bool(obj.get("visible", False)),
                object_distance(obj),
                str(obj.get("objectId") or ""),
            )
        )
        return candidates

    def put_held_object_in_receptacle(
        self,
        agent_id: int,
        receptacle: Dict[str, Any],
    ):
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        held_names = runtime.held_object_names_for_log(agent_id)
        put_name = ", ".join(held_names) if held_names else "nothing"
        settings['log'](f"PutObject names: {put_name}, {runtime.object_name_for_log(receptacle)}")
        payload = {
            "action": "PutObject",
            "objectId": receptacle["objectId"],
            "agentId": agent_id,
            "forceAction": settings['PUT_OBJECT_FORCE_ACTION'],
            "objectResources": [
                *runtime.agent_held_objects_for(agent_id),
                receptacle["objectId"],
            ],
        }
        with runtime.stats_lock:
            runtime.total_exec += 1
        event = runtime.step(
            payload,
            check_success=False,
            retry_on_failure=False,
            max_retries=0,
        )
        if not step_event_failed(event):
            with runtime.stats_lock:
                runtime.success_exec += 1
            runtime.record_operated_object_name(receptacle)
        return event

    def held_item_rotation_failure(self, exc: BaseException) -> bool:
        message = str(exc).lower()
        if "held item" not in message:
            return False
        return "rotateright failed" in message or "rotateleft failed" in message

    def object_action(
        self,
        action: str,
        robot: RobotRef,
        obj_name: Any,
        *,
        force_action: bool = False,
        extra_object_resources: Sequence[str] = (),
        action_parameters: Optional[Dict[str, Any]] = None,
    ):
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        agent_id = runtime.physical_agent_id(robot)
        if action == "PickupObject":
            held_object = runtime.agent_held_object_matching(agent_id, obj_name)
            if held_object is not None:
                settings['log'](
                    "PickupObject name: "
                    f"{runtime.object_id_name_for_log(agent_id, held_object)}"
                )
                settings['log'](f"Skipping PickupObject for agent {agent_id}; already holding {held_object}")
                with runtime.stats_lock:
                    runtime.total_exec += 1
                    runtime.success_exec += 1
                runtime.update_object_alias_for_pattern(obj_name, held_object, agent_id=agent_id)
                return runtime.agent_event(agent_id)
            obj = runtime.find_object(obj_name, agent_id=agent_id)
            if runtime.agent_held_objects_for(agent_id):
                obj = runtime.prepare_hand_for_pickup(robot, obj_name, obj)
        else:
            obj = runtime.find_object(obj_name, agent_id=agent_id)
        if action == "PickupObject":
            settings['log'](f"PickupObject name: {runtime.object_name_for_log(obj)}")
        elif action == "PutObject":
            held_names = runtime.held_object_names_for_log(agent_id)
            put_name = ", ".join(held_names) if held_names else "nothing"
            settings['log'](f"PutObject names: {put_name}, {runtime.object_name_for_log(obj)}")
        settings['log'](f"{action} {obj_name} -> {operated_object_name(obj)} {obj.get('objectId')}")
        return runtime.object_action_by_object(
            action,
            agent_id,
            obj,
            force_action=force_action,
            extra_object_resources=extra_object_resources,
            action_parameters=action_parameters,
            goal_object_name=obj_name,
        )

    def teleport_object_to_hand(self, robot: RobotRef, obj_name: Any):
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        agent_id = runtime.physical_agent_id(robot)
        held_object = runtime.agent_held_object_matching(agent_id, obj_name)
        if held_object is not None:
            settings['log'](
                "Skipping TeleportObjectToHand for agent "
                f"{agent_id}; already holding {held_object}"
            )
            with runtime.stats_lock:
                runtime.total_exec += 1
                runtime.success_exec += 1
            runtime.update_object_alias_for_pattern(obj_name, held_object, agent_id=agent_id)
            return runtime.agent_event(agent_id)

        held_objects = sorted(runtime.agent_held_objects_for(agent_id))
        if held_objects:
            held_description = ", ".join(held_objects)
            raise RuntimeError(
                f"Cannot TeleportObjectToHand {obj_name!r} for agent {agent_id}: "
                "robot hand is not empty. "
                f"Currently holding: {held_description}."
            )

        obj = runtime.find_object(obj_name, agent_id=agent_id)
        settings['log'](
            "TeleportObjectToHand "
            f"{obj_name} -> {operated_object_name(obj)} {obj.get('objectId')}"
        )
        return runtime.object_action_by_object(
            "PickupObject",
            agent_id,
            obj,
            force_action=True,
            goal_object_name=obj_name,
        )

    def pickup_clip_backoff_position(
        self,
        agent_id: int,
        distance: float,
    ) -> Dict[str, float]:
        runtime = self.runtime
        metadata = runtime.agent_event(agent_id).metadata
        agent = metadata.get("agent", {})
        position = dict(agent.get("position") or runtime.current_agent_position(agent_id))
        rotation = agent.get("rotation", {})
        yaw = math.radians(float(rotation.get("y", 0.0) or 0.0))
        position["x"] = float(position.get("x", 0.0)) - math.sin(yaw) * distance
        position["z"] = float(position.get("z", 0.0)) - math.cos(yaw) * distance
        return position

    def retry_pickup_after_clip_error(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        last_event = initial_event
        for distance in settings['PICKUP_OBJECT_CLIP_BACKOFF_DISTANCES']:
            target_position = runtime.pickup_clip_backoff_position(agent_id, distance)
            try:
                runtime.teleport_to_position_direct(agent_id, target_position)
            except Exception as exc:
                raise_if_execution_aborted(runtime, exc)
                settings['log'](
                    "PickupObject clip backoff teleport failed for agent "
                    f"{agent_id} by {distance}: {exc}"
                )
                continue

            try:
                retry_event = runtime.step(
                    payload,
                    check_success=False,
                    retry_on_failure=False,
                )
            except Exception as exc:
                raise_if_execution_aborted(runtime, exc)
                if not is_pickup_object_clip_error(exc):
                    raise
                settings['log'](
                    "PickupObject still clipped after moving agent "
                    f"{agent_id} back by {distance}; trying another distance."
                )
                continue
            metadata = getattr(retry_event, "metadata", {}) or {}
            error = metadata.get("errorMessage")
            last_event = retry_event
            if metadata.get("lastActionSuccess", not bool(error)):
                return retry_event
            if not is_pickup_object_clip_error(error):
                return retry_event
            settings['log'](
                "PickupObject still clipped after moving agent "
                f"{agent_id} back by {distance}; trying another distance."
            )
        return last_event

    def camera_horizon(
        self,
        agent_id: int,
        event: Optional[Any] = None,
    ) -> Optional[float]:
        runtime = self.runtime
        metadata = getattr(event, "metadata", {}) if event is not None else {}
        agent = metadata.get("agent", {}) if isinstance(metadata, dict) else {}
        horizon = agent.get("cameraHorizon")
        if horizon is None:
            try:
                metadata = runtime.agent_event(agent_id).metadata
            except Exception as exc:
                raise_if_execution_aborted(runtime, exc)
                return None
            agent = metadata.get("agent", {}) if isinstance(metadata, dict) else {}
            horizon = agent.get("cameraHorizon")
        try:
            return float(horizon)
        except (TypeError, ValueError):
            return None

    def try_look_to_camera_horizon(
        self,
        agent_id: int,
        target_horizon: float,
        *,
        action_name: str = "PickupObject",
    ) -> bool:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        current_horizon = runtime.camera_horizon(agent_id)
        if current_horizon is None:
            return False
        delta = float(target_horizon) - current_horizon
        if abs(delta) < 1e-3:
            return True
        action = "LookDown" if delta > 0.0 else "LookUp"
        try:
            event = runtime.step(
                {"action": action, "degrees": abs(delta), "agentId": agent_id},
                check_success=False,
                retry_on_failure=False,
            )
        except Exception as exc:
            raise_if_execution_aborted(runtime, exc)
            settings['log'](
                f"{action_name} visibility look adjustment failed for agent "
                f"{agent_id}: {exc}"
            )
            return False
        metadata = getattr(event, "metadata", {}) or {}
        error = metadata.get("errorMessage")
        if metadata.get("lastActionSuccess", not bool(error)):
            return True
        settings['log'](
            f"{action_name} visibility look adjustment failed for agent "
            f"{agent_id}: {error or 'no error message returned'}"
        )
        return False

    def retry_object_action_after_target_visibility_error(
        self,
        action: str,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        initial_horizon = runtime.camera_horizon(agent_id, initial_event)
        if initial_horizon is None:
            if action == "SliceObject":
                return runtime.retry_slice_after_interaction_reposition(
                    agent_id,
                    payload,
                    initial_event,
                )
            return initial_event

        last_event = initial_event
        restore_horizon = True
        try:
            for offset in settings['PICKUP_OBJECT_TARGET_VISIBILITY_LOOK_OFFSETS']:
                target_horizon = initial_horizon + offset
                if not runtime.try_look_to_camera_horizon(
                    agent_id,
                    target_horizon,
                    action_name=action,
                ):
                    continue

                try:
                    retry_event = runtime.step(
                        payload,
                        check_success=False,
                        retry_on_failure=False,
                    )
                except Exception as exc:
                    raise_if_execution_aborted(runtime, exc)
                    if action == "PickupObject" and is_pickup_object_clip_error(exc):
                        retry_event = runtime.retry_pickup_after_clip_error(
                            agent_id,
                            payload,
                            None,
                        )
                        if retry_event is None:
                            continue
                    elif is_object_action_target_visibility_error(exc):
                        settings['log'](
                            f"{action} target still outside visibility for agent "
                            f"{agent_id} after camera offset {offset:g}; "
                            "trying another angle."
                        )
                        continue
                    else:
                        raise

                metadata = getattr(retry_event, "metadata", {}) or {}
                error = metadata.get("errorMessage")
                last_event = retry_event
                if metadata.get("lastActionSuccess", not bool(error)):
                    restore_horizon = False
                    return retry_event
                if action == "PickupObject" and is_pickup_object_clip_error(error):
                    retry_event = runtime.retry_pickup_after_clip_error(
                        agent_id,
                        payload,
                        retry_event,
                    )
                    metadata = getattr(retry_event, "metadata", {}) or {}
                    error = metadata.get("errorMessage")
                    last_event = retry_event
                    if metadata.get("lastActionSuccess", not bool(error)):
                        restore_horizon = False
                    return retry_event
                if not is_object_action_target_visibility_error(error):
                    return retry_event
                settings['log'](
                    f"{action} target still outside visibility for agent "
                    f"{agent_id} after camera offset {offset:g}; "
                    "trying another angle."
                )
        finally:
            if restore_horizon:
                runtime.try_look_to_camera_horizon(
                    agent_id,
                    initial_horizon,
                    action_name=action,
                )
        if action == "SliceObject":
            return runtime.retry_slice_after_interaction_reposition(
                agent_id,
                payload,
                last_event,
            )
        return last_event

    def retry_slice_after_interaction_reposition(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        movement_config = getattr(runtime, "movement_config", None)
        if getattr(getattr(movement_config, "mode", None), "value", None) != "step":
            return initial_event

        target = payload.get("objectId")
        if not target:
            return initial_event
        robot_names = sorted(
            name
            for name, mapped_agent_id in runtime.robot_agent_map.items()
            if int(mapped_agent_id) == int(agent_id)
        )
        if not robot_names:
            return initial_event

        from .action_registry import validate_recovery_capabilities
        validate_recovery_capabilities(runtime, agent_id, 'GoToObject')
        state = runtime._interaction_reposition_state
        active_keys = set(getattr(state, "active_keys", set()))
        recovery_key = (int(agent_id), "SliceObject", str(target))
        if recovery_key in active_keys:
            return initial_event
        active_keys.add(recovery_key)
        state.active_keys = active_keys
        try:
            # Include request construction and the final Slice: both must observe
            # the same static peers as the reposition plan. RLock permits the
            # single-request coordinator to enter the same execution scope.
            with runtime.navigation_execution_scope():
                try:
                    runtime.navigate_to_object(
                        robot_names[0],
                        target,
                        allow_hand_preparation=False,
                        next_action=PlannedAction("SliceObject", (str(target),)),
                        exclude_current_position=True,
                    )
                except TimeoutError:
                    raise
                except Exception as exc:
                    raise_if_execution_aborted(runtime, exc)
                    settings['log'](
                        "SliceObject interaction reposition failed for agent "
                        f"{agent_id}: {exc}"
                    )
                    return initial_event

                runtime.check_navigation_deadline()
                runtime.navigation_metrics.increment("interaction_repositions")
                try:
                    return runtime.step(
                        payload,
                        check_success=False,
                        retry_on_failure=False,
                    )
                except Exception as exc:
                    raise_if_execution_aborted(runtime, exc)
                    if is_object_action_target_visibility_error(exc):
                        return initial_event
                    raise
        finally:
            active_keys = set(getattr(state, "active_keys", set()))
            active_keys.discard(recovery_key)
            state.active_keys = active_keys

    def retry_pickup_after_target_visibility_error(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        runtime = self.runtime
        return runtime.retry_object_action_after_target_visibility_error(
            "PickupObject",
            agent_id,
            payload,
            initial_event,
        )

    def object_action_by_object(
        self,
        action: str,
        agent_id: int,
        obj: Dict[str, Any],
        *,
        force_action: bool = False,
        extra_object_resources: Sequence[str] = (),
        action_parameters: Optional[Dict[str, Any]] = None,
        goal_object_name: Any = None,
    ):
        runtime = self.runtime
        settings = runtime._object_interaction_settings()
        payload = {"action": action, "objectId": obj["objectId"], "agentId": agent_id}
        if action_parameters:
            payload.update(action_parameters)
        object_resources = [
            *[str(resource) for resource in extra_object_resources if resource],
            str(obj["objectId"]),
        ]
        payload["objectResources"] = list(dict.fromkeys(object_resources))
        if "forceAction" not in payload:
            if action == "PutObject":
                payload["forceAction"] = settings['PUT_OBJECT_FORCE_ACTION']
            elif action == "OpenObject":
                payload["forceAction"] = settings['OPEN_OBJECT_FORCE_ACTION']
            elif action == "CloseObject":
                payload["forceAction"] = settings['CLOSE_OBJECT_FORCE_ACTION']
            elif action == "DirtyObject":
                payload["forceAction"] = settings['DIRTY_OBJECT_FORCE_ACTION']
            elif action == "ToggleObjectOn":
                payload["forceAction"] = settings['TOGGLE_OBJECT_ON_FORCE_ACTION']
            elif action == "ToggleObjectOff":
                payload["forceAction"] = settings['TOGGLE_OBJECT_OFF_FORCE_ACTION']
            elif action == "EmptyLiquidFromObject":
                payload["forceAction"] = settings['EMPTY_LIQUID_FORCE_ACTION']
            elif force_action:
                payload["forceAction"] = True
        if action == "PickupObject" and runtime.agent_holds_object(agent_id, obj["objectId"]):
            settings['log'](f"Skipping PickupObject for agent {agent_id}; already holding {obj['objectId']}")
            with runtime.stats_lock:
                runtime.total_exec += 1
                runtime.success_exec += 1
            runtime.update_object_alias_after_action(
                action,
                agent_id,
                obj,
                goal_object_name=goal_object_name,
            )
            runtime.record_operated_object_name(obj)
            return runtime.agent_event(agent_id)
        if action in {"ToggleObjectOn", "ToggleObjectOff"} and runtime.toggle_state_matches(action, obj):
            desired_state = "on" if action == "ToggleObjectOn" else "off"
            settings['log'](
                f"Skipping {action} for agent {agent_id}; "
                f"{obj['objectId']} is already {desired_state}"
            )
            with runtime.stats_lock:
                runtime.total_exec += 1
                runtime.success_exec += 1
            runtime.update_object_alias_after_action(
                action,
                agent_id,
                obj,
                goal_object_name=goal_object_name,
            )
            runtime.record_operated_object_name(obj)
            return runtime.agent_event(agent_id)

        source_resource = (
            goal_object_name
            if goal_object_name is not None
            else (obj.get("objectId") or stable_object_name(obj))
        )
        breaks_egg = action == "BreakObject" and is_egg_query(source_resource)

        known_object_ids = set()
        if action == "SliceObject" or breaks_egg:
            known_object_ids = {
                str(current.get("objectId") or "")
                for current in runtime.current_objects(agent_id)
                if current.get("objectId")
            }

        with runtime.stats_lock:
            runtime.total_exec += 1
        target_visibility_retry_attempted = False
        try:
            event = runtime.step(
                payload,
                check_success=False,
                retry_on_failure=False,
            )
        except Exception as exc:
            raise_if_execution_aborted(runtime, exc)
            if action == "PickupObject" and is_pickup_object_clip_error(exc):
                retry_event = runtime.retry_pickup_after_clip_error(agent_id, payload, None)
            elif (
                action in settings['OBJECT_ACTION_TARGET_VISIBILITY_RETRY_ACTIONS']
                and is_object_action_target_visibility_error(exc)
            ):
                target_visibility_retry_attempted = True
                retry_event = runtime.retry_object_action_after_target_visibility_error(
                    action,
                    agent_id,
                    payload,
                    None,
                )
            else:
                raise
            if retry_event is None:
                raise
            event = retry_event
        metadata = getattr(event, "metadata", {}) or {}
        error = metadata.get("errorMessage")
        if not metadata.get("lastActionSuccess", not bool(error)):
            recovered_from_failure = False
            if action == "PickupObject" and is_pickup_object_clip_error(error):
                event = runtime.retry_pickup_after_clip_error(agent_id, payload, event)
                metadata = getattr(event, "metadata", {}) or {}
                error = metadata.get("errorMessage")
                recovered_from_failure = metadata.get("lastActionSuccess", not bool(error))
            if (
                not recovered_from_failure
                and action in settings['OBJECT_ACTION_TARGET_VISIBILITY_RETRY_ACTIONS']
                and not target_visibility_retry_attempted
                and is_object_action_target_visibility_error(error)
            ):
                event = runtime.retry_object_action_after_target_visibility_error(
                    action,
                    agent_id,
                    payload,
                    event,
                )
                metadata = getattr(event, "metadata", {}) or {}
                error = metadata.get("errorMessage")
                recovered_from_failure = metadata.get("lastActionSuccess", not bool(error))
            if not recovered_from_failure:
                if action == "PutObject":
                    runtime.log_put_object_failure_held_items(agent_id)
                if action in {"ToggleObjectOn", "ToggleObjectOff"}:
                    observed_obj = runtime.current_object_by_id(agent_id, obj["objectId"])
                    if (
                        observed_obj is not None
                        and runtime.toggle_error_matches_desired_state(action, error or "")
                        and runtime.toggle_state_matches(action, observed_obj)
                    ):
                        with runtime.stats_lock:
                            runtime.success_exec += 1
                        runtime.update_object_alias_after_action(
                            action,
                            agent_id,
                            observed_obj,
                            event=event,
                            goal_object_name=goal_object_name,
                        )
                        runtime.record_operated_object_name(observed_obj)
                        return event
                raise RuntimeError(
                    f"{action} failed for agent {agent_id} on {obj['objectId']}: "
                    f"{error or 'no error message returned'}"
                )
        with runtime.stats_lock:
            runtime.success_exec += 1
        runtime.update_object_alias_after_action(
            action,
            agent_id,
            obj,
            event=event,
            goal_object_name=goal_object_name,
            extra_object_resources=extra_object_resources,
            known_object_ids=known_object_ids,
        )
        if action == "OpenObject":
            object_id = str(obj["objectId"])
            opened_obj = runtime.current_object_by_id(agent_id, object_id)
            if opened_obj is None:
                for metadata_obj in metadata.get("objects") or []:
                    if str(metadata_obj.get("objectId") or "") == object_id:
                        opened_obj = metadata_obj
                        break
            openness = "unknown" if opened_obj is None else opened_obj.get("openness", "unknown")
            settings['log'](f"OpenObject openness: {object_id} openness={openness}")
        if action == "SliceObject":
            runtime.record_created_slice_object_names(obj, event, known_object_ids)
            sliced_goal_name = (
                goal_object_name
                if goal_object_name is not None
                else stable_object_name(obj)
            )
            record_verified_goal_state(
                sliced_goal_name,
                "SLICED",
                str(obj.get("objectId") or "") or None,
                getattr(runtime, "evaluation_context", None),
                runtime=runtime,
            )
        if breaks_egg:
            runtime.record_created_broken_egg_object_names(obj, event, known_object_ids)
            broken_goal_name = (
                goal_object_name
                if goal_object_name is not None
                else stable_object_name(obj)
            )
            record_verified_goal_state(
                broken_goal_name,
                "BROKEN",
                str(obj.get("objectId") or "") or None,
                getattr(runtime, "evaluation_context", None),
                runtime=runtime,
            )
        runtime.record_operated_object_name(obj)
        return event

    def throw_object(self, robot: RobotRef, move_magnitude: float = 7):
        runtime = self.runtime
        agent_id = runtime.physical_agent_id(robot)
        held_objects = sorted(runtime.agent_held_objects_for(agent_id))
        with runtime.stats_lock:
            runtime.total_exec += 1
        event = runtime.step(
            {
                "action": "ThrowObject",
                "moveMagnitude": move_magnitude,
                "agentId": agent_id,
                "forceAction": False,
                "objectResources": held_objects,
            },
            check_success=False,
            retry_on_failure=False,
        )
        metadata = getattr(event, "metadata", {}) or {}
        error = metadata.get("errorMessage")
        if not metadata.get("lastActionSuccess", not bool(error)):
            raise RuntimeError(
                f"ThrowObject failed for agent {agent_id}: "
                f"{error or 'no error message returned'}"
            )
        with runtime.stats_lock:
            runtime.success_exec += 1
        runtime.update_object_aliases_for_object_ids(
            held_objects,
            agent_id=agent_id,
            event=event,
        )
        return event

    def toggle_objects(self, action: str, robot: RobotRef, obj_name: Any) -> None:
        runtime = self.runtime
        agent_id = runtime.physical_agent_id(robot)
        matches = runtime.find_objects(obj_name, agent_id=agent_id)
        if not matches:
            raise RuntimeError(f"Could not find switchable object {obj_name!r}")
        obj = matches[0]
        force_action = object_key(obj.get("objectType") or "") == "stoveknob"
        return runtime.object_action_by_object(
            action,
            agent_id,
            obj,
            force_action=force_action,
            goal_object_name=obj_name,
        )

    def current_objects(self, agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.runtime.current_objects(agent_id)

    def find_object(
        self,
        pattern: Any,
        agent_id: Optional[int] = None,
        require_center: bool = False,
    ) -> Dict[str, Any]:
        return self.runtime.find_object(pattern, agent_id=agent_id, require_center=require_center)

    def WaitOneTick(self, robot: RobotRef) -> None:
        self._consume_planned_action("WaitOneTick")
        runtime = self.runtime
        agent_id = runtime.physical_agent_id(robot)
        runtime.step({"action": "Pass", "agentId": agent_id}, check_success=False)

    def GoToObject(self, robot: RobotRef, dest_obj: Any) -> None:
        next_action = self._consume_planned_action("GoToObject", dest_obj)
        self.runtime.navigate_to_object(robot, dest_obj, next_action=next_action)

    def PickupObject(self, robot: RobotRef, pick_obj: Any) -> None:
        self._consume_planned_action("PickupObject", pick_obj)
        self.runtime.object_action("PickupObject", robot, pick_obj)

    def TeleportObjectToHand(self, robot: RobotRef, pick_obj: Any) -> None:
        self._consume_planned_action("TeleportObjectToHand", pick_obj)
        self.runtime.teleport_object_to_hand(robot, pick_obj)

    def PutObject(self, robot: RobotRef, put_obj: Any, recp: Any) -> None:
        self._consume_planned_action("PutObject", put_obj, recp)
        runtime = self.runtime
        agent_id = runtime.physical_agent_id(robot)
        held_object = runtime.agent_held_object_matching(agent_id, put_obj)
        if held_object is None:
            held_objects = sorted(runtime.agent_held_objects_for(agent_id))
            held_description = ", ".join(held_objects) if held_objects else "nothing"
            raise RuntimeError(
                f"Cannot PutObject {put_obj!r} for agent {agent_id}: "
                "robot is not holding it. "
                f"Currently holding: {held_description}."
            )
        runtime.object_action(
            "PutObject",
            robot,
            recp,
            extra_object_resources=(held_object,),
        )

    def SwitchOn(self, robot: RobotRef, sw_obj: Any) -> None:
        self._consume_planned_action("SwitchOn", sw_obj)
        self.runtime.toggle_objects("ToggleObjectOn", robot, sw_obj)

    def SwitchOff(self, robot: RobotRef, sw_obj: Any) -> None:
        self._consume_planned_action("SwitchOff", sw_obj)
        self.runtime.toggle_objects("ToggleObjectOff", robot, sw_obj)

    def OpenObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("OpenObject", obj_name)
        self.runtime.object_action("OpenObject", robot, obj_name)

    def CloseObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("CloseObject", obj_name)
        self.runtime.object_action("CloseObject", robot, obj_name)

    def BreakObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("BreakObject", obj_name)
        self.runtime.object_action("BreakObject", robot, obj_name)

    def BreakEgg(self, robot: RobotRef, obj_name: Any) -> None:
        require_break_egg_target(obj_name)
        self._consume_planned_action("BreakEgg", obj_name)
        self.runtime.object_action("BreakObject", robot, obj_name)

    def PrepareEgg(self, robot: RobotRef, obj_name: Any, container_name: Any) -> None:
        require_break_egg_target(obj_name)
        self._consume_planned_action("PrepareEgg", obj_name, container_name)
        self.runtime.object_action("BreakObject", robot, obj_name)

    def SliceObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("SliceObject", obj_name)
        self.runtime.object_action("SliceObject", robot, obj_name)

    def CleanObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("CleanObject", obj_name)
        self.runtime.object_action("CleanObject", robot, obj_name)

    def DirtyObject(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("DirtyObject", obj_name)
        self.runtime.object_action("DirtyObject", robot, obj_name)

    def EmptyLiquid(self, robot: RobotRef, obj_name: Any) -> None:
        self._consume_planned_action("EmptyLiquid", obj_name)
        self.runtime.object_action("EmptyLiquidFromObject", robot, obj_name)

    def _current_object_by_id(self, agent_id: int, object_id: str) -> Dict[str, Any]:
        for obj in self.current_objects(agent_id):
            if obj.get("objectId") == object_id:
                return obj
        raise RuntimeError(f"Could not find AI2-THOR object with objectId {object_id!r}")

    @staticmethod
    def _object_has_liquid(obj: Dict[str, Any]) -> bool:
        if bool(obj.get("isFilledWithLiquid")):
            return True
        if bool(obj.get("isFilledWithWater")) or bool(obj.get("isFilledWithCoffee")):
            return True
        liquid = (
            obj.get("fillLiquid")
            or obj.get("filledLiquid")
            or obj.get("liquid")
            or obj.get("liquidType")
            or obj.get("filledWith")
            or obj.get("filled_with")
        )
        return bool(liquid)

    def _bound_helper_object(self, role):
        from .action_resources import active_resources
        runtime_obj = self.runtime
        admitted = active_resources(runtime_obj)
        if admitted is None:
            return None
        object_id = admitted.resolved.bindings.get(role)
        if object_id is None:
            admitted.invalid(f'unbound helper role {role}')
        return runtime_obj.find_object(object_id)

    def _find_sink_basin(self, robot: RobotRef, sink: Any) -> Dict[str, Any]:
        bound = self._bound_helper_object('@basin')
        if bound is not None:
            return bound
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        matches = runtime_obj.find_objects(sink, agent_id=agent_id)
        basin_matches = [obj for obj in matches if object_key(obj.get("objectType")) == "sinkbasin"]
        if basin_matches:
            return basin_matches[0]

        sink_basin = runtime_obj.find_objects("SinkBasin", agent_id=agent_id)
        if sink_basin:
            return sink_basin[0]

        raise RuntimeError(f"Could not find SinkBasin for sink {sink!r}.")

    @staticmethod
    def _fillwater_sinkbasin_putobject_has_no_positions(
        exc: BaseException,
        sink_basin: Dict[str, Any],
    ) -> bool:
        message = str(exc)
        normalized = message.casefold()
        sink_basin_values = (
            sink_basin.get("objectId"),
            sink_basin.get("objectType"),
            sink_basin.get("name"),
        )
        has_sink_basin_context = "sinkbasin" in normalized or any(
            "sinkbasin" in str(value).casefold()
            for value in sink_basin_values
            if value
        )
        return (
            "putobject" in normalized
            and has_sink_basin_context
            and "no valid positions to place object found" in normalized
        )

    @staticmethod
    def _discount_skipped_runtime_attempt(runtime_obj: Any) -> None:
        lock = getattr(runtime_obj, "stats_lock", None)
        if lock is None:
            runtime_obj.total_exec = max(0, int(getattr(runtime_obj, "total_exec", 0)) - 1)
            return
        with lock:
            runtime_obj.total_exec = max(0, int(getattr(runtime_obj, "total_exec", 0)) - 1)

    @staticmethod
    def _object_is_toggled_on(obj: Dict[str, Any]) -> bool:
        return (
            bool(obj.get("isToggled"))
            or bool(obj.get("isOn"))
        )

    @staticmethod
    def _object_parent_receptacles(obj: Dict[str, Any]) -> List[str]:
        parents = obj.get("parentReceptacles") or []
        return [str(parent) for parent in parents if parent]

    @staticmethod
    def _object_on_receptacle(obj: Dict[str, Any], receptacle_id: str) -> bool:
        return receptacle_id in ObjectInteractor._object_parent_receptacles(obj)

    @staticmethod
    def _stove_parent_burner_id(obj: Dict[str, Any]) -> Optional[str]:
        for parent in ObjectInteractor._object_parent_receptacles(obj):
            if object_key(parent.split("|", 1)[0]) == "stoveburner":
                return parent
        return None

    def _resolve_stove_burner(
        self,
        robot: RobotRef,
        stove_burner: Any,
        supporting_obj: Optional[Any] = None,
    ) -> Dict[str, Any]:
        bound = self._bound_helper_object('@burner')
        if bound is not None:
            return bound
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        if object_key(stove_burner) == "stoveburner" and supporting_obj is not None:
            try:
                current_obj = runtime_obj.find_object(supporting_obj, agent_id=agent_id)
            except RuntimeError:
                current_obj = None
            if current_obj is not None:
                burner_id = self._stove_parent_burner_id(current_obj)
                if burner_id:
                    return self._current_object_by_id(agent_id, burner_id)
        return runtime_obj.find_object(stove_burner, agent_id=agent_id)

    def _resolve_stove_knob_for_burner(
        self,
        robot: RobotRef,
        burner: Dict[str, Any],
    ) -> Dict[str, Any]:
        bound = self._bound_helper_object('@knob')
        if bound is not None:
            return bound
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        knobs = runtime_obj.find_objects("StoveKnob", agent_id=agent_id)
        if not knobs:
            raise RuntimeError("Could not find a StoveKnob for the target burner.")

        burner_id = str(burner.get("objectId") or "")
        for knob in knobs:
            controlled_objects = [str(obj_id) for obj_id in knob.get("controlledObjects") or []]
            if burner_id and burner_id in controlled_objects:
                return knob

        burner_center = object_center(burner)
        if burner_center is None:
            return knobs[0]

        def knob_distance_sq(knob: Dict[str, Any]) -> float:
            knob_center = object_center(knob)
            if knob_center is None:
                return float("inf")
            dx = float(knob_center["x"]) - float(burner_center["x"])
            dz = float(knob_center["z"]) - float(burner_center["z"])
            return dx * dx + dz * dz

        return min(knobs, key=knob_distance_sq)

    def _set_stove_knob_state(
        self,
        robot: RobotRef,
        knob: Dict[str, Any],
        desired_on: bool,
    ) -> bool:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        current_knob = runtime_obj.find_object(str(knob["objectId"]), agent_id=agent_id)
        if self._object_is_toggled_on(current_knob) == desired_on:
            return False
        if desired_on:
            self.SwitchOn(robot, current_knob["objectId"])
        else:
            self.SwitchOff(robot, current_knob["objectId"])
        return True

    def _ensure_object_on_receptacle(
        self,
        robot: RobotRef,
        obj_name: Any,
        receptacle_id: str,
    ) -> bool:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        obj = runtime_obj.find_object(obj_name, agent_id=agent_id)
        if self._object_on_receptacle(obj, receptacle_id):
            return False
        self.PutObject(robot, obj_name, receptacle_id)
        return True

    def _wait_for_object_cooked(self, robot: RobotRef, obj_name: Any, action_name: str) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(obj_name, agent_id=agent_id)
        target_id = str(target.get("objectId"))

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            if state_satisfied(current, "COOKED"):
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(f"{action_name} timed out waiting for {target_id} to become Cooked.")

    def _wait_for_object_hot(self, robot: RobotRef, obj_name: Any, action_name: str) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(obj_name, agent_id=agent_id)
        target_id = str(target.get("objectId"))

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            if state_satisfied(current, "HOT"):
                record_groundtruth_state(
                    obj_name,
                    current,
                    "HOT",
                    getattr(runtime_obj, "evaluation_context", None),
                    runtime=runtime_obj,
                )
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(f"{action_name} timed out waiting for {target_id} to become Hot.")

    def _wait_for_object_cold(self, robot: RobotRef, obj_name: Any, action_name: str) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(obj_name, agent_id=agent_id)
        target_id = str(target.get("objectId"))
        current_temperature = target.get("temperature", "<unknown>")

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            current_temperature = current.get("temperature", "<unknown>")
            if state_satisfied(current, "COLD"):
                record_groundtruth_state(
                    obj_name,
                    current,
                    "COLD",
                    getattr(runtime_obj, "evaluation_context", None),
                    runtime=runtime_obj,
                )
                return
            if pass_step > 5:  # Some floor plans cannot cool objects in the fridge.
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(
            f"{action_name} timed out waiting for {target_id} to become Cold. "
            f"{obj_name} temperature: {current_temperature}."
        )

    def _wait_for_object_on(self, robot: RobotRef, obj_name: Any, action_name: str) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(obj_name, agent_id=agent_id)
        target_id = str(target.get("objectId"))

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            if state_satisfied(current, "ON"):
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(f"{action_name} timed out waiting for {target_id} to switch on.")

    def _wait_for_microwave_result(self, robot: RobotRef, item: Any) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(item, agent_id=agent_id)
        target_id = str(target.get("objectId"))
        is_cookable = bool(target.get("cookable"))
        is_hot = False
        is_cooked = False

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            is_hot = state_satisfied(current, "HOT")
            is_cooked = state_satisfied(current, "COOKED")
            if is_hot and (not is_cookable or is_cooked):
                record_groundtruth_state(
                    item,
                    current,
                    "HOT",
                    getattr(runtime_obj, "evaluation_context", None),
                    runtime=runtime_obj,
                )
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        required_state = "Hot and Cooked" if is_cookable else "Hot"
        raise RuntimeError(
            f"RunMicrowave timed out waiting for {target_id} to become {required_state}. "
            f"Last observed: hot={is_hot}, cooked={is_cooked}."
        )

    def _wait_for_coffee_machine_result(self, robot: RobotRef, mug: Any) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(mug, agent_id=agent_id)
        target_id = str(target.get("objectId"))

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            if object_filled_with_coffee(current):
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(
            f"RunCoffeeMachine timed out waiting for {target_id} to be filled with Coffee."
        )

    def _wait_for_object_filled_with_water(self, robot: RobotRef, obj_name: Any) -> None:
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        target = runtime_obj.find_object(obj_name, agent_id=agent_id)
        target_id = str(target.get("objectId"))

        for pass_step in range(self.max_pass_steps + 1):
            current = self._current_object_by_id(agent_id, target_id)
            if object_filled_with_water(current):
                return
            if pass_step == self.max_pass_steps:
                break
            runtime_obj.step({"action": "Pass", "agentId": agent_id}, check_success=False)

        raise RuntimeError(f"FillWater timed out waiting for {target_id} to be filled with Water.")

    def _wait_for_toaster_result(self, robot: RobotRef, bread: Any) -> None:
        self._wait_for_object_cooked(robot, bread, "RunToaster")

    def RunMicrowave(self, robot: RobotRef, microwave: Any, item: Any) -> None:
        self._consume_planned_action("RunMicrowave", microwave, item)
        self.SwitchOn(robot, microwave)
        try:
            self._wait_for_microwave_result(robot, item)
        finally:
            self.SwitchOff(robot, microwave)

    def RunCoffeeMachine(self, robot: RobotRef, coffee_machine: Any, mug: Any) -> None:
        self._consume_planned_action("RunCoffeeMachine", coffee_machine, mug)
        self.SwitchOn(robot, coffee_machine)
        try:
            self._wait_for_coffee_machine_result(robot, mug)
        finally:
            self.SwitchOff(robot, coffee_machine)

    def RunToaster(self, robot: RobotRef, toaster: Any, bread: Any) -> None:
        self._consume_planned_action("RunToaster", toaster, bread)
        self.PutObject(robot, bread, toaster)
        self.SwitchOn(robot, toaster)
        try:
            self._wait_for_toaster_result(robot, bread)
        finally:
            self.SwitchOff(robot, toaster)
        self.PickupObject(robot, bread)

    def CookByStoveBurner(
        self,
        robot: RobotRef,
        stove_burner: Any,
        container: Any,
        food: Any,
    ) -> None:
        self._consume_planned_action("CookByStoveBurner", stove_burner, container, food)
        burner = self._resolve_stove_burner(robot, stove_burner, supporting_obj=container)
        self.PutObject(robot, container, str(burner["objectId"]))
        knob = self._resolve_stove_knob_for_burner(robot, burner)
        turned_on = self._set_stove_knob_state(robot, knob, True)
        cooked = False
        try:
            self._wait_for_object_cooked(robot, food, "CookByStoveBurner")
            cooked = True
        finally:
            if turned_on:
                self._set_stove_knob_state(robot, knob, False)
        if cooked:
            self.PickupObject(robot, container)

    def HeatByStoveBurner(self, robot: RobotRef, stove_burner: Any, obj: Any) -> None:
        self._consume_planned_action("HeatByStoveBurner", stove_burner, obj)
        burner = self._resolve_stove_burner(robot, stove_burner, supporting_obj=obj)
        knob = self._resolve_stove_knob_for_burner(robot, burner)
        turned_on = self._set_stove_knob_state(robot, knob, True)
        heated = False
        try:
            self._ensure_object_on_receptacle(robot, obj, str(burner["objectId"]))
            self._wait_for_object_hot(robot, obj, "HeatByStoveBurner")
            heated = True
        finally:
            if turned_on:
                self._set_stove_knob_state(robot, knob, False)
        if heated:
            self.PickupObject(robot, obj)

    def FireByStoveBurner(self, robot: RobotRef, stove_burner: Any, candle: Any) -> None:
        self._consume_planned_action("FireByStoveBurner", stove_burner, candle)
        burner = self._resolve_stove_burner(robot, stove_burner, supporting_obj=candle)
        knob = self._resolve_stove_knob_for_burner(robot, burner)
        turned_on = self._set_stove_knob_state(robot, knob, True)
        try:
            self._ensure_object_on_receptacle(robot, candle, str(burner["objectId"]))
            self._wait_for_object_on(robot, candle, "FireByStoveBurner")
            self.PickupObject(robot, candle)
        finally:
            if turned_on:
                self._set_stove_knob_state(robot, knob, False)

    def FillWater(self, robot: RobotRef, sink: Any, obj: Any) -> None:
        self._consume_planned_action("FillWater", sink, obj)
        filled = False
        runtime_obj = self.runtime
        agent_id = runtime_obj.physical_agent_id(robot)
        if self._object_has_liquid(runtime_obj.find_object(obj, agent_id=agent_id)):
            runtime_obj.object_action("EmptyLiquidFromObject", robot, obj)
        sink_basin = self._find_sink_basin(robot, sink)
        try:
            self.PutObject(robot, obj, sink_basin["objectId"])
        except RuntimeError as exc:
            if not self._fillwater_sinkbasin_putobject_has_no_positions(exc, sink_basin):
                raise
            self._discount_skipped_runtime_attempt(runtime_obj)
            self.helper_log(
                "Skipping FillWater SinkBasin PutObject because no valid "
                f"placement position was found: {exc}"
            )
        self.SwitchOn(robot, "Faucet")
        try:
            if not object_filled_with_water(runtime_obj.find_object(obj, agent_id=agent_id)):
                runtime_obj.object_action(
                    "FillObjectWithLiquid",
                    robot,
                    obj,
                    action_parameters={"fillLiquid": "water"},
                )
            filled = True
        finally:
            self.SwitchOff(robot, "Faucet")
        if filled:
            self.PickupObject(robot, obj)

    def ColdObject(self, robot: RobotRef, fridge: Any, obj: Any) -> None:
        self._consume_planned_action("ColdObject", fridge, obj)
        self._wait_for_object_cold(robot, obj, "ColdObject")

    def ThrowObject(self, robot: RobotRef) -> None:
        self._consume_planned_action("ThrowObject")
        self.runtime.throw_object(robot)

    def _consume_planned_action(self, name, *args):
        if self.planned_action_consumer is not None:
            return self.planned_action_consumer(name, *args)
        return None
