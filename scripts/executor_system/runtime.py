"""AI2-THOR runtime wrapper, navigation, object operations, and media output."""

import json
import os
import random
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .action_plan import PlannedAction
from .config import (
    AGENT_CLEARANCE_DISTANCE,
    DIRECTIONAL_VIEW_NAMES,
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
from .demo_state import ground_truth_lock, verified_ground_truth_goal_signatures
from .dependencies import CloudRendering, Controller, cv2, require_dependencies
from .goals import (
    contains_satisfied,
    format_goal,
    goal_signature,
    goal_state_verified,
    goal_states,
    record_verified_goal_state,
    state_satisfied,
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
    ) -> None:
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
        self.output_root = Path(__file__).resolve().parent
        self.frame_counter = 0
        self.total_exec = 0
        self.success_exec = 0
        self.missing_frame_warning_emitted = False
        self.controller = None
        self.controller_lock = threading.RLock()
        self.stats_lock = threading.Lock()
        self.operated_object_names: Set[str] = set()
        self.operated_object_names_lock = threading.Lock()
        self.agent_held_object_overrides: Dict[int, Set[str]] = {}
        self.agent_held_object_overrides_lock = threading.Lock()
        self.reachable_positions: List[Dict[str, float]] = []

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
        except Exception:
            self.stop()
            raise

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
            "visibilityDistance": 100,
            "fieldOfView": 90,
            "agentCount": self.physical_agent_count,
            "headless": self.controller_headless,
        }
        if self.cloud_rendering:
            controller_args["platform"] = CloudRendering
        return Controller(**controller_args)

    def print_agent_metadata(self, event) -> None:
        events = getattr(event, "events", None) or [event]
        for i, e in enumerate(events):
            print("agent index:", i)
            print("agentId:", e.metadata.get("agentId"))
            print("position:", e.metadata["agent"]["position"])

    def write_final_metadata(self) -> Path:
        with self.controller_lock:
            last_event = (
                None
                if self.controller is None
                else getattr(self.controller, "last_event", None)
            )
            metadata = getattr(last_event, "metadata", {}) or {}
        metadata_path = self.output_root / "metadata.txt"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return metadata_path

    def ensure_display(self) -> None:
        if self.cloud_rendering or not self.render_image or os.environ.get("DISPLAY"):
            return
        raise RuntimeError(
            "DISPLAY is required when CloudRendering=0 and renderImage=1. "
            "Automatic Xvfb fallback has been disabled."
        )

    def prepare_output_dirs(self) -> None:
        for path in self.output_root.glob("agent_*"):
            if path.is_dir():
                shutil.rmtree(path)
        for view_name in THIRD_PARTY_VIEW_NAMES + DIRECTIONAL_VIEW_NAMES:
            view_path = self.output_root / view_name
            if view_path.is_dir():
                shutil.rmtree(view_path)
        for video_path in self.output_root.glob("video_*.mp4"):
            if video_path.is_file():
                video_path.unlink()

        for i in range(self.physical_agent_count):
            (self.output_root / f"agent_{i + 1}").mkdir(parents=True, exist_ok=True)
        for view_name in THIRD_PARTY_VIEW_NAMES:
            (self.output_root / view_name).mkdir(parents=True, exist_ok=True)

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
        self.reachable_positions = event.metadata.get("actionReturn") or []
        if not self.reachable_positions:
            raise RuntimeError("AI2-THOR returned no reachable positions.")

        if self.top_view_enabled:
            if not self.cloud_rendering:
                log("Adding local top-view camera.")
                self.add_third_party_view(TOP_VIEW_NAME, self.local_top_view_camera_props())

        random.seed(0)
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
        copied_payload = dict(payload)
        copied_payload.pop("objectResources", None)
        requested_retry = check_success if retry_on_failure is None else retry_on_failure
        should_retry = bool(
            requested_retry and self.payload_allows_step_retry(copied_payload)
        )
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
            except BaseException:
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

    def _step_direct(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool = True,
        save_frame: bool = True,
    ):
        with self.controller_lock: 
            event = self.controller.step(dict(payload))
            if check_success:
                self.assert_success(event, payload)
            if save_frame:
                self.save_frames(event)
            return event

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

    def held_objects_description_for_log(self, agent_id: Any) -> str:
        try:
            held_objects = sorted(self.agent_held_objects_for(int(agent_id)))
        except (RuntimeError, TypeError, ValueError):
            held_objects = []
        return ", ".join(held_objects) if held_objects else "nothing"

    def object_name_for_log(self, obj: Dict[str, Any]) -> str:
        return stable_object_name(obj) or str(obj.get("objectId") or "")

    def object_id_name_for_log(self, agent_id: int, object_id: Any) -> str:
        object_id_text = str(object_id or "")
        for obj in self.current_objects(agent_id):
            if str(obj.get("objectId") or "") == object_id_text:
                return self.object_name_for_log(obj)
        return object_id_text.split("|", 1)[0] if object_id_text else ""

    def current_object_by_id(
        self,
        agent_id: int,
        object_id: Any,
    ) -> Optional[Dict[str, Any]]:
        object_id_text = str(object_id or "")
        for obj in self.current_objects(agent_id):
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
        desired_state = self.toggle_action_target_state(action)
        current_state = self.object_toggle_state(obj)
        return desired_state is not None and current_state is not None and desired_state == current_state

    def toggle_error_matches_desired_state(self, action: str, error: str) -> bool:
        error_text = str(error or "").lower()
        if action == "ToggleObjectOn":
            return "already on" in error_text
        if action == "ToggleObjectOff":
            return "already off" in error_text
        return False

    def held_object_names_for_log(self, agent_id: int) -> List[str]:
        return [
            self.object_id_name_for_log(agent_id, object_id)
            for object_id in sorted(self.agent_held_objects_for(agent_id))
        ]

    def log_put_object_failure_held_items(self, agent_id: Any) -> None:
        held_description = self.held_objects_description_for_log(agent_id)
        log(
            f"PutObject failed for agent {agent_id}; "
            f"currently holding: {held_description}."
        )

    def save_frames(self, event) -> None:
        events = list(getattr(event, "events", None) or [event])
        wrote_frame = False
        for i, agent_event in enumerate(events[: self.physical_agent_count]):
            frame = event_cv2_frame(agent_event)
            if frame is None:
                continue
            frame_path = self.output_root / f"agent_{i + 1}" / f"img_{self.frame_counter:05d}.png"
            if not cv2.imwrite(str(frame_path), frame):
                log(f"Warning: failed to write frame {frame_path}")
            else:
                wrote_frame = True
            if self.show_windows:
                cv2.imshow(f"agent{i}", frame)

        third_party_frames = self.event_third_party_camera_frames(event, events)
        for view_name, view_frame in zip(self.active_third_party_view_names(), third_party_frames):
            view_bgr = cv2.cvtColor(view_frame, cv2.COLOR_RGB2BGR)
            frame_path = self.output_root / view_name / f"img_{self.frame_counter:05d}.png"
            if not cv2.imwrite(str(frame_path), view_bgr):
                log(f"Warning: failed to write frame {frame_path}")
            else:
                wrote_frame = True
            if self.show_windows:
                cv2.imshow(view_name.replace("_", " ").title(), view_bgr)

        if self.render_image and not wrote_frame and not self.missing_frame_warning_emitted:
            metadata = getattr(event, "metadata", {}) or {}
            action = metadata.get("lastAction") or "<unknown>"
            log(
                "Warning: renderImage=1 but AI2-THOR returned no frame "
                f"for action {action}; no image was saved."
            )
            self.missing_frame_warning_emitted = True

        if self.show_windows:
            cv2.waitKey(25)
        self.frame_counter += 1

    def active_third_party_view_names(self) -> List[str]:
        view_names = getattr(self, "third_party_view_names", None)
        if view_names:
            return list(view_names)
        if getattr(self, "top_view_enabled", False):
            return [TOP_VIEW_NAME]
        return []

    def event_third_party_camera_frames(
        self,
        event: Any,
        events: Sequence[Any],
    ) -> List[Any]:
        frame_sources = [event]
        if events:
            frame_sources.append(events[0])
        for frame_source in frame_sources:
            third_party_frames = getattr(frame_source, "third_party_camera_frames", None)
            if third_party_frames:
                return list(third_party_frames)
        return []

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

    def _operated_object_name_state(self) -> Tuple[Set[str], threading.Lock]:
        names = getattr(self, "operated_object_names", None)
        if names is None:
            names = set()
            self.operated_object_names = names
        lock = getattr(self, "operated_object_names_lock", None)
        if lock is None:
            lock = threading.Lock()
            self.operated_object_names_lock = lock
        return names, lock

    def operated_object_names_snapshot(self) -> Set[str]:
        names, lock = self._operated_object_name_state()
        with lock:
            return set(names)

    def record_operated_object_name(self, obj: Dict[str, Any]) -> None:
        name = operated_object_name(obj)
        name_key = object_key(name)
        if not name_key:
            return
        log(f"Operated object name: {name}")
        names, lock = self._operated_object_name_state()
        with lock:
            names.add(name_key)

    def record_created_slice_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> None:
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
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
                self.record_operated_object_name(obj)

    def record_created_broken_egg_object_names(
        self,
        source_obj: Dict[str, Any],
        event: Any,
        known_object_ids: Set[str],
    ) -> None:
        metadata = getattr(event, "metadata", {}) or {}
        objects = metadata.get("objects") or []
        resource = (
            source_obj.get("objectId")
            or source_obj.get("objectType")
            or source_obj.get("name")
        )
        if not is_egg_query(resource):
            return
        source_id = str(source_obj.get("objectId") or "")
        for obj in objects:
            object_id = str(obj.get("objectId") or "")
            if not object_id:
                continue
            if object_id in known_object_ids and object_id != source_id:
                continue
            if is_broken_egg_object(obj):
                self.record_operated_object_name(obj)

    def object_name_was_operated(
        self,
        obj: Dict[str, Any],
        operated_object_names: Optional[Set[str]] = None,
    ) -> bool:
        names = (
            self.operated_object_names_snapshot()
            if operated_object_names is None
            else operated_object_names
        )
        return bool(operated_object_name_candidate_keys(obj) & names)

    def find_objects(self, pattern: Any, agent_id: Optional[int] = None) -> List[Dict[str, Any]]:
        matches = [obj for obj in self.current_objects(agent_id) if matches_object(pattern, obj)]
        operated_object_names = self.operated_object_names_snapshot()
        sliceable_key = sliceable_food_query_key(pattern)
        egg_query = is_egg_query(pattern)
        if agent_id is not None:
            if sliceable_key is not None:
                matches.sort(
                    key=lambda obj: (
                        operated_sliced_food_query_rank(
                            pattern,
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
                        not is_broken_egg_object(obj),
                        not self.object_name_was_operated(obj, operated_object_names),
                        not bool(obj.get("visible", False)),
                        object_distance(obj),
                        obj.get("objectId", ""),
                    )
                )
            else:
                matches.sort(
                    key=lambda obj: (
                        not self.object_name_was_operated(obj, operated_object_names),
                        not bool(obj.get("visible", False)),
                        object_distance(obj),
                        obj.get("objectId", ""),
                    )
                )
        elif sliceable_key is not None:
            def global_sliceable_sort_key(obj: Dict[str, Any]) -> Tuple[int, float, str]:
                rank = operated_sliced_food_query_rank(
                    pattern,
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
                    not is_broken_egg_object(obj),
                    not self.object_name_was_operated(obj, operated_object_names),
                    not bool(obj.get("visible", False)),
                    object_distance(obj),
                    obj.get("objectId", ""),
                )
            )
        else:
            matches.sort(
                key=lambda obj: (
                    not self.object_name_was_operated(obj, operated_object_names),
                )
            )
        return matches

    def find_object(
        self,
        pattern: Any,
        *,
        agent_id: Optional[int] = None,
        require_center: bool = False,
    ) -> Dict[str, Any]:
        matches = self.find_objects(pattern, agent_id)
        if not matches:
            raise RuntimeError(f"Could not find AI2-THOR object matching {pattern!r}")
        if require_center:
            for obj in matches:
                if object_center(obj):
                    return obj
            raise RuntimeError(f"Object {pattern!r} has no usable center.")
        return matches[0]

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
        if not candidates:
            raise RuntimeError(
                f"No reachable teleport candidate for agent {agent_id} near {target}."
            )

        target_tuple = position_to_tuple(target)
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
        except RuntimeError:
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
        except RuntimeError:
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
        except RuntimeError:
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
            self._step_direct(
                {"action": action, "degrees": abs(delta), "agentId": agent_id},
                check_success=True,
            )

        event = self._step_direct(
            {
                "action": "MoveAhead",
                "moveMagnitude": NAVIGATION_GRID_SIZE,
                "agentId": agent_id,
            },
            check_success=False,
        )
        return not step_event_failed(event)

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
                    except RuntimeError:
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

    def handoff_held_object_direct(
        self,
        from_agent_id: int,
        to_agent_id: int,
        object_resource: str,
    ):
        log(
            "Transferring held object "
            f"{object_resource} from agent {from_agent_id} to agent {to_agent_id}."
        )
        drop_event = self._step_direct(
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

        dropped = self.find_object(object_resource, agent_id=to_agent_id, require_center=True)
        center = object_center(dropped)
        if not center:
            raise RuntimeError(f"Dropped object {object_resource!r} has no usable center.")
        target_position = self.closest_reachable(center, agent_id=to_agent_id)
        try:
            if from_agent_id in self.navigation_blockers(to_agent_id, target_position):
                self.teleport_completed_agent_to_free_position(
                    from_agent_id,
                    to_agent_id,
                    target_position,
                )
        except RuntimeError:
            pass
        self.move_to_position_direct(
            to_agent_id,
            target_position,
        )
        self.face_position_direct(to_agent_id, center)
        pickup_event = self._step_direct(
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
        return object_resource in self.agent_held_objects_for(agent_id)

    def agent_held_object_matching(self, agent_id: int, pattern: Any) -> Optional[str]:
        for object_resource in self.agent_held_objects_for(agent_id):
            stub = {
                "objectId": object_resource,
                "objectType": object_resource.split("|", 1)[0],
            }
            if matches_object(pattern, stub):
                return object_resource
        return None

    def agent_held_objects_for(self, agent_id: int) -> Set[str]:
        held_objects = self.agent_held_object_overrides_snapshot(agent_id)
        held_objects.update(self.metadata_held_objects(agent_id))
        return held_objects

    def _held_object_override_state(self) -> Tuple[Dict[int, Set[str]], threading.Lock]:
        overrides = getattr(self, "agent_held_object_overrides", None)
        if overrides is None:
            overrides = {}
            self.agent_held_object_overrides = overrides
        lock = getattr(self, "agent_held_object_overrides_lock", None)
        if lock is None:
            lock = threading.Lock()
            self.agent_held_object_overrides_lock = lock
        return overrides, lock

    def agent_held_object_overrides_snapshot(self, agent_id: int) -> Set[str]:
        overrides, lock = self._held_object_override_state()
        with lock:
            return set(overrides.get(agent_id, set()))

    def record_agent_held_object(self, agent_id: int, object_id: str) -> None:
        if not object_id:
            return
        overrides, lock = self._held_object_override_state()
        with lock:
            overrides.setdefault(agent_id, set()).add(str(object_id))

    def release_agent_held_objects(self, agent_id: int) -> None:
        overrides, lock = self._held_object_override_state()
        with lock:
            overrides.setdefault(agent_id, set()).clear()

    def metadata_held_objects(self, agent_id: int) -> Set[str]:
        held_objects = set()
        with self.controller_lock:
            event = self._agent_event_unlocked(agent_id)
            metadata = getattr(event, "metadata", {}) or {}
            inventory_objects = metadata.get("inventoryObjects") or []
        for obj in inventory_objects:
            if not isinstance(obj, dict):
                continue
            object_id = obj.get("objectId")
            if object_id:
                held_objects.add(str(object_id))
        return held_objects

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
                last_error = f"Rotate failed at {selected_position}: {exc}"
                excluded_keys.add(selected_key)
                log(
                    "Teleport+face candidate "
                    f"{selected_position} failed while facing target: {exc}; "
                    "trying another reachable position."
                )
                continue

            return dict(selected_position)

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
        if next_action is None or next_action.name != "PickupObject":
            return
        pickup_target = next_action.args[0] if next_action.args else None
        agent_id = self.physical_agent_id(robot)
        if not self.agent_held_objects_for(agent_id):
            return
        if pickup_target is not None and self.agent_held_object_matching(
            agent_id,
            pickup_target,
        ):
            return
        self.place_held_objects_for_pickup(robot, pickup_target)

    def prepare_hand_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
        target_obj: Dict[str, Any],
    ) -> Dict[str, Any]:
        agent_id = self.physical_agent_id(robot)
        if not self.agent_held_objects_for(agent_id):
            return target_obj

        original_position = dict(self.current_agent_position(agent_id))
        target_object_id = str(target_obj.get("objectId") or "")
        can_return_to_target = bool(target_object_id and object_center(target_obj))

        self.place_held_objects_for_pickup(robot, pickup_target)

        if can_return_to_target:
            try:
                return self.navigate_to_object(
                    robot,
                    target_object_id,
                    allow_hand_preparation=False,
                )
            except RuntimeError as exc:
                log(
                    "Could not return directly to pickup target "
                    f"{target_object_id}: {exc}; returning to prior position."
                )

        self.teleport_to_position(agent_id, original_position)
        return self.find_object(pickup_target, agent_id=agent_id)

    def place_held_objects_for_pickup(
        self,
        robot: RobotRef,
        pickup_target: Any,
    ) -> None:
        agent_id = self.physical_agent_id(robot)
        held_objects = sorted(self.agent_held_objects_for(agent_id))
        if not held_objects:
            return

        held_object = held_objects[0]
        held_object_type = self.held_object_type(agent_id, held_object)
        candidates = self.compatible_receptacle_candidates(
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

        last_error = "no candidate was attempted"
        for receptacle in candidates:
            receptacle_id = str(receptacle.get("objectId"))
            try:
                self.navigate_to_object(
                    robot,
                    receptacle_id,
                    allow_hand_preparation=False,
                )
                event = self.put_held_object_in_receptacle(agent_id, receptacle)
            except RuntimeError as exc:
                last_error = str(exc)
                log(
                    f"Could not place held object {held_object} into "
                    f"{receptacle_id}: {last_error}"
                )
                continue

            if not step_event_failed(event):
                log(f"Placed held object {held_object} into {receptacle_id}.")
                return

            metadata = getattr(event, "metadata", {}) or {}
            last_error = metadata.get("errorMessage") or "PutObject failed"
            log(
                f"Could not place held object {held_object} into "
                f"{receptacle_id}: {last_error}"
            )

        raise RuntimeError(
            "Could not place held object "
            f"{held_object} ({held_object_type}) before PickupObject "
            f"{pickup_target!r} for agent {agent_id}: {last_error}"
        )

    def held_object_type(self, agent_id: int, held_object: str) -> str:
        for obj in self.current_objects(agent_id):
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
        allowed_type_keys = self.allowed_receptacle_type_keys(held_object_type)
        if not allowed_type_keys:
            return []

        candidates = []
        for obj in self.current_objects(agent_id):
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
        held_names = self.held_object_names_for_log(agent_id)
        put_name = ", ".join(held_names) if held_names else "nothing"
        log(f"PutObject names: {put_name}, {self.object_name_for_log(receptacle)}")
        payload = {
            "action": "PutObject",
            "objectId": receptacle["objectId"],
            "agentId": agent_id,
            "forceAction": False,
            "objectResources": [
                *self.agent_held_objects_for(agent_id),
                receptacle["objectId"],
            ],
        }
        with self.stats_lock:
            self.total_exec += 1
        event = self.step(
            payload,
            check_success=False,
            retry_on_failure=False,
            max_retries=0,
        )
        if not step_event_failed(event):
            self.release_agent_held_objects(agent_id)
            with self.stats_lock:
                self.success_exec += 1
            self.record_operated_object_name(receptacle)
        return event

    def held_item_rotation_failure(self, exc: BaseException) -> bool:
        message = str(exc).lower()
        if "held item" not in message:
            return False
        return "rotateright failed" in message or "rotateleft failed" in message

    def navigate_to_object(
        self,
        robot: RobotRef,
        dest_obj: Any,
        *,
        allow_hand_preparation: bool = True,
        next_action: Optional[PlannedAction] = None,
        phase_coordinator: Optional[Any] = None,
    ) -> Dict[str, Any]:
        agent_id = self.physical_agent_id(robot)
        if allow_hand_preparation:
            self.prepare_hand_for_goto_if_needed(robot, next_action)
        self.refresh_reachable_positions(agent_id)
        dest = self.find_object(dest_obj, agent_id=agent_id, require_center=True)
        center = object_center(dest)
        if not center:
            raise RuntimeError(f"Object {dest_obj!r} has no usable center.")

        candidate_positions = self.teleport_candidate_positions(
            center,
            agent_id=agent_id,
            include_agent_positions=False,
        )[:TELEPORT_CANDIDATE_LIMIT]
        if phase_coordinator is not None:
            phase_coordinator.wait_until_goto_candidates_clear(
                agent_id,
                candidate_positions,
            )
        target_position = candidate_positions[0]
        object_resource = str(dest.get("objectId")) if dest.get("objectId") else None
        log(
            f"Going to {dest_obj} {dest.get('objectId')} "
            f"at reachable position {target_position}."
        )
        self.teleport_and_face_candidate_positions(
            agent_id,
            candidate_positions,
            face_target=center,
            object_resource=object_resource,
            search_center=center,
            restrict_to_candidate_positions=True,
        )

        log(f"Reached: {dest_obj}")
        self.record_operated_object_name(dest)
        return dest

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
        agent_id = self.physical_agent_id(robot)
        if action == "PickupObject":
            held_object = self.agent_held_object_matching(agent_id, obj_name)
            if held_object is not None:
                log(
                    "PickupObject name: "
                    f"{self.object_id_name_for_log(agent_id, held_object)}"
                )
                log(f"Skipping PickupObject for agent {agent_id}; already holding {held_object}")
                with self.stats_lock:
                    self.total_exec += 1
                    self.success_exec += 1
                return self.agent_event(agent_id)
            obj = self.find_object(obj_name, agent_id=agent_id)
            if self.agent_held_objects_for(agent_id):
                obj = self.prepare_hand_for_pickup(robot, obj_name, obj)
        else:
            obj = self.find_object(obj_name, agent_id=agent_id)
        if action == "PickupObject":
            log(f"PickupObject name: {self.object_name_for_log(obj)}")
        elif action == "PutObject":
            held_names = self.held_object_names_for_log(agent_id)
            put_name = ", ".join(held_names) if held_names else "nothing"
            log(f"PutObject names: {put_name}, {self.object_name_for_log(obj)}")
        log(f"{action} {obj_name} -> {operated_object_name(obj)} {obj.get('objectId')}")
        return self.object_action_by_object(
            action,
            agent_id,
            obj,
            force_action=force_action,
            extra_object_resources=extra_object_resources,
            action_parameters=action_parameters,
            goal_object_name=obj_name,
        )

    def teleport_object_to_hand(self, robot: RobotRef, obj_name: Any):
        agent_id = self.physical_agent_id(robot)
        held_object = self.agent_held_object_matching(agent_id, obj_name)
        if held_object is not None:
            log(
                "Skipping TeleportObjectToHand for agent "
                f"{agent_id}; already holding {held_object}"
            )
            with self.stats_lock:
                self.total_exec += 1
                self.success_exec += 1
            return self.agent_event(agent_id)

        held_objects = sorted(self.agent_held_objects_for(agent_id))
        if held_objects:
            held_description = ", ".join(held_objects)
            raise RuntimeError(
                f"Cannot TeleportObjectToHand {obj_name!r} for agent {agent_id}: "
                "robot hand is not empty. "
                f"Currently holding: {held_description}."
            )

        obj = self.find_object(obj_name, agent_id=agent_id)
        log(
            "TeleportObjectToHand "
            f"{obj_name} -> {operated_object_name(obj)} {obj.get('objectId')}"
        )
        return self.object_action_by_object(
            "PickupObject",
            agent_id,
            obj,
            force_action=True,
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
        payload = {"action": action, "objectId": obj["objectId"], "agentId": agent_id}
        if action_parameters:
            payload.update(action_parameters)
        object_resources = [
            *[str(resource) for resource in extra_object_resources if resource],
            str(obj["objectId"]),
        ]
        payload["objectResources"] = list(dict.fromkeys(object_resources))
        if force_action:
            payload["forceAction"] = True
        if action == "PickupObject" and self.agent_holds_object(agent_id, obj["objectId"]):
            log(f"Skipping PickupObject for agent {agent_id}; already holding {obj['objectId']}")
            with self.stats_lock:
                self.total_exec += 1
                self.success_exec += 1
            self.record_operated_object_name(obj)
            return self.agent_event(agent_id)
        if action in {"ToggleObjectOn", "ToggleObjectOff"} and self.toggle_state_matches(action, obj):
            desired_state = "on" if action == "ToggleObjectOn" else "off"
            log(
                f"Skipping {action} for agent {agent_id}; "
                f"{obj['objectId']} is already {desired_state}"
            )
            with self.stats_lock:
                self.total_exec += 1
                self.success_exec += 1
            self.record_operated_object_name(obj)
            return self.agent_event(agent_id)

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
                for current in self.current_objects(agent_id)
                if current.get("objectId")
            }

        with self.stats_lock:
            self.total_exec += 1
        event = self.step(
            payload,
            check_success=False,
            retry_on_failure=False,
        )
        metadata = getattr(event, "metadata", {}) or {}
        error = metadata.get("errorMessage")
        if not metadata.get("lastActionSuccess", not bool(error)):
            if action == "PutObject":
                self.log_put_object_failure_held_items(agent_id)
            if action in {"ToggleObjectOn", "ToggleObjectOff"}:
                observed_obj = self.current_object_by_id(agent_id, obj["objectId"])
                if (
                    observed_obj is not None
                    and self.toggle_error_matches_desired_state(action, error or "")
                    and self.toggle_state_matches(action, observed_obj)
                ):
                    with self.stats_lock:
                        self.success_exec += 1
                    self.record_operated_object_name(observed_obj)
                    return event
            raise RuntimeError(
                f"{action} failed for agent {agent_id} on {obj['objectId']}: "
                f"{error or 'no error message returned'}"
            )
        with self.stats_lock:
            self.success_exec += 1
        if action == "PickupObject":
            self.record_agent_held_object(agent_id, str(obj["objectId"]))
        elif action in {"PutObject", "ThrowObject"}:
            self.release_agent_held_objects(agent_id)
        if action == "SliceObject":
            self.record_created_slice_object_names(obj, event, known_object_ids)
            sliced_goal_name = (
                goal_object_name
                if goal_object_name is not None
                else stable_object_name(obj)
            )
            record_verified_goal_state(
                sliced_goal_name,
                "SLICED",
            )
        if breaks_egg:
            self.record_created_broken_egg_object_names(obj, event, known_object_ids)
            broken_goal_name = (
                goal_object_name
                if goal_object_name is not None
                else stable_object_name(obj)
            )
            record_verified_goal_state(broken_goal_name, "BROKEN")
        self.record_operated_object_name(obj)
        return event

    def throw_object(self, robot: RobotRef, move_magnitude: float = 7):
        agent_id = self.physical_agent_id(robot)
        held_objects = sorted(self.agent_held_objects_for(agent_id))
        with self.stats_lock:
            self.total_exec += 1
        event = self.step(
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
        with self.stats_lock:
            self.success_exec += 1
        self.release_agent_held_objects(agent_id)
        return event

    def toggle_objects(self, action: str, robot: RobotRef, obj_name: Any) -> None:
        agent_id = self.physical_agent_id(robot)
        matches = self.find_objects(obj_name, agent_id=agent_id)
        if not matches:
            raise RuntimeError(f"Could not find switchable object {obj_name!r}")
        obj = matches[0]
        force_action = object_key(obj.get("objectType") or "") == "stoveknob"
        return self.object_action_by_object(
            action,
            agent_id,
            obj,
            force_action=force_action,
        )

    def goal_satisfied(self, goal: Dict[str, Any]) -> bool:
        with ground_truth_lock:
            if goal_signature(goal) in verified_ground_truth_goal_signatures:
                return True

        obj_name = goal.get("name")
        states = [str(state).upper() for state in goal_states(goal)]
        verified_states = {
            state for state in states if goal_state_verified(obj_name, state)
        }
        contains = goal.get("contains") or []
        if states and all(state in verified_states for state in states) and not contains:
            return True

        candidates = [obj for obj in self.current_objects() if matches_object(obj_name, obj)]

        for obj in candidates:
            if states and not all(
                state in verified_states or state_satisfied(obj, state)
                for state in states
            ):
                continue
            if contains and not contains_satisfied(obj, contains):
                continue
            return True
        return False

    def evaluate(self, goals: Sequence[Dict[str, Any]]) -> Dict[str, float]:
        goals = list(goals)
        goal_count = len(goals)
        complete_count = sum(1 for goal in goals if self.goal_satisfied(goal))
        gcr = 1.0 if goal_count == 0 else complete_count / goal_count
        tc = 1.0 if complete_count == goal_count else 0.0
        with self.stats_lock:
            total_exec = self.total_exec
            success_exec = self.success_exec
        exec_rate = 1.0 if total_exec == 0 else success_exec / total_exec
        ru = 1.0
        sr = 1.0 if tc == 1.0 and ru == 1.0 else 0.0
        return {"sr": sr, "tc": tc, "gcr": gcr, "exec_rate": exec_rate, "ru": ru}

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
        if shutil.which("ffmpeg") is None:
            log("ffmpeg not found; skipping video generation.")
            return

        frame_rate = 5
        view_folders = [self.output_root / view_name for view_name in THIRD_PARTY_VIEW_NAMES]
        for imgs_folder in sorted(self.output_root.glob("agent_*")) + view_folders:
            if not imgs_folder.is_dir() or not any(imgs_folder.glob("img_*.png")):
                continue
            view = imgs_folder.name
            command_set = [
                "ffmpeg",
                "-y",
                "-framerate",
                str(frame_rate),
                "-i",
                str(imgs_folder / "img_%05d.png"),
                "-pix_fmt",
                "yuv420p",
                str(self.output_root / f"video_{view}.mp4"),
            ]
            result = subprocess.run(
                command_set,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                log(f"Warning: ffmpeg failed for {view}: {result.stderr.strip()}")

    def stop(self) -> None:
        if self.show_windows and cv2 is not None:
            cv2.destroyAllWindows()
        with self.controller_lock:
            if self.controller is not None:
                self.controller.stop()
                self.controller = None
