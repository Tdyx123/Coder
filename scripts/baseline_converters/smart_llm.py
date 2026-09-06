#!/usr/bin/env python3
"""Convert legacy SMART-LLM code_plan.py outputs into executor bundles."""

from __future__ import annotations

import argparse
import ast
import json
import py_compile
import re
import sys
import textwrap
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _path in (SCRIPTS_DIR, REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from executor_system.pddlrun_adapter import ObjectNameResolver
from baseline_converters.generation_validation import (
    GenerationValidationError, generation_failure_counts, generation_failure_result,
    finite_nonnegative_number, prepare_generation_robots, validate_generation_plan,
)
from baseline_converters.common import (
    build_bundle_data as common_build_bundle_data,
    load_task_record,
    render_bundle_literal as common_render_bundle_literal,
    render_executable_plan as common_render_executable_plan,
)


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
DEFAULT_ROBOT_BINDING_NAME = "__default_robot__"
IMPLICIT_ROBOT_BINDING_NAMES = {"robots", DEFAULT_ROBOT_BINDING_NAME}
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
    "BreakEgg",
    "PrepareEgg",
    "ThrowObject",
}
THREAD_CONSTRUCTORS = {"threading.Thread", "Thread"}
RECOVERY_MODES = {"aggressive", "conservative"}
AGGRESSIVE_FENCE_RE = re.compile(
    r"```[ \t]*(?:(?:python|py)[ \t]*)?(?:\r?\n)?(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
RECOVERY_SECTION_RE = re.compile(
    r"(?im)^[ \t]*(?:\#{1,3}[ \t]*)?"
    r"(?P<label>CODE(?:[ \t]+Solution)?|TASK[ \t]+ALLOCATION|SOLUTION)"
    r"[ \t]*:?[^\n]*$"
)
ROBOT_KEYWORD_NAMES = {"robot", "robot_id", "agent", "agent_id"}
METADATA_ASSIGNMENT_NAMES = {"robots", "objects"}

RECOVERY_CONFIDENCE_RANK = {None: 0, "high": 1, "medium": 2, "low": 3}
RECOVERY_RULE_CONFIDENCE = {
    "extract_code_section": "high",
    "close_eof_delimiter": "medium",
    "trim_incomplete_tail": "medium",
    "ignore_metadata_assignment": "high",
    "synthesize_driver": "medium",
    "infer_robot_binding": "medium",
    "infer_robot_binding_fallback": "low",
    "lambda_thread": "medium",
    "multi_robot_stage_split": "low",
    "drop_unsupported_node": "low",
    "tolerant_ast_match": "high",
}


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
    objects_literal: Optional[str] = None


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
    default_robot: Optional[str] = None
    function_node: Optional[ast.FunctionDef] = None


@dataclass(frozen=True)
class RecoveryCandidate:
    code: str
    kind: str
    start_line: int
    end_line: int
    allocation_rank: int
    rules: Tuple[str, ...] = ()


@dataclass
class RecoveryContext:
    events: List[Dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        rule: str,
        node: Optional[ast.AST],
        action: str,
        detail: str,
    ) -> None:
        self.events.append(
            {
                "rule": rule,
                "line_start": getattr(node, "lineno", None),
                "line_end": getattr(node, "end_lineno", getattr(node, "lineno", None)),
                "action": action,
                "detail": detail,
            }
        )

    @property
    def rules(self) -> List[str]:
        return list(dict.fromkeys(str(event["rule"]) for event in self.events))

    @property
    def confidence(self) -> Optional[str]:
        confidence: Optional[str] = None
        for rule in self.rules:
            candidate = RECOVERY_RULE_CONFIDENCE.get(rule, "low")
            if RECOVERY_CONFIDENCE_RANK[candidate] > RECOVERY_CONFIDENCE_RANK[confidence]:
                confidence = candidate
        return confidence or "high"


@dataclass(frozen=True)
class RecoveryAttempt:
    candidate: RecoveryCandidate
    classification: CodeClassification
    stages: List["StageQueues"]
    action_count: int
    events: List[Dict[str, Any]]
    rules: List[str]
    confidence: Optional[str]


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
    conversion_kind: str = "direct"
    initial_skip_reason: Optional[str] = None
    recovery_confidence: Optional[str] = None
    recovery_rules: List[str] = field(default_factory=list)
    recovery_events: List[Dict[str, Any]] = field(default_factory=list)
    failure_reason: Optional[str] = None
    validation_error: Optional[Dict[str, Any]] = None
    cleanup_error: Optional[str] = None


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


def _line_number_at(text: str, offset: int) -> int:
    return text.count("\n", 0, max(offset, 0)) + 1


def _recovery_code_start(text: str) -> int:
    match = re.search(
        r"(?m)^[ \t]*(?:import[ \t]+|from[ \t]+|def[ \t]+|"
        r"[A-Za-z_]\w*[ \t]*=|[A-Za-z_]\w*(?:\.\w+)?[ \t]*\()",
        text,
    )
    return match.start() if match else 0


def _parseable_recovery_variants(text: str) -> List[Tuple[str, Tuple[str, ...]]]:
    normalized = textwrap.dedent(text).strip("\n `")
    if not normalized:
        return []

    variants: List[Tuple[str, Tuple[str, ...]]] = []
    seen: set[str] = set()

    def add(code: str, rules: Tuple[str, ...]) -> bool:
        candidate = code.strip()
        if not candidate or candidate in seen:
            return False
        try:
            ast.parse(candidate)
        except SyntaxError:
            return False
        seen.add(candidate)
        variants.append((candidate, rules))
        return True

    add(normalized, ())

    for closing in (")", "]", "}", "'", '"'):
        if add(normalized + closing, ("close_eof_delimiter",)):
            break

    lines = normalized.splitlines()
    parseable_prefixes = 0
    for end in range(len(lines) - 1, 0, -1):
        prefix = "\n".join(lines[:end]).rstrip()
        if add(prefix, ("trim_incomplete_tail",)):
            parseable_prefixes += 1
            if parseable_prefixes >= 6:
                break

    return variants


def recovery_candidates(text: str) -> List[RecoveryCandidate]:
    raw_candidates: List[Tuple[str, str, int, int]] = []

    for match in AGGRESSIVE_FENCE_RE.finditer(text):
        raw_candidates.append(
            (
                match.group(1),
                "fenced_block",
                match.start(1),
                2,
            )
        )

    markers = list(RECOVERY_SECTION_RE.finditer(text))
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        label = marker.group("label").lower()
        allocation_rank = 3 if "solution" in label else 1
        raw_candidates.append((text[start:end], "code_section", start, allocation_rank))
        raw_candidates.append((text[start:], "code_section_suffix", start, allocation_rank))

    extracted = extract_code(text)
    if extracted.code:
        start = text.find(extracted.code)
        raw_candidates.append((extracted.code, "conservative_extract", max(start, 0), 0))

    raw_candidates.append((text, "whole_response", 0, 0))

    candidates: List[RecoveryCandidate] = []
    seen: set[str] = set()
    for raw, kind, source_offset, allocation_rank in raw_candidates:
        code_offset = _recovery_code_start(raw)
        code_text = raw[code_offset:]
        start_offset = source_offset + code_offset
        for code, variant_rules in _parseable_recovery_variants(code_text):
            if code in seen:
                continue
            seen.add(code)
            rules = list(variant_rules)
            if kind.startswith("code_section"):
                rules.insert(0, "extract_code_section")
            candidates.append(
                RecoveryCandidate(
                    code=code,
                    kind=kind,
                    start_line=_line_number_at(text, start_offset),
                    end_line=_line_number_at(text, start_offset + len(code_text)),
                    allocation_rank=allocation_rank,
                    rules=tuple(rules),
                )
            )
    return candidates


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


def iter_plan_source_paths(input_root: Path, floor_plan: Optional[str], limit: Optional[int]) -> List[Path]:
    normalized_floor = normalize_floor_plan(floor_plan) if floor_plan else None
    selected: List[Path] = []
    seen_task_dirs: set[Path] = set()

    for path in sorted(input_root.rglob("code_plan.py")) + sorted(input_root.rglob("decomposed_plan.py")):
        if "plan_to_code" in path.parts:
            continue
        if path.parent in seen_task_dirs:
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

        seen_task_dirs.add(path.parent)
        selected.append(path)
        if limit is not None and len(selected) >= limit:
            break

    return selected


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


def _log_assignment_literal(text: str, name: str) -> Optional[str]:
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*(.*)$")
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def read_log_assignment_from_text(
    text: str, name: str, default: Any = None, *, strict: bool = False,
) -> Any:
    literal = _log_assignment_literal(text, name)
    if literal is None:
        return default

    try:
        return ast.literal_eval(literal)
    except (SyntaxError, ValueError) as exc:
        if strict:
            raise GenerationValidationError(
                "validation_data_missing", f"Invalid {name} assignment in log.txt.", {"field": name},
            ) from exc
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
    robots = read_log_assignment_from_text(text, "robots", default=[], strict=True)
    objects = read_log_assignment_from_text(text, "objects", default=[])
    if not isinstance(objects, list):
        objects = []
    return LogMetadata(
        task=read_task_from_log(task_run_dir),
        floor_plan=read_log_field(text, "Floor Plan"),
        test_set=read_log_field(text, "test-set"),
        robots=robots,
        objects=objects,
        objects_literal=_log_assignment_literal(text, "objects"),
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


def read_task_record_gcr(task_file: Path, task_index: int) -> List[Any]:
    if task_index < 0:
        raise SmartLLMConversionError("invalid_task_file", "task_index must be 0-based and non-negative.")

    with task_file.open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            if index != task_index:
                continue
            line = raw_line.strip()
            if not line:
                raise SmartLLMConversionError(
                    "invalid_task_file",
                    f"Dataset line {task_index} is empty: {task_file}",
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmartLLMConversionError("invalid_task_file", str(exc)) from exc
            gcr = record.get("object_states")
            if not isinstance(gcr, list):
                raise SmartLLMConversionError(
                    "invalid_task_file",
                    "Dataset task record is missing list object_states for BUNDLE_DATA['gcr'].",
                )
            return gcr

    raise SmartLLMConversionError(
        "invalid_task_file",
        f"task_index {task_index} is out of range for {task_file}",
    )


def constant_int(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = constant_int(node.operand)
        if value is not None:
            return -value
    return None


def constant_number(node: ast.AST) -> Optional[float]:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp):
        value = constant_number(node.operand)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
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


def explicit_robots_from_bindings(bindings: Mapping[str, RobotBinding]) -> set[str]:
    return {
        robot
        for name, robots in bindings.items()
        if name not in IMPLICIT_ROBOT_BINDING_NAMES
        for robot in robots
    }


def default_robot_from_bindings(
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
    action_name: str,
) -> str:
    referenced = explicit_robots_from_bindings(bindings)
    if len(referenced) == 1:
        return next(iter(referenced))
    if len(referenced) > 1:
        raise SmartLLMConversionError(
            "multi_robot_team",
            f"Cannot attach {action_name} to multiple robots: {sorted(referenced)!r}.",
        )

    default_binding = tuple(bindings.get(DEFAULT_ROBOT_BINDING_NAME, ()))
    if len(default_binding) == 1:
        return default_binding[0]
    if len(default_binding) > 1:
        raise SmartLLMConversionError(
            "multi_robot_team",
            f"Cannot attach {action_name} to multiple default robots: {list(default_binding)!r}.",
        )

    if not referenced and len(robot_names) == 1:
        return robot_names[0]
    raise SmartLLMConversionError(
        "unsupported_robot_expression",
        f"Cannot infer a robot for {action_name}.",
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

    if invocation.default_robot is not None:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = (invocation.default_robot,)
    elif DEFAULT_ROBOT_BINDING_NAME in caller_bindings:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = tuple(caller_bindings[DEFAULT_ROBOT_BINDING_NAME])

    explicit_robots = explicit_robots_from_bindings(bindings)
    if len(explicit_robots) == 1:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = (next(iter(explicit_robots)),)

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
        robot_id = default_robot_from_bindings(bindings, robot_names, action_type)
        object_nodes: Sequence[ast.AST] = ()
    else:
        try:
            robot_id = single_robot_from_expr(call.args[0], bindings, robot_names)
            object_nodes = call.args[1:]
        except SmartLLMConversionError:
            if isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                robot_id = default_robot_from_bindings(bindings, robot_names, action_type)
                object_nodes = call.args
            else:
                raise

    object_args = tuple(resolver.resolve(string_constant(arg)) for arg in object_nodes)
    if action_type == "PrepareEgg" and len(object_args) != 2:
        raise SmartLLMConversionError(
            "unsupported_action_call",
            "PrepareEgg requires exactly two object arguments: Egg and container.",
        )
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
) -> List[EncodedAction]:
    if call.keywords:
        raise SmartLLMConversionError(
            "unsupported_action_call",
            "Keyword arguments are not supported for time.sleep.",
        )
    if len(call.args) != 1:
        raise SmartLLMConversionError(
            "unsupported_action_call",
            "time.sleep requires exactly one numeric literal argument.",
        )

    duration = constant_number(call.args[0])
    if duration is None:
        raise SmartLLMConversionError(
            "unsupported_action_argument",
            f"time.sleep duration must be a numeric literal: {ast_source(code, call.args[0])}.",
        )

    robot_id = default_robot_from_bindings(bindings, robot_names, "time.sleep")
    raw = ast_source(code, call)
    tick_count = 1 if duration <= 5 else 2
    return [
        EncodedAction(
            robot_id=robot_id,
            action_type="WaitOneTick",
            args=(),
            raw=raw,
        )
        for _ in range(tick_count)
    ]


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
            actions.extend(encode_wait_call(call, bindings, robot_names, code))
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
    global_bindings: RobotBindings = {
        "robots": tuple(robot_names),
        DEFAULT_ROBOT_BINDING_NAME: (robot_names[0],),
    }
    thread_invocations: Dict[str, FunctionInvocation] = {}
    active_threads: List[str] = []
    joined_threads: set[str] = set()
    implicit_thread_count = 0

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
            invocation = thread_invocations[start_target]
            if not invocation.args and invocation.default_robot is None:
                thread_invocations[start_target] = FunctionInvocation(
                    invocation.function_name,
                    invocation.args,
                    invocation.keywords,
                    robot_names[implicit_thread_count % len(robot_names)],
                )
                implicit_thread_count += 1
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

        if call_name(call) == "time.sleep":
            flush_active_threads()
            actions = encode_wait_call(call, global_bindings, robot_names, code)
            append_stage(stages, actions)
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


class RecoveryRobotAllocator:
    def __init__(
        self,
        robots: Sequence[Any],
        objects: Sequence[Any],
        robot_names: Sequence[str],
        context: RecoveryContext,
    ) -> None:
        self.robot_names = list(robot_names)
        self.context = context
        self.used: set[str] = set()
        self.robot_data: Dict[str, Dict[str, Any]] = {}
        for index, item in enumerate(robots):
            name = self.robot_names[index] if index < len(self.robot_names) else f"robot{index + 1}"
            self.robot_data[name] = dict(item) if isinstance(item, dict) else {}
        self.object_masses = {}
        for item in objects:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            mass = finite_nonnegative_number(item.get("mass"))
            if mass is not None:
                self.object_masses[str(item["name"])] = mass

    def _requirements(self, function: ast.FunctionDef) -> Tuple[set[str], float]:
        skills: set[str] = set()
        max_mass = 0.0
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name in SUPPORTED_ACTIONS:
                skills.add(name)
            if name == "PickupObject":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        max_mass = max(max_mass, self.object_masses.get(str(arg.value), 0.0))
        return skills, max_mass

    def choose(self, function: ast.FunctionDef, *, allow_used: bool = True) -> str:
        skills, max_mass = self._requirements(function)
        eligible: List[str] = []
        for name in self.robot_names:
            data = self.robot_data.get(name, {})
            raw_skills = data.get("skills")
            available = set(item for item in raw_skills if isinstance(item, str)) if isinstance(raw_skills, list) else set()
            capacity = finite_nonnegative_number(data.get("mass_capacity"))
            # Allocation remains a heuristic. Invalid/missing metadata is retained
            # for the final validator, which checks only used actions in order.
            if capacity is None:
                capacity = float("inf")
            if (not available or skills.issubset(available)) and capacity >= max_mass:
                eligible.append(name)

        unused = [name for name in eligible if name not in self.used]
        choices = unused or (eligible if allow_used else [])
        if choices:
            selected = choices[0]
            self.used.add(selected)
            return selected

        selected = self.robot_names[0]
        self.used.add(selected)
        self.context.record(
            "infer_robot_binding_fallback",
            function,
            "fallback",
            f"No robot satisfies inferred requirements for {function.name}(); using {selected}.",
        )
        return selected

    def choose_many(self, function: ast.FunctionDef, count: int) -> Tuple[str, ...]:
        selected: List[str] = []
        temporarily_used = set(self.used)
        for _ in range(max(count, 1)):
            choice = self.choose(function)
            if choice in selected:
                alternatives = [name for name in self.robot_names if name not in selected]
                if alternatives:
                    choice = alternatives[0]
                    self.used.add(choice)
            selected.append(choice)
        self.used.update(temporarily_used)
        return tuple(selected)


def _dict_robot_name(node: ast.AST, robot_names: Sequence[str]) -> Optional[str]:
    if not isinstance(node, ast.Dict):
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    name = str(value.get("name", ""))
    return name if name in robot_names else None


def aggressive_robot_sequence_from_expr(
    node: ast.AST,
    bindings: Mapping[str, RobotBinding],
    robot_names: Sequence[str],
) -> RobotBinding:
    if isinstance(node, ast.Name):
        if node.id in bindings:
            return tuple(bindings[node.id])
        if node.id in robot_names:
            return (node.id,)
        normalized = re.fullmatch(r"robot_?(\d+)", node.id, flags=re.IGNORECASE)
        if normalized:
            candidate = f"robot{normalized.group(1)}"
            if candidate in robot_names:
                return (candidate,)
        if node.id == "robots":
            return tuple(robot_names)
        if node.id in {"robot", "agent"} and DEFAULT_ROBOT_BINDING_NAME in bindings:
            return tuple(bindings[DEFAULT_ROBOT_BINDING_NAME])
        raise SmartLLMConversionError(
            "unsupported_robot_expression",
            f"Cannot resolve robot variable {node.id!r}.",
        )

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = str(node.value)
        if value in robot_names:
            return (value,)
        normalized = re.fullmatch(r"robot[ _]?(\d+)", value, flags=re.IGNORECASE)
        if normalized and f"robot{normalized.group(1)}" in robot_names:
            return (f"robot{normalized.group(1)}",)
        raise SmartLLMConversionError("unsupported_robot_expression")

    dict_name = _dict_robot_name(node, robot_names)
    if dict_name:
        return (dict_name,)

    if isinstance(node, ast.Attribute) and node.attr == "name":
        return aggressive_robot_sequence_from_expr(node.value, bindings, robot_names)

    if isinstance(node, ast.Subscript):
        base = aggressive_robot_sequence_from_expr(node.value, bindings, robot_names)
        index = subscript_index(node)
        if index is None:
            raise SmartLLMConversionError("unsupported_robot_expression")
        try:
            return (base[index],)
        except IndexError as exc:
            raise SmartLLMConversionError("invalid_robot_reference") from exc

    if isinstance(node, (ast.List, ast.Tuple)):
        values: List[str] = []
        for item in node.elts:
            values.extend(aggressive_robot_sequence_from_expr(item, bindings, robot_names))
        return tuple(values)

    raise SmartLLMConversionError(
        "unsupported_robot_expression",
        f"Unsupported robot expression: {ast.dump(node, include_attributes=False)}.",
    )


def _team_parameter_size(function: ast.FunctionDef, parameter: str) -> int:
    maximum = -1
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == parameter
        ):
            index = subscript_index(node)
            if index is not None:
                maximum = max(maximum, index)
    return max(maximum + 1, 1)


def aggressive_bind_function_parameters(
    function: ast.FunctionDef,
    invocation: FunctionInvocation,
    caller_bindings: Mapping[str, RobotBinding],
    caller_values: Mapping[str, Any],
    robot_names: Sequence[str],
    allocator: RecoveryRobotAllocator,
    context: RecoveryContext,
) -> Tuple[RobotBindings, Dict[str, Any]]:
    parameters = [arg.arg for arg in function.args.args]
    supplied: Dict[str, ast.AST] = {
        parameter: value for parameter, value in zip(parameters, invocation.args)
    }
    for keyword in invocation.keywords:
        if keyword.arg and keyword.arg in parameters:
            supplied[keyword.arg] = keyword.value

    bindings: RobotBindings = {}
    values: Dict[str, Any] = dict(caller_values)
    defaults_start = len(parameters) - len(function.args.defaults)

    for index, parameter in enumerate(parameters):
        value_node = supplied.get(parameter)
        if value_node is not None:
            try:
                bindings[parameter] = aggressive_robot_sequence_from_expr(
                    value_node, caller_bindings, robot_names
                )
                continue
            except SmartLLMConversionError:
                try:
                    values[parameter] = ast.literal_eval(value_node)
                    continue
                except (ValueError, TypeError):
                    pass

        default_index = index - defaults_start
        if value_node is None and default_index >= 0:
            try:
                values[parameter] = ast.literal_eval(function.args.defaults[default_index])
                continue
            except (ValueError, TypeError):
                pass

        if parameter in TEAM_PARAMETER_NAMES:
            inferred = allocator.choose_many(function, _team_parameter_size(function, parameter))
        else:
            inferred = (allocator.choose(function),)
        bindings[parameter] = inferred
        context.record(
            "infer_robot_binding",
            function,
            "bind_parameter",
            f"Bound missing parameter {parameter!r} in {function.name}() to {list(inferred)!r}.",
        )

    if invocation.default_robot:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = (invocation.default_robot,)
    explicit = explicit_robots_from_bindings(bindings)
    if len(explicit) == 1:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = (next(iter(explicit)),)
    elif DEFAULT_ROBOT_BINDING_NAME in caller_bindings:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = tuple(caller_bindings[DEFAULT_ROBOT_BINDING_NAME])
    elif robot_names:
        bindings[DEFAULT_ROBOT_BINDING_NAME] = (robot_names[0],)
    return bindings, values


def _aggressive_scalar(node: ast.AST, values: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise SmartLLMConversionError("unsupported_action_argument") from exc


def aggressive_string_value(
    node: ast.AST,
    values: Mapping[str, Any],
    robot_names: Sequence[str],
) -> str:
    value = _aggressive_scalar(node, values)
    if not isinstance(value, str) or value in robot_names:
        raise SmartLLMConversionError("unsupported_action_argument")
    return value


def aggressive_encode_action_call(
    call: ast.Call,
    action_type: str,
    bindings: Mapping[str, RobotBinding],
    values: Mapping[str, Any],
    robot_names: Sequence[str],
    resolver: ObjectNameResolver,
    code: str,
) -> EncodedAction:
    positional = list(call.args)
    robot_node: Optional[ast.AST] = None
    object_nodes: List[ast.AST] = []

    for keyword in call.keywords:
        if keyword.arg in ROBOT_KEYWORD_NAMES:
            robot_node = keyword.value
        else:
            object_nodes.append(keyword.value)

    if positional:
        try:
            aggressive_robot_sequence_from_expr(positional[0], bindings, robot_names)
            robot_node = positional.pop(0)
        except SmartLLMConversionError:
            pass
    object_nodes = positional + object_nodes

    if robot_node is not None:
        robots = aggressive_robot_sequence_from_expr(robot_node, bindings, robot_names)
        if not robots:
            raise SmartLLMConversionError("unsupported_robot_expression")
        robot_id = robots[0]
    else:
        default = tuple(bindings.get(DEFAULT_ROBOT_BINDING_NAME, ()))
        robot_id = default[0] if default else robot_names[0]

    object_args = tuple(
        resolver.resolve(aggressive_string_value(node, values, robot_names))
        for node in object_nodes
    )
    if action_type == "PrepareEgg" and len(object_args) != 2:
        raise SmartLLMConversionError("unsupported_action_call")
    return EncodedAction(robot_id, action_type, object_args, ast_source(code, call))


def _evaluate_recovery_value(
    node: ast.AST,
    values: Mapping[str, Any],
    object_names: Sequence[str],
) -> Optional[Any]:
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _evaluate_recovery_value(node.operand, values, object_names)
        return None if value is None else not bool(value)
    if isinstance(node, ast.Call) and call_name(node) == "any":
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Compare)
                and len(child.ops) == 1
                and isinstance(child.ops[0], ast.Eq)
                and len(child.comparators) == 1
                and isinstance(child.comparators[0], ast.Constant)
                and isinstance(child.comparators[0].value, str)
            ):
                return str(child.comparators[0].value) in object_names
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _supported_call_count(nodes: Sequence[ast.stmt]) -> int:
    return sum(
        1
        for node in nodes
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and call_name(child) in SUPPORTED_ACTIONS
    )


def aggressive_encode_function_invocation(
    invocation: FunctionInvocation,
    functions: Mapping[str, ast.FunctionDef],
    caller_bindings: Mapping[str, RobotBinding],
    caller_values: Mapping[str, Any],
    robot_names: Sequence[str],
    object_names: Sequence[str],
    resolver: ObjectNameResolver,
    allocator: RecoveryRobotAllocator,
    context: RecoveryContext,
    code: str,
    stack: Tuple[str, ...] = (),
) -> List[EncodedAction]:
    function = invocation.function_node or functions.get(invocation.function_name)
    if function is None or invocation.function_name in stack:
        context.record(
            "drop_unsupported_node",
            function,
            "drop_call",
            f"Cannot expand function call {invocation.function_name}().",
        )
        return []

    bindings, values = aggressive_bind_function_parameters(
        function,
        invocation,
        caller_bindings,
        caller_values,
        robot_names,
        allocator,
        context,
    )
    local_functions = dict(functions)
    actions: List[EncodedAction] = []

    def encode_statements(statements: Sequence[ast.stmt]) -> None:
        for stmt in statements:
            if isinstance(stmt, ast.FunctionDef):
                local_functions[stmt.name] = stmt
                continue
            if isinstance(stmt, (ast.Pass,)):
                continue
            if isinstance(stmt, ast.Return):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                try:
                    bindings[target] = aggressive_robot_sequence_from_expr(
                        stmt.value, bindings, robot_names
                    )
                    continue
                except SmartLLMConversionError:
                    value = _evaluate_recovery_value(stmt.value, values, object_names)
                    if value is not None:
                        values[target] = value
                        continue
                context.record(
                    "drop_unsupported_node",
                    stmt,
                    "drop_assignment",
                    f"Dropped unsupported assignment in {function.name}().",
                )
                continue
            if isinstance(stmt, ast.If):
                condition = _evaluate_recovery_value(stmt.test, values, object_names)
                if condition is None:
                    chosen = (
                        stmt.body
                        if _supported_call_count(stmt.body)
                        >= _supported_call_count(stmt.orelse)
                        else stmt.orelse
                    )
                    context.record(
                        "drop_unsupported_node",
                        stmt,
                        "choose_branch",
                        f"Condition in {function.name}() is unknown; selected the branch with more supported actions.",
                    )
                else:
                    chosen = stmt.body if bool(condition) else stmt.orelse
                encode_statements(chosen)
                continue
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                context.record(
                    "drop_unsupported_node",
                    stmt,
                    "drop_statement",
                    f"Dropped unsupported statement in {function.name}().",
                )
                continue

            call = stmt.value
            name = call_name(call)
            if name in SUPPORTED_ACTIONS:
                try:
                    actions.append(
                        aggressive_encode_action_call(
                            call, name, bindings, values, robot_names, resolver, code
                        )
                    )
                except SmartLLMConversionError:
                    context.record(
                        "drop_unsupported_node",
                        stmt,
                        "drop_action",
                        f"Dropped action with unresolved arguments: {ast_source(code, call)}.",
                    )
            elif name == "time.sleep":
                try:
                    actions.extend(encode_wait_call(call, bindings, robot_names, code))
                except SmartLLMConversionError:
                    default = tuple(bindings.get(DEFAULT_ROBOT_BINDING_NAME, (robot_names[0],)))[0]
                    actions.append(EncodedAction(default, "WaitOneTick", (), ast_source(code, call)))
                    context.record(
                        "drop_unsupported_node",
                        stmt,
                        "normalize_wait",
                        "Replaced an unsupported sleep expression with one wait tick.",
                    )
            elif name in local_functions:
                nested = FunctionInvocation(
                    name,
                    tuple(call.args),
                    tuple(call.keywords),
                    function_node=local_functions[name],
                )
                actions.extend(
                    aggressive_encode_function_invocation(
                        nested,
                        local_functions,
                        bindings,
                        values,
                        robot_names,
                        object_names,
                        resolver,
                        allocator,
                        context,
                        code,
                        stack + (invocation.function_name,),
                    )
                )
            else:
                context.record(
                    "drop_unsupported_node",
                    stmt,
                    "drop_call",
                    f"Dropped unsupported call {name or '<dynamic>'} in {function.name}().",
                )

    encode_statements(function.body)
    return actions


ThreadSpec = Tuple[FunctionInvocation, ...]


def aggressive_thread_spec_from_constructor(
    call: ast.Call,
    functions: Mapping[str, ast.FunctionDef],
    context: RecoveryContext,
) -> ThreadSpec:
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

    if args_node is None:
        args: Tuple[ast.AST, ...] = ()
    elif isinstance(args_node, (ast.Tuple, ast.List)):
        args = tuple(args_node.elts)
    else:
        args = (args_node,)

    if isinstance(target_node, ast.Name) and target_node.id in functions:
        return (
            FunctionInvocation(
                target_node.id,
                args,
                function_node=functions[target_node.id],
            ),
        )

    if isinstance(target_node, ast.Lambda):
        body_nodes = (
            target_node.body.elts
            if isinstance(target_node.body, (ast.List, ast.Tuple))
            else [target_node.body]
        )
        invocations: List[FunctionInvocation] = []
        for node in body_nodes:
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name in functions:
                invocations.append(
                    FunctionInvocation(
                        name,
                        tuple(node.args),
                        tuple(node.keywords),
                        function_node=functions[name],
                    )
                )
        if invocations:
            context.record(
                "lambda_thread",
                target_node,
                "expand_lambda",
                f"Expanded lambda thread target into {len(invocations)} local function calls.",
            )
            return tuple(invocations)

    raise SmartLLMConversionError("unsupported_threading")


def _actions_to_recovery_stages(
    actions: Sequence[EncodedAction],
    context: RecoveryContext,
) -> List[StageQueues]:
    if not actions:
        return []
    stages: List[StageQueues] = []
    current_robot = actions[0].robot_id
    current_actions: List[EncodedAction] = []
    used_robots: set[str] = set()
    for action in actions:
        used_robots.add(action.robot_id)
        if action.robot_id != current_robot and current_actions:
            stages.append({current_robot: [action_to_dict(item) for item in current_actions]})
            current_actions = []
            current_robot = action.robot_id
        current_actions.append(action)
    if current_actions:
        stages.append({current_robot: [action_to_dict(item) for item in current_actions]})
    if len(used_robots) > 1:
        context.record(
            "multi_robot_stage_split",
            None,
            "split_stages",
            f"Split a sequential function across robots {sorted(used_robots)!r}.",
        )
    return stages


def aggressive_build_stage_plan_from_ast(
    tree: ast.Module,
    resolver: ObjectNameResolver,
    metadata: LogMetadata,
    code: str,
    context: RecoveryContext,
) -> Tuple[List[StageQueues], int]:
    robot_names = robot_names_from_log(metadata.robots)
    if not robot_names:
        raise SmartLLMConversionError("missing_metadata")
    object_names = object_names_from_log(metadata.objects)
    allocator = RecoveryRobotAllocator(metadata.robots, metadata.objects, robot_names, context)
    functions: Dict[str, ast.FunctionDef] = {}
    definition_order: List[ast.FunctionDef] = []
    stages: List[StageQueues] = []
    global_bindings: RobotBindings = {
        "robots": tuple(robot_names),
        DEFAULT_ROBOT_BINDING_NAME: (robot_names[0],),
    }
    global_values: Dict[str, Any] = {}
    thread_specs: Dict[str, ThreadSpec] = {}
    active_threads: List[str] = []
    joined_threads: set[str] = set()

    def encode_spec(spec: ThreadSpec) -> List[StageQueues]:
        actions: List[EncodedAction] = []
        for invocation in spec:
            actions.extend(
                aggressive_encode_function_invocation(
                    invocation,
                    functions,
                    global_bindings,
                    global_values,
                    robot_names,
                    object_names,
                    resolver,
                    allocator,
                    context,
                    code,
                )
            )
        return _actions_to_recovery_stages(actions, context)

    def flush_threads() -> None:
        nonlocal active_threads
        if not active_threads:
            return
        branches = [encode_spec(thread_specs[name]) for name in active_threads if name in thread_specs]
        for index in range(max((len(branch) for branch in branches), default=0)):
            merged: StageQueues = {}
            for branch in branches:
                if index >= len(branch):
                    continue
                for robot, queue in branch[index].items():
                    if robot in merged:
                        context.record(
                            "multi_robot_stage_split",
                            None,
                            "serialize_thread_conflict",
                            f"Serialized concurrent thread queues for {robot}.",
                        )
                    merged.setdefault(robot, []).extend(queue)
            if merged:
                stages.append(merged)
        active_threads = []
        joined_threads.clear()

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.FunctionDef):
            functions[stmt.name] = stmt
            definition_order.append(stmt)
            continue
        if isinstance(stmt, ast.Assign):
            if isinstance(stmt.value, ast.Call) and is_thread_constructor(stmt.value):
                try:
                    spec = aggressive_thread_spec_from_constructor(stmt.value, functions, context)
                except SmartLLMConversionError:
                    context.record(
                        "drop_unsupported_node",
                        stmt,
                        "drop_thread",
                        "Dropped an unsupported thread constructor.",
                    )
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        thread_specs[target.id] = spec
                continue

            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                if target in METADATA_ASSIGNMENT_NAMES:
                    context.record(
                        "ignore_metadata_assignment",
                        stmt,
                        "ignore_assignment",
                        f"Ignored top-level metadata assignment {target!r}.",
                    )
                    continue
                dict_name = _dict_robot_name(stmt.value, robot_names)
                if dict_name:
                    global_bindings[target] = (dict_name,)
                    continue
                try:
                    global_bindings[target] = aggressive_robot_sequence_from_expr(
                        stmt.value, global_bindings, robot_names
                    )
                    continue
                except SmartLLMConversionError:
                    value = _evaluate_recovery_value(stmt.value, global_values, object_names)
                    if value is not None:
                        global_values[target] = value
                        continue
            context.record(
                "drop_unsupported_node",
                stmt,
                "drop_assignment",
                "Dropped unsupported top-level assignment.",
            )
            continue

        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            context.record(
                "drop_unsupported_node",
                stmt,
                "drop_statement",
                "Dropped unsupported top-level statement.",
            )
            continue

        call = stmt.value
        start_target = thread_method_target(call, "start")
        if start_target:
            if start_target in thread_specs and start_target not in active_threads:
                active_threads.append(start_target)
            else:
                context.record(
                    "drop_unsupported_node",
                    stmt,
                    "drop_thread_start",
                    f"Ignored unmatched {start_target}.start().",
                )
            continue
        join_target = thread_method_target(call, "join")
        if join_target:
            joined_threads.add(join_target)
            if active_threads and all(name in joined_threads for name in active_threads):
                flush_threads()
            continue

        name = call_name(call)
        if name in functions:
            flush_threads()
            invocation = FunctionInvocation(
                name,
                tuple(call.args),
                tuple(call.keywords),
                function_node=functions[name],
            )
            actions = aggressive_encode_function_invocation(
                invocation,
                functions,
                global_bindings,
                global_values,
                robot_names,
                object_names,
                resolver,
                allocator,
                context,
                code,
            )
            stages.extend(_actions_to_recovery_stages(actions, context))
            continue
        if name in SUPPORTED_ACTIONS:
            flush_threads()
            try:
                action = aggressive_encode_action_call(
                    call, name, global_bindings, global_values, robot_names, resolver, code
                )
                stages.extend(_actions_to_recovery_stages([action], context))
            except SmartLLMConversionError:
                context.record(
                    "drop_unsupported_node",
                    stmt,
                    "drop_action",
                    f"Dropped top-level action {ast_source(code, call)}.",
                )
            continue
        if name == "time.sleep":
            flush_threads()
            try:
                actions = encode_wait_call(call, global_bindings, robot_names, code)
                stages.extend(_actions_to_recovery_stages(actions, context))
            except SmartLLMConversionError:
                context.record("drop_unsupported_node", stmt, "drop_wait", "Dropped unsupported wait.")
            continue
        context.record(
            "drop_unsupported_node",
            stmt,
            "drop_call",
            f"Dropped unsupported top-level call {name or '<dynamic>'}.",
        )

    flush_threads()

    if not stages:
        selected_definitions = [
            function for function in definition_order if functions.get(function.name) is function
        ]
        for function in selected_definitions:
            default_robot = allocator.choose(function)
            invocation = FunctionInvocation(
                function.name,
                (),
                default_robot=default_robot,
                function_node=function,
            )
            actions = aggressive_encode_function_invocation(
                invocation,
                functions,
                global_bindings,
                global_values,
                robot_names,
                object_names,
                resolver,
                allocator,
                context,
                code,
            )
            stages.extend(_actions_to_recovery_stages(actions, context))
        if stages:
            context.record(
                "synthesize_driver",
                None,
                "invoke_functions",
                f"Synthesized a driver for {len(selected_definitions)} top-level functions.",
            )

    action_count = sum(len(queue) for stage in stages for queue in stage.values())
    if not stages or action_count == 0:
        raise SmartLLMConversionError("no_clear_driver")
    return stages, action_count


