"""Side-effect-free action/plan data and compatibility constants.

This foundational module depends only on the standard library. Execution,
validation and controller/media setup belong to their respective services.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# Supported flat THOR payload fields. Nested parameters remain the canonical
# representation; Action.from_any preserves these fields before normalization.
DIRECT_PAYLOAD_FIELDS = (
    'objectId', 'agentId', 'degrees', 'moveMagnitude', 'position', 'rotation',
    'horizon', 'throwMagnitude', 'standing', 'forceAction', 'placeStationary',
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


