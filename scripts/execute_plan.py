#!/usr/bin/env python3
"""Compatibility entry point for executing one generated plan directory."""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED_RUNTIME_IMPORT = "executor_system.generated_plan_runtime"
REQUIRED_LEGACY_CONTEXT = (
    "floor_no",
    "robots",
    "ground_truth",
    "no_trans_gt",
    "max_trans",
)


class ExecutePlanError(RuntimeError):
    """Raised when a compatibility execution cannot be resolved safely."""


def append_trans_ctr(allocated_plan: str) -> int:
    count = 0
    for segment in allocated_plan.split("\n\n"):
        stripped = segment.strip()
        if (
            stripped
            and "def" not in stripped
            and "threading.Thread" not in stripped
            and "join" not in stripped
            and stripped.endswith(")")
        ):
            count += 1
    print("No Breaks: ", count)
    return count


def _literal_assignments(source: str, names: Sequence[str]) -> Dict[str, Any]:
    wanted = set(names)
    values: Dict[str, Any] = {}
    for line in source.splitlines():
        try:
            tree = ast.parse(line)
        except SyntaxError:
            continue
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
            continue
        assignment = tree.body[0]
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            continue
        name = assignment.targets[0].id
        if name not in wanted:
            continue
        try:
            values[name] = ast.literal_eval(assignment.value)
        except (ValueError, SyntaxError):
            continue
    return values


def _legacy_context_errors(values: Dict[str, Any]) -> Tuple[str, ...]:
    missing = []
    floor = values.get("floor_no")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor <= 0:
        missing.append("floor_no")
    robots = values.get("robots")
    if not isinstance(robots, list) or not robots:
        missing.append("robots")
    goals = values.get("ground_truth")
    if not isinstance(goals, list) or not goals:
        missing.append("ground_truth")
    for name in ("no_trans_gt", "max_trans"):
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            missing.append(name)
    return tuple(missing)


def _shared_runtime_aliases(tree: ast.Module) -> Tuple[str, ...]:
    aliases = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != GENERATED_RUNTIME_IMPORT:
            continue
        aliases.extend(alias.asname or alias.name for alias in node.names if alias.name == "main")
    return tuple(aliases)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    comparison = node.test
    if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
        return False
    if len(comparison.comparators) != 1:
        return False
    values = (comparison.left, comparison.comparators[0])
    return any(isinstance(value, ast.Name) and value.id == "__name__" for value in values) and any(
        isinstance(value, ast.Constant) and value.value == "__main__" for value in values
    )


def _is_shared_runtime_call(node: ast.AST, aliases: Sequence[str]) -> bool:
    if not isinstance(node, ast.Call) or node.keywords:
        return False
    if not isinstance(node.func, ast.Name) or node.func.id not in aliases:
        return False
    expected = ("BUNDLE_DATA", "TASK_FILE", "TASK_INDEX", "__file__")
    return len(node.args) == len(expected) and all(
        isinstance(argument, ast.Name) and argument.id == name
        for argument, name in zip(node.args, expected)
    )


def _walk_executable_nodes(node: ast.AST):
    """Walk a guarded execution path without entering deferred definitions."""
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk_executable_nodes(child)


def _exits_with_shared_runtime(tree: ast.Module, aliases: Sequence[str]) -> bool:
    for guard in (node for node in tree.body if _is_main_guard(node)):
        for node in _walk_executable_nodes(guard):
            if isinstance(node, ast.Raise):
                exit_call = node.exc
                expected_exit = "raise"
            elif isinstance(node, ast.Expr):
                exit_call = node.value
                expected_exit = "call"
            else:
                continue
            if not isinstance(exit_call, ast.Call) or len(exit_call.args) != 1:
                continue
            is_system_exit = (
                expected_exit == "raise"
                and isinstance(exit_call.func, ast.Name)
                and exit_call.func.id == "SystemExit"
            )
            is_builtin_exit = (
                expected_exit == "call"
                and isinstance(exit_call.func, ast.Name)
                and exit_call.func.id == "exit"
            )
            is_sys_exit = (
                expected_exit == "call"
                and isinstance(exit_call.func, ast.Attribute)
                and isinstance(exit_call.func.value, ast.Name)
                and exit_call.func.value.id == "sys"
                and exit_call.func.attr == "exit"
            )
            if (is_system_exit or is_builtin_exit or is_sys_exit) and _is_shared_runtime_call(
                exit_call.args[0], aliases
            ):
                return True
    return False


