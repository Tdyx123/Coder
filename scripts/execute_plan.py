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
REQUIRED_GENERATED_BINDINGS = ("BUNDLE_DATA", "TASK_FILE", "TASK_INDEX")


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


def _matches_python(statements: Sequence[ast.stmt], source: str) -> bool:
    expected = ast.parse(source).body
    return len(statements) == len(expected) and all(
        ast.dump(actual) == ast.dump(template)
        for actual, template in zip(statements, expected)
    )


def _has_canonical_generated_prefix(statements: Sequence[ast.stmt]) -> bool:
    """Recognize the bootstrap emitted by baseline_converters.common, not arbitrary Python."""
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            statements = statements[1:]
    if statements and _matches_python(statements[:1], "from __future__ import annotations"):
        statements = statements[1:]
    # Minimal historical/test wrappers rely on the caller's import path.
    if not statements or _matches_python(statements, "import sys"):
        return True
    try:
        repo_root = ast.literal_eval(statements[3].value.args[0])
    except (AttributeError, IndexError, ValueError, TypeError):
        return False
    if not isinstance(repo_root, str):
        return False
    return _matches_python(statements, f'''
import os
import sys
from pathlib import Path
REPO_ROOT = Path({repo_root!r})
_SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (_SCRIPT_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)
RUNNER_MODE_ARG = "--runner-mode"
if RUNNER_MODE_ARG in sys.argv[1:]:
    os.environ["renderImage"] = "0"
''')


def _has_canonical_generated_bindings(tree: ast.Module, runtime_alias: str) -> bool:
    # Only the shared entrypoint names used by supported generated wrappers.
    if runtime_alias not in {"main", "run_generated_plan"}:
        return False
    imports = []
    guards = []
    for index, node in enumerate(tree.body):
        if _is_main_guard(node):
            guards.append(index)
        if (not isinstance(node, ast.ImportFrom)
                or node.module != GENERATED_RUNTIME_IMPORT or node.level):
            continue
        if len(node.names) != 1:
            continue
        imported = node.names[0]
        if imported.name == "main" and (imported.asname or imported.name) == runtime_alias:
            imports.append(index)
    if len(imports) != 1 or len(guards) != 1 or imports[0] >= guards[0]:
        return False
    if guards[0] != len(tree.body) - 1:
        return False
    if not _has_canonical_generated_prefix(tree.body[:imports[0]]):
        return False

    seen = set()
    for statement in tree.body[imports[0] + 1:guards[0]]:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        else:
            return False
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return False
        name = targets[0].id
        if name not in REQUIRED_GENERATED_BINDINGS or name in seen:
            return False
        seen.add(name)
    return seen == set(REQUIRED_GENERATED_BINDINGS)


def _exits_with_shared_runtime(tree: ast.Module, aliases: Sequence[str]) -> bool:
    guards = [node for node in tree.body if _is_main_guard(node)]
    if len(guards) != 1 or len(aliases) != 1:
        return False
    call = f"{aliases[0]}(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__)"
    # Match the complete guard, including the known diagnostic handler. Extra
    # branches, finalizers, exception changes, and exit arguments are unsupported.
    return any(_matches_python(guards, source) for source in (
        f"if __name__ == '__main__':\n    raise SystemExit({call})",
        f"if __name__ == '__main__':\n    try:\n        raise SystemExit({call})\n"
        "    except RuntimeError:\n        raise SystemExit(1)",
        f"if __name__ == '__main__':\n    try:\n        raise SystemExit({call})\n"
        "    except RuntimeError as exc:\n        print(f'ERROR: {exc}')\n"
        "        raise SystemExit(1)",
    ))


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
    if len(aliases) != 1 or not _has_canonical_generated_bindings(tree, aliases[0]):
        return "the imported shared runtime alias is rebound or generated bindings are ambiguous"
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
            replacements.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    encoded = source.encode("utf-8")
    line_starts = []
    offset = 0
    for line in encoded.splitlines(keepends=True):
        line_starts.append(offset)
        offset += len(line)
    replacement = f"robots = {list(robots)!r}".encode("utf-8")
    for start_line, start_column, end_line, end_column in reversed(replacements):
        start = line_starts[start_line - 1] + start_column
        end = line_starts[end_line - 1] + end_column
        encoded = encoded[:start] + replacement + encoded[end:]
    return encoded.decode("utf-8")


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
