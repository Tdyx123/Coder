"""Robot-level plan executor.

Each Executor owns one robot's action queue.  Phase-level code may run several
Executor instances concurrently; ThorRuntime remains responsible for serializing
the actual controller.step boundary with its controller lock.
"""

import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

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
    ROBOT_EXECUTING,
    ROBOT_FINISHED_STAGE,
    ROBOT_WAITING_CONDITION,
    action_allows_failure_retry,
)
from .goals import record_satisfied_temperature_goal_states
from .movement import (
    ActionWave,
    MovementMode,
    NavigationBatchAborted,
    NavigationBatchResult,
    NavigationDeferred,
    NavigationRequest,
    NavigationResult,
)
from .utils import log


GOTO_CANDIDATE_WAIT_SECONDS = 0.1


@dataclass
class _ActionWaveState:
    wave_id: int
    announced: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    participant_agent_ids: Optional[Tuple[int, ...]] = None
    navigation_agent_ids: Tuple[int, ...] = ()
    completed_agent_ids: frozenset = frozenset()
    requests: Dict[int, NavigationRequest] = field(default_factory=dict)
    results: Dict[int, NavigationResult] = field(default_factory=dict)
    failed_agent_errors: Dict[int, Exception] = field(default_factory=dict)
    deferred_agent_ids: frozenset = frozenset()
    fallback_status: Optional[str] = None
    batch_started: bool = False
    navigation_complete: bool = False
    root_exception: Optional[BaseException] = None
    root_agent_id: Optional[int] = None
    departed_agent_ids: Set[int] = field(default_factory=set)


