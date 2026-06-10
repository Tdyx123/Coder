"""Robot-level plan executor.

Each Executor owns one robot's action queue.  Phase-level code may run several
Executor instances concurrently; ThorRuntime remains responsible for serializing
the actual controller.step boundary with its controller lock.
"""

import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .action_plan import (
    ACTION_FAILED,
    ACTION_SUCCESS,
    FAILURE_RETRY,
    FAILURE_SKIP,
    FAILURE_WAIT_AND_RETRY,
    Action,
    ActionResult,
    AI2ThorAdapter,
    ExecutionLogger,
    RobotExecutionState,
    WorldState,
    ROBOT_ACTION_FAILED,
    ROBOT_ACTION_SUCCESS,
    ROBOT_BLOCKED,
    ROBOT_EXECUTING,
    ROBOT_FINISHED_STAGE,
    ROBOT_WAITING_CONDITION,
)


class PhaseCoordinator:
    """Shared state for robot executors running in the same phase."""

    def __init__(self, runtime: Any, active_agent_ids: Sequence[int]) -> None:
        self.runtime = runtime
        self.condition = threading.Condition()
        self.active_agent_ids: Set[int] = {int(agent_id) for agent_id in active_agent_ids}
        agent_count = max(
            len(self.active_agent_ids),
            int(getattr(runtime, "physical_agent_count", len(self.active_agent_ids)) or 0),
        )
        self.all_agent_ids: Set[int] = set(range(agent_count))
        self.completed_agent_ids: Set[int] = set(self.all_agent_ids - self.active_agent_ids)
        self.failed_agent_errors: Dict[int, BaseException] = {}
        self.relocating_agent_ids: Set[int] = set()

    def mark_agent_done(self, agent_id: int) -> None:
        with self.condition:
            self.completed_agent_ids.add(int(agent_id))
            self.condition.notify_all()

    def mark_agent_failed(self, agent_id: int, exc: BaseException) -> None:
        with self.condition:
            self.failed_agent_errors[int(agent_id)] = exc
            self.condition.notify_all()

    def wait_until_goto_candidates_clear(
        self,
        agent_id: int,
        candidate_positions: Sequence[dict],
    ) -> None:
        protected_positions = [dict(position) for position in candidate_positions]
        if not protected_positions:
            return

        while True:
            blockers = self.runtime.agent_blocker_ids_for_positions(
                protected_positions,
                int(agent_id),
            )
            with self.condition:
                self._raise_if_failed_locked()
                blockers = set(blockers)
                if not blockers:
                    return

                completed_blockers = (
                    blockers
                    & self.completed_agent_ids
                    - self.relocating_agent_ids
                )
                if completed_blockers:
                    blocker_agent_id = min(completed_blockers)
                    self.relocating_agent_ids.add(blocker_agent_id)
                else:
                    self.condition.wait()
                    continue

            try:
                self.runtime.teleport_completed_agent_away_from_positions(
                    blocker_agent_id,
                    int(agent_id),
                    protected_positions,
                )
            finally:
                with self.condition:
                    self.relocating_agent_ids.discard(blocker_agent_id)
                    self.condition.notify_all()

    def _raise_if_failed_locked(self) -> None:
        if not self.failed_agent_errors:
            return
        agent_id = min(self.failed_agent_errors)
        exc = self.failed_agent_errors[agent_id]
        raise RuntimeError(f"blocking agent {agent_id} failed: {exc}") from exc


