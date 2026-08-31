#!/usr/bin/env python3
"""Compatibility wrapper for the COT direct-plan converter."""

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

from baseline_converters import cot as _impl
from baseline_converters.cot import *  # noqa: F401,F403


def default_baseline_root() -> Path:
    return REPO_ROOT / "baselines" / "COT"


def baseline_summary_root(root: Path) -> Path:
    return root / "parallel_runs"


def baseline_output_dir(root: Path) -> Path:
    return root / "plan_to_code_results"


def baseline_data_repo_root(baseline_root: Path) -> Path:
    root = baseline_root.expanduser()
    if root.parent.name == "baselines":
        return root.parent.parent
    return REPO_ROOT


def resolve_baseline_paths(args: argparse.Namespace) -> Tuple[Path, Path, Path, Path]:
    baseline_root = (
        Path(args.root).expanduser().resolve()
        if args.root
        else default_baseline_root().resolve()
    )
    summary_root = (
        Path(args.summary_root).expanduser().resolve()
        if args.summary_root
        else baseline_summary_root(baseline_root)
    )
    output_root = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else baseline_output_dir(baseline_root)
    )
    return baseline_root, summary_root, output_root, baseline_data_repo_root(baseline_root)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert COT direct-planner JSON plans into executable bundles."
    )
    parser.add_argument(
        "--root",
        default="",
        help="COT baseline root. Defaults to baselines/COT.",
    )
    parser.add_argument(
        "--summary-root",
        default="",
        help="COT parallel_runs root or top-level summary.json. Defaults to <root>/parallel_runs.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for plan-to-code summary files. Defaults to <root>/plan_to_code_results.",
    )
    parser.add_argument(
        "--floor-plan",
        default="",
        help="Optional FloorPlan filter, e.g. 1 or FloorPlan1.",
    )
    parser.add_argument("--limit", type=int, default=None)
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
    baseline_root, summary_root, output_root, repo_root = resolve_baseline_paths(args)
    return _impl.convert(
        repo_root=repo_root,
        baseline_root=baseline_root,
        summary_root=summary_root,
        output_root=output_root,
        floor_plan=args.floor_plan or None,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        validate_code=bool(args.validate_code),
    )


if __name__ == "__main__":
    raise SystemExit(main())
