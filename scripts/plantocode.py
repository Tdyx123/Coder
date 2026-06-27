#!/usr/bin/env python3
"""Generate executor bundles from pddlrun allocation and planner artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
for path in (SCRIPTS_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from baseline_converters import pddlrun


PlanToCodeError = pddlrun.PlanToCodeError
GenerationPaths = pddlrun.GenerationPaths


def resolve_generation_paths(args: argparse.Namespace) -> GenerationPaths:
    return GenerationPaths(
        logs_dir=Path(args.logs_dir or "./logs").expanduser(),
        output_dir=Path(args.output_dir or "./plan_to_code_results").expanduser(),
    )


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate demo-style Python executor scripts from pddlrun allocation "
            "and planner artifacts."
        )
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="",
        help="Path to pddlrun logs containing run_manifest.json files. Defaults to ./logs.",
    )
    parser.add_argument(
        "--parallel-run",
        type=str,
        default="",
        help=(
            "Path to a pddlrun parallel_runs output directory or summary.json. "
            "When set, this is used instead of --logs-dir."
        ),
    )
    parser.add_argument(
        "--floor-plan",
        type=str,
        default="",
        help="Optional FloorPlan filter, e.g. 6 or FloorPlan6.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Directory to save generation summaries. Defaults to ./plan_to_code_results.",
    )
    parser.add_argument(
        "--validate-code",
        action="store_true",
        default=True,
        help="Compile generated executable_plan.py files after writing them (default: True).",
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
    paths = resolve_generation_paths(args)

    return pddlrun.convert(
        logs_dir=paths.logs_dir,
        output_dir=paths.output_dir,
        validate_code=args.validate_code,
        parallel_run=args.parallel_run,
        floor_plan=args.floor_plan or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