class Executor:
    """Execute one robot's plan queue sequentially."""

    def __init__(
        self,
        runtime: Any,
        robot_id: Optional[str] = None,
        actions: Sequence[Action] = (),
        *,
        stage_id: str = "stage",
        logger: Optional[ExecutionLogger] = None,
        phase_coordinator: Optional[PhaseCoordinator] = None,
    ) -> None:
        self.runtime = runtime
        self.robot_id = str(robot_id) if robot_id is not None else ""
        self.state = RobotExecutionState(
            robot_id=self.robot_id,
            current_stage_id=str(stage_id),
            action_queue=[
                Action.from_any(action).with_robot(self.robot_id)
                for action in actions
            ],
        )
        self.adapter = AI2ThorAdapter(runtime)
        self.logger = logger or ExecutionLogger()
        self.world_state = WorldState(runtime)
        self.closed = False
        self.active_agent_ids = set()
        self.agent_phase_done = set()
        self.phase_coordinator = phase_coordinator

    def start(self) -> None:
        """Compatibility hook for the removed central worker."""

        self.closed = False

    def stop(self) -> None:
        """Compatibility hook for the removed central worker."""

        self.closed = True

    def set_active_agents(self, agent_ids: Any) -> None:
        self.active_agent_ids = set(agent_ids or set())

    def mark_agent_phase_done(self, agent_id: int) -> None:
        self.agent_phase_done.add(agent_id)

    def execute(self) -> WorldState:
        if not self.robot_id:
            raise RuntimeError("Executor requires a robot_id to execute an action queue.")

        agent_id = self.runtime.physical_agent_id(self.robot_id)
        try:
            return self._execute_queue()
        except BaseException as exc:
            if self.phase_coordinator is not None:
                self.phase_coordinator.mark_agent_failed(agent_id, exc)
            raise

    def _execute_queue(self) -> WorldState:
        if not self.robot_id:
            raise RuntimeError("Executor requires a robot_id to execute an action queue.")

        tick = 0
        while not self.state.finished():
            action = self.state.next_action()
            if action is None:
                break

            self.world_state.tick = tick
            self.world_state.refresh([self.state])
            self.wait_for_condition(action)
            self.state.status = ROBOT_EXECUTING
            try:
                event = self.execute_action(action)
            except BaseException as exc:
                if self.handle_failure(action, exc, tick):
                    tick += 1
                continue

            result = ActionResult(self.robot_id, action, ACTION_SUCCESS, event=event)
            self.state.last_action_result = result
            self.state.action_cursor += 1
            self.state.wait_ticks = 0
            self.state.status = (
                ROBOT_FINISHED_STAGE
                if self.state.finished()
                else ROBOT_ACTION_SUCCESS
            )
            self.logger.result(tick, result)
            tick += 1

        self.state.status = ROBOT_FINISHED_STAGE
        self.world_state.refresh([self.state])
        if self.phase_coordinator is not None:
            self.phase_coordinator.mark_agent_done(
                self.runtime.physical_agent_id(self.robot_id)
            )
        return self.world_state

    def wait_for_condition(self, action: Action) -> None:
        if action.wait_until is None:
            return

        while True:
            self.world_state.refresh([self.state])
            if action.wait_until(self.world_state):
                return
            if (
                action.timeout_ticks is not None
                and self.state.wait_ticks >= action.timeout_ticks
            ):
                raise RuntimeError(
                    f"{self.robot_id} timed out waiting for {action.action_type}."
                )
            self.state.wait_ticks += 1
            self.state.status = ROBOT_WAITING_CONDITION
            agent_id = self.runtime.physical_agent_id(self.robot_id)
            self.runtime.step(
                {"action": "Pass", "agentId": agent_id},
                check_success=False,
            )

    def execute_action(self, action: Action) -> Any:
        return self.adapter.execute(
            self.robot_id,
            action,
            next_action=self.state.peek_after_current(),
            world_state=self.world_state,
            phase_coordinator=self.phase_coordinator,
        )

    def handle_failure(self, action: Action, exc: BaseException, tick: int) -> bool:
        action_key = action.stable_id(self.robot_id, self.state.action_cursor)
        retries = self.state.retries_by_action.get(action_key, 0)

        if action.on_failure == FAILURE_SKIP:
            result = ActionResult(
                self.robot_id,
                action,
                ACTION_FAILED,
                error_message=str(exc),
            )
            self.state.last_action_result = result
            self.state.action_cursor += 1
            self.state.wait_ticks = 0
            self.state.status = (
                ROBOT_FINISHED_STAGE
                if self.state.finished()
                else ROBOT_ACTION_FAILED
            )
            self.logger.result(tick, result)
            return True

        if (
            action.on_failure in {FAILURE_RETRY, FAILURE_WAIT_AND_RETRY}
            and retries < action.max_retries
        ):
            self.state.retries_by_action[action_key] = retries + 1
            self.state.wait_ticks += 1
            self.state.status = ROBOT_ACTION_FAILED
            result = ActionResult(
                self.robot_id,
                action,
                ACTION_FAILED,
                error_message=str(exc),
                attempts=retries + 1,
            )
            self.state.last_action_result = result
            self.logger.result(tick, result)
            if action.on_failure == FAILURE_WAIT_AND_RETRY:
                agent_id = self.runtime.physical_agent_id(self.robot_id)
                self.runtime.step(
                    {"action": "Pass", "agentId": agent_id},
                    check_success=False,
                )
            return False

        self.state.status = ROBOT_BLOCKED
        raise RuntimeError(
            f"Stage {self.state.current_stage_id} failed on {self.robot_id} "
            f"{action.action_type}: {exc}"
        ) from exc

    def submit(
        self,
        payload: dict,
        *,
        check_success: bool,
        save_frame: bool,
        retry_on_failure: bool,
        max_retries: int,
    ) -> Any:
        """Deprecated compatibility path for direct step submission."""

        copied_payload = dict(payload)
        copied_payload.pop("objectResources", None)
        event = self.runtime._step_with_retries(
            copied_payload,
            check_success=check_success,
            save_frame=save_frame,
            retry_on_failure=retry_on_failure,
            max_retries=max_retries,
        )
        self._record_payload_effect(copied_payload, event)
        return event

    def submit_move_to_position(
        self,
        agent_id: int,
        target_position: dict,
        *,
        chunk_steps: int,
        max_requeues: int,
        object_resource: Optional[str] = None,
    ) -> None:
        self.runtime.move_to_position_direct(
            agent_id,
            target_position,
            chunk_steps=chunk_steps,
            max_requeues=max_requeues,
        )

    def submit_teleport_to_position(
        self,
        agent_id: int,
        target_position: Optional[dict] = None,
        *,
        candidate_positions: Optional[Sequence[dict]] = None,
        search_center: Optional[dict] = None,
        max_retries: int,
        object_resource: Optional[str] = None,
        excluded_grid_keys: Optional[set] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> dict:
        candidates = self._candidate_positions(target_position, candidate_positions)
        return self.runtime.teleport_to_first_working_candidate(
            agent_id,
            candidates,
            search_center=(
                dict(search_center) if search_center is not None else candidates[0]
            ),
            max_retries=max_retries,
            excluded_grid_keys=excluded_grid_keys,
            restrict_to_candidate_positions=restrict_to_candidate_positions,
        )

    def submit_teleport_and_face_position(
        self,
        agent_id: int,
        candidate_positions: Sequence[dict],
        *,
        face_target: dict,
        search_center: Optional[dict] = None,
        max_retries: int,
        object_resource: Optional[str] = None,
        excluded_grid_keys: Optional[set] = None,
        restrict_to_candidate_positions: bool = False,
    ) -> dict:
        candidates = [dict(position) for position in candidate_positions]
        if not candidates:
            raise RuntimeError("TeleportAndFacePosition requires at least one target position.")
        return self.runtime.teleport_and_face_first_working_candidate(
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

    def agent_holds_object(self, agent_id: int, object_resource: str) -> bool:
        return self.runtime.agent_holds_object(agent_id, object_resource)

    def agent_held_objects_snapshot(self, agent_id: int) -> set:
        return self.runtime.agent_held_objects_for(agent_id)

    def agent_held_object_matching(self, agent_id: int, pattern: Any) -> Optional[str]:
        return self.runtime.agent_held_object_matching(agent_id, pattern)

    def _candidate_positions(
        self,
        target_position: Optional[dict],
        candidate_positions: Optional[Sequence[dict]],
    ) -> List[dict]:
        if candidate_positions is None:
            if target_position is None:
                raise RuntimeError("TeleportToPosition requires at least one target position.")
            candidate_positions = [target_position]
        candidates = [dict(position) for position in candidate_positions]
        if not candidates:
            raise RuntimeError("TeleportToPosition requires at least one target position.")
        return candidates

    def _record_payload_effect(self, payload: dict, event: Any) -> None:
        metadata = getattr(event, "metadata", {}) or {}
        if not metadata.get("lastActionSuccess", not bool(metadata.get("errorMessage"))):
            return
        action = payload.get("action")
        agent_id = int(payload.get("agentId", 0))
        object_id = payload.get("objectId")
        if action == "PickupObject" and object_id:
            self.runtime.record_agent_held_object(agent_id, str(object_id))
        elif action in {"PutObject", "ThrowObject", "DropHandObject"}:
            self.runtime.release_agent_held_objects(agent_id)


CentralStepExecutor = Executor
SynchronousExecutor = Executor

__all__ = [
    "Executor",
    "PhaseCoordinator",
    "CentralStepExecutor",
    "SynchronousExecutor",
]
