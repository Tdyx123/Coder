import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.runtime import ThorRuntime


class FakeEvent:
    def __init__(self, objects):
        self.metadata = {
            "lastActionSuccess": True,
            "objects": [dict(obj) for obj in objects],
        }


def runtime_with_objects(objects):
    runtime = object.__new__(ThorRuntime)
    runtime.operated_object_names = set()
    runtime.operated_object_names_lock = threading.Lock()
    runtime._test_objects = [dict(obj) for obj in objects]
    runtime.current_objects = lambda _agent_id=None: [
        dict(obj) for obj in runtime._test_objects
    ]
    runtime._ensure_object_alias_state()
    return runtime


class RuntimeObjectAliasTest(unittest.TestCase):
    def test_numbered_aliases_resolve_to_bound_object_ids(self):
        first_id = "Drawer|+01.00|+00.20|-00.30"
        second_id = "Drawer|+01.00|+00.60|-00.30"
        runtime = runtime_with_objects(
            [
                {"objectId": first_id, "objectType": "Drawer", "visible": True},
                {"objectId": second_id, "objectType": "Drawer", "visible": True},
            ]
        )

        runtime.register_object_id_bindings(
            [
                {
                    "object": "Drawer_1",
                    "object_type": "Drawer",
                    "object_id": first_id,
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Drawer_2",
                    "object_type": "Drawer",
                    "object_id": second_id,
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
            ]
        )

        self.assertEqual(runtime.find_object("Drawer_2", agent_id=0)["objectId"], second_id)
        self.assertEqual(runtime.find_object("drawer_2", agent_id=0)["objectId"], second_id)
        self.assertEqual(runtime.find_object("drawer2", agent_id=0)["objectId"], second_id)

    def test_putobject_refreshes_alias_when_object_id_changes(self):
        old_id = "Mug|+00.00|+00.90|+00.00"
        new_id = "Mug|+01.00|+00.95|+00.00"
        table_id = "Table|+01.00|+00.80|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": old_id,
                    "objectType": "Mug",
                    "visible": True,
                    "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                },
                {"objectId": table_id, "objectType": "Table", "visible": True},
            ]
        )
        runtime.register_object_id_bindings(
            [
                {
                    "object": "Mug_1",
                    "object_type": "Mug",
                    "object_id": old_id,
                    "number": 1,
                    "count": 1,
                    "multiple": False,
                }
            ]
        )

        runtime._test_objects = [
            {
                "objectId": new_id,
                "objectType": "Mug",
                "visible": True,
                "parentReceptacles": [table_id],
                "position": {"x": 1.0, "y": 0.95, "z": 0.0},
            },
            {"objectId": table_id, "objectType": "Table", "visible": True},
        ]
        runtime.update_object_aliases_for_object_ids(
            [old_id],
            agent_id=0,
            event=FakeEvent(runtime._test_objects),
            preferred_parent_id=table_id,
        )

        self.assertEqual(runtime.find_object("Mug_1", agent_id=0)["objectId"], new_id)

    def test_sliceobject_refreshes_alias_to_created_sliced_object(self):
        old_id = "Apple|+00.00|+00.90|+00.00"
        sliced_id = "AppleSliced|+00.10|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": old_id,
                    "objectType": "Apple",
                    "visible": True,
                    "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                }
            ]
        )
        runtime.register_object_id_bindings(
            [
                {
                    "object": "Apple_1",
                    "object_type": "Apple",
                    "object_id": old_id,
                    "number": 1,
                    "count": 1,
                    "multiple": False,
                }
            ]
        )

        runtime._test_objects = [
            {
                "objectId": sliced_id,
                "objectType": "AppleSliced",
                "visible": True,
                "isSliced": True,
                "position": {"x": 0.1, "y": 0.9, "z": 0.0},
            }
        ]
        runtime.update_object_alias_after_action(
            "SliceObject",
            0,
            {"objectId": old_id, "objectType": "Apple"},
            event=FakeEvent(runtime._test_objects),
            goal_object_name="Apple_1",
            known_object_ids={old_id},
        )

        self.assertEqual(runtime.find_object("Apple_1", agent_id=0)["objectId"], sliced_id)

    def test_breakobject_refreshes_egg_alias_to_broken_object(self):
        old_id = "Egg|+00.00|+00.90|+00.00"
        broken_id = "EggCracked|+00.10|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": old_id,
                    "objectType": "Egg",
                    "visible": True,
                    "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                }
            ]
        )
        runtime.register_object_id_bindings(
            [
                {
                    "object": "Egg_1",
                    "object_type": "Egg",
                    "object_id": old_id,
                    "number": 1,
                    "count": 1,
                    "multiple": False,
                }
            ]
        )

        runtime._test_objects = [
            {
                "objectId": broken_id,
                "objectType": "EggCracked",
                "visible": True,
                "isBroken": True,
                "position": {"x": 0.1, "y": 0.9, "z": 0.0},
            }
        ]
        runtime.update_object_alias_after_action(
            "BreakObject",
            0,
            {"objectId": old_id, "objectType": "Egg"},
            event=FakeEvent(runtime._test_objects),
            goal_object_name="Egg_1",
            known_object_ids={old_id},
        )

        self.assertEqual(runtime.find_object("Egg_1", agent_id=0)["objectId"], broken_id)


if __name__ == "__main__":
    unittest.main()