def recover_stage_plan(
    source_text: str,
    metadata: LogMetadata,
    resolver: ObjectNameResolver,
) -> RecoveryAttempt:
    attempts: List[Tuple[Tuple[int, int, int, int, int], RecoveryAttempt]] = []
    errors: Counter[str] = Counter()
    for candidate in recovery_candidates(source_text):
        try:
            tree = ast.parse(candidate.code)
            classification = classify_parsed_code(tree)
            context = RecoveryContext()
            for rule in candidate.rules:
                context.events.append(
                    {
                        "rule": rule,
                        "line_start": candidate.start_line,
                        "line_end": candidate.end_line,
                        "action": "select_candidate",
                        "detail": f"Applied {rule} while preparing {candidate.kind}.",
                    }
                )
            stages, action_count = aggressive_build_stage_plan_from_ast(
                tree, resolver, metadata, candidate.code, context
            )
            if not context.events:
                context.events.append(
                    {
                        "rule": "tolerant_ast_match",
                        "line_start": candidate.start_line,
                        "line_end": candidate.end_line,
                        "action": "accept_candidate",
                        "detail": (
                            "Accepted the candidate with tolerant AST argument and binding rules "
                            "after conservative conversion rejected it."
                        ),
                    }
                )
        except SmartLLMConversionError as exc:
            errors[exc.skip_reason] += 1
            continue
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            errors[type(exc).__name__] += 1
            continue

        explicit_driver = int(classification.schedule_type != "no_clear_driver")
        dropped = sum(
            1 for event in context.events if event["rule"] == "drop_unsupported_node"
        )
        score = (
            candidate.allocation_rank,
            explicit_driver,
            action_count,
            -dropped,
            candidate.start_line,
        )
        attempts.append(
            (
                score,
                RecoveryAttempt(
                    candidate=candidate,
                    classification=classification,
                    stages=stages,
                    action_count=action_count,
                    events=list(context.events),
                    rules=context.rules,
                    confidence=context.confidence,
                ),
            )
        )

    if not attempts:
        detail = ", ".join(f"{reason}={count}" for reason, count in errors.most_common())
        raise SmartLLMConversionError(
            "recovery_exhausted",
            f"No aggressive recovery candidate produced an action plan ({detail}).",
        )
    return max(attempts, key=lambda item: item[0])[1]


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
    gcr: Sequence[Any],
    no_trans: int,
    object_mappings: Dict[str, str],
    object_mapping_warnings: Sequence[str],
) -> Dict[str, Any]:
    return common_build_bundle_data(
        task=task,
        task_plan_data=task_plan_data,
        gcr=gcr,
        no_trans=no_trans,
        object_mappings=object_mappings,
        object_mapping_warnings=object_mapping_warnings,
    )


