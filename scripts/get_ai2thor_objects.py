import argparse
import json
from typing import Any, Dict, List

from ai2thor.controller import Controller


def fetch_all_object_properties(floor_plan: int) -> List[Dict[str, Any]]:
    """Fetch all object properties from AI2-THOR floorplan."""
    controller = None
    try:
        controller = Controller(scene=f"FloorPlan{floor_plan}")
        controller.step(
            action="Initialize",
            agentMode="default",
            snapGrid=False,
            gridSize=0.25,
            rotateStepDegrees=20,
            visibilityDistance=100,
            fieldOfView=90,
        )
        objects = controller.last_event.metadata["objects"]
        return objects
    finally:
        if controller is not None:
            controller.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Get all object properties from AI2-THOR floorplan."
    )
    parser.add_argument(
        "floor_plan",
        type=int,
        help="Floor plan number (e.g., 1, 201, 301, 401)",
    )
    args = parser.parse_args()

    objects = fetch_all_object_properties(args.floor_plan)
    print(f"FloorPlan{args.floor_plan} - {len(objects)} objects:\n")
    print(json.dumps(objects, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()