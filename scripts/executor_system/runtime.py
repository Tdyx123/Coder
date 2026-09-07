"""AI2-THOR runtime wrapper, navigation, object operations, and media output."""

import math
import os
import random
import re
# Compatibility patch points shared with runtime_artifacts.
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from functools import wraps
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .plan_types import (PlannedAction)
from .controller_client import ControllerClient
from .runtime_artifacts import RuntimeArtifacts
from .object_resolver import ObjectResolver
from .object_interactor import (
    ObjectInteractor, is_pickup_object_clip_error,
    is_object_action_target_visibility_error, is_pickup_object_target_visibility_error,
)
from .config import (
    AGENT_CLEARANCE_DISTANCE,
    DIRECTIONAL_VIEW_NAMES,
    GENERATE_METADATA,
    LOCAL_TOP_VIEW_EXTENT_SCALE,
    LOCAL_TOP_VIEW_MIN_HEIGHT,
    NAVIGATION_CHUNK_STEPS,
    NAVIGATION_GRID_SIZE,
    PLACEMENT_RESTRICTIONS,
    parse_env_bool,
    TELEPORT_CANDIDATE_LIMIT,
    THIRD_PARTY_VIEW_NAMES,
    TOP_VIEW_CAMERA_FOV,
    TOP_VIEW_NAME,
    TOP_VIEW_HEIGHT_OFFSET,
    SceneObjectFootprint,
)
from .evaluation import EvaluationContext, GoalSpec
from .execution_control import (
    ensure_control,
    ExecutionShutdownTimeout,
    raise_if_execution_aborted,
    close_runtime,
)
from .dependencies import CloudRendering, Controller, cv2, require_dependencies
from .goals import (
    format_goal,
    record_verified_goal_state,
)
from .movement import (
    ActionWave,
    MovementConfig,
    NavigationMetrics,
    NavigationDeferred,
    NavigationRequest,
    NoInteractionPoseError,
    create_movement_strategy,
)
from .utils import (
    RobotRef,
    distance_pts,
    distance_to_aabb_footprint,
    event_cv2_frame,
    event_error_message,
    is_broken_egg_object,
    is_egg_query,
    is_sliced_food_object_for_base,
    matches_object,
    object_aabb_bounds,
    object_center,
    object_distance,
    object_footprint_clearance,
    object_key,
    object_mass,
    operated_object_name,
    operated_object_name_candidate_keys,
    operated_sliced_food_query_rank,
    position_inside_aabb_footprint,
    position_to_grid_key,
    position_to_tuple,
    robot_agent_id,
    robot_name,
    shortest_yaw_delta,
    sliceable_food_query_key,
    stable_object_name,
    step_event_failed,
    teleport_collision_object_id,
    yaw_to_face,
    log,
)

from .runtime_metrics import RuntimeMetrics


LOOK_ACTIONS = {"LookUp", "LookDown"}
MOVE_BLOCKER_PATTERN = re.compile(
    r"^(?P<object_name>.+?) is blocking Agent \d+ from moving by \("
)
LOOK_DEGREES_INCREMENT = 0.1
PUT_OBJECT_FORCE_ACTION = True
OPEN_OBJECT_FORCE_ACTION = True
CLOSE_OBJECT_FORCE_ACTION = True
DIRTY_OBJECT_FORCE_ACTION = True
TOGGLE_OBJECT_ON_FORCE_ACTION = True
TOGGLE_OBJECT_OFF_FORCE_ACTION = True
EMPTY_LIQUID_FORCE_ACTION = True
PICKUP_OBJECT_CLIP_ERROR = (
    "Picking up object would cause it to collide and clip into something!"
)
PICKUP_OBJECT_CLIP_BACKOFF_DISTANCES = (0.25, 0.5, 0.75)
PICKUP_OBJECT_TARGET_VISIBILITY_ERROR = (
    "Target object not found within the specified visibility"
)
PICKUP_OBJECT_TARGET_VISIBILITY_LOOK_OFFSETS = (
    -10.0,
    -20.0,
    -30.0,
    10.0,
    20.0,
    30.0,
)
OBJECT_ACTION_TARGET_VISIBILITY_RETRY_ACTIONS = {
    "PickupObject",
    "BreakObject",
    "SliceObject",
}

INTERACTION_TARGET_ARGUMENT_INDEX = {
    "PickupObject": 0,
    "BreakObject": 0,
    "BreakEgg": 0,
    "SliceObject": 0,
    "OpenObject": 0,
    "CloseObject": 0,
    "SwitchOn": 0,
    "SwitchOff": 0,
    "ToggleObjectOn": 0,
    "ToggleObjectOff": 0,
    "CleanObject": 0,
    "DirtyObject": 0,
    "EmptyLiquid": 0,
    "EmptyLiquidFromObject": 0,
    "ColdObject": 0,
    "PrepareEgg": 0,
    "RunMicrowave": 0,
    "RunCoffeeMachine": 0,
    "RunToaster": 0,
    "CookByStoveBurner": 0,
    "HeatByStoveBurner": 0,
    "FireByStoveBurner": 0,
    "FillWater": 0,
    "PutObject": 1,
}


