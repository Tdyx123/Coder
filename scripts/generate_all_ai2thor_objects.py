#!/usr/bin/env python3
"""Generate a flat AI2-THOR object metadata file for all target floor plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from ai2thor.controller import Controller
from ai2thor.platform import CloudRendering


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "all_ai2thor_objects.json"

INITIALIZE_KWARGS = {
    "action": "Initialize",
    "agentMode": "default",
    "snapGrid": False,
    "gridSize": 0.25,
    "rotateStepDegrees": 20,
    "visibilityDistance": 100,
    "fieldOfView": 90,
}


def get_default_floor_plans() -> List[int]:
    return (
        list(range(1, 31))
        + list(range(201, 231))
        + list(range(301, 331))
        + list(range(401, 431))
    )


def normalize_floor_plan(value: str) -> int:
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan") :]
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"invalid floor plan: {value!r}")
    return int(text)


def fetch_floor_plan_objects(floor_plan: int) -> List[Dict[str, Any]]:
    scene_name = f"FloorPlan{floor_plan}"
    controller = None
    try:
        controller = Controller(scene=scene_name, platform=CloudRendering)
        controller.step(**INITIALIZE_KWARGS)
        objects = controller.last_event.metadata["objects"]
        return [{**item, "scene": scene_name} for item in objects]
    finally:
        if controller is not None:
            controller.stop()


def fetch_all_objects(floor_plans: Sequence[int]) -> List[Dict[str, Any]]:
    all_objects: List[Dict[str, Any]] = []
    for floor_plan in floor_plans:
        scene_objects = fetch_floor_plan_objects(floor_plan)
        all_objects.extend(scene_objects)
        print(f"FloorPlan{floor_plan}: {len(scene_objects)} objects")
    return all_objects


def write_objects(objects: List[Dict[str, Any]], output_path: Path, indent: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(objects, output_file, ensure_ascii=False, indent=indent)
        output_file.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate data/all_ai2thor_objects.json from AI2-THOR metadata."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output JSON path. Defaults to {DEFAULT_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation level.",
    )
    parser.add_argument(
        "--floor-plans",
        nargs="+",
        type=normalize_floor_plan,
        default=None,
        help="Floor plans to fetch, e.g. 1 201 FloorPlan301.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    floor_plans = args.floor_plans if args.floor_plans is not None else get_default_floor_plans()

    print(f"Fetching {len(floor_plans)} floor plans with CloudRendering...")
    objects = fetch_all_objects(floor_plans)
    write_objects(objects, args.output, args.indent)
    print(f"Wrote {len(objects)} objects to {args.output}")


if __name__ == "__main__":
    main()
