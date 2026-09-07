"""Action-level plans, stage control, resource inference, and robot execution."""

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from .config import NAVIGATION_GRID_SIZE
from .execution_policy import ExecutionPolicy, resolve_failure
from .utils import (
    log,
    object_center,
    object_key,
    position_to_grid_key,
)

@dataclass(frozen=True)
class PlannedAction:
    name: str
    args: Tuple[Any, ...] = ()


ROBOT_READY = "READY"
ROBOT_WAITING_CONFLICT = "WAITING_CONFLICT"
ROBOT_WAITING_CONDITION = "WAITING_CONDITION"
ROBOT_EXECUTING = "EXECUTING"
ROBOT_ACTION_SUCCESS = "ACTION_SUCCESS"
ROBOT_ACTION_FAILED = "ACTION_FAILED"
ROBOT_FINISHED_STAGE = "FINISHED_STAGE"
ROBOT_BLOCKED = "BLOCKED"

ACTION_SUCCESS = "SUCCESS"
ACTION_FAILED = "FAILED"
ACTION_DELAYED_BY_CONFLICT = "DELAYED_BY_CONFLICT"
ACTION_WAITING_CONDITION = "WAITING_CONDITION"

SYNC_BARRIER_AT_STAGE_END = "BARRIER_AT_STAGE_END"
SYNC_BARRIER_EACH_STEP = "BARRIER_EACH_STEP"
SYNC_EVENT_CONDITION = "EVENT_CONDITION"

CONFLICT_WAIT = "WAIT"
CONFLICT_RETRY_NEXT_TICK = "RETRY_NEXT_TICK"
CONFLICT_SKIP = "SKIP"
CONFLICT_FAIL_STAGE = "FAIL_STAGE"

FAILURE_RETRY = "RETRY"
FAILURE_WAIT_AND_RETRY = "WAIT_AND_RETRY"
FAILURE_SKIP = "SKIP"
FAILURE_FAIL_ROBOT = "FAIL_ROBOT"
FAILURE_FAIL_STAGE = "FAIL_STAGE"
FAILURE_SKIP_IF_EFFECT_ALREADY_TRUE = "SKIP_IF_EFFECT_ALREADY_TRUE"

RETRYABLE_FAILURE_ACTION_TYPES = {"Teleport"}
DEFAULT_PRE_TASK_STAGE_ID = "PreTask"
PRE_TASK_ROBOT_ID = "robot1"


def action_allows_failure_retry(action: "Action") -> bool:
    return action.action_type in RETRYABLE_FAILURE_ACTION_TYPES


@dataclass(frozen=True)
class Action:
    action_type: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    action_id: Optional[str] = None
    robot_id: Optional[str] = None
    expected_preconditions: Tuple[Any, ...] = ()
    expected_effects: Tuple[Any, ...] = ()
    resource_policy: Dict[str, Any] = field(default_factory=dict)
    wait_until: Optional[Callable[["WorldState"], bool]] = None
    on_conflict: str = CONFLICT_WAIT
    on_failure: str = FAILURE_FAIL_STAGE
    max_retries: int = 2
    timeout_ticks: Optional[int] = None
    base_priority: int = 0
    critical: bool = False

    @classmethod
    def from_any(cls, value: Any) -> "Action":
        if isinstance(value, cls):
            return value
        if isinstance(value, PlannedAction):
            return cls(value.name, {"args": tuple(value.args)})
        if isinstance(value, str):
            return cls(value)
        if not isinstance(value, dict):
            raise RuntimeError(f"Unsupported action plan entry: {value!r}")

        action_type = (
            value.get("action_type")
            or value.get("action")
            or value.get("name")
            or value.get("type")
        )
        if not action_type:
            raise RuntimeError(f"Action entry is missing action_type: {value!r}")

        parameters = dict(value.get("parameters") or {})
        if "args" in value and "args" not in parameters:
            parameters["args"] = tuple(value["args"])
        from .action_registry import DIRECT_PAYLOAD_FIELDS
        for key in DIRECT_PAYLOAD_FIELDS:
            if key in value and key not in parameters:
                parameters[key] = value[key]

        return cls(
            str(action_type),
            parameters,
            action_id=value.get("action_id") or value.get("id"),
            robot_id=value.get("robot_id"),
            expected_preconditions=tuple(value.get("expected_preconditions") or ()),
            expected_effects=tuple(value.get("expected_effects") or ()),
            resource_policy=dict(value.get("resource_policy") or {}),
            wait_until=value.get("wait_until"),
            on_conflict=value.get("on_conflict", CONFLICT_WAIT),
            on_failure=value.get("on_failure", FAILURE_FAIL_STAGE),
            max_retries=int(value.get("max_retries", 2)),
            timeout_ticks=value.get("timeout_ticks"),
            base_priority=int(value.get("base_priority", 0)),
            critical=bool(value.get("critical", False)),
        )

    def with_robot(self, robot_id: str) -> "Action":
        if self.robot_id == robot_id:
            return self
        return Action(
            self.action_type,
            dict(self.parameters),
            action_id=self.action_id,
            robot_id=robot_id,
            expected_preconditions=tuple(self.expected_preconditions),
            expected_effects=tuple(self.expected_effects),
            resource_policy=dict(self.resource_policy),
            wait_until=self.wait_until,
            on_conflict=self.on_conflict,
            on_failure=self.on_failure,
            max_retries=self.max_retries,
            timeout_ticks=self.timeout_ticks,
            base_priority=self.base_priority,
            critical=self.critical,
        )

    def args(self) -> Tuple[Any, ...]:
        args = self.parameters.get("args", ())
        if isinstance(args, tuple):
            return args
        if isinstance(args, list):
            return tuple(args)
        return (args,)

    def stable_id(self, robot_id: str, cursor: int) -> str:
        if self.action_id:
            return self.action_id
        return f"{robot_id}:{self.action_type}:{cursor}"