def navigation_operation(method):
    """Serialize complete relocation/recovery sequences, never wave collection."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        scope = (self.navigation_execution_scope()
                 if hasattr(self, '_navigation_execution_lock') else nullcontext())
        with scope:
            return method(self, *args, **kwargs)
    return guarded


def navigation_interaction_target(
    dest_obj: Any,
    next_action: Optional[PlannedAction],
) -> tuple[Any, bool]:
    if next_action is None or next_action.name not in INTERACTION_TARGET_ARGUMENT_INDEX:
        return dest_obj, False
    argument_index = INTERACTION_TARGET_ARGUMENT_INDEX[next_action.name]
    if (
        len(next_action.args) <= argument_index
        or next_action.args[argument_index] in (None, "")
    ):
        raise RuntimeError(
            f"{next_action.name} is missing interaction target argument "
            f"{argument_index}."
        )
    target = next_action.args[argument_index]
    return target, object_key(target) != object_key(dest_obj)


def _normalize_look_degrees(degrees: Any) -> float:
    value = float(degrees)
    if value == 0.0:
        return 0.0
    sign = -1.0 if value < 0 else 1.0
    magnitude = abs(value)
    steps = math.floor(magnitude / LOOK_DEGREES_INCREMENT + 0.5)
    rounded = steps * LOOK_DEGREES_INCREMENT
    if rounded == 0.0:
        rounded = LOOK_DEGREES_INCREMENT
    return sign * round(rounded, 1)


def _normalize_look_payload(payload: Dict[str, Any]) -> None:
    if payload.get("action") not in LOOK_ACTIONS or "degrees" not in payload:
        return
    payload["degrees"] = _normalize_look_degrees(payload["degrees"])


class ThorRuntime:
    @staticmethod
    def payload_allows_step_retry(payload: Dict[str, Any]) -> bool:
        return payload.get("action") == "Teleport"

    def __init__(
        self,
        robot_defs: Sequence[RobotRef],
        floor: str,
        cloud_rendering: bool,
        render_image: bool,
        movement_mode: Optional[str] = None,
        *,
        output_root: Optional[Path] = None,
        reachable_refresh_mode: str = "full",
    ) -> None:
        # Validate the cleanup target before dependency/controller setup so an
        # invalid caller path can never reach any initialization cleanup.
        if reachable_refresh_mode != 'full':
            raise ValueError('event reachable refresh is not implemented yet; use full')
        self.reachable_refresh_mode = reachable_refresh_mode
        self.runtime_metrics = RuntimeMetrics()
        self.seed = 0
        self.output_root = self.resolve_output_root(output_root)
        require_dependencies()
        self.robots = list(robot_defs)
        self.no_robot = len(self.robots)
        if self.no_robot < 1:
            raise RuntimeError("At least one robot must be configured.")
        self.floor = floor
        self.cloud_rendering = cloud_rendering
        self.render_image = render_image
        self.physical_agent_count = self.resolve_physical_agent_count()
        self.agent_mode = self.resolve_agent_mode()
        self.controller_headless = not self.render_image
        self.show_windows = self.resolve_show_windows()
        self.top_view_enabled = self.render_image
        self.third_party_view_names: List[str] = []
        self.robot_agent_map = self.build_robot_agent_map()
        self.frame_counter = 0
        self.total_exec = 0
        self.success_exec = 0
        self.evaluation_context: Optional[EvaluationContext] = None
        self.missing_frame_warning_emitted = False
        self.controller = None
        self._stopped = False
        self.controller_lock = threading.RLock()
        self.controller_client = ControllerClient(self)
        self.object_resolver = ObjectResolver(self)
        self.object_interactor = ObjectInteractor(self)
        self.artifacts = self._make_artifacts()
        self.state_version = 0
        self._committed_held_object_overrides = {}
        self.stats_lock = threading.Lock()
        self.operated_object_names: Set[str] = set()
        self.operated_object_names_lock = threading.Lock()
        self.agent_held_object_overrides: Dict[int, Set[str]] = {}
        self.agent_held_object_overrides_lock = threading.Lock()
        self.object_alias_bindings: Dict[str, Dict[str, Any]] = {}
        self.object_alias_key_to_token: Dict[str, str] = {}
        self.object_alias_by_object_id: Dict[str, Set[str]] = {}
        self.object_alias_warnings: Set[str] = set()
        self.object_alias_lock = threading.Lock()
        self.reachable_positions: List[Dict[str, float]] = []
        self.global_reachable_positions: List[Dict[str, float]] = []
        self.configure_movement(movement_mode)

        if self.show_windows:
            log("OpenCV camera preview windows enabled.")
        log("Preparing output directories.")
        self.prepare_output_dirs()
        try:
            log("Creating AI2-THOR controller.")
            self.controller = self.create_controller()
            with self.controller_lock:
                event = self.controller.last_event
            self.print_agent_metadata(event)
            # self.validate_physical_agent_count()
            log("Initializing AI2-THOR scene.")
            self.initialize_scene()
        except BaseException as exc:
            self.cleanup_errors = []
            close_runtime(self, self.cleanup_errors)
            # Constructor assignment never returns this runtime to the caller.
            # Carry cleanup evidence on the original exception without wrapping
            # or replacing it, so entrypoint error handling can persist both.
            exc.execution_cleanup_errors = (
                list(getattr(exc, 'execution_cleanup_errors', [])) + self.cleanup_errors
            )
            raise

    @staticmethod
    def resolve_output_root(output_root: Optional[Path]) -> Path:
        return RuntimeArtifacts.resolve_output_root(output_root)

    def resolve_physical_agent_count(self) -> int:
        expected_count = self.no_robot
        configured = os.environ.get("LAMMAP_PHYSICAL_AGENT_COUNT")
        if configured:
            try:
                count = int(configured)
            except ValueError as exc:
                raise RuntimeError(
                    "LAMMAP_PHYSICAL_AGENT_COUNT must be a positive integer."
                ) from exc
            if count < 1:
                raise RuntimeError("LAMMAP_PHYSICAL_AGENT_COUNT must be at least 1.")
            if count != expected_count:
                raise RuntimeError(
                    "LAMMAP_PHYSICAL_AGENT_COUNT must match the number of logical robots "
                    f"({expected_count})."
                )
            return count

        return expected_count

    def resolve_agent_mode(self) -> str:
        configured = os.environ.get("LAMMAP_AGENT_MODE")
        if configured:
            return configured
        return "default"

    def resolve_show_windows(self) -> bool:
        if not self.render_image:
            return False
        configured = os.environ.get("LAMMAP_SHOW_WINDOWS")
        if configured is not None:
            return parse_env_bool("LAMMAP_SHOW_WINDOWS", configured)
        return not self.cloud_rendering

    def build_robot_agent_map(self) -> Dict[str, int]:
        mapping = {}
        for robot in self.robots:
            nominal_agent_id = robot_agent_id(robot)
            mapping[robot_name(robot)] = nominal_agent_id % self.physical_agent_count
        return mapping

    def actual_agent_count(self) -> int:
        if self.controller is None:
            return 0
        with self.controller_lock:
            last_event = getattr(self.controller, "last_event", None)
        events = getattr(last_event, "events", None)
        if events:
            return len(events)
        metadata = getattr(last_event, "metadata", {}) or {}
        agents = metadata.get("agents")
        if agents:
            return len(agents)
        return 1 if last_event is not None else 0

    def validate_physical_agent_count(self) -> None:
        actual_count = self.actual_agent_count()
        if actual_count == self.physical_agent_count:
            return
        raise RuntimeError(
            "AI2-THOR created "
            f"{actual_count} physical agent(s), expected {self.physical_agent_count}. "
            f"agentMode={self.agent_mode!r}. Use LAMMAP_AGENT_MODE to choose an "
            "AI2-THOR agent mode that supports the configured robot count."
        )

    def create_controller(self):
        self.ensure_display()
        controller_args = {
            "height": 1000,
            "width": 1000,
            "scene": f"FloorPlan{self.floor}",
            "agentMode": self.agent_mode,
            "snapToGrid": False,
            "gridSize": 0.25,
            "rotateStepDegrees": 20,
            "visibilityDistance": 5,
            "fieldOfView": 90,
            "agentCount": self.physical_agent_count,
            "headless": self.controller_headless,
        }
        if self.cloud_rendering:
            controller_args["platform"] = CloudRendering
        self.effective_controller_config = {
            key: ('CloudRendering' if key == 'platform' else value)
            for key, value in controller_args.items()
        }
        return Controller(**controller_args)

    def print_agent_metadata(self, event) -> None:
        events = getattr(event, "events", None) or [event]
        for i, e in enumerate(events):
            print("agent index:", i)
            print("agentId:", e.metadata.get("agentId"))
            print("position:", e.metadata["agent"]["position"])

    def write_final_metadata(self) -> Optional[Path]:
        return self._get_artifacts().write_final_metadata()

    def ensure_display(self) -> None:
        if self.cloud_rendering or not self.render_image or os.environ.get("DISPLAY"):
            return
        raise RuntimeError(
            "DISPLAY is required when CloudRendering=0 and renderImage=1. "
            "Automatic Xvfb fallback has been disabled."
        )

    def _make_artifacts(self) -> RuntimeArtifacts:
        # Resolve providers at use time to preserve legacy runtime module patches.
        return RuntimeArtifacts(
            self, cv2_provider=lambda: cv2,
            frame_converter=lambda event: event_cv2_frame(event),
            metadata_enabled=lambda: GENERATE_METADATA,
            logger=lambda message: log(message),
        )

    def _get_artifacts(self) -> RuntimeArtifacts:
        artifacts = getattr(self, "artifacts", None)
        if artifacts is None:
            artifacts = self.artifacts = self._make_artifacts()
        return artifacts

    def prepare_output_dirs(self) -> None:
        return self._get_artifacts().prepare()

    def initialize_scene(self) -> None:
        log(
            f"FloorPlan{self.floor} initialized with "
            f"{self.physical_agent_count} physical agent(s) for {self.no_robot} logical robot(s)."
        )
        if self.physical_agent_count != self.no_robot:
            logical_map = ", ".join(
                f"{robot_name(robot)}->agent{self.physical_agent_id(robot)}"
                for robot in self.robots
            )
            log(f"Logical robot mapping: {logical_map}.")
        if self.top_view_enabled and self.cloud_rendering:
            log("Adding top-view camera.")
            event = self.step({"action": "GetMapViewCameraProperties"}, save_frame=False)
            camera_props = event.metadata.get("actionReturn")
            if camera_props:
                self.add_third_party_view(
                    TOP_VIEW_NAME,
                    self.top_view_camera_props_with_height_offset(camera_props),
                )
        elif not self.top_view_enabled:
            log("Skipping top-view camera because renderImage=0.")

        log("Loading reachable positions.")
        event = self.step({"action": "GetReachablePositions"}, save_frame=False)
        self.reachable_positions = [
            dict(position) for position in (event.metadata.get("actionReturn") or [])
        ]
        if not self.reachable_positions:
            raise RuntimeError("AI2-THOR returned no reachable positions.")
        self.global_reachable_positions = [
            dict(position) for position in self.reachable_positions
        ]

        if self.top_view_enabled:
            if not self.cloud_rendering:
                log("Adding local top-view camera.")
                self.add_third_party_view(TOP_VIEW_NAME, self.local_top_view_camera_props())

        self.random = random.Random(0)
        initial_positions = []
        for agent_id in range(self.physical_agent_count):
            log(f"Placing agent {agent_id}.")
            init_pos = self.initial_agent_position(agent_id, initial_positions)
            initial_positions.append(init_pos)
            self.step({"action": "Teleport", "position": init_pos, "agentId": agent_id})

        for agent_id in range(self.physical_agent_count):
            log(f"Setting initial look angle for agent {agent_id}.")
            self.set_initial_look_angle(agent_id)

    def add_third_party_view(self, view_name: str, camera_props: Dict[str, Any]) -> None:
        payload = {
            "action": "AddThirdPartyCamera",
            **camera_props,
            "renderImage": False,
        }
        self.step(
            payload,
            save_frame=False,
            retry_on_failure=False,
            max_retries=0,
        )
        self.third_party_view_names.append(view_name)

    def top_view_camera_props_with_height_offset(
        self,
        camera_props: Dict[str, Any],
    ) -> Dict[str, Any]:
        adjusted_props = dict(camera_props)
        position = dict(adjusted_props.get("position") or {})
        if "y" in position:
            position["y"] = float(position["y"]) + TOP_VIEW_HEIGHT_OFFSET
            adjusted_props["position"] = position
        return adjusted_props

    def local_top_view_camera_props(self) -> Dict[str, Any]:
        min_x = min(float(position["x"]) for position in self.reachable_positions)
        max_x = max(float(position["x"]) for position in self.reachable_positions)
        min_z = min(float(position["z"]) for position in self.reachable_positions)
        max_z = max(float(position["z"]) for position in self.reachable_positions)
        floor_y = min(float(position.get("y", 0.0)) for position in self.reachable_positions)
        center_x = (min_x + max_x) / 2.0
        center_z = (min_z + max_z) / 2.0
        scene_extent = max(max_x - min_x, max_z - min_z)
        camera_y = floor_y + max(
            LOCAL_TOP_VIEW_MIN_HEIGHT,
            scene_extent * LOCAL_TOP_VIEW_EXTENT_SCALE,
        ) + TOP_VIEW_HEIGHT_OFFSET
        return {
            "fieldOfView": TOP_VIEW_CAMERA_FOV,
            "position": {"x": center_x, "y": camera_y, "z": center_z},
            "rotation": {"x": 90.0, "y": 0.0, "z": 0.0},
        }

    def initial_agent_position(
        self,
        agent_id: int,
        existing_positions: Sequence[Dict[str, float]],
    ) -> Dict[str, float]:
        if not existing_positions:
            return self.reachable_positions[(agent_id * 7) % len(self.reachable_positions)]

        ordered = (
            self.reachable_positions[(agent_id * 7) % len(self.reachable_positions) :]
            + self.reachable_positions[: (agent_id * 7) % len(self.reachable_positions)]
        )
        for minimum_distance in (0.75, 0.5, 0.35):
            for position in ordered:
                pos_tuple = position_to_tuple(position)
                if all(
                    distance_pts(pos_tuple, position_to_tuple(other)) > minimum_distance
                    for other in existing_positions
                ):
                    return position
        return ordered[0]

    def set_initial_look_angle(self, agent_id: int) -> None:
        payload = {"action": "LookDown", "degrees": 35, "agentId": agent_id}
        event = self.step(payload, check_success=False)
        metadata = getattr(event, "metadata", {}) or {}
        if metadata.get("lastActionSuccess", True):
            return
        error = metadata.get("errorMessage") or ""
        if "degrees == 0" in error:
            log(f"Skipping initial look angle for agent {agent_id}: {error}")
            return
        self.assert_success(event, payload)

    def step(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool = True,
        save_frame: bool = True,
        retry_on_failure: Optional[bool] = None,
        max_retries: int = 3,
    ):
        from .action_resources import active_resources
        admitted = active_resources(self)
        if admitted is not None:
            admitted.before_step(payload, mark_effects=False)
        copied_payload = dict(payload)
        copied_payload.pop("objectResources", None)
        _normalize_look_payload(copied_payload)
        requested_retry = check_success if retry_on_failure is None else retry_on_failure
        should_retry = bool(
            requested_retry and self.payload_allows_step_retry(copied_payload)
        )
        moves_agent = str(copied_payload.get('action', '')).startswith(('Move', 'Rotate', 'Look', 'Teleport'))
        scope = (self.navigation_execution_scope()
                 if moves_agent and hasattr(self, '_navigation_execution_lock') else nullcontext())
        with scope:
            return self._step_with_retries(
                copied_payload,
                check_success=check_success,
                save_frame=save_frame,
                retry_on_failure=should_retry,
                max_retries=max_retries,
            )

    def _step_with_retries(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool,
        save_frame: bool,
        retry_on_failure: bool,
        max_retries: int,
    ):
        retry_on_failure = bool(
            retry_on_failure and self.payload_allows_step_retry(payload)
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                event = self._step_direct(
                    payload,
                    check_success=False,
                    save_frame=save_frame,
                )
            except BaseException as exc:
                raise_if_execution_aborted(self, exc)
                if retry_on_failure and attempts <= max_retries:
                    self.log_retry(payload, attempts, max_retries)
                    continue
                raise

            if step_event_failed(event) and retry_on_failure and attempts <= max_retries:
                self.log_retry(payload, attempts, max_retries)
                continue
            if check_success:
                self.assert_success(event, payload)
            return event

    def set_step_executor(self, executor: Optional[Any]) -> None:
        """Deprecated compatibility hook.

        Runtime no longer delegates execution through a central executor.  The
        controller lock in _step_direct is the only serialization boundary.
        """

        self._deprecated_step_executor = executor

    def log_retry(self, payload: Dict[str, Any], attempts: int, max_retries: int) -> None:
        action = payload.get("action", "<unknown>")
        agent_id = payload.get("agentId", "n/a")
        log(
            f"Retrying {action} for agent {agent_id} "
            f"({attempts}/{max_retries}); retrying serially."
        )

    def _get_controller_client(self) -> ControllerClient:
        # Legacy callers and test fixtures may build a runtime via __new__.
        client = getattr(self, "controller_client", None)
        if client is None:
            client = self.controller_client = ControllerClient(self)
        return client

    def _step_direct(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool = True,
        save_frame: bool = True,
    ):
        return self._get_controller_client().step(
            payload, check_success=check_success, save_frame=save_frame,
        )

    def _commit_transformation_identities(self, event, payload, before, held_before=()):
        """Publish source/descendant identity under the same lock as the event.

        The pre-step object set is captured under controller_lock. Therefore all
        newly created matching descendants belong to this one serialized step,
        even when other objects of the same type transform concurrently.
        """
        # Published descendants remain the same physical lineage even when
        # the step reports failure after producing an observable side effect.
        if before is None:
            return
        source_id = str(payload.get('objectId') or '')
        source = before.get(source_id, {})
        action = payload.get('action')
        from .action_resources import manager_for
        manager = manager_for(self)
        after = {str(obj.get('objectId')): obj for obj in self._event_objects(event)
                 if obj.get('objectId')}
        if action in ('PickupObject', 'PutObject', 'ThrowObject', 'DropHandObject'):
            sources = (source_id,) if action == 'PickupObject' else held_before
            for old_id in sources:
                if old_id in after or old_id not in before:
                    continue
                old_type = before[old_id].get('objectType') or old_id.split('|', 1)[0]
                candidates = [new_id for new_id, obj in after.items()
                              if new_id not in before
                              and object_key(obj.get('objectType') or new_id.split('|', 1)[0]) == object_key(old_type)]
                if action == 'PutObject':
                    on_target = [new_id for new_id in candidates
                                 if source_id in (after[new_id].get('parentReceptacles') or ())]
                    if on_target:
                        candidates = on_target
                if len(candidates) == 1:
                    manager.bind_identity(old_id, candidates[0])
            return
        if action == 'SliceObject':
            matches = lambda obj: is_sliced_food_object_for_base(source_id, obj)
        elif action == 'BreakObject' and is_egg_query(source.get('objectType') or source_id):
            matches = is_broken_egg_object
        else:
            return
        for obj in after.values():
            object_id = str(obj.get('objectId') or '')
            if object_id and object_id not in before and matches(obj):
                manager.bind_identity(source_id, object_id)

    def _commit_world_event(self, event, payload: Dict[str, Any]) -> None:
        return self._get_controller_client().commit_world_event(event, payload)

    def assert_success(self, event, payload: Dict[str, Any]) -> None:
        metadata = getattr(event, "metadata", {}) or {}
        if metadata.get("lastActionSuccess", True):
            return
        action = payload.get("action", "<unknown>")
        agent_id = payload.get("agentId", "n/a")
        error = metadata.get("errorMessage") or "no error message returned"
        if action == "PutObject":
            self.log_put_object_failure_held_items(agent_id)
        raise RuntimeError(f"{action} failed for agent {agent_id}: {error}")

    def _get_object_interactor(self) -> ObjectInteractor:
        interactor = getattr(self, 'object_interactor', None)
        if interactor is None:
            interactor = self.object_interactor = ObjectInteractor(self)
        return interactor

    @staticmethod
    def _object_interaction_settings():
        # Preserve the established runtime module configuration/patch points.
        return {
            'MOVE_BLOCKER_PATTERN': MOVE_BLOCKER_PATTERN,
            'PUT_OBJECT_FORCE_ACTION': PUT_OBJECT_FORCE_ACTION,
            'OPEN_OBJECT_FORCE_ACTION': OPEN_OBJECT_FORCE_ACTION,
            'CLOSE_OBJECT_FORCE_ACTION': CLOSE_OBJECT_FORCE_ACTION,
            'DIRTY_OBJECT_FORCE_ACTION': DIRTY_OBJECT_FORCE_ACTION,
            'TOGGLE_OBJECT_ON_FORCE_ACTION': TOGGLE_OBJECT_ON_FORCE_ACTION,
            'TOGGLE_OBJECT_OFF_FORCE_ACTION': TOGGLE_OBJECT_OFF_FORCE_ACTION,
            'EMPTY_LIQUID_FORCE_ACTION': EMPTY_LIQUID_FORCE_ACTION,
            'PICKUP_OBJECT_CLIP_BACKOFF_DISTANCES': PICKUP_OBJECT_CLIP_BACKOFF_DISTANCES,
            'PICKUP_OBJECT_TARGET_VISIBILITY_LOOK_OFFSETS': PICKUP_OBJECT_TARGET_VISIBILITY_LOOK_OFFSETS,
            'OBJECT_ACTION_TARGET_VISIBILITY_RETRY_ACTIONS': OBJECT_ACTION_TARGET_VISIBILITY_RETRY_ACTIONS,
            'log': log,
        }

    def held_objects_description_for_log(self, agent_id: Any) -> str:
        return self._get_object_interactor().held_objects_description_for_log(agent_id)

    def object_name_for_log(self, obj: Dict[str, Any]) -> str:
        return self._get_object_interactor().object_name_for_log(obj)

    def object_id_name_for_log(self, agent_id: int, object_id: Any) -> str:
        return self._get_object_interactor().object_id_name_for_log(agent_id, object_id)

    def current_object_by_id(
        self,
        agent_id: int,
        object_id: Any,
    ) -> Optional[Dict[str, Any]]:
        return self._get_object_interactor().current_object_by_id(agent_id, object_id)

    def toggle_action_target_state(self, action: str) -> Optional[bool]:
        return self._get_object_interactor().toggle_action_target_state(action)

    def object_toggle_state(self, obj: Dict[str, Any]) -> Optional[bool]:
        return self._get_object_interactor().object_toggle_state(obj)

    def toggle_state_matches(self, action: str, obj: Dict[str, Any]) -> bool:
        return self._get_object_interactor().toggle_state_matches(action, obj)

    def toggle_error_matches_desired_state(self, action: str, error: str) -> bool:
        return self._get_object_interactor().toggle_error_matches_desired_state(action, error)

    def held_object_names_for_log(self, agent_id: int) -> List[str]:
        return self._get_object_interactor().held_object_names_for_log(agent_id)

    def log_put_object_failure_held_items(self, agent_id: Any) -> None:
        return self._get_object_interactor().log_put_object_failure_held_items(agent_id)

    def save_frames(self, event) -> None:
        return self._get_artifacts().save_frames(event)

    def active_third_party_view_names(self) -> List[str]:
        return self._get_artifacts().active_third_party_view_names()

    def event_third_party_camera_frames(
        self,
        event: Any,
        events: Sequence[Any],
    ) -> List[Any]:
        return self._get_artifacts().event_third_party_camera_frames(event, events)

    def agent_event(self, agent_id: int):
        with self.controller_lock:
            return self._agent_event_unlocked(agent_id)

    def _agent_event_unlocked(self, agent_id: int):
        events = getattr(self.controller.last_event, "events", None)
        if events and agent_id < len(events):
            return events[agent_id]
        return self.controller.last_event

    def physical_agent_id(self, robot: RobotRef) -> int:
        name = robot_name(robot)
        if name not in self.robot_agent_map:
            raise RuntimeError(f"Robot {name!r} is not configured in this runtime.")
        agent_id = self.robot_agent_map[name]
        if not 0 <= agent_id < self.physical_agent_count:
            raise RuntimeError(
                f"Robot {name!r} maps to agent {agent_id}, "
                f"but only {self.physical_agent_count} physical agent(s) are configured."
            )
        return agent_id

    def agent_positions(self, exclude_agent_id: Optional[int] = None) -> List[Dict[str, float]]:
        return [
            position
            for _, position in self.agent_position_items(exclude_agent_id=exclude_agent_id)
        ]

    def agent_position_items(
        self,
        exclude_agent_id: Optional[int] = None,
    ) -> List[Tuple[int, Dict[str, float]]]:
        with self.controller_lock:
            items = []
            events = getattr(self.controller.last_event, "events", None) or []
            if not events:
                events = [self.controller.last_event]
            for i, event in enumerate(events):
                metadata = getattr(event, "metadata", {}) or {}
                agent_id = int(metadata.get("agentId", i))
                if exclude_agent_id is not None and agent_id == exclude_agent_id:
                    continue
                position = metadata.get("agent", {}).get("position")
                if position:
                    items.append((agent_id, dict(position)))
            return items

    def current_objects(self, agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.controller_lock:
            if agent_id is not None:
                event = self._agent_event_unlocked(agent_id)
                return list(event.metadata.get("objects", []))
            return list(self.controller.last_event.metadata.get("objects", []))

    def _get_object_resolver(self) -> ObjectResolver:
        resolver = getattr(self, 'object_resolver', None)
        if resolver is None:
            resolver = self.object_resolver = ObjectResolver(self)
        return resolver

    def _ensure_object_alias_state(self) -> None:
        return self._get_object_resolver()._ensure_object_alias_state()

    def _iter_object_id_bindings(self, bindings: Any) -> List[Dict[str, Any]]:
        return self._get_object_resolver()._iter_object_id_bindings(bindings)

    def object_alias_keys_for_binding(self, binding: Dict[str, Any]) -> List[str]:
        return self._get_object_resolver().object_alias_keys_for_binding(binding)

    def register_object_id_bindings(self, bindings: Any) -> None:
        return self._get_object_resolver().register_object_id_bindings(bindings)

    def _refresh_binding_from_object(
        self,
        binding: Dict[str, Any],
        obj: Dict[str, Any],
    ) -> None:
        return self._get_object_resolver()._refresh_binding_from_object(binding, obj)

    def _current_object_by_id_optional(
        self,
        agent_id: Optional[int],
        object_id: Any,
        objects: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._get_object_resolver()._current_object_by_id_optional(agent_id, object_id, objects)

    def object_alias_token_for_pattern(self, pattern: Any) -> Optional[str]:
        return self._get_object_resolver().object_alias_token_for_pattern(pattern)

    def object_alias_current_id(self, pattern: Any) -> Optional[str]:
        return self._get_object_resolver().object_alias_current_id(pattern)

    def _object_alias_binding_snapshot(self, token: str) -> Optional[Dict[str, Any]]:
        return self._get_object_resolver()._object_alias_binding_snapshot(token)

    def _warn_object_alias_once(self, message: str) -> None:
        return self._get_object_resolver()._warn_object_alias_once(message)

    def resolve_object_alias(self, pattern: Any, agent_id: Optional[int] = None) -> Any:
        return self._get_object_resolver().resolve_object_alias(pattern, agent_id)

    def _set_object_alias_current_object(
        self,
        token: str,
        obj: Dict[str, Any],
    ) -> Optional[str]:
        return self._get_object_resolver()._set_object_alias_current_object(token, obj)

    def _record_object_alias_match(
        self,
        pattern: Any,
        obj: Dict[str, Any],
        match_count: int,
    ) -> Optional[str]:
        return self._get_object_resolver()._record_object_alias_match(pattern, obj, match_count)

    def _event_objects(self, event: Any) -> List[Dict[str, Any]]:
        return self._get_object_resolver()._event_objects(event)

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
        return self._get_object_resolver().repair_object_alias(token, agent_id=agent_id, objects=objects, preferred_parent_id=preferred_parent_id, transform=transform, exclude_object_ids=exclude_object_ids)

    def update_object_alias_for_pattern(
        self,
        pattern: Any,
        obj_or_object_id: Any,
        *,
        agent_id: Optional[int] = None,
        objects: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        return self._get_object_resolver().update_object_alias_for_pattern(pattern, obj_or_object_id, agent_id=agent_id, objects=objects)

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
        return self._get_object_resolver().update_object_aliases_for_object_ids(object_ids, agent_id=agent_id, event=event, preferred_parent_id=preferred_parent_id, transform=transform, exclude_object_ids=exclude_object_ids)

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
        return self._get_object_resolver().update_object_alias_after_action(action, agent_id, obj, event=event, goal_object_name=goal_object_name, extra_object_resources=extra_object_resources, known_object_ids=known_object_ids)

    def _operated_object_name_state(self) -> Tuple[Set[str], threading.Lock]:
        return self._get_object_resolver()._operated_object_name_state()

    def operated_object_names_snapshot(self) -> Set[str]:
        return self._get_object_resolver().operated_object_names_snapshot()

    def record_operated_object_name(self, obj: Dict[str, Any]) -> None:
        return self._get_object_resolver().record_operated_object_name(obj)

    def record_created_slice_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        return self._get_object_resolver().record_created_slice_object_names(source_obj, event, known_object_ids)

    def record_created_broken_egg_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        return self._get_object_resolver().record_created_broken_egg_object_names(source_obj, event, known_object_ids)

    def object_name_was_operated(
        self,
        obj: Dict[str, Any],
        operated_object_names: Optional[Set[str]] = None,
    ) -> bool:
        return self._get_object_resolver().object_name_was_operated(obj, operated_object_names)

    def stove_burner_occupied(
        self,
        burner: Dict[str, Any],
        objects: Sequence[Dict[str, Any]],
    ) -> bool:
        return self._get_object_resolver().stove_burner_occupied(burner, objects)

    def find_objects(self, pattern: Any, agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._get_object_resolver().find_objects(pattern, agent_id)

    def find_object(
        self,
        pattern: Any,
        *,
        agent_id: Optional[int] = None,
        require_center: bool = False,
    ) -> Dict[str, Any]:
        return self._get_object_resolver().find_object(pattern, agent_id=agent_id, require_center=require_center)

    def refresh_reachable_positions(self, agent_id: int) -> List[Dict[str, float]]:
        event = self.step(
            {"action": "GetReachablePositions", "agentId": agent_id},
            check_success=True,
            save_frame=False,
            retry_on_failure=False,
        )
        positions = event.metadata.get("actionReturn") or []
        if not positions:
            raise RuntimeError(f"No reachable positions returned for agent {agent_id}.")
        self.reachable_positions = [dict(position) for position in positions]
        return list(self.reachable_positions)

    def scene_object_bounds(self, agent_id: Optional[int] = None) -> List[SceneObjectFootprint]:
        bounds = []
        for obj in self.current_objects(agent_id):
            object_id = str(obj.get("objectId") or obj.get("name") or obj.get("objectType") or "")
            object_type = object_key(obj.get("objectType") or object_id)
            if object_type in {"floor", "wall", "ceiling"}:
                continue
            object_bounds = object_aabb_bounds(obj)
            if object_bounds is None:
                continue
            bounds.append((object_id, object_bounds, object_footprint_clearance(obj)))
        return bounds

    def teleport_position_clear_of_scene_objects(
        self,
        position: Dict[str, float],
        object_bounds: Sequence[SceneObjectFootprint],
    ) -> bool:
        return not any(
            position_inside_aabb_footprint(position, bounds, clearance=clearance)
            for _, bounds, clearance in object_bounds
        )

    def teleport_position_can_accommodate(
        self,
        agent_id: int,
        position: Dict[str, float],
        *,
        object_bounds: Optional[
            Sequence[SceneObjectFootprint]
        ] = None,
    ) -> bool:
        if self.target_position_blockers(agent_id, position):
            return False
        if object_bounds is None:
            object_bounds = self.scene_object_bounds(agent_id)
        return self.teleport_position_clear_of_scene_objects(position, object_bounds)

    def empty_teleport_candidate_positions(
        self,
        target: Dict[str, float],
        *,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]] = (),
        object_bounds: Optional[
            Sequence[SceneObjectFootprint]
        ] = None,
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        include_reachable_positions: bool = True,
    ) -> List[Dict[str, float]]:
        excluded_keys = set(excluded_grid_keys or set())
        candidates_by_key: Dict[Tuple[int, int], Dict[str, float]] = {}
        for position in candidate_positions:
            candidates_by_key[position_to_grid_key(position)] = dict(position)
        if include_reachable_positions:
            for position in self.reachable_positions:
                candidates_by_key.setdefault(position_to_grid_key(position), dict(position))

        if object_bounds is None:
            object_bounds = self.scene_object_bounds(agent_id)
        other_positions = self.agent_positions(exclude_agent_id=agent_id)
        candidates = [
            position
            for position in candidates_by_key.values()
            if position_to_grid_key(position) not in excluded_keys
            and self.position_clear_of_positions(position, other_positions)
            and self.teleport_position_clear_of_scene_objects(position, object_bounds)
        ]

        target_tuple = position_to_tuple(target)
        candidates.sort(
            key=lambda position: (
                distance_pts(position_to_tuple(position), target_tuple),
                position_to_grid_key(position),
            )
        )
        return candidates

    def select_teleport_position(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        candidate_positions: Sequence[Dict[str, float]] = (),
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> Dict[str, float]:
        excluded_keys = set(excluded_grid_keys or set())
        object_bounds = self.scene_object_bounds(agent_id)
        candidate_copies = [dict(position) for position in candidate_positions]
        target_key = position_to_grid_key(target_position)
        target_tuple = position_to_tuple(target_position)
        target_matches_candidate = any(
            all(
                abs(candidate_coord - target_coord) < 1e-6
                for candidate_coord, target_coord in zip(
                    position_to_tuple(candidate),
                    target_tuple,
                )
            )
            for candidate in candidate_copies
        )
        target_is_allowed = (
            not restrict_to_candidate_positions
            or target_matches_candidate
        )
        candidates = []
        if target_is_allowed:
            candidates.append(dict(target_position))
        candidates.extend(candidate_copies)
        if (
            target_is_allowed
            and target_key not in excluded_keys
            and self.teleport_position_can_accommodate(
                agent_id,
                target_position,
                object_bounds=object_bounds,
            )
        ):
            return dict(target_position)

        empty_candidates = self.empty_teleport_candidate_positions(
            target_position,
            agent_id=agent_id,
            candidate_positions=candidates,
            object_bounds=object_bounds,
            excluded_grid_keys=excluded_keys,
            include_reachable_positions=not restrict_to_candidate_positions,
        )
        if not empty_candidates:
            raise RuntimeError(
                f"No empty teleport grid position for agent {agent_id} near {target_position}."
            )
        selected = empty_candidates[0]
        log(
            "Teleport target cannot accommodate agent "
            f"{agent_id}; using nearby free grid position {selected}."
        )
        return dict(selected)

    def minimum_distance_to_positions(
        self,
        position: Dict[str, float],
        positions: Sequence[Dict[str, float]],
    ) -> float:
        if not positions:
            return 999999.0
        position_tuple = position_to_tuple(position)
        return min(
            distance_pts(position_tuple, position_to_tuple(other))
            for other in positions
        )

    def minimum_distance_to_object_footprints(
        self,
        position: Dict[str, float],
        object_bounds: Sequence[SceneObjectFootprint],
    ) -> float:
        if not object_bounds:
            return 999999.0
        return min(
            max(0.0, distance_to_aabb_footprint(position, bounds) - clearance)
            for _, bounds, clearance in object_bounds
        )

    def teleport_candidate_positions(
        self,
        target: Dict[str, float],
        *,
        agent_id: int,
        include_agent_positions: bool = True,
    ) -> List[Dict[str, float]]:
        if not self.reachable_positions:
            raise RuntimeError("Cannot navigate without reachable positions.")
        candidates_by_key: Dict[Tuple[int, int], Dict[str, float]] = {
            position_to_grid_key(position): dict(position)
            for position in self.reachable_positions
        }
        if include_agent_positions:
            for other_agent_id, position in self.agent_position_items(
                exclude_agent_id=agent_id
            ):
                candidates_by_key.setdefault(
                    position_to_grid_key(position),
                    dict(position),
                )

        object_bounds = self.scene_object_bounds(agent_id)
        candidates = [
            position
            for position in candidates_by_key.values()
            if self.teleport_position_clear_of_scene_objects(position, object_bounds)
        ]
        target_tuple = position_to_tuple(target)
        if len(candidates) < TELEPORT_CANDIDATE_LIMIT:
            candidate_keys = {
                position_to_grid_key(position)
                for position in candidates
            }
            global_candidates = [
                dict(position)
                for position in getattr(self, "global_reachable_positions", []) or []
                if position_to_grid_key(position) not in candidate_keys
                and self.teleport_position_clear_of_scene_objects(position, object_bounds)
            ]
            global_candidates.sort(
                key=lambda position: distance_pts(position_to_tuple(position), target_tuple)
            )
            for position in global_candidates:
                candidate_key = position_to_grid_key(position)
                if candidate_key in candidate_keys:
                    continue
                candidates.append(position)
                candidate_keys.add(candidate_key)
                if len(candidates) >= TELEPORT_CANDIDATE_LIMIT:
                    break

        if not candidates:
            raise RuntimeError(
                f"No reachable teleport candidate for agent {agent_id} near {target}."
            )

        candidates.sort(
            key=lambda position: distance_pts(position_to_tuple(position), target_tuple)
        )
        return candidates

    def closest_reachable(
        self,
        target: Dict[str, float],
        *,
        agent_id: Optional[int] = None,
    ) -> Dict[str, float]:
        if not self.reachable_positions:
            raise RuntimeError("Cannot navigate without reachable positions.")
        target_tuple = position_to_tuple(target)
        other_positions = self.agent_positions(exclude_agent_id=agent_id)

        def clear_of_other_agents(position: Dict[str, float]) -> bool:
            pos_tuple = position_to_tuple(position)
            return all(
                distance_pts(pos_tuple, position_to_tuple(other))
                > AGENT_CLEARANCE_DISTANCE
                for other in other_positions
            )

        candidates = [pos for pos in self.reachable_positions if clear_of_other_agents(pos)]
        if not candidates:
            candidates = self.reachable_positions
        return min(candidates, key=lambda pos: distance_pts(position_to_tuple(pos), target_tuple))

    def nearest_reachable(self, target: Dict[str, float]) -> Dict[str, float]:
        if not self.reachable_positions:
            raise RuntimeError("Cannot navigate without reachable positions.")
        target_tuple = position_to_tuple(target)
        return min(
            self.reachable_positions,
            key=lambda pos: distance_pts(position_to_tuple(pos), target_tuple),
        )

    def move_requeue_budget(self) -> int:
        configured = getattr(self, "navigation_requeue_budget", None)
        if configured is not None:
            return int(configured)
        return max(16, 2 * len(self.reachable_positions))

    def agent_blocked_grid_keys(self, agent_id: int) -> Set[Tuple[int, int]]:
        other_positions = self.agent_positions(exclude_agent_id=agent_id)
        blocked_keys = {
            position_to_grid_key(position)
            for position in other_positions
        }
        for position in self.reachable_positions:
            if not self.position_clear_of_positions(position, other_positions):
                blocked_keys.add(position_to_grid_key(position))
        return blocked_keys

    def position_clear_of_positions(
        self,
        position: Dict[str, float],
        other_positions: Sequence[Dict[str, float]],
    ) -> bool:
        pos_tuple = position_to_tuple(position)
        return all(
            distance_pts(pos_tuple, position_to_tuple(other))
            > AGENT_CLEARANCE_DISTANCE
            for other in other_positions
        )

    def agent_blocker_ids_for_keys(
        self,
        protected_keys: Set[Tuple[int, int]],
        agent_id: int,
    ) -> Set[int]:
        return {
            other_agent_id
            for other_agent_id, position in self.agent_position_items(exclude_agent_id=agent_id)
            if position_to_grid_key(position) in protected_keys
        }

    def agent_blocker_ids_for_positions(
        self,
        protected_positions: Sequence[Dict[str, float]],
        agent_id: int,
    ) -> Set[int]:
        return {
            other_agent_id
            for other_agent_id, position in self.agent_position_items(exclude_agent_id=agent_id)
            if not self.position_clear_of_positions(position, protected_positions)
        }

    def position_blocked_by_other_agent(
        self,
        position: Dict[str, float],
        agent_id: int,
    ) -> bool:
        return position_to_grid_key(position) in self.agent_blocked_grid_keys(agent_id)

    def target_position_blockers(
        self,
        agent_id: int,
        target_position: Dict[str, float],
    ) -> Set[int]:
        return self.agent_blocker_ids_for_positions([target_position], agent_id)

    def current_agent_position(self, agent_id: int) -> Dict[str, float]:
        metadata = self.agent_event(agent_id).metadata
        agent = metadata.get("agent", {})
        current_position = agent.get("position")
        if not current_position:
            raise RuntimeError(f"Agent {agent_id} has no current position for navigation.")
        return current_position

    def reachable_position_map(self) -> Dict[Tuple[int, int], Dict[str, float]]:
        return {
            position_to_grid_key(position): position
            for position in self.reachable_positions
        }

    def plan_grid_path(
        self,
        start_position: Dict[str, float],
        target_position: Dict[str, float],
        *,
        agent_id: int,
        block_other_agents: bool = True,
        extra_blocked_keys: Optional[Set[Tuple[int, int]]] = None,
    ) -> List[Dict[str, float]]:
        positions_by_key = self.reachable_position_map()
        if not positions_by_key:
            raise RuntimeError("Cannot navigate without reachable positions.")

        start = position_to_grid_key(start_position)
        target = position_to_grid_key(target_position)
        if start not in positions_by_key:
            raise RuntimeError(f"Navigation start is not reachable: {start_position}")
        if target not in positions_by_key:
            raise RuntimeError(f"Navigation target is not reachable: {target_position}")

        blocked: Set[Tuple[int, int]] = set()
        if block_other_agents:
            blocked = self.agent_blocked_grid_keys(agent_id)
        if extra_blocked_keys:
            blocked.update(extra_blocked_keys)
        blocked.discard(start)

        parents: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
        queue_keys = deque([start])
        offsets = [(1, 0), (-1, 0), (0, 1), (0, -1)]

        while queue_keys:
            key = queue_keys.popleft()
            if key == target:
                break
            for dx, dz in offsets:
                neighbor = (key[0] + dx, key[1] + dz)
                if neighbor in parents or neighbor in blocked:
                    continue
                if neighbor not in positions_by_key:
                    continue
                parents[neighbor] = key
                queue_keys.append(neighbor)

        if target not in parents:
            raise RuntimeError(
                f"No reachable grid path for agent {agent_id} "
                f"from {start_position} to {target_position}."
            )

        path_keys = []
        key: Optional[Tuple[int, int]] = target
        while key is not None:
            path_keys.append(key)
            key = parents[key]
        path_keys.reverse()
        return [positions_by_key[path_key] for path_key in path_keys]

    def static_grid_path_exists(
        self,
        start_position: Dict[str, float],
        target_position: Dict[str, float],
        *,
        agent_id: int,
    ) -> bool:
        try:
            self.plan_grid_path(
                start_position,
                target_position,
                agent_id=agent_id,
                block_other_agents=False,
            )
        except RuntimeError as exc:
            raise_if_execution_aborted(self, exc)
            return False
        return True

    def navigation_path_positions(
        self,
        agent_id: int,
        target_position: Dict[str, float],
    ) -> List[Dict[str, float]]:
        current_position = self.current_agent_position(agent_id)
        start_position = self.nearest_reachable(current_position)
        target_key = position_to_grid_key(target_position)
        if position_to_grid_key(start_position) == target_key:
            return []
        path = self.plan_grid_path(
            start_position,
            target_position,
            agent_id=agent_id,
            block_other_agents=False,
        )
        return path[1:]

    def navigation_path_keys(
        self,
        agent_id: int,
        target_position: Dict[str, float],
    ) -> Set[Tuple[int, int]]:
        return {
            position_to_grid_key(position)
            for position in self.navigation_path_positions(agent_id, target_position)
        }

    def navigation_blockers(
        self,
        agent_id: int,
        target_position: Dict[str, float],
    ) -> Set[int]:
        current_position = self.current_agent_position(agent_id)
        start_position = self.nearest_reachable(current_position)
        target_key = position_to_grid_key(target_position)
        if position_to_grid_key(start_position) == target_key:
            return set()

        try:
            self.plan_grid_path(
                start_position,
                target_position,
                agent_id=agent_id,
                block_other_agents=True,
            )
        except RuntimeError as exc:
            raise_if_execution_aborted(self, exc)
            if not self.static_grid_path_exists(
                start_position,
                target_position,
                agent_id=agent_id,
            ):
                raise
            protected_positions = self.navigation_path_positions(agent_id, target_position)
            return self.agent_blocker_ids_for_positions(protected_positions, agent_id)
        return set()

    def move_to_position(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        chunk_steps: int = NAVIGATION_CHUNK_STEPS,
        max_requeues: Optional[int] = None,
        object_resource: Optional[str] = None,
    ) -> None:
        if max_requeues is None:
            max_requeues = self.move_requeue_budget()
        self.move_to_position_direct(
            agent_id,
            target_position,
            chunk_steps=chunk_steps,
            max_requeues=max_requeues,
        )

    @navigation_operation
    def move_to_position_direct(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        chunk_steps: int = NAVIGATION_CHUNK_STEPS,
        max_requeues: Optional[int] = None,
    ) -> None:
        if max_requeues is None:
            max_requeues = self.move_requeue_budget()
        requeues_remaining = max_requeues
        while True:
            if self.move_to_position_chunk(
                agent_id,
                target_position,
                chunk_steps=chunk_steps,
            ):
                return
            if requeues_remaining <= 0:
                raise RuntimeError(
                    "MoveToPosition failed for agent "
                    f"{agent_id} after {max_requeues} requeues."
                )
            requeues_remaining -= 1
            log(
                "Requeueing MoveToPosition for agent "
                f"{agent_id} ({requeues_remaining} requeues left)."
            )

    @navigation_operation
    def move_to_position_chunk(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        chunk_steps: int = NAVIGATION_CHUNK_STEPS,
    ) -> bool:
        if chunk_steps < 1:
            raise RuntimeError("MoveToPosition chunk_steps must be at least 1.")

        current_position = self.current_agent_position(agent_id)
        start_position = self.nearest_reachable(current_position)
        target_key = position_to_grid_key(target_position)
        if position_to_grid_key(start_position) == target_key:
            return True

        try:
            path = self.plan_grid_path(
                start_position,
                target_position,
                agent_id=agent_id,
                block_other_agents=True,
            )
        except RuntimeError as exc:
            raise_if_execution_aborted(self, exc)
            if self.static_grid_path_exists(
                start_position,
                target_position,
                agent_id=agent_id,
            ):
                return False
            raise

        moves_completed = 0
        for next_position in path[1:]:
            if moves_completed >= chunk_steps:
                break
            if self.position_blocked_by_other_agent(next_position, agent_id):
                return False
            if not self.move_to_adjacent_position_direct(agent_id, next_position):
                return False
            moves_completed += 1

            current_position = self.current_agent_position(agent_id)
            start_position = self.nearest_reachable(current_position)
            if position_to_grid_key(start_position) == target_key:
                return True

        current_position = self.current_agent_position(agent_id)
        start_position = self.nearest_reachable(current_position)
        return position_to_grid_key(start_position) == target_key

    @navigation_operation
    def move_to_adjacent_position_direct(
        self,
        agent_id: int,
        next_position: Dict[str, float],
    ) -> bool:
        current_position = self.current_agent_position(agent_id)
        target_yaw = yaw_to_face(current_position, next_position)
        if target_yaw is None:
            return True
        current_event = self.agent_event(agent_id)
        current_yaw = float(
            current_event.metadata.get("agent", {}).get("rotation", {}).get("y", 0.0)
        )
        delta = shortest_yaw_delta(target_yaw, current_yaw)
        if abs(delta) > 1e-3:
            action = "RotateRight" if delta > 0 else "RotateLeft"
            try:
                self._step_direct(
                    {"action": action, "degrees": abs(delta), "agentId": agent_id},
                    check_success=True,
                )
            except RuntimeError as exc:
                raise_if_execution_aborted(self, exc)
                if not self.held_item_rotation_failure(exc):
                    raise
                opposite_action = (
                    "RotateLeft" if action == "RotateRight" else "RotateRight"
                )
                opposite_degrees = 360.0 - abs(delta)
                log(
                    f"{action} was blocked by a held item for agent {agent_id}; "
                    f"trying equivalent {opposite_action} rotation."
                )
                self._step_direct(
                    {
                        "action": opposite_action,
                        "degrees": opposite_degrees,
                        "agentId": agent_id,
                    },
                    check_success=True,
                )

        move_payload = {
            "action": "MoveAhead",
            "moveMagnitude": NAVIGATION_GRID_SIZE,
            "agentId": agent_id,
        }
        event = self._step_direct(move_payload, check_success=False)
        if not step_event_failed(event):
            return True
        recovered = self.retry_move_past_open_object_blocker(
            agent_id,
            move_payload,
            event,
        )
        return bool(recovered)

    def retry_move_past_open_object_blocker(
        self,
        agent_id: int,
        move_payload: Dict[str, Any],
        failed_event: Any,
    ) -> Optional[bool]:
        return self._get_object_interactor().retry_move_past_open_object_blocker(agent_id, move_payload, failed_event)

    @navigation_operation
    def teleport_completed_agent_to_free_position(
        self,
        blocker_agent_id: int,
        priority_agent_id: int,
        priority_target_position: Dict[str, float],
        *,
        protect_navigation_path: bool = True,
    ) -> None:
        priority_position = self.current_agent_position(priority_agent_id)
        if protect_navigation_path:
            protected_positions = [
                priority_position,
                *self.navigation_path_positions(priority_agent_id, priority_target_position),
            ]
        else:
            protected_positions = [priority_position, priority_target_position]
        protected_keys = {
            position_to_grid_key(position)
            for position in protected_positions
        }
        occupied_keys = {
            position_to_grid_key(position)
            for other_agent_id, position in self.agent_position_items()
            if other_agent_id != blocker_agent_id
        }
        other_positions = [
            position
            for other_agent_id, position in self.agent_position_items()
            if other_agent_id != blocker_agent_id
        ]
        object_bounds = self.scene_object_bounds(blocker_agent_id)
        current_position = self.current_agent_position(blocker_agent_id)
        candidates = [
            position
            for position in self.reachable_positions
            if position_to_grid_key(position) not in protected_keys
            and position_to_grid_key(position) not in occupied_keys
            and self.position_clear_of_positions(position, other_positions)
            and self.position_clear_of_positions(position, protected_positions)
            and self.teleport_position_clear_of_scene_objects(position, object_bounds)
        ]

        def relocation_score(position: Dict[str, float]) -> Tuple[float, float, float, float]:
            return (
                self.minimum_distance_to_positions(position, protected_positions),
                self.minimum_distance_to_positions(position, other_positions),
                self.minimum_distance_to_object_footprints(position, object_bounds),
                distance_pts(position_to_tuple(position), position_to_tuple(current_position)),
            )

        candidates.sort(
            key=relocation_score,
            reverse=True,
        )
        last_error = "no free reachable position found"
        for candidate in candidates:
            event = self._step_direct(
                {
                    "action": "Teleport",
                    "position": dict(candidate),
                    "agentId": blocker_agent_id,
                },
                check_success=False,
            )
            if not step_event_failed(event):
                if protect_navigation_path:
                    try:
                        remaining_blockers = self.navigation_blockers(
                            priority_agent_id,
                            priority_target_position,
                        )
                    except RuntimeError as exc:
                        raise_if_execution_aborted(self, exc)
                        remaining_blockers = {blocker_agent_id}
                else:
                    remaining_blockers = self.target_position_blockers(
                        priority_agent_id,
                        priority_target_position,
                    )
                if blocker_agent_id not in remaining_blockers:
                    return
                blocked_area = "path" if protect_navigation_path else "target"
                last_error = (
                    f"candidate {candidate} still blocks agent "
                    f"{priority_agent_id}'s {blocked_area}"
                )
                continue
            metadata = getattr(event, "metadata", {}) or {}
            last_error = metadata.get("errorMessage") or "Teleport failed"
        raise RuntimeError(
            f"Could not move completed agent {blocker_agent_id} "
            f"to a free position: {last_error}"
        )

    @navigation_operation
    def teleport_completed_agent_away_from_positions(
        self,
        blocker_agent_id: int,
        priority_agent_id: int,
        protected_positions: Sequence[Dict[str, float]],
    ) -> None:
        protected_position_copies = [dict(position) for position in protected_positions]
        if not protected_position_copies:
            return
        protected_keys = {
            position_to_grid_key(position)
            for position in protected_position_copies
        }
        occupied_keys = {
            position_to_grid_key(position)
            for other_agent_id, position in self.agent_position_items()
            if other_agent_id != blocker_agent_id
        }
        other_positions = [
            position
            for other_agent_id, position in self.agent_position_items()
            if other_agent_id != blocker_agent_id
        ]
        object_bounds = self.scene_object_bounds(blocker_agent_id)
        current_position = self.current_agent_position(blocker_agent_id)
        candidates = [
            position
            for position in self.reachable_positions
            if position_to_grid_key(position) not in protected_keys
            and position_to_grid_key(position) not in occupied_keys
            and self.position_clear_of_positions(position, other_positions)
            and self.position_clear_of_positions(position, protected_position_copies)
            and self.teleport_position_clear_of_scene_objects(position, object_bounds)
        ]

        def relocation_score(position: Dict[str, float]) -> Tuple[float, float, float, float]:
            return (
                self.minimum_distance_to_positions(position, protected_position_copies),
                self.minimum_distance_to_positions(position, other_positions),
                self.minimum_distance_to_object_footprints(position, object_bounds),
                distance_pts(position_to_tuple(position), position_to_tuple(current_position)),
            )

        candidates.sort(key=relocation_score, reverse=True)
        last_error = "no free reachable position found"
        for candidate in candidates:
            event = self._step_direct(
                {
                    "action": "Teleport",
                    "position": dict(candidate),
                    "agentId": blocker_agent_id,
                },
                check_success=False,
            )
            if not step_event_failed(event):
                remaining_blockers = self.agent_blocker_ids_for_positions(
                    protected_position_copies,
                    priority_agent_id,
                )
                if blocker_agent_id not in remaining_blockers:
                    return
                last_error = (
                    f"candidate {candidate} still blocks agent "
                    f"{priority_agent_id}'s GoToObject candidates"
                )
                continue
            metadata = getattr(event, "metadata", {}) or {}
            last_error = metadata.get("errorMessage") or "Teleport failed"
        raise RuntimeError(
            f"Could not move completed agent {blocker_agent_id} "
            f"away from GoToObject candidates: {last_error}"
        )

    @navigation_operation
    def face_position_direct(self, agent_id: int, target: Dict[str, float]) -> None:
        metadata = self.agent_event(agent_id).metadata
        agent = metadata.get("agent", {})
        position = agent.get("position")
        rotation = agent.get("rotation", {})
        if not position:
            return
        target_yaw = yaw_to_face(position, target)
        if target_yaw is None:
            return
        current_yaw = float(rotation.get("y", 0.0))
        delta = shortest_yaw_delta(target_yaw, current_yaw)
        if abs(delta) < 1e-3:
            return
        action = "RotateRight" if delta > 0 else "RotateLeft"
        self._step_direct({"action": action, "degrees": abs(delta), "agentId": agent_id})

    @navigation_operation
    def handoff_held_object_direct(
        self,
        from_agent_id: int,
        to_agent_id: int,
        object_resource: str,
    ):
        return self._get_object_interactor().handoff_held_object_direct(from_agent_id, to_agent_id, object_resource)

    def agent_holds_object(self, agent_id: int, object_resource: str) -> bool:
        return self._get_object_interactor().agent_holds_object(agent_id, object_resource)

    def agent_held_object_matching(self, agent_id: int, pattern: Any) -> Optional[str]:
        return self._get_object_interactor().agent_held_object_matching(agent_id, pattern)

    def agent_held_objects_for(self, agent_id: int) -> Set[str]:
        return self._get_object_interactor().agent_held_objects_for(agent_id)

    def _held_object_override_state(self) -> Tuple[Dict[int, Set[str]], threading.Lock]:
        return self._get_object_interactor()._held_object_override_state()

    def agent_held_object_overrides_snapshot(self, agent_id: int) -> Set[str]:
        return self._get_object_interactor().agent_held_object_overrides_snapshot(agent_id)

    def record_agent_held_object(self, agent_id: int, object_id: str) -> None:
        return self._get_object_interactor().record_agent_held_object(agent_id, object_id)

    def release_agent_held_objects(self, agent_id: int) -> None:
        return self._get_object_interactor().release_agent_held_objects(agent_id)

    def metadata_held_objects(self, agent_id: int) -> Set[str]:
        return self._get_object_interactor().metadata_held_objects(agent_id)

    def teleport_to_position(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        max_retries: int = 3,
    ) -> Dict[str, float]:
        self.refresh_reachable_positions(agent_id)
        candidate_positions = self.teleport_candidate_positions(
            target_position,
            agent_id=agent_id,
            include_agent_positions=False,
        )[:TELEPORT_CANDIDATE_LIMIT]
        return self.teleport_to_candidate_positions(
            agent_id,
            candidate_positions,
            max_retries=max_retries,
            search_center=target_position,
            restrict_to_candidate_positions=True,
        )

    def teleport_to_candidate_positions(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        max_retries: int = 3,
        object_resource: Optional[str] = None,
        search_center: Optional[Dict[str, float]] = None,
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> Dict[str, float]:
        candidates = [dict(position) for position in candidate_positions]
        if not candidates:
            raise RuntimeError(
                f"No reachable teleport candidate for agent {agent_id}."
            )
        return self.teleport_to_first_working_candidate(
            agent_id,
            candidates,
            search_center=(
                dict(search_center) if search_center is not None else candidates[0]
            ),
            max_retries=max_retries,
            excluded_grid_keys=excluded_grid_keys,
            restrict_to_candidate_positions=restrict_to_candidate_positions,
        )

    def teleport_and_face_candidate_positions(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        face_target: Dict[str, float],
        max_retries: int = 3,
        object_resource: Optional[str] = None,
        search_center: Optional[Dict[str, float]] = None,
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> Dict[str, float]:
        candidates = [dict(position) for position in candidate_positions]
        if not candidates:
            raise RuntimeError(
                f"No reachable teleport candidate for agent {agent_id}."
            )
        return self.teleport_and_face_first_working_candidate(
            agent_id,
            candidates,
            face_target=face_target,
            search_center=(
                dict(search_center) if search_center is not None else candidates[0]
            ),
            max_retries=max_retries,
            excluded_grid_keys=excluded_grid_keys,
            restrict_to_candidate_positions=restrict_to_candidate_positions,
        )

    @navigation_operation
    def teleport_to_first_working_candidate(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        search_center: Dict[str, float],
        max_retries: int = 3,
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> Dict[str, float]:
        candidates = [dict(position) for position in candidate_positions]
        excluded_keys: Set[Tuple[int, int]] = set(excluded_grid_keys or set())
        last_error = "no teleport attempted"
        while True:
            try:
                selected_position = self.select_teleport_position(
                    agent_id,
                    search_center,
                    candidate_positions=candidates,
                    excluded_grid_keys=excluded_keys,
                    restrict_to_candidate_positions=restrict_to_candidate_positions,
                )
            except RuntimeError as exc:
                raise_if_execution_aborted(self, exc)
                raise RuntimeError(f"{exc} Last teleport error: {last_error}") from exc

            event = self.try_teleport_to_position_direct(
                agent_id,
                selected_position,
                max_retries=max_retries,
            )
            if not step_event_failed(event):
                return dict(selected_position)

            last_error = event_error_message(event) or "no error message returned"
            collided_object = teleport_collision_object_id(event)
            selected_key = position_to_grid_key(selected_position)
            if selected_key in excluded_keys:
                raise RuntimeError(
                    f"Teleport failed for agent {agent_id} to {selected_position}: "
                    f"{last_error}"
                )
            excluded_keys.add(selected_key)
            failure_detail = (
                f"collided with {collided_object}"
                if collided_object is not None
                else last_error
            )
            log(
                "Teleport candidate "
                f"{selected_position} failed ({failure_detail}); "
                "trying another reachable position."
            )

    @navigation_operation
    def teleport_and_face_first_working_candidate(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        face_target: Dict[str, float],
        search_center: Dict[str, float],
        max_retries: int = 3,
        excluded_grid_keys: Optional[Set[Tuple[int, int]]] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> Dict[str, float]:
        candidates = [dict(position) for position in candidate_positions]
        excluded_keys: Set[Tuple[int, int]] = set(excluded_grid_keys or set())
        last_error = "no candidate was attempted"
        while True:
            try:
                selected_position = self.select_teleport_position(
                    agent_id,
                    search_center,
                    candidate_positions=candidates,
                    excluded_grid_keys=excluded_keys,
                    restrict_to_candidate_positions=restrict_to_candidate_positions,
                )
            except RuntimeError as exc:
                raise_if_execution_aborted(self, exc)
                raise RuntimeError(
                    "Could not teleport and face agent "
                    f"{agent_id} toward {face_target}: no candidate succeeded. "
                    f"Last unit failure: {last_error}"
                ) from exc

            selected_key = position_to_grid_key(selected_position)
            try:
                event = self.try_teleport_to_position_direct(
                    agent_id,
                    selected_position,
                    max_retries=max_retries,
                )
            except Exception as exc:
                raise_if_execution_aborted(self, exc)
                last_error = f"Teleport failed at {selected_position}: {exc}"
                excluded_keys.add(selected_key)
                log(
                    "Teleport+face candidate "
                    f"{selected_position} failed during teleport: {exc}; "
                    "trying another reachable position."
                )
                continue
            if step_event_failed(event):
                error = event_error_message(event) or "no error message returned"
                collided_object = teleport_collision_object_id(event)
                failure_detail = (
                    f"collided with {collided_object}"
                    if collided_object is not None
                    else error
                )
                last_error = (
                    f"Teleport failed at {selected_position}: {failure_detail}"
                )
                excluded_keys.add(selected_key)
                log(
                    "Teleport+face candidate "
                    f"{selected_position} failed during teleport ({failure_detail}); "
                    "trying another reachable position."
                )
                continue

            try:
                self.face_position_direct(agent_id, face_target)
            except Exception as exc:
                raise_if_execution_aborted(self, exc)
                last_error = f"Rotate failed at {selected_position}: {exc}"
                excluded_keys.add(selected_key)
                log(
                    "Teleport+face candidate "
                    f"{selected_position} failed while facing target: {exc}; "
                    "trying another reachable position."
                )
                continue

            return dict(selected_position)

    @navigation_operation
    def try_teleport_to_position_direct(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        max_retries: int = 3,
    ):
        return self._step_with_retries(
            {
                "action": "Teleport",
                "position": dict(target_position),
                "agentId": agent_id,
            },
            check_success=False,
            save_frame=True,
            retry_on_failure=True,
            max_retries=max_retries,
        )

    @navigation_operation
    def teleport_to_position_direct(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        max_retries: int = 3,
    ) -> None:
        event = self.try_teleport_to_position_direct(
            agent_id,
            target_position,
            max_retries=max_retries,
        )
        if step_event_failed(event):
            error = event_error_message(event) or "no error message returned"
            raise RuntimeError(
                f"Teleport failed for agent {agent_id} to {target_position}: {error}"
            )

    def prepare_hand_for_goto_if_needed(
        self,
        robot: RobotRef,
        next_action: Optional[PlannedAction],
    ) -> None:
        return self._get_object_interactor().prepare_hand_for_goto_if_needed(robot, next_action)

    @navigation_operation
    def prepare_hand_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
        target_obj: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._get_object_interactor().prepare_hand_for_pickup(robot, pickup_target, target_obj)

    @navigation_operation
    def place_held_objects_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
    ) -> None:
        return self._get_object_interactor().place_held_objects_for_pickup(robot, pickup_target)

    def held_object_type(self, agent_id: int, held_object: str) -> str:
        return self._get_object_interactor().held_object_type(agent_id, held_object)

    def allowed_receptacle_type_keys(self, held_object_type: str) -> Set[str]:
        return self._get_object_interactor().allowed_receptacle_type_keys(held_object_type)

    def compatible_receptacle_candidates(
        self,
        agent_id: int,
        held_object: str,
        held_object_type: str,
    ) -> List[Dict[str, Any]]:
        return self._get_object_interactor().compatible_receptacle_candidates(agent_id, held_object, held_object_type)

    def put_held_object_in_receptacle(
        self,
        agent_id: int,
        receptacle: Dict[str, Any],
    ):
        return self._get_object_interactor().put_held_object_in_receptacle(agent_id, receptacle)

    def held_item_rotation_failure(self, exc: BaseException) -> bool:
        return self._get_object_interactor().held_item_rotation_failure(exc)

    def configure_movement(
        self,
        movement_mode: Optional[str] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        ensure_control(self)
        if not hasattr(self, "_navigation_action_scope_state"):
            self._navigation_action_scope_state = threading.local()
            # Initialize before workers start: a controller-step lock alone cannot
            # protect a planned trajectory from another navigation's movements.
            self._navigation_execution_lock = threading.RLock()
            self._navigation_deadline_state = threading.local()
            self._interaction_reposition_state = threading.local()
        self.movement_config = MovementConfig.resolve(movement_mode, environ)
        self.navigation_metrics = NavigationMetrics(self.movement_config.mode)
        self.movement_strategy = create_movement_strategy(
            self,
            self.movement_config,
            self.navigation_metrics,
        )

    @contextmanager
    def action_deadline_scope(
        self,
        deadline=None,
        timeout_error_factory=None,
        *,
        control=None,
    ):
        del timeout_error_factory  # ExecutionControl determines the exception type.
        scope_state = getattr(self, "_execution_control_scope_state", None)
        if scope_state is None:
            scope_state = self._execution_control_scope_state = threading.local()
        previous = getattr(scope_state, "control", None)
        active_control = control or ensure_control(self)
        active_control.tighten_deadline(deadline)
        scope_state.control = active_control
        try:
            active_control.check()
            yield
        finally:
            if previous is None:
                try:
                    del scope_state.control
                except AttributeError:
                    pass
            else:
                scope_state.control = previous

    def check_navigation_deadline(self) -> None:
        ensure_control(self).check()

    @contextmanager
    def navigation_execution_scope(self):
        """Lock order: navigation -> controller; wave collection stays outside."""
        control = ensure_control(self)
        control.check()
        while not self._navigation_execution_lock.acquire(timeout=0.05):
            control.check()
        try:
            control.check()
            yield
        finally:
            self._navigation_execution_lock.release()

    @contextmanager
    def navigation_action_scope(self):
        state = self._navigation_action_scope_state
        previous_depth = int(getattr(state, "depth", 0))
        state.depth = previous_depth + 1
        try:
            yield
        finally:
            state.depth = previous_depth

    def build_navigation_request(
        self,
        robot: RobotRef,
        dest_obj: Any,
        *,
        next_action: Optional[PlannedAction] = None,
        phase_coordinator: Optional[Any] = None,
        action_wave: Optional[ActionWave] = None,
    ) -> NavigationRequest:
        self.check_navigation_deadline()
        agent_id = self.physical_agent_id(robot)
        self.refresh_reachable_positions(agent_id)
        destination = self.find_object(
            dest_obj,
            agent_id=agent_id,
            require_center=True,
        )
        center = object_center(destination)
        if not center:
            raise RuntimeError(f"Object {dest_obj!r} has no usable center.")

        interaction_target, interaction_target_replaced = navigation_interaction_target(
            dest_obj,
            next_action,
        )

        interaction_destination = destination
        interaction_center = center
        if interaction_target_replaced:
            interaction_destination = self.find_object(
                interaction_target,
                agent_id=agent_id,
                require_center=True,
            )
            interaction_center = object_center(interaction_destination)
            if not interaction_center:
                raise RuntimeError(
                    f"Interaction object {interaction_target!r} has no usable center."
                )
            self.navigation_metrics.increment("interaction_target_substitutions")

        try:
            candidate_positions = self.teleport_candidate_positions(
                interaction_center,
                agent_id=agent_id,
                include_agent_positions=False,
            )[:TELEPORT_CANDIDATE_LIMIT]
        except RuntimeError as exc:
            raise_if_execution_aborted(self, exc)
            raise NoInteractionPoseError(
                "NO_INTERACTION_POSE: no reachable candidate for agent "
                f"{agent_id} target {interaction_target!r}"
            ) from exc
        if not candidate_positions:
            raise NoInteractionPoseError(
                "NO_INTERACTION_POSE: no reachable candidate for agent "
                f"{agent_id} target {interaction_target!r}"
            )
        object_resource = (
            str(destination.get("objectId"))
            if destination.get("objectId")
            else None
        )
        interaction_object_resource = (
            str(interaction_destination.get("objectId"))
            if interaction_destination.get("objectId")
            else None
        )
        return NavigationRequest(
            robot=robot,
            agent_id=agent_id,
            dest_obj=dest_obj,
            destination=dict(destination),
            center=dict(center),
            candidate_positions=tuple(
                dict(position) for position in candidate_positions
            ),
            object_resource=object_resource,
            next_action=next_action,
            phase_coordinator=phase_coordinator,
            action_wave=action_wave,
            interaction_target=interaction_target,
            interaction_destination=dict(interaction_destination),
            interaction_center=dict(interaction_center),
            interaction_object_resource=interaction_object_resource,
            interaction_target_replaced=interaction_target_replaced,
        )

    def navigate_to_object(
        self,
        robot: RobotRef,
        dest_obj: Any,
        *,
        allow_hand_preparation: bool = True,
        next_action: Optional[PlannedAction] = None,
        phase_coordinator: Optional[Any] = None,
        action_wave: Optional[ActionWave] = None,
        exclude_current_position: bool = False,
    ) -> Dict[str, Any]:
        self.navigation_metrics.record_request_started()
        try:
            navigation_interaction_target(dest_obj, next_action)
            if allow_hand_preparation:
                self.prepare_hand_for_goto_if_needed(robot, next_action)
            request = self.build_navigation_request(
                robot,
                dest_obj,
                next_action=next_action,
                phase_coordinator=phase_coordinator,
                action_wave=action_wave,
            )
            if exclude_current_position:
                current_key = position_to_grid_key(
                    self.current_agent_position(request.agent_id)
                )
                candidate_positions = tuple(
                    position
                    for position in request.candidate_positions
                    if position_to_grid_key(position) != current_key
                )
                if not candidate_positions:
                    raise NoInteractionPoseError(
                        "NO_INTERACTION_POSE: interaction reposition has no "
                        f"candidate away from agent {request.agent_id}'s current pose"
                    )
                request = replace(
                    request,
                    candidate_positions=candidate_positions,
                )
            result = self.movement_strategy.navigate(request)
            log(f"Reached: {dest_obj}")
            self.record_operated_object_name(result.destination)
        except NavigationDeferred:
            raise
        except Exception as exc:
            raise_if_execution_aborted(self, exc)
            self.navigation_metrics.record_request_failed()
            raise
        self.navigation_metrics.record_request_succeeded()
        return dict(result.destination)

    @navigation_operation
    def face_position(
        self,
        agent_id: int,
        target: Dict[str, float],
        *,
        retry_on_failure: Optional[bool] = None,
    ) -> None:
        metadata = self.agent_event(agent_id).metadata
        agent = metadata.get("agent", {})
        position = agent.get("position")
        rotation = agent.get("rotation", {})
        if not position:
            return
        target_yaw = yaw_to_face(position, target)
        if target_yaw is None:
            return
        current_yaw = float(rotation.get("y", 0.0))
        delta = shortest_yaw_delta(target_yaw, current_yaw)
        if abs(delta) < 1e-3:
            return
        action = "RotateRight" if delta > 0 else "RotateLeft"
        self.step(
            {"action": action, "degrees": abs(delta), "agentId": agent_id},
            retry_on_failure=retry_on_failure,
        )

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
        return self._get_object_interactor().object_action(action, robot, obj_name, force_action=force_action, extra_object_resources=extra_object_resources, action_parameters=action_parameters)

    def teleport_object_to_hand(self, robot: RobotRef, obj_name: Any):
        return self._get_object_interactor().teleport_object_to_hand(robot, obj_name)

    def pickup_clip_backoff_position(
        self,
        agent_id: int,
        distance: float,
    ) -> Dict[str, float]:
        return self._get_object_interactor().pickup_clip_backoff_position(agent_id, distance)

    @navigation_operation
    def retry_pickup_after_clip_error(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        return self._get_object_interactor().retry_pickup_after_clip_error(agent_id, payload, initial_event)

    def camera_horizon(
        self,
        agent_id: int,
        event: Optional[Any] = None,
    ) -> Optional[float]:
        return self._get_object_interactor().camera_horizon(agent_id, event)

    def try_look_to_camera_horizon(
        self,
        agent_id: int,
        target_horizon: float,
        *,
        action_name: str = "PickupObject",
    ) -> bool:
        return self._get_object_interactor().try_look_to_camera_horizon(agent_id, target_horizon, action_name=action_name)

    @navigation_operation
    def retry_object_action_after_target_visibility_error(
        self,
        action: str,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        return self._get_object_interactor().retry_object_action_after_target_visibility_error(action, agent_id, payload, initial_event)

    def retry_slice_after_interaction_reposition(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        return self._get_object_interactor().retry_slice_after_interaction_reposition(agent_id, payload, initial_event)

    def retry_pickup_after_target_visibility_error(
        self,
        agent_id: int,
        payload: Dict[str, Any],
        initial_event: Optional[Any],
    ) -> Optional[Any]:
        return self._get_object_interactor().retry_pickup_after_target_visibility_error(agent_id, payload, initial_event)

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
        return self._get_object_interactor().object_action_by_object(action, agent_id, obj, force_action=force_action, extra_object_resources=extra_object_resources, action_parameters=action_parameters, goal_object_name=goal_object_name)

    def throw_object(self, robot: RobotRef, move_magnitude: float = 7):
        return self._get_object_interactor().throw_object(robot, move_magnitude)

    def toggle_objects(self, action: str, robot: RobotRef, obj_name: Any) -> None:
        return self._get_object_interactor().toggle_objects(action, robot, obj_name)

    def goal_satisfied(self, goal: Dict[str, Any]) -> bool:
        goal_spec = GoalSpec.from_value(goal)
        context = getattr(self, "evaluation_context", None)
        if context is None or goal_spec not in context.goals:
            context = EvaluationContext.from_goals([goal_spec])
        return context.evaluate_goal(self, goal_spec)["status"] == "satisfied"

    def evaluate(self, goals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        if not getattr(self, "execution_quiescent", True):
            raise ExecutionShutdownTimeout("cannot evaluate a runtime with active workers")
        context = getattr(self, "evaluation_context", None)
        if context is None:
            context = EvaluationContext.from_goals(goals)
            self.evaluation_context = context
        elif not context.matches_goals(goals):
            raise ValueError("Evaluation goals do not match this runtime's fixed goals.")
        return context.evaluate(self)

    def unmet_goals(self, goals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [dict(goal) for goal in goals if not self.goal_satisfied(goal)]

    def log_unmet_goals(self, goals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        missing_goals = self.unmet_goals(goals)
        if not missing_goals:
            log("Unmet goals: none")
            return missing_goals

        log("Unmet goals:")
        for goal in missing_goals:
            log(f"- {format_goal(goal)}")
        return missing_goals

    def generate_video(self) -> None:
        return self._get_artifacts().generate_video()

    def stop(self) -> None:
        if self._get_controller_client().stop():
            self._get_artifacts().close_windows()
