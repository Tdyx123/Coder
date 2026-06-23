#!/usr/bin/env python3
"""Convert legacy SMART-LLM code_plan.py outputs into clean Python files."""

from __future__ import annotations

import argparse
import ast
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_INPUT_ROOT = Path("baselines/SMART-LLM/logs")
DEFAULT_OUTPUT_ROOT = Path("baselines/SMART-LLM")

PYTHON_FENCE_RE = re.compile(
    r"```(?:python|py)?[ \t]*\n(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
FALLBACK_CODE_LINE_RE = re.compile(
    r"(?m)^\s*(?:import\s+|from\s+|def\s+|#\s*CODE\b)",
)
TEAM_PARAMETER_NAMES = {"robot_list", "robot_team", "team"}


class SmartLLMConversionError(Exception):
    """Raised when a SMART-LLM conversion request cannot be processed."""


@dataclass
class ExtractedCode:
    code: str
    method: str
    block_count: int


@dataclass
class CodeClassification:
    parse_status: str
    schedule_type: str
    function_count: int
    top_level_driver_count: int
    uses_team_or_robot_list: bool
    uses_time_sleep: bool
    syntax_error: Optional[str] = None


@dataclass
class ConversionResult:
    status: str
    source_path: str
    relative_task_dir: str
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
    generated: Dict[str, Optional[str]]


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
        isinstance(node, ast.List)
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

    if has_threading and driver_count:
        schedule_type = "staged_mixed"
    elif has_threading:
        schedule_type = "threaded_parallel"
    elif function_count == 1:
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
    return output_dir / "code_plan.py", output_dir / "conversion_summary.json", str(relative_task_dir)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def convert_one(source_path: Path, input_root: Path, output_root: Path, dry_run: bool) -> ConversionResult:
    source_text = source_path.read_text(encoding="utf-8", errors="replace")
    extracted = extract_code(source_text)
    classification, _ = classify_code(extracted.code)
    code_plan_path, summary_path, relative_task_dir = output_paths_for(source_path, input_root, output_root)

    generated: Dict[str, Optional[str]] = {
        "code_plan": str(code_plan_path),
        "conversion_summary": str(summary_path),
    }
    skip_reason: Optional[str] = None
    status = "converted"

    if classification.parse_status != "parse_ok":
        status = "skipped"
        skip_reason = classification.parse_status
        generated["code_plan"] = None

    result = ConversionResult(
        status=status,
        source_path=str(source_path),
        relative_task_dir=relative_task_dir,
        parse_status=classification.parse_status,
        schedule_type=classification.schedule_type,
        function_count=classification.function_count,
        top_level_driver_count=classification.top_level_driver_count,
        uses_team_or_robot_list=classification.uses_team_or_robot_list,
        uses_time_sleep=classification.uses_time_sleep,
        extraction_method=extracted.method,
        extracted_block_count=extracted.block_count,
        skip_reason=skip_reason,
        syntax_error=classification.syntax_error,
        generated=generated,
    )

    if dry_run:
        return result

    summary_payload = asdict(result)
    if status == "converted":
        code_plan_path.parent.mkdir(parents=True, exist_ok=True)
        code_plan_path.write_text(extracted.code.rstrip() + "\n", encoding="utf-8")
    write_json(summary_path, summary_payload)
    return result


def build_global_summary(
    results: Sequence[ConversionResult],
    input_root: Path,
    output_root: Path,
    dry_run: bool,
    duration_seconds: float,
) -> Dict[str, Any]:
    parse_counts = Counter(result.parse_status for result in results)
    schedule_counts = Counter(result.schedule_type for result in results)
    status_counts = Counter(result.status for result in results)

    return {
        "status": "success",
        "input_root": str(input_root),
        "output_root": str(output_root),
        "dry_run": dry_run,
        "total": len(results),
        "converted": status_counts.get("converted", 0),
        "skipped": status_counts.get("skipped", 0),
        "duration_seconds": round(duration_seconds, 3),
        "parse_status_counts": dict(sorted(parse_counts.items())),
        "schedule_type_counts": dict(sorted(schedule_counts.items())),
        "results": [asdict(result) for result in results],
    }


def summary_path(output_root: Path) -> Path:
    return output_root / "smart_llm_conversion_summary.json"


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
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="SMART-LLM baseline root for converted output, default baselines/SMART-LLM.",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of source code_plan.py files to process.")
    parser.add_argument("--floor-plan", help="Optional floor/log folder filter, e.g. 2 or FloorPlan2.")
    parser.add_argument("--dry-run", action="store_true", help="Classify and write only the global summary.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_root = Path(args.input_root).resolve()
    output_root = Path(args.output_root).resolve()

    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if not input_root.exists() or not input_root.is_dir():
        parser.error(f"input root not found or not a directory: {input_root}")

    started_at = time.time()
    paths = iter_code_plan_paths(input_root, args.floor_plan, args.limit)
    results = [
        convert_one(path, input_root=input_root, output_root=output_root, dry_run=bool(args.dry_run))
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
    write_json(summary_path(output_root), global_summary)

    print(
        "Processed {total} SMART-LLM code_plan.py file(s): "
        "{converted} converted, {skipped} skipped. Summary: {summary}".format(
            total=global_summary["total"],
            converted=global_summary["converted"],
            skipped=global_summary["skipped"],
            summary=summary_path(output_root),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