class PhaseCoordinator:
    """Shared state for robot executors running in the same phase."""

    def __init__(
        self,
        runtime: Any,
        active_agent_ids: Sequence[int],
        *,
        deadline: Optional[float] = None,
        timeout_error_factory: Optional[Callable[[str], BaseException]] = None,
    ) -> None:
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
        self.deadline = deadline
        self.timeout_error_factory = timeout_error_factory
        movement_config = getattr(runtime, "movement_config", None)
        movement_mode = getattr(movement_config, "mode", None)
        self.action_waves_enabled = movement_mode in {
            MovementMode.STEP,
            MovementMode.STEP.value,
        }
        self._action_wave = _ActionWaveState(wave_id=0)

    def mark_agent_done(self, agent_id: int) -> None:
        with self.condition:
            self.completed_agent_ids.add(int(agent_id))
            self._freeze_action_wave_if_ready_locked()
            self.condition.notify_all()

    def mark_agent_failed(self, agent_id: int, exc: BaseException) -> None:
        with self.condition:
            self.failed_agent_errors[int(agent_id)] = exc
            self._freeze_action_wave_if_ready_locked()
            self.condition.notify_all()

    def notify_agent_position_changed(self, agent_id: int) -> None:
        with self.condition:
            self.condition.notify_all()

    def before_action(
        self,
        agent_id: int,
        action_type: str,
        action_cursor: int,
    ) -> Optional[ActionWave]:
        """Join the deterministic action wave for one logical plan action."""

        if not self.action_waves_enabled:
            return None

        current_agent_id = int(agent_id)
        with self.condition:
            self._raise_if_deadline_expired()
            while current_agent_id in self._action_wave.announced:
                self._raise_if_deadline_expired()
                self.condition.wait(timeout=self._condition_wait_seconds())

            state = self._action_wave
            state.announced[current_agent_id] = (
                str(action_type),
                int(action_cursor),
            )
            self._freeze_action_wave_if_ready_locked()
            self.condition.notify_all()

            while state.participant_agent_ids is None:
                self._raise_if_deadline_expired()
                self.condition.wait(timeout=self._condition_wait_seconds())

            wave = ActionWave(
                wave_id=state.wave_id,
                navigation_agent_ids=state.navigation_agent_ids,
            )
            if current_agent_id in state.navigation_agent_ids:
                return wave

            while not state.navigation_complete:
                self._raise_if_deadline_expired()
                self.condition.wait(timeout=self._condition_wait_seconds())

            root_exception = state.root_exception
            root_agent_id = state.root_agent_id
            self._depart_action_wave_locked(state, current_agent_id)
            if root_exception is not None:
                raise NavigationBatchAborted(
                    f"action wave {state.wave_id} was aborted by navigation "
                    f"agent {root_agent_id}: {root_exception}"
                ) from root_exception
            return None

    def submit_step_navigation(
        self,
        wave: ActionWave,
        request: NavigationRequest,
        execute_batch: Callable[
            [Sequence[NavigationRequest], frozenset],
            Any,
        ],
    ) -> NavigationResult:
        """Collect a wave's GoTo requests and execute exactly one joint batch."""

        agent_id = int(request.agent_id)
        batch = None
        completed_agent_ids = frozenset()
        state: _ActionWaveState
        with self.condition:
            state = self._action_wave
            if state.wave_id != wave.wave_id:
                raise RuntimeError(f"action wave {wave.wave_id} is no longer active")
            if agent_id not in state.navigation_agent_ids:
                raise RuntimeError(
                    f"agent {agent_id} is not a navigator in action wave {wave.wave_id}"
                )
            state.requests[agent_id] = request
            self.condition.notify_all()

            while not state.navigation_complete:
                self._raise_if_deadline_expired()
                all_requests_ready = set(state.navigation_agent_ids) <= set(
                    state.requests
                )
                is_batch_leader = agent_id == min(state.navigation_agent_ids)
                if all_requests_ready and is_batch_leader and not state.batch_started:
                    state.batch_started = True
                    batch = tuple(
                        state.requests[item]
                        for item in state.navigation_agent_ids
                    )
                    completed_agent_ids = frozenset(self.completed_agent_ids)
                    break
                self.condition.wait(timeout=self._condition_wait_seconds())

        if batch is not None:
            try:
                batch_results = execute_batch(batch, completed_agent_ids)
                if isinstance(batch_results, NavigationBatchResult):
                    result_map = dict(batch_results.results)
                    failed_agent_errors = dict(batch_results.failed_agent_errors)
                    deferred_agent_ids = frozenset(
                        int(item) for item in batch_results.deferred_agent_ids
                    )
                    fallback_status = batch_results.fallback_status
                else:
                    result_map = dict(batch_results)
                    failed_agent_errors = {}
                    deferred_agent_ids = frozenset()
                    fallback_status = None
                result_agent_ids = set(result_map)
                failed_agent_ids = set(failed_agent_errors)
                overlap = (
                    (result_agent_ids & failed_agent_ids)
                    | (result_agent_ids & deferred_agent_ids)
                    | (failed_agent_ids & deferred_agent_ids)
                )
                reported = result_agent_ids | failed_agent_ids | deferred_agent_ids
                expected = set(state.navigation_agent_ids)
                missing = expected - reported
                unexpected = reported - expected
                invalid_errors = {
                    item
                    for item, error in failed_agent_errors.items()
                    if not isinstance(error, Exception)
                    or isinstance(error, TimeoutError)
                }
                if missing or unexpected or overlap or invalid_errors:
                    raise RuntimeError(
                        "joint navigation batch result partition is invalid: "
                        f"missing={sorted(missing)}, "
                        f"unexpected={sorted(unexpected)}, "
                        f"overlap={sorted(overlap)}, "
                        f"invalid_errors={sorted(invalid_errors)}"
                    )
            except BaseException as exc:
                with self.condition:
                    state.root_exception = exc
                    state.root_agent_id = agent_id
                    state.navigation_complete = True
                    self.condition.notify_all()
            else:
                with self.condition:
                    state.results = result_map
                    state.failed_agent_errors = failed_agent_errors
                    state.deferred_agent_ids = deferred_agent_ids
                    state.fallback_status = fallback_status
                    state.navigation_complete = True
                    self.condition.notify_all()

        with self.condition:
            while not state.navigation_complete:
                self._raise_if_deadline_expired()
                self.condition.wait(timeout=self._condition_wait_seconds())

            root_exception = state.root_exception
            root_agent_id = state.root_agent_id
            result = state.results.get(agent_id)
            request_error = state.failed_agent_errors.get(agent_id)
            deferred = agent_id in state.deferred_agent_ids
            fallback_status = state.fallback_status
            self._depart_action_wave_locked(state, agent_id)
            if root_exception is not None:
                if agent_id == root_agent_id:
                    raise root_exception
                raise NavigationBatchAborted(
                    f"action wave {state.wave_id} was aborted by navigation "
                    f"agent {root_agent_id}: {root_exception}"
                ) from root_exception
            if request_error is not None:
                raise request_error
            if deferred:
                raise NavigationDeferred(
                    agent_id,
                    fallback_status=fallback_status,
                )
            if result is None:
                raise RuntimeError(
                    f"action wave {state.wave_id} has no result for agent {agent_id}"
                )
            return result

    def abort_action_wave(
        self,
        wave: ActionWave,
        agent_id: int,
        exc: BaseException,
    ) -> None:
        """Wake a joint wave when navigation fails before batch submission."""

        with self.condition:
            state = self._action_wave
            current_agent_id = int(agent_id)
            if (
                state.wave_id != wave.wave_id
                or current_agent_id in state.departed_agent_ids
                or state.navigation_complete
            ):
                return
            state.root_exception = exc
            state.root_agent_id = current_agent_id
            state.navigation_complete = True
            self._depart_action_wave_locked(state, current_agent_id)

    def _active_action_wave_agent_ids_locked(self) -> Set[int]:
        return (
            self.active_agent_ids
            - self.completed_agent_ids
            - set(self.failed_agent_errors)
        )

    def _freeze_action_wave_if_ready_locked(self) -> None:
        state = self._action_wave
        if state.participant_agent_ids is not None:
            return
        active_agent_ids = self._active_action_wave_agent_ids_locked()
        if not active_agent_ids or not active_agent_ids <= set(state.announced):
            return
        state.participant_agent_ids = tuple(sorted(active_agent_ids))
        state.navigation_agent_ids = tuple(
            agent_id
            for agent_id in state.participant_agent_ids
            if state.announced[agent_id][0] == "GoToObject"
        )
        state.completed_agent_ids = frozenset(self.completed_agent_ids)
        state.navigation_complete = not state.navigation_agent_ids

    def _depart_action_wave_locked(
        self,
        state: _ActionWaveState,
        agent_id: int,
    ) -> None:
        state.departed_agent_ids.add(int(agent_id))
        participant_agent_ids = set(state.participant_agent_ids or ())
        if participant_agent_ids <= state.departed_agent_ids:
            self._action_wave = _ActionWaveState(wave_id=state.wave_id + 1)
        self.condition.notify_all()

    def wait_until_goto_candidates_clear(
        self,
        agent_id: int,
        candidate_positions: Sequence[dict],
    ) -> bool:
        protected_positions = [dict(position) for position in candidate_positions]
        if not protected_positions:
            return False

        current_agent_id = int(agent_id)
        waited_or_relocated = False
        while True:
            self._raise_if_deadline_expired()
            blockers = self.runtime.agent_blocker_ids_for_positions(
                protected_positions,
                current_agent_id,
            )
            with self.condition:
                self._raise_if_failed_locked()
                self._raise_if_deadline_expired()
                blockers = set(blockers)
                if not blockers:
                    return waited_or_relocated

                completed_blockers = (
                    blockers
                    & self.completed_agent_ids
                    - self.relocating_agent_ids
                )
                if completed_blockers:
                    blocker_agent_id = min(completed_blockers)
                    self.relocating_agent_ids.add(blocker_agent_id)
                    waited_or_relocated = True
                else:
                    if current_agent_id == min(blockers | {current_agent_id}):
                        return waited_or_relocated
                    waited_or_relocated = True
                    self.condition.wait(timeout=self._condition_wait_seconds())
                    continue

            try:
                self.runtime.teleport_completed_agent_away_from_positions(
                    blocker_agent_id,
                    current_agent_id,
                    protected_positions,
                )
            finally:
                with self.condition:
                    self.relocating_agent_ids.discard(blocker_agent_id)
                    self.condition.notify_all()

    def _condition_wait_seconds(self) -> float:
        if self.deadline is None:
            return GOTO_CANDIDATE_WAIT_SECONDS
        remaining = max(0.0, self.deadline - time.monotonic())
        return min(GOTO_CANDIDATE_WAIT_SECONDS, remaining)

    def _raise_if_deadline_expired(self) -> None:
        if self.deadline is None or time.monotonic() < self.deadline:
            return
        message = "GoToObject candidate wait exceeded timeout."
        if self.timeout_error_factory is not None:
            raise self.timeout_error_factory(message)
        raise TimeoutError(message)

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
            action_wave = None
            try:
                self.wait_for_condition(action)
                self.state.status = ROBOT_EXECUTING
                action_wave = self.before_action(action)
                event = self.execute_action(action, action_wave=action_wave)
                self.record_temperature_goal_progress()
            except NavigationDeferred:
                tick += 1
                continue
            except BaseException as exc:
                if self.phase_coordinator is not None and action_wave is not None:
                    self.phase_coordinator.abort_action_wave(
                        action_wave,
                        self.runtime.physical_agent_id(self.robot_id),
                        exc,
                    )
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

    def before_action(self, action: Action) -> Optional[ActionWave]:
        if self.phase_coordinator is None:
            return None
        return self.phase_coordinator.before_action(
            self.runtime.physical_agent_id(self.robot_id),
            action.action_type,
            self.state.action_cursor,
        )

    def execute_action(
        self,
        action: Action,
        *,
        action_wave: Optional[ActionWave] = None,
    ) -> Any:
        deadline = getattr(self, "deadline", None)
        factory = getattr(self, "timeout_error_factory", None)
        if self.phase_coordinator is not None:
            phase_deadline = self.phase_coordinator.deadline
            if phase_deadline is not None and (deadline is None or phase_deadline <= deadline):
                deadline = phase_deadline
                factory = self.phase_coordinator.timeout_error_factory
        scope = getattr(self.runtime, "action_deadline_scope", None)
        # Only propagate context here; locking before wave request collection
        # would deadlock joint navigation waiting for its other participants.
        with scope(deadline, factory) if callable(scope) else nullcontext():
            return self.adapter.execute(
                self.robot_id,
                action,
                next_action=self.state.peek_after_current(),
                world_state=self.world_state,
                phase_coordinator=self.phase_coordinator,
                action_wave=action_wave,
            )

    def record_temperature_goal_progress(self) -> None:
        current_objects = getattr(self.runtime, "current_objects", None)
        if not callable(current_objects):
            return
        try:
            record_satisfied_temperature_goal_states(current_objects())
        except Exception as exc:
            log(f"Skipping HOT/COLD ground-truth check: {exc}")

    def handle_failure(self, action: Action, exc: BaseException, tick: int) -> bool:
        action_key = action.stable_id(self.robot_id, self.state.action_cursor)
        retries = self.state.retries_by_action.get(action_key, 0)

        if (
            action.on_failure != FAILURE_SKIP
            and action_allows_failure_retry(action)
            and action.on_failure in {FAILURE_RETRY, FAILURE_WAIT_AND_RETRY}
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
