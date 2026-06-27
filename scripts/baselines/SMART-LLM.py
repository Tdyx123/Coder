#!/usr/bin/env python3
"""Convert legacy SMART-LLM code_plan.py outputs into executor bundles."""

from __future__ import annotations

import argparse
import ast
import json
import py_compile
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _path in (SCRIPTS_DIR, REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from executor_system.pddlrun_adapter import ObjectNameResolver


DEFAULT_INPUT_ROOT = REPO_ROOT / "baselines" / "SMART-LLM" / "logs"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "baselines" / "SMART-LLM"

PYTHON_FENCE_RE = re.compile(
    r"```(?:python|py)?[ \t]*\n(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
FALLBACK_CODE_LINE_RE = re.compile(
    r"(?m)^\s*(?:import\s+|from\s+|def\s+|#\s*CODE\b)",
)
TEAM_PARAMETER_NAMES = {"robot_list", "robot_team", "team"}
SUPPORTED_ACTIONS = {
    "GoToObject",
    "PickupObject",
    "PutObject",
    "OpenObject",
    "CloseObject",
    "BreakObject",
    "SliceObject",
    "SwitchOn",
    "SwitchOff",
    "CleanObject",
    "DirtyObject",
    "EmptyLiquid",
    "FillWater",
    "RunMicrowave",
    "RunCoffeeMachine",
    "RunToaster",
    "CookByStoveBurner",
    "HeatByStoveBurner",
    "FireByStoveBurner",
    "ColdObject",
    "PrepareEgg",
    "ThrowObject",
}
THREAD_CONSTRUCTORS = {"threading.Thread", "Thread"}


class SmartLLMConversionError(Exception):
    """Raised when a SMART-LLM run cannot be converted safely."""

    def __init__(self, skip_reason: str, message: Optional[str] = None) -> None:
        self.skip_reason = skip_reason
        super().__init__(message or skip_reason)


@dataclass(frozen=True)
class ExtractedCode:
    code: str
    method: str
    block_count: int


@dataclass(frozen=True)
class CodeClassification:
    parse_status: str
    schedule_type: str
    function_count: int
    top_level_driver_count: int
    uses_team_or_robot_list: bool
    uses_time_sleep: bool
    syntax_error: Optional[str] = None


@dataclass(frozen=True)
class LogMetadata:
    task: str
    floor_plan: Optional[str]
    test_set: Optional[str]
    robots: List[Any] = field(default_factory=list)
    objects: List[Any] = field(default_factory=list)


@dataclass(frozen=True)
class EncodedAction:
    robot_id: str
    action_type: str
    args: Tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class FunctionInvocation:
    function_name: str
    args: Tuple[ast.AST, ...]
    keywords: Tuple[ast.keyword, ...] = ()


@dataclass
class ConversionResult:
    status: str
    success: bool
    source_path: str
    task_run_dir: str
    relative_task_dir: str
    category: str
    parse_status: str
    schedule_type: str
    function_count: int
    top_level_driver_count: int
    uses_team_or_robot_list: bool
    uses_time_sleep: bool
    extraction_method: str
    extracted_block_count: int
    skip_reason: Optional[str]
    syntax_error: Optional[str]
    error: Optional[str]
    task: Optional[str]
    floor_plan: Optional[str]
    test_set: Optional[str]
    task_file: Optional[str]
    task_index: Optional[int]
    action_count: int
    stage_count: int
    no_trans: Optional[int]
    object_mappings: Dict[str, str]
    object_mapping_warnings: List[str]
    generation_time: float
    generated: Dict[str, Optional[str]]


RobotBinding = Tuple[str, ...]
RobotBindings = Dict[str, RobotBinding]
StageQueues = Dict[str, List[Dict[str, Any]]]


def normalize_floor_plan(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("floorplan"):
        return text[len("FloorPlan") :]
    return text


def extract_code(text: str) -> ExtractedCode:
    blocks = [match.group(1).strip("\n") for match in PYTHON_FENCE_RE.finditer(text)]
    if blocks:
        return ExtractedCode(
            code=max(blocks, key=len).strip(),
            method="fenced_python_block",
            block_count=len(blocks),
        )

    match = FALLBACK_CODE_LINE_RE.search(text)
    if not match:
        return ExtractedCode(code="", method="none", block_count=0)

    return ExtractedCode(
        code=text[match.start() :].strip(),
        method="fallback_first_code_line",
        block_count=0,
    )


def call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return ""


def contains_call(node: ast.AST, target_name: str) -> bool:
    return any(
        isinstance(child, ast.Call) and call_name(child) == target_name
        for child in ast.walk(node)
    )


def is_robot_subscript(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "robots"


def list_contains_robot_team(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.List, ast.Tuple))
        and sum(1 for item in node.elts if is_robot_subscript(item)) > 1
    )


def has_team_usage(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for arg in node.args.args:
                if arg.arg in TEAM_PARAMETER_NAMES:
                    return True
        elif isinstance(node, ast.Name) and node.id in TEAM_PARAMETER_NAMES:
            return True
        elif list_contains_robot_team(node):
            return True
    return False


def has_time_sleep(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and call_name(node) == "time.sleep"
        for node in ast.walk(tree)
    )


def top_level_direct_driver_count(tree: ast.Module, function_names: Iterable[str]) -> int:
    names = set(function_names)
    count = 0
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if call_name(stmt.value) in names:
                count += 1
    return count


def classify_parsed_code(tree: ast.Module) -> CodeClassification:
    function_names = [
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    ]
    function_count = len(function_names)
    driver_count = top_level_direct_driver_count(tree, function_names)
    has_threading = any(contains_call(stmt, "threading.Thread") for stmt in tree.body)
    has_threading = has_threading or any(contains_call(stmt, "Thread") for stmt in tree.body)

    if has_threading and driver_count:
        schedule_type = "staged_mixed"
    elif has_threading:
        schedule_type = "threaded_parallel"
    elif function_count == 1 and driver_count:
        schedule_type = "single_wrapper_sequential"
    elif driver_count:
        schedule_type = "multi_function_sequential"
    else:
        schedule_type = "no_clear_driver"

    return CodeClassification(
        parse_status="parse_ok",
        schedule_type=schedule_type,
        function_count=function_count,
        top_level_driver_count=driver_count,
        uses_team_or_robot_list=has_team_usage(tree),
        uses_time_sleep=has_time_sleep(tree),
    )


def classify_code(code: str) -> Tuple[CodeClassification, Optional[ast.Module]]:
    if not code.strip():
        return (
            CodeClassification(
                parse_status="no_code",
                schedule_type="no_clear_driver",
                function_count=0,
                top_level_driver_count=0,
                uses_team_or_robot_list=False,
                uses_time_sleep=False,
            ),
            None,
        )

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        message = f"{exc.msg} at line {exc.lineno}, offset {exc.offset}"
        return (
            CodeClassification(
                parse_status="syntax_error",
                schedule_type="no_clear_driver",
                function_count=0,
                top_level_driver_count=0,
                uses_team_or_robot_list=False,
                uses_time_sleep=False,
                syntax_error=message,
            ),
            None,
        )

    return classify_parsed_code(tree), tree


def iter_code_plan_paths(input_root: Path, floor_plan: Optional[str], limit: Optional[int]) -> List[Path]:
    normalized_floor = normalize_floor_plan(floor_plan) if floor_plan else None
    selected: List[Path] = []

    for path in sorted(input_root.rglob("code_plan.py")):
        if "plan_to_code" in path.parts:
            continue

        try:
            relative_parent = path.parent.relative_to(input_root)
        except ValueError:
            continue

        parts = relative_parent.parts
        if not parts:
            continue
        if normalized_floor and normalize_floor_plan(parts[0]) != normalized_floor:
            continue

        selected.append(path)
        if limit is not None and len(selected) >= limit:
            break

    return selected


def output_paths_for(source_path: Path, input_root: Path, output_root: Path) -> Tuple[Path, Path, str]:
    relative_task_dir = source_path.parent.relative_to(input_root)
    output_dir = output_root / "logs" / relative_task_dir / "plan_to_code"
    return output_dir / "executable_plan.py", output_dir / "conversion_summary.json", str(relative_task_dir)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_log_text(task_run_dir: Path) -> str:
    log_path = task_run_dir / "log.txt"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def read_log_assignment_from_text(text: str, name: str, default: Any = None) -> Any:
    pattern = re.compile(rf"(?m)^{re.escape(name)}\s*=\s*(.+)$")
    match = pattern.search(text)
    if not match:
        return default

    try:
        return ast.literal_eval(match.group(1).strip())
    except (SyntaxError, ValueError):
        return default


def read_log_assignment(task_run_dir: Path, name: str, default: Any = None) -> Any:
    return read_log_assignment_from_text(read_log_text(task_run_dir), name, default)


def read_log_field(text: str, label: str) -> Optional[str]:
    pattern = re.compile(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$")
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def read_task_from_log(task_run_dir: Path) -> str:
    for line in read_log_text(task_run_dir).splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def read_log_metadata(task_run_dir: Path) -> LogMetadata:
    text = read_log_text(task_run_dir)
    robots = read_log_assignment_from_text(text, "robots", default=[])
    objects = read_log_assignment_from_text(text, "objects", default=[])
    if not isinstance(robots, list):
        robots = []
    if not isinstance(objects, list):
        objects = []
    return LogMetadata(
        task=read_task_from_log(task_run_dir),
        floor_plan=read_log_field(text, "Floor Plan"),
        test_set=read_log_field(text, "test-set"),
        robots=robots,
        objects=objects,
    )


def object_names_from_log(objects: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for item in objects:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return names


def robot_names_from_log(robots: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for index, item in enumerate(robots):
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        names.append(str(name or f"robot{index + 1}"))
    return names


def dataset_path_for_metadata(metadata: LogMetadata) -> Path:
    if not metadata.test_set or not metadata.floor_plan:
        raise SmartLLMConversionError(
            "missing_metadata",
            "log.txt is missing test-set or Floor Plan.",
        )
    task_file = (
        REPO_ROOT
        / "data"
        / metadata.test_set
        / f"FloorPlan{normalize_floor_plan(metadata.floor_plan)}.jsonl"
    )
    if not task_file.is_file():
        raise SmartLLMConversionError("missing_task_file", f"Dataset task file not found: {task_file}")
    return task_file


def find_task_index(task_file: Path, task_text: str) -> int:
    normalized_task = task_text.strip()
    if not normalized_task:
        raise SmartLLMConversionError("missing_metadata", "log.txt has no task text.")

    with task_file.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmartLLMConversionError("invalid_task_file", str(exc)) from exc
            if str(record.get("task", "")).strip() == normalized_task:
                return index

    raise SmartLLMConversionError(
        "task_not_found",
        f"Task text was not found by exact match in {task_file}.",
    )


def constant_int(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = constant_int(node.operand)
        if value is not None:
            return -value
    return None


def subscript_index(node: ast.Subscript) -> Optional[int]:
    return constant_int(node.slice)


def robot_sequence_from_expr(
    node: ast.AST,
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
) -> RobotBinding:
    if isinstance(node, ast.Name):
        if node.id in bindings:
            return tuple(bindings[node.id])
        if node.id in robot_names:
            return (node.id,)
        if node.id == "robots":
            return tuple(robot_names)
        raise SmartLLMConversionError(
            "unsupported_robot_expression",
            f"Cannot resolve robot variable {node.id!r}.",
        )

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = str(node.value)
        if value in robot_names or re.fullmatch(r"robot\d+", value):
            return (value,)
        raise SmartLLMConversionError(
            "unsupported_robot_expression",
            f"String {value!r} is not a robot reference.",
        )

    if isinstance(node, ast.Subscript):
        base = robot_sequence_from_expr(node.value, bindings, robot_names)
        index = subscript_index(node)
        if index is None:
            raise SmartLLMConversionError(
                "unsupported_robot_expression",
                "Robot subscript must use a constant integer index.",
            )
        try:
            return (base[index],)
        except IndexError as exc:
            raise SmartLLMConversionError(
                "invalid_robot_reference",
                f"Robot index {index} is out of range for {list(base)!r}.",
            ) from exc

    if isinstance(node, (ast.List, ast.Tuple)):
        robots: List[str] = []
        for item in node.elts:
            robots.extend(robot_sequence_from_expr(item, bindings, robot_names))
        if len(robots) > 1:
            raise SmartLLMConversionError(
                "multi_robot_team",
                f"Team binding resolves multiple robots: {robots!r}.",
            )
        return tuple(robots)

    raise SmartLLMConversionError(
        "unsupported_robot_expression",
        f"Unsupported robot expression: {ast.dump(node, include_attributes=False)}.",
    )


def single_robot_from_expr(
    node: ast.AST,
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
) -> str:
    robots = robot_sequence_from_expr(node, bindings, robot_names)
    if len(robots) != 1:
        raise SmartLLMConversionError(
            "multi_robot_team",
            f"Expected one robot, got {list(robots)!r}.",
        )
    return robots[0]


def default_robot_from_bindings(bindings: Mapping[str, RobotBinding], robot_names: Sequence[str]) -> str:
    referenced = {robot for robots in bindings.values() for robot in robots}
    if not referenced and len(robot_names) == 1:
        return robot_names[0]
    if len(referenced) == 1:
        return next(iter(referenced))
    if len(referenced) > 1:
        raise SmartLLMConversionError(
            "multi_robot_team",
            f"Cannot attach Wait to multiple robots: {sorted(referenced)!r}.",
        )
    raise SmartLLMConversionError(
        "unsupported_robot_expression",
        "Cannot infer a robot for time.sleep.",
    )


def bind_function_parameters(
    function: ast.FunctionDef,
    invocation: FunctionInvocation,
    caller_bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
) -> RobotBindings:
    if invocation.keywords:
        raise SmartLLMConversionError(
            "unsupported_function_call",
            f"Keyword arguments are not supported for {function.name}().",
        )

    parameters = [arg.arg for arg in function.args.args]
    if len(invocation.args) > len(parameters):
        raise SmartLLMConversionError(
            "unsupported_function_call",
            f"{function.name}() has too many arguments.",
        )

    bindings: RobotBindings = {}
    for parameter, value_node in zip(parameters, invocation.args):
        resolved = robot_sequence_from_expr(value_node, caller_bindings, robot_names)
        if parameter in TEAM_PARAMETER_NAMES and len(resolved) != 1:
            raise SmartLLMConversionError(
                "multi_robot_team",
                f"{parameter} for {function.name}() resolves to {list(resolved)!r}.",
            )
        if len(resolved) > 1:
            raise SmartLLMConversionError(
                "multi_robot_team",
                f"{parameter} for {function.name}() resolves to {list(resolved)!r}.",
            )
        bindings[parameter] = tuple(resolved)

    for parameter in parameters[len(invocation.args) :]:
        if parameter in TEAM_PARAMETER_NAMES:
            raise SmartLLMConversionError(
                "unsupported_function_call",
                f"{function.name}() is missing team parameter {parameter!r}.",
            )

    return bindings


def string_constant(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return str(node.value)
    raise SmartLLMConversionError(
        "unsupported_action_argument",
        f"Object argument must be a string literal: {ast.dump(node, include_attributes=False)}.",
    )


def ast_source(code: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(code, node)
    return segment.strip() if segment else ast.dump(node, include_attributes=False)


def encode_action_call(
    call: ast.Call,
    action_type: str,
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
    resolver: ObjectNameResolver,
    code: str,
) -> EncodedAction:
    if call.keywords:
        raise SmartLLMConversionError(
            "unsupported_action_call",
            f"Keyword arguments are not supported for {action_type}.",
        )
    if not call.args:
        robot_id = default_robot_from_bindings(bindings, robot_names)
        object_nodes: Sequence[ast.AST] = ()
    else:
        robot_id = single_robot_from_expr(call.args[0], bindings, robot_names)
        object_nodes = call.args[1:]

    object_args = tuple(resolver.resolve(string_constant(arg)) for arg in object_nodes)
    return EncodedAction(
        robot_id=robot_id,
        action_type=action_type,
        args=object_args,
        raw=ast_source(code, call),
    )


def encode_wait_call(
    call: ast.Call,
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
    code: str,
) -> EncodedAction:
    return EncodedAction(
        robot_id=default_robot_from_bindings(bindings, robot_names),
        action_type="Wait",
        args=(),
        raw=ast_source(code, call),
    )


def action_to_dict(action: EncodedAction) -> Dict[str, Any]:
    return {
        "action_type": action.action_type,
        "parameters": {"args": list(action.args)},
        "robot_id": action.robot_id,
    }


def function_invocation_from_call(call: ast.Call, function_names: Iterable[str]) -> Optional[FunctionInvocation]:
    name = call_name(call)
    if name not in set(function_names):
        return None
    return FunctionInvocation(
        function_name=name,
        args=tuple(call.args),
        keywords=tuple(call.keywords),
    )


def encode_function_invocation(
    invocation: FunctionInvocation,
    functions: Mapping[str, ast.FunctionDef],
    caller_bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
    resolver: ObjectNameResolver,
    code: str,
    stack: Tuple[str, ...] = (),
) -> List[EncodedAction]:
    if invocation.function_name not in functions:
        raise SmartLLMConversionError(
            "unsupported_function_call",
            f"Unknown function {invocation.function_name!r}.",
        )
    if invocation.function_name in stack:
        raise SmartLLMConversionError(
            "unsupported_function_body",
            f"Recursive function call is unsupported: {' -> '.join(stack + (invocation.function_name,))}.",
        )

    function = functions[invocation.function_name]
    bindings = bind_function_parameters(function, invocation, caller_bindings, robot_names)
    actions: List[EncodedAction] = []

    for stmt in function.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Return) and stmt.value is None:
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            try:
                bindings[stmt.targets[0].id] = robot_sequence_from_expr(stmt.value, bindings, robot_names)
                continue
            except SmartLLMConversionError:
                raise SmartLLMConversionError(
                    "unsupported_function_body",
                    f"Unsupported assignment in {function.name}(): {ast_source(code, stmt)}.",
                )
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            raise SmartLLMConversionError(
                "unsupported_function_body",
                f"Unsupported statement in {function.name}(): {ast_source(code, stmt)}.",
            )

        call = stmt.value
        name = call_name(call)
        if name in SUPPORTED_ACTIONS:
            actions.append(encode_action_call(call, name, bindings, robot_names, resolver, code))
        elif name == "time.sleep":
            actions.append(encode_wait_call(call, bindings, robot_names, code))
        elif name in functions:
            nested = FunctionInvocation(name, tuple(call.args), tuple(call.keywords))
            actions.extend(
                encode_function_invocation(
                    nested,
                    functions,
                    bindings,
                    robot_names,
                    resolver,
                    code,
                    stack + (invocation.function_name,),
                )
            )
        else:
            raise SmartLLMConversionError(
                "unsupported_function_body",
                f"Unsupported call in {function.name}(): {ast_source(code, call)}.",
            )

    used_robots = {action.robot_id for action in actions}
    if len(used_robots) > 1:
        raise SmartLLMConversionError(
            "multi_robot_team",
            f"{function.name}() emits actions for multiple robots: {sorted(used_robots)!r}.",
        )

    return actions


def is_thread_constructor(call: ast.Call) -> bool:
    return call_name(call) in THREAD_CONSTRUCTORS


def thread_invocation_from_constructor(
    call: ast.Call,
    function_names: Iterable[str],
) -> FunctionInvocation:
    names = set(function_names)
    target_node: Optional[ast.AST] = None
    args_node: Optional[ast.AST] = None

    for keyword in call.keywords:
        if keyword.arg == "target":
            target_node = keyword.value
        elif keyword.arg == "args":
            args_node = keyword.value

    if target_node is None and len(call.args) >= 2:
        target_node = call.args[1]
    if args_node is None and len(call.args) >= 3:
        args_node = call.args[2]

    if not isinstance(target_node, ast.Name) or target_node.id not in names:
        raise SmartLLMConversionError(
            "unsupported_threading",
            "threading.Thread target must be a named local function.",
        )

    if args_node is None:
        args: Tuple[ast.AST, ...] = ()
    elif isinstance(args_node, (ast.Tuple, ast.List)):
        args = tuple(args_node.elts)
    else:
        raise SmartLLMConversionError(
            "unsupported_threading",
            "threading.Thread args must be a tuple or list literal.",
        )

    return FunctionInvocation(target_node.id, args)


def thread_method_target(call: ast.Call, method_name: str) -> Optional[str]:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == method_name
        and isinstance(func.value, ast.Name)
    ):
        return func.value.id
    return None


def append_stage(stages: List[StageQueues], actions: Sequence[EncodedAction]) -> None:
    if not actions:
        return
    queues: StageQueues = {}
    for action in actions:
        queues.setdefault(action.robot_id, []).append(action_to_dict(action))
    if queues:
        stages.append(queues)


def build_stage_plan_from_ast(
    tree: ast.Module,
    resolver: ObjectNameResolver,
    robot_names: Sequence[str],
    code: str,
) -> Tuple[List[StageQueues], int]:
    if not robot_names:
        raise SmartLLMConversionError("missing_metadata", "log.txt is missing robots.")

    functions: Dict[str, ast.FunctionDef] = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    stages: List[StageQueues] = []
    global_bindings: RobotBindings = {"robots": tuple(robot_names)}
    thread_invocations: Dict[str, FunctionInvocation] = {}
    active_threads: List[str] = []
    joined_threads: set[str] = set()

    def flush_active_threads() -> None:
        nonlocal active_threads
        if not active_threads:
            return
        queues: StageQueues = {}
        for thread_name in active_threads:
            invocation = thread_invocations.get(thread_name)
            if invocation is None:
                raise SmartLLMConversionError(
                    "unsupported_threading",
                    f"{thread_name}.start() has no static threading.Thread assignment.",
                )
            actions = encode_function_invocation(
                invocation,
                functions,
                global_bindings,
                robot_names,
                resolver,
                code,
            )
            for action in actions:
                queues.setdefault(action.robot_id, []).append(action_to_dict(action))
        if queues:
            stages.append(queues)
        active_threads = []
        joined_threads.clear()

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue

        if isinstance(stmt, ast.Assign):
            if isinstance(stmt.value, ast.Call) and is_thread_constructor(stmt.value):
                invocation = thread_invocation_from_constructor(stmt.value, functions)
                for target in stmt.targets:
                    if not isinstance(target, ast.Name):
                        raise SmartLLMConversionError(
                            "unsupported_threading",
                            "threading.Thread assignment target must be a name.",
                        )
                    thread_invocations[target.id] = invocation
                continue

            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                try:
                    global_bindings[stmt.targets[0].id] = robot_sequence_from_expr(
                        stmt.value,
                        global_bindings,
                        robot_names,
                    )
                    continue
                except SmartLLMConversionError:
                    pass

            raise SmartLLMConversionError(
                "unsupported_top_level",
                f"Unsupported top-level assignment: {ast_source(code, stmt)}.",
            )

        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue

        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            raise SmartLLMConversionError(
                "unsupported_top_level",
                f"Unsupported top-level statement: {ast_source(code, stmt)}.",
            )

        call = stmt.value
        start_target = thread_method_target(call, "start")
        if start_target:
            if start_target not in thread_invocations:
                raise SmartLLMConversionError(
                    "unsupported_threading",
                    f"{start_target}.start() has no static threading.Thread assignment.",
                )
            if start_target not in active_threads:
                active_threads.append(start_target)
            continue

        join_target = thread_method_target(call, "join")
        if join_target:
            joined_threads.add(join_target)
            if active_threads and all(name in joined_threads for name in active_threads):
                flush_active_threads()
            continue

        direct_invocation = function_invocation_from_call(call, functions)
        if direct_invocation is not None:
            flush_active_threads()
            actions = encode_function_invocation(
                direct_invocation,
                functions,
                global_bindings,
                robot_names,
                resolver,
                code,
            )
            append_stage(stages, actions)
            continue

        if call_name(call) in SUPPORTED_ACTIONS:
            flush_active_threads()
            action = encode_action_call(call, call_name(call), global_bindings, robot_names, resolver, code)
            append_stage(stages, [action])
            continue

        raise SmartLLMConversionError(
            "unsupported_top_level",
            f"Unsupported top-level call: {ast_source(code, call)}.",
        )

    flush_active_threads()
    action_count = sum(len(actions) for stage in stages for actions in stage.values())
    if not stages or action_count == 0:
        raise SmartLLMConversionError(
            "no_clear_driver",
            "No executable top-level schedule could be inferred.",
        )

    return stages, action_count


def build_task_plan_data(task_id: str, stages: Sequence[StageQueues]) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "stages": [
            {
                "stage_id": f"Stage {index + 1}",
                "robot_action_queues": stage,
            }
            for index, stage in enumerate(stages)
        ],
    }


def build_bundle_data(
    *,
    task: str,
    task_plan_data: Dict[str, Any],
    no_trans: int,
    object_mappings: Dict[str, str],
    object_mapping_warnings: Sequence[str],
) -> Dict[str, Any]:
    return {
        "task": task,
        "task_plan": task_plan_data,
        "no_trans": no_trans,
        "phases": [],
        "plan_files": {},
        "object_mappings": dict(object_mappings),
        "object_mapping_warnings": list(object_mapping_warnings),
        "object_id_bindings": [],
    }


def render_bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return pformat(bundle_data, width=100, sort_dicts=False)


def render_executable_plan(
    *,
    bundle_data: Dict[str, Any],
    task_file: Path,
    task_index: int,
) -> str:
    bundle_literal = render_bundle_literal(bundle_data)
    return f'''#!/usr/bin/env python3
"""Run a hardcoded SMART-LLM bundle through executor_system."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path({str(REPO_ROOT)!r})
_SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (_SCRIPT_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

RUNNER_MODE_ARG = "--runner-mode"
if RUNNER_MODE_ARG in sys.argv[1:]:
    os.environ["renderImage"] = "0"

from executor_system.generated_plan_runtime import main as run_generated_plan


BUNDLE_DATA = {bundle_literal}

TASK_FILE = {str(task_file)!r}
TASK_INDEX = {task_index!r}


if __name__ == "__main__":
    try:
        raise SystemExit(run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__))
    except RuntimeError as exc:
        print(f"ERROR: {{exc}}")
        raise SystemExit(1)
'''


def compile_python(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def base_result(
    *,
    source_path: Path,
    input_root: Path,
    output_root: Path,
    extracted: ExtractedCode,
    classification: CodeClassification,
) -> ConversionResult:
    executable_path, summary_path, relative_task_dir = output_paths_for(source_path, input_root, output_root)
    return ConversionResult(
        status="success",
        success=True,
        source_path=str(source_path),
        task_run_dir=str(source_path.parent),
        relative_task_dir=relative_task_dir,
        category=classification.schedule_type,
        parse_status=classification.parse_status,
        schedule_type=classification.schedule_type,
        function_count=classification.function_count,
        top_level_driver_count=classification.top_level_driver_count,
        uses_team_or_robot_list=classification.uses_team_or_robot_list,
        uses_time_sleep=classification.uses_time_sleep,
        extraction_method=extracted.method,
        extracted_block_count=extracted.block_count,
        skip_reason=None,
        syntax_error=classification.syntax_error,
        error=None,
        task=None,
        floor_plan=None,
        test_set=None,
        task_file=None,
        task_index=None,
        action_count=0,
        stage_count=0,
        no_trans=None,
        object_mappings={},
        object_mapping_warnings=[],
        generation_time=0.0,
        generated={
            "executable_plan": str(executable_path),
            "conversion_summary": str(summary_path),
        },
    )


def mark_skipped(result: ConversionResult, reason: str, message: Optional[str] = None) -> None:
    result.status = "skipped"
    result.success = False
    result.skip_reason = reason
    result.error = message
    result.generated["executable_plan"] = None


def mark_failed(result: ConversionResult, message: str) -> None:
    result.status = "failed"
    result.success = False
    result.error = message
    result.generated["executable_plan"] = None


def convert_one(
    source_path: Path,
    input_root: Path,
    output_root: Path,
    dry_run: bool,
    validate_code: bool,
) -> ConversionResult:
    started_at = time.time()
    source_text = source_path.read_text(encoding="utf-8", errors="replace")
    extracted = extract_code(source_text)
    classification, tree = classify_code(extracted.code)
    result = base_result(
        source_path=source_path,
        input_root=input_root,
        output_root=output_root,
        extracted=extracted,
        classification=classification,
    )
    executable_path, summary_path, _ = output_paths_for(source_path, input_root, output_root)

    try:
        if classification.parse_status != "parse_ok" or tree is None:
            raise SmartLLMConversionError(classification.parse_status)
        if classification.schedule_type == "no_clear_driver":
            raise SmartLLMConversionError("no_clear_driver")

        metadata = read_log_metadata(source_path.parent)
        result.task = metadata.task or None
        result.floor_plan = normalize_floor_plan(metadata.floor_plan or "") or None
        result.test_set = metadata.test_set

        task_file = dataset_path_for_metadata(metadata)
        task_index = find_task_index(task_file, metadata.task)
        robot_names = robot_names_from_log(metadata.robots)
        resolver = ObjectNameResolver(object_names_from_log(metadata.objects))
        stages, action_count = build_stage_plan_from_ast(
            tree,
            resolver=resolver,
            robot_names=robot_names,
            code=extracted.code,
        )
        task_plan_data = build_task_plan_data(
            f"smart_llm_{result.floor_plan or 'unknown'}_{task_index}",
            stages,
        )
        bundle_data = build_bundle_data(
            task=metadata.task,
            task_plan_data=task_plan_data,
            no_trans=action_count,
            object_mappings=dict(resolver.mappings),
            object_mapping_warnings=list(resolver.warnings),
        )

        result.task_file = str(task_file)
        result.task_index = task_index
        result.action_count = action_count
        result.stage_count = len(stages)
        result.no_trans = action_count
        result.object_mappings = dict(resolver.mappings)
        result.object_mapping_warnings = list(resolver.warnings)

        executable_plan = render_executable_plan(
            bundle_data=bundle_data,
            task_file=task_file,
            task_index=task_index,
        )
        compile(executable_plan, "executable_plan.py", "exec")

        if not dry_run:
            executable_path.parent.mkdir(parents=True, exist_ok=True)
            executable_path.write_text(executable_plan, encoding="utf-8")
            if validate_code:
                compile_python(executable_path)
    except SmartLLMConversionError as exc:
        mark_skipped(result, exc.skip_reason, str(exc))
    except (SyntaxError, OSError, py_compile.PyCompileError) as exc:
        mark_failed(result, str(exc))
    finally:
        result.generation_time = time.time() - started_at

    if not dry_run:
        write_json(summary_path, asdict(result))
    return result


def build_global_summary(
    results: Sequence[ConversionResult],
    input_root: Path,
    output_root: Path,
    dry_run: bool,
    duration_seconds: float,
) -> Dict[str, Any]:
    status_counts = Counter(result.status for result in results)
    success_count = status_counts.get("success", 0)

    return {
        "total_results": len(results),
        "successful_generations": success_count,
        "failed_generations": len(results) - success_count,
        "success_rate": success_count / len(results) * 100 if results else 0,
        "total_generation_time": sum(float(result.generation_time) for result in results),
        "dry_run": dry_run,
        "input_root": str(input_root),
        "output_dir": str(output_root),
        "duration_seconds": round(duration_seconds, 3),
    }


def write_global_summary(results: Sequence[ConversionResult], output_root: Path, summary: Dict[str, Any]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "plan_to_code_summary.json", summary)
    write_json(output_root / "plan_to_code_results.json", [asdict(result) for result in results])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify and convert legacy SMART-LLM code_plan.py outputs."
    )
    parser.add_argument(
        "--input-root",
        default=str(DEFAULT_INPUT_ROOT),
        help="Root containing SMART-LLM log folders, default baselines/SMART-LLM/logs.",
    )
    parser.add_argument(
        "--output-dir",
        "--output-root",
        dest="output_dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for plan_to_code_summary.json/results.json and SMART-LLM output tree.",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of source code_plan.py files to process.")
    parser.add_argument("--floor-plan", help="Optional floor/log folder filter, e.g. 2 or FloorPlan2.")
    parser.add_argument("--dry-run", action="store_true", help="Classify and summarize without writing code.")
    parser.add_argument(
        "--validate-code",
        action="store_true",
        default=True,
        help="Compile generated executable_plan.py files after writing them (default: true).",
    )
    parser.add_argument(
        "--no-validate-code",
        dest="validate_code",
        action="store_false",
        help="Skip py_compile validation of generated executable_plan.py files.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_root = Path(args.input_root).resolve()
    output_root = Path(args.output_dir).resolve()

    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if not input_root.exists() or not input_root.is_dir():
        parser.error(f"input root not found or not a directory: {input_root}")

    started_at = time.time()
    paths = iter_code_plan_paths(input_root, args.floor_plan, args.limit)
    results = [
        convert_one(
            path,
            input_root=input_root,
            output_root=output_root,
            dry_run=bool(args.dry_run),
            validate_code=bool(args.validate_code),
        )
        for path in paths
    ]
    duration = time.time() - started_at

    global_summary = build_global_summary(
        results=results,
        input_root=input_root,
        output_root=output_root,
        dry_run=bool(args.dry_run),
        duration_seconds=duration,
    )
    write_global_summary(results, output_root, global_summary)

    print(
        "\n=== SMART-LLM PLAN-TO-CODE BUNDLE GENERATION SUMMARY ===\n"
        "Total runs processed: {total}\n"
        "Successful generations: {success} ({rate:.1f}%)\n"
        "Summary files saved to: {output_dir}".format(
            total=global_summary["total_results"],
            success=global_summary["successful_generations"],
            rate=global_summary["success_rate"],
            output_dir=output_root,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
