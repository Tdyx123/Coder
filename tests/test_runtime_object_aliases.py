import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.demo_state import ground_truth_lock, verified_ground_truth_goal_signatures
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
    def setUp(self):
        with ground_truth_lock:
            verified_ground_truth_goal_signatures.clear()

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

    def test_find_objects_records_inferred_alias_for_unregistered_pattern(self):
        mug_id = "Mug|+00.00|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": mug_id,
                    "objectType": "Mug",
                    "visible": True,
                    "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                }
            ]
        )

        matches = runtime.find_objects("Mug", agent_id=0)

        self.assertEqual([obj["objectId"] for obj in matches], [mug_id])
        self.assertEqual(runtime.object_alias_bindings["Mug"]["object_id"], mug_id)
        self.assertEqual(runtime.object_alias_bindings["Mug"]["object_type"], "Mug")
        self.assertEqual(
            runtime.object_alias_bindings["Mug"]["last_position"],
            {"x": 0.0, "y": 0.9, "z": 0.0},
        )
        self.assertEqual(runtime.object_alias_bindings["Mug"]["count"], 1)
        self.assertFalse(runtime.object_alias_bindings["Mug"]["multiple"])
        self.assertTrue(runtime.object_alias_bindings["Mug"]["inferred"])
        self.assertEqual(runtime.object_alias_key_to_token["mug"], "Mug")
        self.assertIn("Mug", runtime.object_alias_by_object_id[mug_id])

    def test_find_objects_records_first_sorted_match_for_multiple_matches(self):
        hidden_id = "Apple|+00.00|+00.90|+00.00"
        visible_id = "Apple|+01.00|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": hidden_id,
                    "objectType": "Apple",
                    "visible": False,
                    "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                },
                {
                    "objectId": visible_id,
                    "objectType": "Apple",
                    "visible": True,
                    "position": {"x": 1.0, "y": 0.9, "z": 0.0},
                },
            ]
        )

        matches = runtime.find_objects("Apple", agent_id=0)

        self.assertEqual(
            [obj["objectId"] for obj in matches],
            [visible_id, hidden_id],
        )
        self.assertEqual(runtime.object_alias_bindings["Apple"]["object_id"], visible_id)
        self.assertEqual(runtime.object_alias_bindings["Apple"]["count"], 2)
        self.assertTrue(runtime.object_alias_bindings["Apple"]["multiple"])
        self.assertIn("Apple", runtime.object_alias_by_object_id[visible_id])

    def test_find_objects_refreshes_existing_alias_without_new_token(self):
        old_id = "Mug|+00.00|+00.90|+00.00"
        new_id = "Mug|+01.00|+00.95|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": new_id,
                    "objectType": "Mug",
                    "visible": True,
                    "position": {"x": 1.0, "y": 0.95, "z": 0.0},
                }
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

        matches = runtime.find_objects("Mug_1", agent_id=0)

        self.assertEqual([obj["objectId"] for obj in matches], [new_id])
        self.assertEqual(runtime.object_alias_bindings["Mug_1"]["object_id"], new_id)
        self.assertEqual(list(runtime.object_alias_bindings), ["Mug_1"])
        self.assertNotIn(old_id, runtime.object_alias_by_object_id)
        self.assertIn("Mug_1", runtime.object_alias_by_object_id[new_id])

    def test_find_objects_does_not_record_alias_when_no_match(self):
        runtime = runtime_with_objects(
            [
                {
                    "objectId": "Mug|+00.00|+00.90|+00.00",
                    "objectType": "Mug",
                    "visible": True,
                }
            ]
        )

        self.assertEqual(runtime.find_objects("MissingObject", agent_id=0), [])
        self.assertNotIn("MissingObject", runtime.object_alias_bindings)
        self.assertNotIn("missingobject", runtime.object_alias_key_to_token)
        self.assertEqual(runtime.object_alias_by_object_id, {})

    def test_goal_satisfied_resolves_numbered_alias_name(self):
        first_id = "Drawer|+01.00|+00.20|-00.30"
        second_id = "Drawer|+01.00|+00.60|-00.30"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": first_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "isOpen": False,
                },
                {
                    "objectId": second_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "isOpen": True,
                },
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

        self.assertTrue(
            runtime.goal_satisfied(
                {"name": "Drawer_2", "contains": [], "states": ["OPENED"]}
            )
        )
        self.assertFalse(
            runtime.goal_satisfied(
                {"name": "Drawer_1", "contains": [], "states": ["OPENED"]}
            )
        )

    def test_goal_satisfied_resolves_numbered_alias_contains(self):
        drawer_id = "Drawer|+01.00|+00.60|-00.30"
        first_card_id = "CreditCard|+00.00|+00.90|+00.00"
        second_card_id = "CreditCard|+01.00|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": drawer_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "receptacleObjectIds": [second_card_id],
                },
                {
                    "objectId": first_card_id,
                    "objectType": "CreditCard",
                    "visible": True,
                },
                {
                    "objectId": second_card_id,
                    "objectType": "CreditCard",
                    "visible": True,
                },
            ]
        )
        runtime.register_object_id_bindings(
            [
                {
                    "object": "Drawer_1",
                    "object_type": "Drawer",
                    "object_id": drawer_id,
                    "number": 1,
                    "count": 1,
                    "multiple": False,
                },
                {
                    "object": "CreditCard_1",
                    "object_type": "CreditCard",
                    "object_id": first_card_id,
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "CreditCard_2",
                    "object_type": "CreditCard",
                    "object_id": second_card_id,
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
            ]
        )

        self.assertTrue(
            runtime.goal_satisfied(
                {"name": "Drawer_1", "contains": ["CreditCard_2"], "states": []}
            )
        )
        self.assertFalse(
            runtime.goal_satisfied(
                {"name": "Drawer_1", "contains": ["CreditCard_1"], "states": []}
            )
        )

    def test_goal_satisfied_only_checks_first_state_candidate(self):
        first_id = "Drawer|+01.00|+00.20|-00.30"
        second_id = "Drawer|+01.00|+00.60|-00.30"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": first_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "isOpen": False,
                },
                {
                    "objectId": second_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "isOpen": True,
                },
            ]
        )

        self.assertFalse(
            runtime.goal_satisfied(
                {"name": "Drawer", "contains": [], "states": ["OPENED"]}
            )
        )

        runtime._test_objects = [
            {
                "objectId": second_id,
                "objectType": "Drawer",
                "visible": True,
                "isOpen": True,
            },
            {
                "objectId": first_id,
                "objectType": "Drawer",
                "visible": True,
                "isOpen": False,
            },
        ]

        self.assertTrue(
            runtime.goal_satisfied(
                {"name": "Drawer", "contains": [], "states": ["OPENED"]}
            )
        )

    def test_goal_satisfied_only_checks_first_contains_candidate(self):
        first_drawer_id = "Drawer|+01.00|+00.20|-00.30"
        second_drawer_id = "Drawer|+01.00|+00.60|-00.30"
        credit_card_id = "CreditCard|+01.00|+00.90|+00.00"
        runtime = runtime_with_objects(
            [
                {
                    "objectId": first_drawer_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "receptacleObjectIds": [],
                },
                {
                    "objectId": second_drawer_id,
                    "objectType": "Drawer",
                    "visible": True,
                    "receptacleObjectIds": [credit_card_id],
                },
                {
                    "objectId": credit_card_id,
                    "objectType": "CreditCard",
                    "visible": True,
                },
            ]
        )

        self.assertFalse(
            runtime.goal_satisfied(
                {"name": "Drawer", "contains": ["CreditCard"], "states": []}
            )
        )

        runtime._test_objects = [
            {
                "objectId": second_drawer_id,
                "objectType": "Drawer",
                "visible": True,
                "receptacleObjectIds": [credit_card_id],
            },
            {
                "objectId": first_drawer_id,
                "objectType": "Drawer",
                "visible": True,
                "receptacleObjectIds": [],
            },
            {
                "objectId": credit_card_id,
                "objectType": "CreditCard",
                "visible": True,
            },
        ]

        self.assertTrue(
            runtime.goal_satisfied(
                {"name": "Drawer", "contains": ["CreditCard"], "states": []}
            )
        )


if __name__ == "__main__":
    unittest.main()
