import argparse
import re
from pathlib import Path
from typing import Iterable, List

from ai2thor_object_cache import get_ai2_thor_objects_cached


def convert_to_dict_objprop(objs, obj_mass):
    return [{"name": obj, "mass": mass} for obj, mass in zip(objs, obj_mass)]


def collect_floor_plans(dataset_dir: Path) -> List[int]:
    floor_plans = set()
    for dataset_file in dataset_dir.glob("FloorPlan*.jsonl"):
        match = re.match(r"FloorPlan(\d+)", dataset_file.stem)
        if match:
            floor_plans.add(int(match.group(1)))
    return sorted(floor_plans)


def warm_floor_plan_cache(floor_plans: Iterable[int], force_refresh: bool = False) -> None:
    for floor_plan in floor_plans:
        objects = get_ai2_thor_objects_cached(
            floor_plan,
            convert_to_dict_objprop,
            force_refresh=force_refresh,
        )
        print(f"Cached FloorPlan{floor_plan}: {len(objects)} objects")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-cache AI2-THOR objects for all floor plans used by a dataset directory."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path("data/final_test"),
        help="Directory containing FloorPlan*.jsonl dataset files.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore existing cache files and rebuild them from AI2-THOR.",
    )
    args = parser.parse_args()

    dataset_dir = args.dataset_dir.resolve()
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Dataset directory does not exist: {dataset_dir}")

    floor_plans = collect_floor_plans(dataset_dir)
    if not floor_plans:
        print(f"No floor plan files found in {dataset_dir}")
        return

    print(f"Found {len(floor_plans)} unique floor plans in {dataset_dir}")
    warm_floor_plan_cache(floor_plans, force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