@dataclass
class StagePlan:
    stage_id: str
    robot_action_queues: Dict[str, List[Action]]
    stage_success_condition: Optional[Callable[["WorldState"], bool]] = None
    stage_failure_policy: str = FAILURE_FAIL_STAGE
    synchronization_policy: str = SYNC_BARRIER_AT_STAGE_END

    @classmethod
    def _action_queues_from_any(
        cls,
        raw_queues: Any,
        *,
        field_name: str,
        robot_id_override: Optional[str] = None,
    ) -> Dict[str, List[Action]]:
        if not isinstance(raw_queues, dict):
            raise RuntimeError(f"{field_name} must be a mapping of robot id to actions.")
        queues: Dict[str, List[Action]] = {}
        for robot_id, actions in raw_queues.items():
            target_robot_id = robot_id_override or str(robot_id)
            queues.setdefault(target_robot_id, []).extend(
                Action.from_any(action).with_robot(target_robot_id)
                for action in (actions or ())
            )
        return queues

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StagePlan":
        raw_queues = data.get("robot_action_queues") or {}
        queues = cls._action_queues_from_any(
            raw_queues,
            field_name="robot_action_queues",
        )
        return cls(
            stage_id=str(data.get("stage_id") or data.get("id") or "stage"),
            robot_action_queues=queues,
            stage_success_condition=data.get("stage_success_condition"),
            stage_failure_policy=data.get("stage_failure_policy", FAILURE_FAIL_STAGE),
            synchronization_policy=data.get(
                "synchronization_policy",
                SYNC_BARRIER_AT_STAGE_END,
            ),
        )

    @classmethod
    def pre_task_from_dict(cls, data: Dict[str, Any]) -> Optional["StagePlan"]:
        raw_queues = data.get("pre_task_action_queues") or {}
        if not raw_queues:
            return None
        if not isinstance(raw_queues, dict):
            raise RuntimeError(
                "pre_task_action_queues must be a mapping of robot id to actions."
            )

        invalid_robot_ids = [
            str(robot_id)
            for robot_id, actions in raw_queues.items()
            if str(robot_id) != PRE_TASK_ROBOT_ID and actions
        ]
        if invalid_robot_ids:
            raise RuntimeError(
                "pre_task_action_queues can only target "
                f"{PRE_TASK_ROBOT_ID!r}; got {invalid_robot_ids!r}."
            )

        queues = cls._action_queues_from_any(
            raw_queues,
            field_name="pre_task_action_queues",
            robot_id_override=PRE_TASK_ROBOT_ID,
        )
        queues = {
            robot_id: actions
            for robot_id, actions in queues.items()
            if actions
        }
        if not queues:
            return None
        return cls(
            stage_id=str(data.get("pre_task_stage_id") or DEFAULT_PRE_TASK_STAGE_ID),
            robot_action_queues=queues,
        )