def render_bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return common_render_bundle_literal(bundle_data)


def render_executable_plan(
    *,
    bundle_data: Dict[str, Any],
    task_file: Path,
    task_index: int,
) -> str:
    return common_render_executable_plan(
        bundle_data=bundle_data,
        task_file=task_file,
        task_index=task_index,
        description="Run a hardcoded SMART-LLM bundle through executor_system.",
        repo_root=REPO_ROOT,
    )


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
    result.conversion_kind = "skipped"
    result.skip_reason = reason
    result.error = message
    result.generated["executable_plan"] = None


def mark_failed(result: ConversionResult, message: str) -> None:
    result.status = "failed"
    result.success = False
    result.conversion_kind = "failed"
    result.error = message
    result.generated["executable_plan"] = None


def mark_generation_failed(
    result: ConversionResult, exc: GenerationValidationError, executable_path: Path, *, dry_run: bool,
) -> None:
    mark_failed(result, str(exc))
    result.skip_reason = None
    for key, value in generation_failure_result(exc, executable_path, dry_run=dry_run).items():
        setattr(result, key, value)


def prepare_conversion_context(
    source_path: Path,
) -> Tuple[LogMetadata, Path, int, Sequence[Any], List[str], ObjectNameResolver]:
    metadata = read_log_metadata(source_path.parent)
    task_file = dataset_path_for_metadata(metadata)
    task_index = find_task_index(task_file, metadata.task)
    gcr = read_task_record_gcr(task_file, task_index)
    metadata = replace(metadata, robots=prepare_generation_robots(
        metadata.robots, load_task_record(task_file, task_index),
    ))
    robot_names = robot_names_from_log(metadata.robots)
    resolver = ObjectNameResolver(object_names_from_log(metadata.objects))
    return metadata, task_file, task_index, gcr, robot_names, resolver


