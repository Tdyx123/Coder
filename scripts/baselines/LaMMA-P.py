#!/usr/bin/env python3
"""Compatibility wrapper for the LaMMA-P final-plan converter."""

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

from baseline_converters import lammap as _impl
from baseline_converters import pddlrun
from baseline_converters.lammap import *  # noqa: F401,F403


SCRIPTS_DIR = _impl.SCRIPTS_DIR
REPO_ROOT = _impl.REPO_ROOT
DEFAULT_LOGS_DIR = _impl.DEFAULT_LOGS_DIR
DEFAULT_OUTPUT_DIR = _impl.DEFAULT_OUTPUT_DIR


def default_baseline_root() -> Path:
    return REPO_ROOT / "baselines" / "LaMMA-P"


def baseline_logs_dir(root: Path) -> Path:
    return root / "logs" / "intermediate_runs"


def baseline_output_dir(root: Path) -> Path:
    return root / "plan_to_code_results"


def baseline_data_repo_root(baseline_root: Path) -> Path:
    root = baseline_root.expanduser()
    if root.parent.name == "baselines":
        return root.parent.parent
    return root


def _sync_globals(repo_root: Optional[Path] = None) -> None:
    data_repo_root = Path(repo_root or REPO_ROOT).expanduser()
    _impl.SCRIPTS_DIR = SCRIPTS_DIR
    _impl.REPO_ROOT = data_repo_root
    _impl.DEFAULT_LOGS_DIR = DEFAULT_LOGS_DIR
    _impl.DEFAULT_OUTPUT_DIR = DEFAULT_OUTPUT_DIR


def resolve_baseline_paths(args: argparse.Namespace) -> Tuple[Path, Path, Path, Path]:
    baseline_root = (
        Path(args.root).expanduser()
        if args.root
        else default_baseline_root()
    )
    logs_dir = (
        Path(args.logs_dir).expanduser()
        if args.logs_dir
        else baseline_logs_dir(baseline_root)
    )
    output_root = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else baseline_output_dir(baseline_root)
    )
    return baseline_root, logs_dir, output_root, baseline_data_repo_root(baseline_root)


def has_native_artifacts(logs_dir: Path) -> bool:
    return bool(_impl.discover_task_runs(logs_dir))


def run_pddlrun_conversion(
    *,
    logs_dir: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> int:
    return pddlrun.convert(
        logs_dir=logs_dir,
        output_dir=output_root,
        validate_code=args.validate_code,
        parallel_run=args.parallel_run,
        floor_plan=args.floor_plan or None,
    )


def process_task_run(*args, **kwargs):
    _sync_globals()
    return _impl.process_task_run(*args, **kwargs)


def convert(*args, **kwargs):
    _sync_globals()
    return _impl.convert(*args, **kwargs)


def dataset_path_for_run(*args, **kwargs):
    _sync_globals()
    return _impl.dataset_path_for_run(*args, **kwargs)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify and convert LaMMA-P final-plan or pddlrun artifacts."
    )
    parser.add_argument(
        "--root",
        default="",
        help="LaMMA-P baseline root. Defaults to baselines/LaMMA-P.",
    )
    parser.add_argument(
        "--logs-dir",
        default="",
        help=(
            "Path to LaMMA-P logs. Defaults to "
            "<root>/logs/intermediate_runs."
        ),
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
        "--floor-plan",
        default="",
        help="Optional FloorPlan filter, e.g. 6 or FloorPlan6.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for plan_to_code summary files. Defaults to <root>/plan_to_code_results.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Only process a native LaMMA-P category. Can be supplied multiple times.",
    )
    parser.add_argument("--dry-run", action="store_true")
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
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    _, logs_dir, output_root, data_repo_root = resolve_baseline_paths(args)
    _sync_globals(data_repo_root)

    if args.parallel_run:
        return run_pddlrun_conversion(logs_dir=logs_dir, output_root=output_root, args=args)

    if has_native_artifacts(logs_dir):
        return _impl.convert(
            logs_dir=logs_dir,
            output_root=output_root,
            floor_plan=args.floor_plan or None,
            limit=args.limit,
            categories=args.category,
            dry_run=args.dry_run,
            validate_code=args.validate_code,
        )

    print(
        f"No native LaMMA-P plan-to-code artifacts found under {logs_dir}; "
        "falling back to pddlrun task-run scan."
    )
    return run_pddlrun_conversion(logs_dir=logs_dir, output_root=output_root, args=args)


if __name__ == "__main__":
    raise SystemExit(main())
