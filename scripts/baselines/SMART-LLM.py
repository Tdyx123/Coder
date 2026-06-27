#!/usr/bin/env python3
"""Compatibility wrapper for the SMART-LLM plan-to-code converter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent
for _path in (SCRIPTS_DIR, REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from baseline_converters import pddlrun
from baseline_converters import smart_llm as _impl
from baseline_converters.smart_llm import *  # noqa: F401,F403


REPO_ROOT = _impl.REPO_ROOT
SCRIPTS_DIR = _impl.SCRIPTS_DIR
DEFAULT_INPUT_ROOT = _impl.DEFAULT_INPUT_ROOT
DEFAULT_OUTPUT_DIR = _impl.DEFAULT_OUTPUT_DIR


def default_baseline_root() -> Path:
    return REPO_ROOT / "baselines" / "SMART-LLM"


def baseline_logs_dir(root: Path) -> Path:
    return root / "logs"


def baseline_output_dir(root: Path) -> Path:
    return root


def baseline_data_repo_root(baseline_root: Path) -> Path:
    root = baseline_root.expanduser()
    if root.parent.name == "baselines":
        return root.parent.parent
    return root


def _sync_globals(repo_root: Optional[Path] = None) -> None:
    data_repo_root = Path(repo_root or REPO_ROOT).expanduser()
    _impl.REPO_ROOT = data_repo_root
    _impl.SCRIPTS_DIR = data_repo_root / "scripts"
    _impl.DEFAULT_INPUT_ROOT = DEFAULT_INPUT_ROOT
    _impl.DEFAULT_OUTPUT_DIR = DEFAULT_OUTPUT_DIR


def resolve_baseline_paths(args: argparse.Namespace) -> Tuple[Path, Path, Path, Path]:
    baseline_root = (
        Path(args.root).expanduser()
        if args.root
        else default_baseline_root()
    )
    input_root = (
        Path(args.input_root).expanduser()
        if args.input_root
        else baseline_logs_dir(baseline_root)
    )
    output_root = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else baseline_output_dir(baseline_root)
    )
    return baseline_root, input_root, output_root, baseline_data_repo_root(baseline_root)


def has_native_artifacts(input_root: Path, floor_plan: Optional[str]) -> bool:
    return bool(_impl.iter_plan_source_paths(input_root, floor_plan, limit=1))


def run_pddlrun_conversion(
    *,
    input_root: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> int:
    return pddlrun.convert(
        logs_dir=input_root,
        output_dir=output_root,
        validate_code=args.validate_code,
        parallel_run=args.parallel_run,
        floor_plan=args.floor_plan or None,
    )


def convert_one(*args, **kwargs):
    _sync_globals()
    return _impl.convert_one(*args, **kwargs)


def convert(*args, **kwargs):
    _sync_globals()
    return _impl.convert(*args, **kwargs)


def dataset_path_for_metadata(*args, **kwargs):
    _sync_globals()
    return _impl.dataset_path_for_metadata(*args, **kwargs)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify and convert SMART-LLM code-plan or pddlrun artifacts."
    )
    parser.add_argument(
        "--root",
        default="",
        help="SMART-LLM baseline root. Defaults to baselines/SMART-LLM.",
    )
    parser.add_argument(
        "--input-root",
        "--logs-dir",
        dest="input_root",
        default="",
        help="Root containing SMART-LLM log folders. Defaults to <root>/logs.",
    )
    parser.add_argument(
        "--parallel-run",
        default="",
        help=(
            "Path to a pddlrun parallel_runs output directory or summary.json. "
            "When set, pddlrun fallback conversion is used."
        ),
    )
    parser.add_argument(
        "--output-dir",
        "--output-root",
        dest="output_dir",
        default="",
        help="Directory for plan_to_code summary files. Defaults to <root>.",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of native source files to process.")
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
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)

    _, input_root, output_root, data_repo_root = resolve_baseline_paths(args)
    _sync_globals(data_repo_root)

    floor_plan = args.floor_plan or None
    if args.parallel_run:
        return run_pddlrun_conversion(input_root=input_root, output_root=output_root, args=args)

    if has_native_artifacts(input_root, floor_plan):
        return _impl.convert(
            input_root=input_root.resolve(),
            output_root=output_root.resolve(),
            floor_plan=floor_plan,
            limit=args.limit,
            dry_run=bool(args.dry_run),
            validate_code=bool(args.validate_code),
        )

    print(
        f"No native SMART-LLM plan-to-code artifacts found under {input_root}; "
        "falling back to pddlrun task-run scan."
    )
    return run_pddlrun_conversion(input_root=input_root, output_root=output_root, args=args)


if __name__ == "__main__":
    raise SystemExit(main())