def finish_conversion_result(
    result: ConversionResult,
    metadata: LogMetadata,
    task_file: Path,
    task_index: int,
    gcr: Sequence[Any],
    resolver: ObjectNameResolver,
    stages: Sequence[StageQueues],
    action_count: int,
) -> str:
    result.task = metadata.task or None
    result.floor_plan = normalize_floor_plan(metadata.floor_plan or "") or None
    result.test_set = metadata.test_set
    result.task_file = str(task_file)
    result.task_index = task_index
    result.action_count = action_count
    result.stage_count = len(stages)
    result.no_trans = action_count
    result.object_mappings = dict(resolver.mappings)
    result.object_mapping_warnings = list(resolver.warnings)

    task_plan_data = build_task_plan_data(
        f"smart_llm_{result.floor_plan or 'unknown'}_{task_index}",
        stages,
    )
    validate_generation_plan(
        task_plan_data, robots=metadata.robots, objects=metadata.objects, repo_root=REPO_ROOT,
        task_context={"objects_ai": f"objects = {metadata.objects_literal}"}
        if metadata.objects_literal is not None else {},
        floor_plan=metadata.floor_plan, object_mappings=resolver.mappings,
        task_record=load_task_record(task_file, task_index),
    )
    bundle_data = build_bundle_data(
        task=metadata.task,
        task_plan_data=task_plan_data,
        gcr=gcr,
        no_trans=action_count,
        object_mappings=dict(resolver.mappings),
        object_mapping_warnings=list(resolver.warnings),
    )
    return render_executable_plan(
        bundle_data=bundle_data,
        task_file=task_file,
        task_index=task_index,
    )


