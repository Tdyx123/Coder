"""Thin AI2-THOR adapter at the controller.step boundary."""

from typing import Any, Dict, Optional, Sequence, Set, Tuple


class ThorAdapter:
    """Delegates serialized low-level execution to the runtime's THOR helpers."""

    def __init__(self, runtime: "ThorRuntime") -> None:
        self.runtime = runtime

    def step(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool,
        save_frame: bool,
    ) -> Any:
        return self.runtime._step_direct(
            payload,
            check_success=check_success,
            save_frame=save_frame,
        )

    def move_to_position_chunk(
        self,
        agent_id: int,
        target_position: Dict[str, float],
        *,
        chunk_steps: int,
    ) -> bool:
        return self.runtime.move_to_position_chunk(
            agent_id,
            target_position,
            chunk_steps=chunk_steps,
        )

    def teleport_to_first_working_candidate(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        search_center: Dict[str, float],
        max_retries: int,
        excluded_grid_keys: Set[Tuple[int, int]],
        restrict_to_candidate_positions: bool,
    ) -> Dict[str, float]:
        return self.runtime.teleport_to_first_working_candidate(
            agent_id,
            candidate_positions,
            search_center=search_center,
            max_retries=max_retries,
            excluded_grid_keys=excluded_grid_keys,
            restrict_to_candidate_positions=restrict_to_candidate_positions,
        )

    def teleport_and_face_first_working_candidate(
        self,
        agent_id: int,
        candidate_positions: Sequence[Dict[str, float]],
        *,
        face_target: Dict[str, float],
        search_center: Dict[str, float],
        max_retries: int,
        excluded_grid_keys: Set[Tuple[int, int]],
        restrict_to_candidate_positions: bool,
    ) -> Dict[str, float]:
        return self.runtime.teleport_and_face_first_working_candidate(
            agent_id,
            candidate_positions,
            face_target=face_target,
            search_center=search_center,
            max_retries=max_retries,
            excluded_grid_keys=excluded_grid_keys,
            restrict_to_candidate_positions=restrict_to_candidate_positions,
        )

    def teleport_completed_agent_to_free_position(
        self,
        blocker_agent_id: int,
        priority_agent_id: int,
        priority_target_position: Dict[str, float],
        *,
        protect_navigation_path: bool,
    ) -> None:
        self.runtime.teleport_completed_agent_to_free_position(
            blocker_agent_id,
            priority_agent_id,
            priority_target_position,
            protect_navigation_path=protect_navigation_path,
        )

    def handoff_held_object_direct(
        self,
        from_agent_id: int,
        to_agent_id: int,
        object_resource: str,
    ) -> Any:
        return self.runtime.handoff_held_object_direct(
            from_agent_id,
            to_agent_id,
            object_resource,
        )
