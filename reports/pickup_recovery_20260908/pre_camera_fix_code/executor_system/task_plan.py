"""Task plan parser and phase execution helpers."""
import ast
import inspect
import textwrap
from types import FunctionType

from typing import Any, Dict, List, Sequence, Tuple

from .plan_types import (Action, PlannedAction, StagePlan, TaskPlan)
from .action_plan import (TaskRunner)
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
    run_action_plan(plan)


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
    "PrepareEgg",
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


def _validate_recordable_subtask(function, robot):
    """Validate the whole body before executing a globals-isolated recorder copy.

    Even attribute/subscript reads and operators may run arbitrary user code;
    the compatibility subset permits only names and literal data containers.
    More expressive tasks must provide explicit planned_actions.
    """
    def reject():
        raise RuntimeError('Cannot safely record this subtask; provide explicit planned_actions.')

    if not isinstance(function, FunctionType):
        reject()
    try:
        module = ast.parse(textwrap.dedent(inspect.getsource(function.__code__)))
    except (OSError, IOError, TypeError, SyntaxError, IndentationError):
        reject()
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        reject()
    definition = module.body[0]
    if definition.name != function.__name__:
        reject()
    helper_names = set(_ACTION_HELPER_NAMES)
    arguments = definition.args
    parameter_names = {arg.arg for arg in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}
    parameter_names.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg)
    if helper_names.intersection(parameter_names | set(function.__code__.co_freevars)):
        reject()

    values = dict(function.__globals__)
    values.update(zip(function.__code__.co_freevars,
                      (cell.cell_contents for cell in function.__closure__ or ())))
    positional = function.__code__.co_varnames[:function.__code__.co_argcount]
    defaults = function.__defaults__ or ()
    if defaults:
        values.update(zip(positional[-len(defaults):], defaults))
    values.update(function.__kwdefaults__ or {})
    if positional:
        values[positional[0]] = robot
    if arguments.vararg:
        values[arguments.vararg.arg] = ()
    if arguments.kwarg:
        values[arguments.kwarg.arg] = {}
    safe_locals = set()

    def inert(value, seen=None):
        if type(value) in (str, bytes, int, float, bool, type(None)):
            return True
        if type(value) not in (list, tuple, dict):
            return False
        seen = set() if seen is None else seen
        if id(value) in seen:
            return False
        seen = seen | {id(value)}
        entries = list(value.items()) if type(value) is dict else value
        return all(inert(entry, seen) for entry in entries)

    def literal(expression):
        if isinstance(expression, ast.Constant):
            return
        if isinstance(expression, ast.Name) and expression.id not in helper_names:
            if expression.id in safe_locals:
                return
            if expression.id in values and inert(values[expression.id]):
                return
            reject()
        if isinstance(expression, (ast.List, ast.Tuple)):
            for element in expression.elts:
                literal(element)
            return
        if isinstance(expression, ast.Dict):
            for key, value in zip(expression.keys, expression.values):
                # Dict keys must not dispatch user-defined __hash__/__eq__.
                if not isinstance(key, ast.Constant):
                    reject()
                literal(value)
            return
        if (isinstance(expression, ast.UnaryOp) and isinstance(expression.op, (ast.USub, ast.UAdd))
                and isinstance(expression.operand, ast.Constant)
                and type(expression.operand.value) in (int, float)):
            return
        reject()

    for statement in definition.body:
        if isinstance(statement, ast.Assign):
            if any(not isinstance(target, ast.Name) or target.id in helper_names
                   for target in statement.targets):
                reject()
            literal(statement.value)
            safe_locals.update(target.id for target in statement.targets)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if not isinstance(call.func, ast.Name) or call.func.id not in helper_names:
                reject()
            for argument in call.args:
                literal(argument)
            for keyword in call.keywords:
                if keyword.arg is None:
                    reject()
                literal(keyword.value)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue  # docstrings/literals have no runtime side effects
        elif isinstance(statement, ast.Pass):
            continue
        else:
            reject()


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
        if hasattr(subtask_function, 'planned_actions'):
            return [Action.from_any(action) for action in
                    (subtask_function.planned_actions or ())]
        _validate_recordable_subtask(subtask_function, robot)
        recorded_actions: List[Action] = []
        copied_globals = dict(subtask_function.__globals__)
        for action_name in _ACTION_HELPER_NAMES:
            copied_globals[action_name] = self._make_action_recorder(
                action_name, recorded_actions)
        recorded_function = FunctionType(
            subtask_function.__code__, copied_globals, subtask_function.__name__,
            subtask_function.__defaults__, subtask_function.__closure__)
        recorded_function.__kwdefaults__ = (
            dict(subtask_function.__kwdefaults__)
            if subtask_function.__kwdefaults__ is not None else None)
        recorded_function(robot)
        return recorded_actions

    def _make_action_recorder(
        self,
        action_type: str,
        recorded_actions: List[Action],
    ) -> Any:
        def record_action(_robot: RobotRef, *args: Any, **kwargs: Any) -> None:
            recorded_actions.append(plan_action(action_type, *args, **kwargs))

        return record_action


_DEFAULT_TIMEOUT = object()


def run_action_plan(plan: TaskPlan, *, timeout_seconds=_DEFAULT_TIMEOUT, execution_policy="legacy") -> None:
    runtime = get_runtime()
    if timeout_seconds is _DEFAULT_TIMEOUT:
        from .movement import MovementConfig, MovementMode
        mode = getattr(runtime, 'movement_config', None) or MovementConfig.resolve(None)
        timeout_seconds = 30.0 if mode.mode == MovementMode.TELEPORT else 60.0
    TaskRunner(runtime, execution_policy=execution_policy).execute(plan, timeout_seconds=timeout_seconds)