def convert_one(
    source_path: Path,
    input_root: Path,
    output_root: Path,
    dry_run: bool,
    validate_code: bool,
    recovery_mode: str = "aggressive",
) -> ConversionResult:
    if recovery_mode not in RECOVERY_MODES:
        raise ValueError(f"Unsupported recovery mode: {recovery_mode!r}.")
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
    executable_plan: Optional[str] = None

    try:
        if classification.parse_status != "parse_ok" or tree is None:
            raise SmartLLMConversionError(classification.parse_status)
        if classification.schedule_type == "no_clear_driver":
            raise SmartLLMConversionError("no_clear_driver")

        metadata, task_file, task_index, gcr, robot_names, resolver = prepare_conversion_context(source_path)
        stages, action_count = build_stage_plan_from_ast(
            tree,
            resolver=resolver,
            robot_names=robot_names,
            code=extracted.code,
        )
        executable_plan = finish_conversion_result(
            result,
            metadata,
            task_file,
            task_index,
            gcr,
            resolver,
            stages,
            action_count,
        )
        compile(executable_plan, "executable_plan.py", "exec")
    except SmartLLMConversionError as exc:
        result.initial_skip_reason = exc.skip_reason
        mark_skipped(result, exc.skip_reason, str(exc))
        if recovery_mode == "aggressive":
            try:
                metadata, task_file, task_index, gcr, _, resolver = prepare_conversion_context(source_path)
                recovery = recover_stage_plan(source_text, metadata, resolver)
                result.status = "success"
                result.success = True
                result.conversion_kind = "recovered"
                result.skip_reason = None
                result.error = None
                result.parse_status = "parse_ok"
                result.syntax_error = None
                result.schedule_type = (
                    "recovered_synthesized"
                    if "synthesize_driver" in recovery.rules
                    else recovery.classification.schedule_type
                )
                result.category = result.schedule_type
                result.function_count = recovery.classification.function_count
                result.top_level_driver_count = recovery.classification.top_level_driver_count
                result.uses_team_or_robot_list = recovery.classification.uses_team_or_robot_list
                result.uses_time_sleep = recovery.classification.uses_time_sleep
                result.extraction_method = f"recovery_{recovery.candidate.kind}"
                result.recovery_confidence = recovery.confidence
                result.recovery_rules = list(recovery.rules)
                result.recovery_events = list(recovery.events)
                result.generated["executable_plan"] = str(executable_path)
                executable_plan = finish_conversion_result(
                    result,
                    metadata,
                    task_file,
                    task_index,
                    gcr,
                    resolver,
                    recovery.stages,
                    recovery.action_count,
                )
                compile(executable_plan, "executable_plan.py", "exec")
            except GenerationValidationError as recovery_exc:
                mark_generation_failed(result, recovery_exc, executable_path, dry_run=dry_run)
            except SmartLLMConversionError as recovery_exc:
                mark_skipped(result, recovery_exc.skip_reason, str(recovery_exc))
            except (SyntaxError, OSError, py_compile.PyCompileError) as recovery_exc:
                mark_failed(result, str(recovery_exc))
    except GenerationValidationError as exc:
        mark_generation_failed(result, exc, executable_path, dry_run=dry_run)
    except (SyntaxError, OSError, py_compile.PyCompileError) as exc:
        mark_failed(result, str(exc))
    finally:
        result.generation_time = time.time() - started_at

    if result.success and executable_plan is not None and not dry_run:
        try:
            executable_path.parent.mkdir(parents=True, exist_ok=True)
            executable_path.write_text(executable_plan, encoding="utf-8")
            if validate_code:
                compile_python(executable_path)
        except (OSError, py_compile.PyCompileError) as exc:
            mark_failed(result, str(exc))

    if not dry_run:
        write_json(summary_path, asdict(result))
    return result


