"""Task plan parser and phase execution helpers."""
from typing import Any, Dict, List, Sequence, Tuple

from .action_plan import Action, PlannedAction, StagePlan, TaskPlan, TaskRunner
from .actions import _PlannedActionContext, _planned_action_local
from .context import get_runtime
from .utils import RobotRef, log, robot_name


def run_subtask_sequence(
    robot: RobotRef,
    subtask_functions: Sequence[Any],
) -> None:
    planned_actions: List[PlannedAction] = []
    for subtask_function in subtask_functions:
        planned_actions.extend(getattr(subtask_function, "planned_actions", ()))

    previous_context = getattr(_planned_action_local, "context", None)
    if planned_actions:
        _planned_action_local.context = _PlannedActionContext(list(planned_actions))
    try:
        for subtask_function in subtask_functions:
            subtask_function(robot)
    finally:
        if previous_context is None:
            if hasattr(_planned_action_local, "context"):
                delattr(_planned_action_local, "context")
        else:
            _planned_action_local.context = previous_context


def run_phase(
    phase_name: str,
    assignments: Sequence[Tuple[RobotRef, Sequence[Any]]],
) -> None:
    log(phase_name)
    if not assignments:
        return

    plan = TaskPlanParser(str(phase_name)).parse([(str(phase_name), assignments)])
    TaskRunner(get_runtime()).execute(plan)


def plan_action(action_type: str, *args: Any, **kwargs: Any) -> Action:
    parameters = dict(kwargs.pop("parameters", {}) or {})
    if args:
        parameters["args"] = tuple(args)
    return Action(action_type, parameters, **kwargs)


_ACTION_HELPER_NAMES = (
    "WaitOneTick",
    "GoToObject",
    "PickupObject",
    "TeleportObjectToHand",
    "PutObject",
    "SwitchOn",
    "SwitchOff",
    "OpenObject",
    "CloseObject",
    "BreakObject",
    "BreakEgg",
    "SliceObject",
    "CleanObject",
    "DirtyObject",
    "EmptyLiquid",
    "RunMicrowave",
    "RunCoffeeMachine",
    "RunToaster",
    "CookByStoveBurner",
    "HeatByStoveBurner",
    "FireByStoveBurner",
    "FillWater",
    "ColdObject",
    "ThrowObject",
)

_MISSING_GLOBAL = object()


class TaskPlanParser:
    """Record demo1-style task functions into a multi-stage action plan."""

    def __init__(self, task_id: str = "task") -> None:
        self.task_id = task_id
        self.stages: List[StagePlan] = []

    def phase(
        self,
        phase_name: str,
        assignments: Sequence[Tuple[RobotRef, Sequence[Any]]],
    ) -> "TaskPlanParser":
        queues: Dict[str, List[Action]] = {}
        for robot, subtask_functions in assignments:
            robot_id = robot_name(robot)
            if not robot_id:
                raise RuntimeError(f"Cannot infer robot name from {robot!r}.")
            robot_actions: List[Action] = []
            for subtask_function in subtask_functions:
                robot_actions.extend(self.record_subtask(robot, subtask_function))
            queues[robot_id] = [
                action.with_robot(robot_id)
                for action in robot_actions
            ]
        self.stages.append(StagePlan(str(phase_name), queues))
        return self

    def parse(
        self,
        phases: Sequence[Tuple[str, Sequence[Tuple[RobotRef, Sequence[Any]]]]],
    ) -> TaskPlan:
        for phase_name, assignments in phases:
            self.phase(phase_name, assignments)
        return self.to_task_plan()

    def to_task_plan(self) -> TaskPlan:
        return TaskPlan(self.task_id, list(self.stages))

    def record_subtask(
        self,
        robot: RobotRef,
        subtask_function: Any,
    ) -> List[Action]:
        recorded_actions: List[Action] = []
        planned_actions = tuple(getattr(subtask_function, "planned_actions", ()) or ())

        subtask_globals = getattr(subtask_function, "__globals__", None)
        if not isinstance(subtask_globals, dict):
            if planned_actions:
                return [Action.from_any(action) for action in planned_actions]
            raise RuntimeError(f"Cannot record actions from {subtask_function!r}.")

        originals = {
            name: subtask_globals.get(name, _MISSING_GLOBAL)
            for name in _ACTION_HELPER_NAMES
        }
        try:
            for action_name in _ACTION_HELPER_NAMES:
                subtask_globals[action_name] = self._make_action_recorder(
                    action_name,
                    recorded_actions,
                )
            subtask_function(robot)
        finally:
            for action_name, original in originals.items():
                if original is _MISSING_GLOBAL:
                    subtask_globals.pop(action_name, None)
                else:
                    subtask_globals[action_name] = original

        if not recorded_actions and planned_actions:
            return [Action.from_any(action) for action in planned_actions]
        return recorded_actions

    def _make_action_recorder(
        self,
        action_type: str,
        recorded_actions: List[Action],
    ) -> Any:
        def record_action(_robot: RobotRef, *args: Any, **kwargs: Any) -> None:
            recorded_actions.append(plan_action(action_type, *args, **kwargs))

        return record_action


def run_action_plan(plan: TaskPlan) -> None:
    TaskRunner(get_runtime()).execute(plan)