@dataclass
class MultiStageActionPlan:
    task_id: str
    stages: List[StagePlan]
    global_success_condition: Optional[Callable[["WorldState"], bool]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MultiStageActionPlan":
        stages = [StagePlan.from_dict(stage) for stage in data.get("stages", [])]
        pre_task_stage = StagePlan.pre_task_from_dict(data)
        if pre_task_stage is not None:
            stages.insert(0, pre_task_stage)
        return cls(
            task_id=str(data.get("task_id") or "task"),
            stages=stages,
            global_success_condition=data.get("global_success_condition"),
        )


TaskPlan = MultiStageActionPlan


@dataclass
class RobotExecutionState:
    robot_id: str
    current_stage_id: str
    action_queue: List[Action]
    action_cursor: int = 0
    status: str = ROBOT_READY
    last_action_result: Optional["ActionResult"] = None
    wait_ticks: int = 0
    held_object: Optional[str] = None
    retries_by_action: Dict[str, int] = field(default_factory=dict)

    def next_action(self) -> Optional[Action]:
        if self.action_cursor >= len(self.action_queue):
            return None
        return self.action_queue[self.action_cursor]

    def peek_after_current(self) -> Optional[Action]:
        next_index = self.action_cursor + 1
        if next_index >= len(self.action_queue):
            return None
        return self.action_queue[next_index]

    def remaining_actions(self) -> int:
        return max(0, len(self.action_queue) - self.action_cursor)

    def finished(self) -> bool:
        return self.action_cursor >= len(self.action_queue)


@dataclass
class ActionResult:
    robot_id: str
    action: Action
    status: str
    event: Any = None
    error_message: str = ""
    attempts: int = 0
    conflict_reason: str = ""
    requested_failure_policy: str = ""
    failure_decision: str = ""
    failure_error_code: str = ""


@dataclass(frozen=True)
class ResourceRequest:
    robot_id: str
    action: Action
    position_resources: Tuple[str, ...] = ()
    object_resources: Tuple[str, ...] = ()
    preconditions: Tuple[Any, ...] = ()

    def all_resources(self) -> Tuple[str, ...]:
        return self.position_resources + self.object_resources


class WorldState:
    """Frozen world facts plus scheduler-confirmed high-level progress.

    ``tick`` counts observed terminal action outcomes across robots in this
    stage (success, final failure, or conflict skip), starting at zero per stage.
    Retries, deferrals, idle Passes and unexecuted tails/whole-stage skips do not
    advance it. Worker callbacks sample it at admission; scheduler callbacks
    see current confirmed progress. It is independent of snapshot ``version``.
    """
    def __init__(self, runtime_obj: Optional["ThorRuntime"]) -> None:
        self.runtime = runtime_obj
        self.tick = 0
        from .world_snapshot import WorldSnapshot
        self.snapshot = WorldSnapshot(0, {}, {}, {}, {})

    @property
    def version(self):
        return self.snapshot.version

    @property
    def robot_positions(self):
        return self.snapshot.robot_positions

    @property
    def robot_rotations(self):
        return self.snapshot.robot_rotations

    @property
    def held_objects(self):
        return self.snapshot.held_objects

    @property
    def objects_by_id(self):
        return self.snapshot.objects_by_id

    @property
    def held_object_sources(self):
        return self.snapshot.held_object_sources

    def refresh(self, robot_states: Sequence[RobotExecutionState]) -> None:
        if self.runtime is None:
            return
        from .execution_control import ensure_control
        from .world_snapshot import SnapshotStore

        snapshot = SnapshotStore().capture(self.runtime, ensure_control(self.runtime))
        self.snapshot = snapshot
        for state in robot_states:
            held = snapshot.held_objects[state.robot_id]
            state.held_object = sorted(held)[0] if held else None


class PlanLoader:
    def load(self, raw_plan: Union[MultiStageActionPlan, Dict[str, Any]]) -> MultiStageActionPlan:
        if isinstance(raw_plan, MultiStageActionPlan):
            return raw_plan
        return MultiStageActionPlan.from_dict(raw_plan)


from .plan_validator import PlanValidator

class ActionQueueManager:
    def __init__(self, stage: StagePlan) -> None:
        self.stage = stage
        self.robot_states: Dict[str, RobotExecutionState] = {
            robot_id: RobotExecutionState(
                robot_id=robot_id,
                current_stage_id=stage.stage_id,
                action_queue=list(actions),
            )
            for robot_id, actions in stage.robot_action_queues.items()
        }

    def states(self) -> List[RobotExecutionState]:
        return list(self.robot_states.values())

    def stage_finished(self) -> bool:
        return all(state.finished() for state in self.robot_states.values())

    def next_requests(self, robot_order: Sequence[str]) -> List[Tuple[RobotExecutionState, Action]]:
        ready: List[Tuple[RobotExecutionState, Action]] = []
        for robot_id in robot_order:
            state = self.robot_states[robot_id]
            action = state.next_action()
            if action is None:
                state.status = ROBOT_FINISHED_STAGE
                continue
            ready.append((state, action))
        if self.stage.synchronization_policy != SYNC_BARRIER_EACH_STEP:
            return ready

        active_cursors = [
            state.action_cursor
            for state in self.robot_states.values()
            if not state.finished()
        ]
        if not active_cursors:
            return ready
        barrier_cursor = min(active_cursors)
        return [
            (state, action)
            for state, action in ready
            if state.action_cursor == barrier_cursor
        ]

    def mark_success(self, state: RobotExecutionState, result: ActionResult) -> None:
        state.last_action_result = result
        state.action_cursor += 1
        state.wait_ticks = 0
        state.status = (
            ROBOT_FINISHED_STAGE if state.finished() else ROBOT_ACTION_SUCCESS
        )

    def mark_delayed(
        self,
        state: RobotExecutionState,
        action: Action,
        reason: str,
    ) -> None:
        state.wait_ticks += 1
        state.status = ROBOT_WAITING_CONFLICT
        state.last_action_result = ActionResult(
            state.robot_id,
            action,
            ACTION_DELAYED_BY_CONFLICT,
            conflict_reason=reason,
        )

    def mark_waiting_condition(self, state: RobotExecutionState, action: Action) -> None:
        state.wait_ticks += 1
        state.status = ROBOT_WAITING_CONDITION
        state.last_action_result = ActionResult(
            state.robot_id,
            action,
            ACTION_WAITING_CONDITION,
        )


class ResourceInferencer:
    from .action_registry import ActionRegistry as _Registry
    OBJECT_ACTIONS = _Registry().object_action_names()

    def infer(
        self,
        action: Action,
        state: RobotExecutionState,
        world_state: WorldState,
    ) -> ResourceRequest:
        position_resources = self.infer_position_resources(action, state, world_state)
        object_resources = self.infer_object_resources(action)
        return ResourceRequest(
            state.robot_id,
            action,
            position_resources=position_resources,
            object_resources=object_resources,
            preconditions=tuple(action.expected_preconditions),
        )

    def infer_position_resources(
        self,
        action: Action,
        state: RobotExecutionState,
        world_state: WorldState,
    ) -> Tuple[str, ...]:
        if action.action_type in {"Done"}:
            return ()
        if action.action_type == "MoveAhead":
            position = world_state.robot_positions.get(state.robot_id)
            if not position:
                return ()
            target = self.moveahead_target(action, state.robot_id, world_state)
            if target is None:
                return ()
            current_cell = self.cell_resource(position)
            target_cell = self.cell_resource(target)
            edge = self.edge_resource(position, target)
            return (current_cell, target_cell, edge)

        target_name = self.primary_object_name(action)
        if action.action_type == "GoToObject" and target_name:
            return self.goto_position_resources(state, world_state, target_name)
        if action.action_type in self.OBJECT_ACTIONS and target_name:
            resources = [f"interaction:{object_key(target_name)}"]
            position = world_state.robot_positions.get(state.robot_id)
            if position:
                resources.append(self.cell_resource(position))
            return tuple(dict.fromkeys(resources))

        position = world_state.robot_positions.get(state.robot_id)
        if position:
            return (self.cell_resource(position),)
        return ()

    def goto_position_resources(
        self,
        state: RobotExecutionState,
        world_state: WorldState,
        target_name: Any,
    ) -> Tuple[str, ...]:
        resources = [f"interaction:{object_key(target_name)}"]
        target_position = self.goto_target_position(state, world_state, target_name)
        if target_position is not None:
            resources.append(self.cell_resource(target_position))
        return tuple(dict.fromkeys(resources))

    def goto_target_position(
        self,
        state: RobotExecutionState,
        world_state: WorldState,
        target_name: Any,
    ) -> Optional[Dict[str, float]]:
        runtime = world_state.runtime
        if runtime is None:
            return None
        try:
            agent_id = runtime.physical_agent_id(state.robot_id)
            dest = runtime.find_object(target_name, agent_id=agent_id, require_center=True)
            center = object_center(dest)
            if not center:
                return None
            candidate_positions = runtime.teleport_candidate_positions(
                center,
                agent_id=agent_id,
                include_agent_positions=False,
            )
        except BaseException:
            return None
        if not candidate_positions:
            return None
        return candidate_positions[0]

    def infer_object_resources(self, action: Action) -> Tuple[str, ...]:
        resources = []
        for obj_name in self.object_names(action):
            obj_key = object_key(obj_name)
            if obj_key:
                resources.append(f"object:{obj_key}")
        return tuple(resources)

    def object_names(self, action: Action) -> Tuple[Any, ...]:
        return self._Registry().object_names(action)

    def primary_object_name(self, action: Action) -> Optional[Any]:
        names = self.object_names(action)
        if not names:
            return None
        return names[0]

    def moveahead_target(
        self,
        action: Action,
        robot_id: str,
        world_state: WorldState,
    ) -> Optional[Dict[str, float]]:
        position = world_state.robot_positions.get(robot_id)
        if not position:
            return None
        yaw = math.radians(world_state.robot_rotations.get(robot_id, 0.0))
        magnitude = float(action.parameters.get("moveMagnitude", NAVIGATION_GRID_SIZE))
        return {
            "x": float(position["x"]) + math.sin(yaw) * magnitude,
            "y": float(position.get("y", 0.0)),
            "z": float(position["z"]) + math.cos(yaw) * magnitude,
        }

    def cell_resource(self, position: Dict[str, float]) -> str:
        x, z = position_to_grid_key(position)
        return f"cell:{x}:{z}"

    def edge_resource(
        self,
        start_position: Dict[str, float],
        end_position: Dict[str, float],
    ) -> str:
        start = position_to_grid_key(start_position)
        end = position_to_grid_key(end_position)
        ordered = tuple(sorted((start, end)))
        return f"edge:{ordered[0][0]}:{ordered[0][1]}:{ordered[1][0]}:{ordered[1][1]}"


class ResourceConflictManager:
    CURRENT_CELL_INTERACTION_ACTIONS = set(ResourceInferencer.OBJECT_ACTIONS)
    CURRENT_CELL_INTERACTION_PRIORITY = 1000000

    def resolve(
        self,
        requests: Sequence[ResourceRequest],
        states: Dict[str, RobotExecutionState],
        robot_order: Sequence[str],
    ) -> Tuple[Dict[str, ResourceRequest], Dict[str, str]]:
        approved: Dict[str, ResourceRequest] = {}
        delayed: Dict[str, str] = {}
        claimed: Dict[str, str] = {}
        order_rank = {robot_id: index for index, robot_id in enumerate(robot_order)}
        for request in sorted(
            requests,
            key=lambda req: self.priority_key(req, states[req.robot_id], order_rank),
        ):
            conflict_owner = None
            conflict_resource = None
            for resource in request.all_resources():
                if resource in claimed:
                    conflict_owner = claimed[resource]
                    conflict_resource = resource
                    break
            if conflict_owner is not None:
                delayed[request.robot_id] = (
                    f"{conflict_resource} already reserved by {conflict_owner}"
                )
                continue
            approved[request.robot_id] = request
            for resource in request.all_resources():
                claimed[resource] = request.robot_id
        return approved, delayed

    def priority_key(
        self,
        request: ResourceRequest,
        state: RobotExecutionState,
        order_rank: Dict[str, int],
    ) -> Tuple[int, int, int]:
        score = int(request.action.base_priority)
        if self.is_current_cell_interaction(request):
            score += self.CURRENT_CELL_INTERACTION_PRIORITY
        score += state.wait_ticks * 10
        if state.held_object:
            score += 100
        if request.action.critical:
            score += 50
        return (-score, state.remaining_actions(), order_rank.get(state.robot_id, 9999))

    def is_current_cell_interaction(self, request: ResourceRequest) -> bool:
        if request.action.action_type not in self.CURRENT_CELL_INTERACTION_ACTIONS:
            return False
        return any(
            resource.startswith("cell:")
            for resource in request.position_resources
        )


class FailureHandler:
    def __init__(self, policy: Union[ExecutionPolicy, str] = ExecutionPolicy.LEGACY) -> None:
        self.policy = ExecutionPolicy(policy)

    def handle_conflict(
        self,
        queue_manager: ActionQueueManager,
        state: RobotExecutionState,
        action: Action,
        reason: str,
    ) -> None:
        if action.on_conflict == CONFLICT_SKIP:
            queue_manager.mark_success(
                state,
                ActionResult(
                    state.robot_id,
                    action,
                    ACTION_DELAYED_BY_CONFLICT,
                    conflict_reason=reason,
                ),
            )
            return
        if action.on_conflict == CONFLICT_FAIL_STAGE:
            raise RuntimeError(
                f"Stage {state.current_stage_id} failed because {state.robot_id} "
                f"could not run {action.action_type}: {reason}"
            )
        queue_manager.mark_delayed(state, action, reason)

    def handle_failure(
        self,
        queue_manager: ActionQueueManager,
        state: RobotExecutionState,
        action: Action,
        exc: BaseException,
        *,
        effects_satisfied: Optional[bool] = None,
    ) -> None:
        action_key = action.stable_id(state.robot_id, state.action_cursor)
        retries = state.retries_by_action.get(action_key, 0)
        decision = resolve_failure(
            self.policy,
            action,
            attempts=retries + 1,
            effects_satisfied=effects_satisfied,
        )
        result = ActionResult(
            state.robot_id,
            action,
            ACTION_FAILED,
            error_message=str(exc),
            attempts=retries + 1,
            requested_failure_policy=action.on_failure,
            failure_decision=decision.kind,
            failure_error_code=decision.error_code,
        )
        if decision.kind == "skip":
            queue_manager.mark_success(
                state,
                result,
            )
            return
        if decision.kind in {"retry", "wait_retry"}:
            state.retries_by_action[action_key] = decision.retry_number
            state.wait_ticks += 1
            state.status = ROBOT_ACTION_FAILED
            state.last_action_result = result
            return
        if decision.kind == "fail_robot":
            state.status = ROBOT_BLOCKED
            state.action_cursor = len(state.action_queue)
            return
        raise RuntimeError(
            f"Stage {state.current_stage_id} failed on {state.robot_id} "
            f"{action.action_type}: {exc}"
        ) from exc


class ExecutionLogger:
    def __init__(self) -> None:
        self.records: List[ActionResult] = []

    def stage_started(self, stage: StagePlan) -> None:
        log(f"{stage.stage_id} started with {len(stage.robot_action_queues)} robot queue(s).")

    def delayed(self, tick: int, state: RobotExecutionState, reason: str) -> None:
        action = state.next_action()
        action_name = action.action_type if action is not None else "Wait"
        log(f"Tick {tick}: {state.robot_id} delayed {action_name}: {reason}")

    def result(self, tick: int, result: ActionResult) -> None:
        self.records.append(result)
        if result.status == ACTION_SUCCESS:
            log(f"Tick {tick}: {result.robot_id} completed {result.action.action_type}.")
        elif result.status == ACTION_FAILED:
            log(
                f"Tick {tick}: {result.robot_id} failed "
                f"{result.action.action_type}: {result.error_message}"
            )


class AI2ThorAdapter:
    def __init__(self, runtime_obj: "ThorRuntime") -> None:
        self.runtime = runtime_obj

    def execute(
        self,
        robot_id: str,
        action: Action,
        *,
        next_action: Optional[Action] = None,
        world_state: Optional[WorldState] = None,
        phase_coordinator: Optional[Any] = None,
        action_wave: Optional[Any] = None,
    ) -> Any:
        from .action_registry import ActionRegistry, PreparedAction
        from .action_resources import active_resources
        registry = ActionRegistry()
        normalized = registry.normalize(action)
        scope = active_resources(self.runtime)
        if scope is not None:
            prepared = PreparedAction(normalized, scope.resolved)
        else:
            from .world_snapshot import SnapshotStore
            from .execution_control import ensure_control
            snapshot = SnapshotStore().capture(self.runtime, ensure_control(self.runtime))
            prepared = registry.prepare(self.runtime, snapshot, robot_id, action)
        return registry.execute(self.runtime, robot_id, prepared, {
            'next_action': next_action, 'world_state': world_state,
            'phase_coordinator': phase_coordinator, 'action_wave': action_wave})

    def execute_direct_step(self, robot_id, action):
        return self.execute(robot_id, action)

    def put_object(self, robot_id: str, args: Tuple[Any, ...]) -> Any:
        return self.execute(robot_id, Action('PutObject', {'args': args}))

    def call_generated_helper(self, robot_id: str, action: Action) -> Any:
        return self.execute(robot_id, action)

    def to_planned_action(self, action: Optional[Action]) -> Optional[PlannedAction]:
        if action is None:
            return None
        return PlannedAction(action.action_type, action.args())


class StageRunner:
    def __init__(
        self,
        runtime_obj: "ThorRuntime",
        *,
        max_ticks_per_stage: int = 10000,
        logger: Optional[ExecutionLogger] = None,
        stage_index: int = 0,
        execution_policy: Union[ExecutionPolicy, str] = ExecutionPolicy.LEGACY,
        control: Optional[Any] = None,
    ) -> None:
        self.runtime = runtime_obj
        self.max_ticks_per_stage = max_ticks_per_stage
        self.world_state = WorldState(runtime_obj)
        self.queue_manager: Optional[ActionQueueManager] = None
        self.inferencer = ResourceInferencer()
        self.conflict_manager = ResourceConflictManager()
        self.execution_policy = ExecutionPolicy(execution_policy)
        self.failure_handler = FailureHandler(self.execution_policy)
        self.control = control
        self.adapter = AI2ThorAdapter(runtime_obj)
        self.logger = logger or ExecutionLogger()
        self.stage_index = int(stage_index)

    def execute_stage(self, stage: StagePlan) -> WorldState:
        self.logger.stage_started(stage)
        from .stage_scheduler import StageScheduler
        from .execution_control import ensure_control
        root_control = self.control or ensure_control(self.runtime)
        self.scheduler = StageScheduler(
            self.runtime, stage, control=root_control.child(deadline=getattr(self, 'deadline', None)),
            policy=self.execution_policy, logger=self.logger, stage_index=self.stage_index,
            stats=getattr(self, 'stats', None),
        )
        self.outcome = self.scheduler.run()
        self.world_state.snapshot = self.outcome.snapshot
        self.world_state.tick = self.scheduler.world.tick
        return self.world_state

    def ready_items(
        self,
        robot_order: Sequence[str],
    ) -> List[Tuple[RobotExecutionState, Action]]:
        if self.queue_manager is None:
            return []
        ready_items = []
        for state, action in self.queue_manager.next_requests(robot_order):
            if action.wait_until is None and action.action_type != "WaitUntil":
                ready_items.append((state, action))
                continue
            if self.wait_condition_satisfied(action):
                ready_items.append((state, action))
                continue
            self.queue_manager.mark_waiting_condition(state, action)
        return ready_items

    def wait_condition_satisfied(self, action: Action) -> bool:
        if action.wait_until is None:
            return True
        return bool(action.wait_until(self.world_state))

    def robot_order(self, stage: StagePlan, tick: int) -> List[str]:
        robots_in_stage = list(stage.robot_action_queues)
        if not robots_in_stage:
            return []
        offset = tick % len(robots_in_stage)
        return robots_in_stage[offset:] + robots_in_stage[:offset]


SynchronousRoundRobinExecutor = StageRunner


class TaskRunner:
    def __init__(
        self,
        runtime_obj: "ThorRuntime",
        *,
        logger: Optional[ExecutionLogger] = None,
        execution_policy: Union[ExecutionPolicy, str] = ExecutionPolicy.LEGACY,
    ) -> None:
        self.runtime = runtime_obj
        self.loader = PlanLoader()
        self.validator = PlanValidator(runtime_obj)
        self.logger = logger or ExecutionLogger()
        self.execution_policy = ExecutionPolicy(execution_policy)

    def execute(
        self,
        raw_plan: Union[MultiStageActionPlan, Dict[str, Any]],
        *,
        timeout_seconds: Optional[float] = None,
    ) -> WorldState:
        import math
        from .execution_control import install_control, error_record, PlanExecutionTimeout, ExecutionCancelled
        from .execution_policy import PlanExecutionError, ConditionEvaluationError, evaluate_condition, snapshot_report
        from .run_results import ActionLedger
        from .parallel_runner import TolerantRunStats
        if timeout_seconds is not None and (not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0):
            raise ValueError('timeout_seconds must be finite and positive')
        plan = self.loader.load(raw_plan)
        self.validator.validate(plan)
        control = install_control(self.runtime, timeout_seconds)
        planned_keys = [
            f"{stage_index}:{robot_id}:{cursor}"
            for stage_index, stage in enumerate(plan.stages)
            for robot_id, actions in stage.robot_action_queues.items()
            for cursor, _action in enumerate(actions)
        ]
        stats = TolerantRunStats(action_ledger=ActionLedger(planned_keys),
                                 execution_policy=self.execution_policy.value)
        self.runtime.action_ledger = stats.action_ledger
        world_state = WorldState(self.runtime)
        stage_reports, errors = [], []
        status = 'completed'
        global_result = None
        exception = None
        try:
            for stage_index, stage in enumerate(plan.stages):
                control.check()
                runner = StageRunner(self.runtime, logger=self.logger, stage_index=stage_index,
                                     execution_policy=self.execution_policy, control=control)
                runner.stats = stats
                try:
                    world_state = runner.execute_stage(stage)
                finally:
                    if getattr(runner, 'scheduler', None) is not None:
                        stage_reports.append(runner.scheduler.report)
                        world_state = runner.scheduler.world
                outcome = runner.outcome
                errors.extend(outcome.errors)
                if outcome.status == 'failed':
                    status = 'failed'
                elif outcome.status == 'partial' and status == 'completed':
                    status = 'partial'
                if not outcome.continue_task:
                    break
            # Always capture all physical robots before an explicit final condition.
            world_state.refresh([])
            if plan.global_success_condition is not None:
                global_result = evaluate_condition(plan.global_success_condition, world_state)
                if global_result is not True:
                    status = 'failed'
                    errors.append({'phase': 'global_condition', 'exception_type': 'GlobalConditionUnsatisfied',
                                   'message': f'Global success condition failed for {plan.task_id}.'})
        except ConditionEvaluationError as exc:
            control.cancel(str(exc))
            exception = exc
            status = 'failed'
            errors.append(error_record(exc, phase='condition'))
        except BaseException as exc:
            exception = exc
            status = 'timeout' if isinstance(exc, PlanExecutionTimeout) else 'cancelled' if isinstance(exc, ExecutionCancelled) else 'failed'
            if status == 'timeout':
                stats.record_timeout(str(exc))
            errors.append(error_record(exc, phase='task'))
            raise
        finally:
            actions = [action for stage_report in stage_reports for action in stage_report['actions']]
            known = {action['action_key'] for action in actions}
            for stage_index, stage in enumerate(plan.stages):
                for robot_id, queue in stage.robot_action_queues.items():
                    for cursor, action in enumerate(queue):
                        key = f'{stage_index}:{robot_id}:{cursor}'
                        if key not in known:
                            actions.append({'action_key': key, 'stage_id': stage.stage_id,
                                'robot_id': robot_id, 'cursor': cursor, 'action_type': action.action_type,
                                'status': 'unexecuted', 'reason': 'previous_stage_failed' if exception is None else status,
                                'attempts': 0, 'requested_failure_policy': action.on_failure})
            report = stats.to_dict(status='failed' if status == 'failed' else 'success')
            report.update(task_id=plan.task_id, execution_status=status, scheduler_version=2,
                          stages=stage_reports, actions=actions, errors=errors,
                          global_condition_satisfied=global_result,
                          final_snapshot=snapshot_report(world_state.snapshot),
                          worker_errors=list(getattr(self.runtime, 'worker_errors', [])),
                          execution_quiescent=getattr(self.runtime, 'execution_quiescent', True))
            self.runtime.execution_report = report
            self.runtime.action_metrics = {key: report[key] for key in
                ('action_counts', 'raw_action_sr', 'ignored_failure_count')}
        if status == 'failed' and self.execution_policy is ExecutionPolicy.STRICT:
            raise PlanExecutionError(report) from exception
        return world_state


class StageController(TaskRunner):
    """Compatibility name for the task-level runner."""

    pass