def build_global_summary(
    results: Sequence[ConversionResult],
    input_root: Path,
    output_root: Path,
    dry_run: bool,
    duration_seconds: float,
    recovery_mode: str = "aggressive",
) -> Dict[str, Any]:
    status_counts = Counter(result.status for result in results)
    success_count = status_counts.get("success", 0)
    conversion_kind_counts = Counter(result.conversion_kind for result in results)
    recovery_confidence_counts = Counter(
        result.recovery_confidence
        for result in results
        if result.conversion_kind == "recovered" and result.recovery_confidence
    )
    recovery_rule_counts = Counter(
        rule
        for result in results
        if result.conversion_kind == "recovered"
        for rule in result.recovery_rules
    )

    return {
        "total_results": len(results),
        "successful_generations": success_count,
        "failed_generations": len(results) - success_count,
        "success_rate": success_count / len(results) * 100 if results else 0,
        "direct_successful_generations": conversion_kind_counts.get("direct", 0),
        "recovered_generations": conversion_kind_counts.get("recovered", 0),
        "conversion_kind_counts": dict(sorted(conversion_kind_counts.items())),
        "recovery_confidence_counts": dict(sorted(recovery_confidence_counts.items())),
        "recovery_rule_counts": dict(sorted(recovery_rule_counts.items())),
        "total_generation_time": sum(float(result.generation_time) for result in results),
        **generation_failure_counts(asdict(result) for result in results),
        "dry_run": dry_run,
        "recovery_mode": recovery_mode,
        "input_root": str(input_root),
        "output_dir": str(output_root),
        "duration_seconds": round(duration_seconds, 3),
    }


