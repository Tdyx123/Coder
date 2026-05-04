import argparse
from typing import Iterable, List

from ai2thor_object_cache import get_ai2_thor_objects_cached


def convert_to_dict_objprop(objs, obj_mass):
    return [{"name": obj, "mass": mass} for obj, mass in zip(objs, obj_mass)]


def get_all_floor_plans() -> List[int]:
    return (
        list(range(1, 31)) +
        list(range(201, 231)) +
        list(range(301, 331)) +
        list(range(401, 431))
    )


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
        description="Pre-cache AI2-THOR objects for all floor plans."
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore existing cache files and rebuild them from AI2-THOR.",
    )
    args = parser.parse_args()

    floor_plans = get_all_floor_plans()
    print(f"Caching {len(floor_plans)} floor plans...")
    warm_floor_plan_cache(floor_plans, force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
