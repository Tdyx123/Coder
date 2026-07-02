#!/usr/bin/env python3
"""Compatibility wrapper for the Scale-Plan plan-to-code converter."""

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

from baseline_converters import scale_plan as _impl
from baseline_converters.scale_plan import *  # noqa: F401,F403


REPO_ROOT = _impl.REPO_ROOT
SCRIPTS_DIR = _impl.SCRIPTS_DIR
DEFAULT_BASELINE_ROOT = _impl.DEFAULT_BASELINE_ROOT
DEFAULT_LOGS_DIR = _impl.DEFAULT_LOGS_DIR
DEFAULT_SUMMARY_ROOT = _impl.DEFAULT_SUMMARY_ROOT
DEFAULT_OUTPUT_DIR = _impl.DEFAULT_OUTPUT_DIR


def default_baseline_root() -> Path:
    return REPO_ROOT / "baselines" / "Scale-Plan"


def baseline_logs_dir(root: Path) -> Path:
    return root / "logs" / "intermediate_runs"


def baseline_summary_root(root: Path) -> Path:
    return root / "logs" / "scale_plan_parallel"


def baseline_output_dir(root: Path) -> Path:
    return root / "plan_to_code_results"


def baseline_data_repo_root(baseline_root: Path) -> Path:
    root = baseline_root.expanduser()
    if root.parent.name == "baselines":
        return root.parent.parent
    return root


def _sync_globals(repo_root: Optional[Path] = None) -> None:
    data_repo_root = Path(repo_root or REPO_ROOT).expanduser()
    _impl.REPO_ROOT = data_repo_root
    _impl.SCRIPTS_DIR = data_repo_root / "scripts"
    _impl.DEFAULT_BASELINE_ROOT = data_repo_root / "baselines" / "Scale-Plan"
    _impl.DEFAULT_LOGS_DIR = _impl.DEFAULT_BASELINE_ROOT / "logs" / "intermediate_runs"
    _impl.DEFAULT_SUMMARY_ROOT = _impl.DEFAULT_BASELINE_ROOT / "logs" / "scale_plan_parallel"
    _impl.DEFAULT_OUTPUT_DIR = _impl.DEFAULT_BASELINE_ROOT / "plan_to_code_results"


def resolve_baseline_paths(args: argparse.Namespace) -> Tuple[Path, Path, Path, Path, Path]:
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
    summary_root = baseline_summary_root(baseline_root)
    output_root = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else baseline_output_dir(baseline_root)
    )
    return (
        baseline_root,
        summary_root,
        logs_dir,
        output_root,
        baseline_data_repo_root(baseline_root),
    )


def convert(*args, **kwargs):
    _sync_globals()
    return _impl.convert(*args, **kwargs)


def process_indexed_run(*args, **kwargs):
    _sync_globals()
    return _impl.process_indexed_run(*args, **kwargs)


def dataset_path_for_run(*args, **kwargs):
    _sync_globals()
    return _impl.dataset_path_for_run(*args, **kwargs)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Scale-Plan final-plan JSON artifacts."
    )
    parser.add_argument(
        "--root",
        default="",
        help="Scale-Plan baseline root. Defaults to baselines/Scale-Plan.",
    )
    parser.add_argument(
        "--logs-dir",
        default="",
        help="Local Scale-Plan intermediate_runs root. Defaults to <root>/logs/intermediate_runs.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for plan_to_code summary files. Defaults to <root>/plan_to_code_results.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--floor-plan",
        default="",
        help="Optional FloorPlan filter, e.g. 6 or FloorPlan6.",
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
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    _, summary_root, logs_dir, output_root, data_repo_root = resolve_baseline_paths(args)
    _sync_globals(data_repo_root)

    return _impl.convert(
        summary_root=summary_root,
        logs_dir=logs_dir,
        output_root=output_root,
        floor_plan=args.floor_plan or None,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        validate_code=bool(args.validate_code),
    )


if __name__ == "__main__":
    raise SystemExit(main())