def write_global_summary(results: Sequence[ConversionResult], output_root: Path, summary: Dict[str, Any]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "plan_to_code_summary.json", summary)
    write_json(output_root / "plan_to_code_results.json", [asdict(result) for result in results])


def convert(
    *,
    input_root: Path,
    output_root: Path,
    floor_plan: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    validate_code: bool = True,
    recovery_mode: str = "aggressive",
) -> int:
    if recovery_mode not in RECOVERY_MODES:
        raise ValueError(f"Unsupported recovery mode: {recovery_mode!r}.")
    started_at = time.time()
    paths = iter_plan_source_paths(input_root, floor_plan, limit)
    results = [
        convert_one(
            path,
            input_root=input_root,
            output_root=output_root,
            dry_run=dry_run,
            validate_code=validate_code,
            recovery_mode=recovery_mode,
        )
        for path in paths
    ]
    duration = time.time() - started_at

    global_summary = build_global_summary(
        results=results,
        input_root=input_root,
        output_root=output_root,
        dry_run=dry_run,
        duration_seconds=duration,
        recovery_mode=recovery_mode,
    )
    if not dry_run:
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
    parser.add_argument("--dry-run", action="store_true", help="Classify and validate without writing files.")
    parser.add_argument(
        "--recovery-mode",
        choices=sorted(RECOVERY_MODES),
        default="aggressive",
        help=(
            "Plan recovery policy. aggressive (default) applies deterministic free-format "
            "recovery after conservative conversion fails; conservative preserves the legacy baseline."
        ),
    )
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

    return convert(
        input_root=input_root,
        output_root=output_root,
        floor_plan=args.floor_plan,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        validate_code=bool(args.validate_code),
        recovery_mode=str(args.recovery_mode),
    )


if __name__ == "__main__":
    raise SystemExit(main())