def verify_generated_runtime(path: Path) -> Optional[str]:
    """Return an explanation when *path* is not a current generated runtime."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
    except (OSError, SyntaxError) as exc:
        return f"cannot read or compile it: {exc}"
    aliases = _shared_runtime_aliases(tree)
    if not aliases:
        return f"it does not import {GENERATED_RUNTIME_IMPORT}.main"
    if not _exits_with_shared_runtime(tree, aliases):
        return "it does not invoke the imported shared runtime main from the __main__ exit path"

    values: Dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in {
                "BUNDLE_DATA", "TASK_FILE", "TASK_INDEX"
            }:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return f"{target.id} is not a literal assignment"

    missing = sorted({"BUNDLE_DATA", "TASK_FILE", "TASK_INDEX"} - set(values))
    if missing:
        return "missing generated assignment(s): " + ", ".join(missing)
    bundle = values["BUNDLE_DATA"]
    if not isinstance(bundle, dict) or not isinstance(bundle.get("task_plan"), dict):
        return "BUNDLE_DATA.task_plan must be a dictionary"
    if not isinstance(bundle.get("gcr"), list):
        return "BUNDLE_DATA.gcr must be a list"
    if not isinstance(values["TASK_FILE"], str) or not values["TASK_FILE"]:
        return "TASK_FILE must be a non-empty path string"
    if isinstance(values["TASK_INDEX"], bool) or not isinstance(values["TASK_INDEX"], int):
        return "TASK_INDEX must be an integer"
    if values["TASK_INDEX"] < 0:
        return "TASK_INDEX must be non-negative"
    return None


def resolve_command_directory(command: str, cwd: Optional[Path] = None) -> Path:
    base = (cwd or Path.cwd()).resolve()
    supplied = Path(command).expanduser()
    candidates = [supplied] if supplied.is_absolute() else [base / supplied, base / "logs" / supplied]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise ExecutePlanError(f"command directory does not exist; checked: {searched}")


def find_generated_runtime(command_dir: Path) -> Tuple[Optional[Path], Tuple[str, ...]]:
    rejected = []
    for candidate in (
        command_dir / "plan_to_code" / "executable_plan.py",
        command_dir / "executable_plan.py",
    ):
        if not candidate.is_file():
            continue
        problem = verify_generated_runtime(candidate)
        if problem is None:
            return candidate, tuple(rejected)
        rejected.append(f"{candidate}: {problem}")
    return None, tuple(rejected)


def _rewrite_legacy_robot_placeholders(source: str, robots: Sequence[Any]) -> str:
    """Replace the three historical top-level robot placeholders structurally."""
    try:
        tree = ast.parse(source, filename="code_plan.py")
    except SyntaxError as exc:
        raise ExecutePlanError(f"legacy code_plan.py does not compile: {exc}") from exc
    replacements = []
    placeholders = ([], ["robot1"], ["Robot2"])
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.targets[0], ast.Name) or node.targets[0].id != "robots":
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if value in placeholders:
            replacements.append((node.lineno - 1, node.end_lineno))
    lines = source.splitlines(keepends=True)
    for start, end in reversed(replacements):
        newline = "\n" if lines[end - 1].endswith("\n") else ""
        lines[start:end] = [f"robots = {list(robots)!r}{newline}"]
    return "".join(lines)


def compile_aithor_exec_file(command_dir: Path) -> Path:
    """Build the historical concatenated entry point only from recorded context."""
    log_file = command_dir / "log.txt"
    code_plan = command_dir / "code_plan.py"
    missing_files = [str(path.name) for path in (log_file, code_plan) if not path.is_file()]
    if missing_files:
        raise ExecutePlanError("legacy input is missing file(s): " + ", ".join(missing_files))

    log_data = log_file.read_text(encoding="utf-8")
    values = _literal_assignments(log_data, REQUIRED_LEGACY_CONTEXT)
    missing_context = _legacy_context_errors(values)
    if missing_context:
        raise ExecutePlanError(
            "legacy input is missing valid required context: "
            + ", ".join(missing_context)
            + ". Run scripts/plantocode.py for this task before execution; "
              "the compatibility entry point will not invent floor, robots, or goals."
        )

    imports = (REPO_ROOT / "data" / "aithor_connect" / "imports_aux_fn.py").read_text(encoding="utf-8")
    connector = (REPO_ROOT / "data" / "aithor_connect" / "aithor_connect.py").read_text(encoding="utf-8")
    termination = (REPO_ROOT / "data" / "aithor_connect" / "end_thread.py").read_text(encoding="utf-8")
    allocated_plan = _rewrite_legacy_robot_placeholders(
        code_plan.read_text(encoding="utf-8"), values["robots"]
    )
    breaks = append_trans_ctr(allocated_plan)
    context = "\n".join(
        (
            f"robots = {values['robots']!r}",
            f"floor_no = {values['floor_no']!r}",
            f"ground_truth = {values['ground_truth']!r}",
            f"no_trans_gt = {values['no_trans_gt']!r}",
            f"max_trans = {values['max_trans']!r}",
        )
    )
    executable = "\n".join((imports, context, connector, allocated_plan, f"no_trans = {breaks}", termination, ""))
    output = command_dir / "executable_plan.py"
    output.write_text(executable, encoding="utf-8")
    return output


def parse_arguments(argv: Optional[Sequence[str]] = None) -> Tuple[argparse.Namespace, Sequence[str]]:
    parser = argparse.ArgumentParser(
        description="Execute a verified generated plan directory, with legacy log compatibility."
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Task directory path, or a directory name directly below ./logs.",
    )
    return parser.parse_known_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args, generated_arguments = parse_arguments(argv)
    try:
        command_dir = resolve_command_directory(args.command)
        generated, rejected = find_generated_runtime(command_dir)
        if generated is None:
            try:
                generated = compile_aithor_exec_file(command_dir)
            except ExecutePlanError as exc:
                details = f" Rejected generated runtime(s): {'; '.join(rejected)}" if rejected else ""
                raise ExecutePlanError(f"{exc}{details}") from exc
        completed = subprocess.run([sys.executable, str(generated), *generated_arguments], check=False)
        return int(completed.returncode)
    except (ExecutePlanError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
