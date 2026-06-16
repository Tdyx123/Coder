import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import data_engine
import generate_single_subtask_code as generator


class GenerateSingleSubtaskCodeTests(unittest.TestCase):
    def _write_properties(self, objects):
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "objects.json"
        path.write_text(json.dumps(objects), encoding="utf-8")
        self.addCleanup(tmp_dir.cleanup)
        return path

    def _write_bad_subtasks_config(self, config):
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "bad_subtasks.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.addCleanup(tmp_dir.cleanup)
        return path

    def _write_no_valid_positions_config(self, records):
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "no_valid_positions.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        self.addCleanup(tmp_dir.cleanup)
        return path

    def _fixture_properties(self):
        return self._write_properties(
            [
                {"scene": "FloorPlan1", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Bowl", "pickupable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Bread", "pickupable": True, "sliceable": True},
                {"scene": "FloorPlan1", "objectType": "Cabinet", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Egg", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Knife", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Microwave", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Pan", "pickupable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Plate", "pickupable": True, "dirtyable": True},
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "StoveBurner", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Toaster"},
            ]
        )

    def _action_pairs(self, actions):
        return [
            (item["action_type"], item["parameters"]["args"])
            for item in actions
        ]

    def test_floor_specific_bad_subtask_filter_only_excludes_matching_floor(self):
        config_path = self._write_bad_subtasks_config(
            {
                "version": 1,
                "bad_subtasks": [
                    {
                        "floor_plan": "FloorPlan1",
                        "subtask": {"skill": "Break", "objects": ["WineBottle"]},
                        "reason": "Known runner failure in FloorPlan1",
                    }
                ],
            }
        )
        rules = generator.load_bad_subtask_rules(config_path, missing_ok=False)
        enumerated = [
            generator.EnumeratedSubtask(
                floor_plan=1,
                subtask={"skill": "Break", "objects": ["WineBottle"]},
            ),
            generator.EnumeratedSubtask(
                floor_plan=2,
                subtask={"skill": "Break", "objects": ["WineBottle"]},
            ),
            generator.EnumeratedSubtask(
                floor_plan=1,
                subtask={"skill": "Open", "objects": ["Cabinet"]},
            ),
        ]

        filtered, excluded = generator.filter_bad_subtasks(enumerated, rules)

        self.assertEqual(excluded, 1)
        self.assertEqual(filtered, enumerated[1:])

    def test_global_bad_subtask_filter_excludes_every_floor(self):
        config_path = self._write_bad_subtasks_config(
            {
                "version": 1,
                "bad_subtasks": [
                    {
                        "floor_plan": None,
                        "subtask": {"skill": "Break", "objects": ["WineBottle"]},
                        "reason": "Known runner failure on all floors",
                    }
                ],
            }
        )
        rules = generator.load_bad_subtask_rules(config_path, missing_ok=False)
        enumerated = [
            generator.EnumeratedSubtask(
                floor_plan=1,
                subtask={"skill": "Break", "objects": ["WineBottle"]},
            ),
            generator.EnumeratedSubtask(
                floor_plan=2,
                subtask={"skill": "Break", "objects": ["WineBottle"]},
            ),
            generator.EnumeratedSubtask(
                floor_plan=1,
                subtask={"skill": "Open", "objects": ["Cabinet"]},
            ),
        ]

        filtered, excluded = generator.filter_bad_subtasks(enumerated, rules)

        self.assertEqual(excluded, 2)
        self.assertEqual(filtered, [enumerated[2]])

    def test_bad_subtask_filter_applies_before_limit(self):
        bad_subtask = {"skill": "Break", "objects": ["WineBottle"]}
        good_subtask = {"skill": "Open", "objects": ["Cabinet"]}
        config_path = self._write_bad_subtasks_config(
            {
                "version": 1,
                "bad_subtasks": [
                    {
                        "floor_plan": 1,
                        "subtask": bad_subtask,
                        "reason": "Known runner failure in FloorPlan1",
                    }
                ],
            }
        )
        captured_enumerated = []
        summary_holder = {}

        original_enumerate = generator.enumerate_single_subtasks
        original_prepare = generator.prepare_generated_subtasks
        original_write_flat = generator.write_flat_outputs

        def fake_enumerate(_floor_plans):
            return [
                generator.EnumeratedSubtask(floor_plan=1, subtask=bad_subtask),
                generator.EnumeratedSubtask(floor_plan=1, subtask=good_subtask),
            ]

        def fake_prepare(enumerated, _object_properties_path=generator.DEFAULT_OBJECT_PROPERTIES_PATH):
            captured_enumerated.extend(enumerated)
            return []

        def fake_write_flat(_generated, output_dir, **_kwargs):
            summary = {
                "output_layout": "flat",
                "floor_count": 0,
                "total_subtasks": len(captured_enumerated),
                "successful_generations": len(captured_enumerated),
                "failed_generations": 0,
                "skipped_existing": 0,
                "output_dir": str(output_dir),
                "errors": [],
            }
            summary_holder["summary"] = summary
            return summary

        generator.enumerate_single_subtasks = fake_enumerate
        generator.prepare_generated_subtasks = fake_prepare
        generator.write_flat_outputs = fake_write_flat
        self.addCleanup(lambda: setattr(generator, "enumerate_single_subtasks", original_enumerate))
        self.addCleanup(lambda: setattr(generator, "prepare_generated_subtasks", original_prepare))
        self.addCleanup(lambda: setattr(generator, "write_flat_outputs", original_write_flat))

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = generator.main(
                [
                    "--floor-plans",
                    "1",
                    "--limit",
                    "1",
                    "--bad-subtasks-config",
                    str(config_path),
                    "--output-dir",
                    str(Path(tmp_dir) / "generated"),
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            captured_enumerated,
            [generator.EnumeratedSubtask(floor_plan=1, subtask=good_subtask)],
        )
        self.assertEqual(summary_holder["summary"]["excluded_subtasks"], 1)
        self.assertEqual(
            summary_holder["summary"]["bad_subtasks_config"],
            str(config_path),
        )

    def test_missing_bad_subtask_config_is_empty_when_allowed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing_bad_subtasks.json"
            self.assertEqual(generator.load_bad_subtask_rules(missing_path), [])

    def test_bad_subtask_config_rejects_malformed_json(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        path = Path(tmp_dir.name) / "bad_subtasks.json"
        path.write_text("{", encoding="utf-8")

        with self.assertRaises(ValueError):
            generator.load_bad_subtask_rules(path, missing_ok=False)

    def test_bad_subtask_config_requires_bad_subtasks_list(self):
        config_path = self._write_bad_subtasks_config({"version": 1})

        with self.assertRaises(ValueError):
            generator.load_bad_subtask_rules(config_path, missing_ok=False)

    def test_no_valid_positions_filter_only_excludes_matching_triple(self):
        objects_path = self._write_properties(
            [
                {"scene": "FloorPlan1", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Bowl", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Pan", "receptacle": True},
                {"scene": "FloorPlan2", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan2", "objectType": "Bowl", "receptacle": True},
            ]
        )
        config_path = self._write_no_valid_positions_config(
            [
                {"floorplan": "FloorPlan1", "object": "Apple", "receptacle": "Bowl"},
                {"floorplan": "FloorPlan1", "object": "Apple", "receptacle": "Bowl"},
            ]
        )
        rules = generator.load_no_valid_position_rules(config_path, missing_ok=False)
        floor1_bad = generator.EnumeratedSubtask(
            floor_plan=1,
            subtask={"skill": "PutOn", "objects": ["Apple", "Bowl"]},
        )
        floor1_other_receptacle = generator.EnumeratedSubtask(
            floor_plan=1,
            subtask={"skill": "PutOn", "objects": ["Apple", "Pan"]},
        )
        floor2_same_pair = generator.EnumeratedSubtask(
            floor_plan=2,
            subtask={"skill": "PutOn", "objects": ["Apple", "Bowl"]},
        )

        filtered, excluded = generator.filter_no_valid_position_subtasks(
            [floor1_bad, floor1_other_receptacle, floor2_same_pair],
            rules,
            objects_path,
        )

        self.assertEqual(rules, {(1, "Apple", "Bowl")})
        self.assertEqual(excluded, 1)
        self.assertEqual(filtered, [floor1_other_receptacle, floor2_same_pair])

    def test_no_valid_positions_filter_checks_all_putobject_action_templates(self):
        objects_path = self._fixture_properties()
        rules = {(1, "Apple", "Microwave")}
        microwave_subtask = generator.EnumeratedSubtask(
            floor_plan=1,
            subtask={"skill": "RunMicrowave", "objects": ["Apple", "Microwave"]},
        )
        open_subtask = generator.EnumeratedSubtask(
            floor_plan=1,
            subtask={"skill": "Open", "objects": ["Cabinet"]},
        )

        filtered, excluded = generator.filter_no_valid_position_subtasks(
            [microwave_subtask, open_subtask],
            rules,
            objects_path,
        )

        self.assertEqual(excluded, 1)
        self.assertEqual(filtered, [open_subtask])

    def test_no_valid_positions_filter_applies_before_limit(self):
        bad_subtask = {"skill": "PutOn", "objects": ["Apple", "Bowl"]}
        good_subtask = {"skill": "Open", "objects": ["Cabinet"]}
        no_valid_positions_path = self._write_no_valid_positions_config(
            [{"floorplan": "FloorPlan1", "object": "Apple", "receptacle": "Bowl"}]
        )
        bad_subtasks_path = self._write_bad_subtasks_config(
            {"version": 1, "bad_subtasks": []}
        )
        captured_enumerated = []
        summary_holder = {}

        original_enumerate = generator.enumerate_single_subtasks
        original_prepare = generator.prepare_generated_subtasks
        original_write_flat = generator.write_flat_outputs

        def fake_enumerate(_floor_plans):
            return [
                generator.EnumeratedSubtask(floor_plan=1, subtask=bad_subtask),
                generator.EnumeratedSubtask(floor_plan=1, subtask=good_subtask),
            ]

        def fake_prepare(enumerated, _object_properties_path=generator.DEFAULT_OBJECT_PROPERTIES_PATH):
            captured_enumerated.extend(enumerated)
            return []

        def fake_write_flat(_generated, output_dir, **_kwargs):
            summary = {
                "output_layout": "flat",
                "floor_count": 0,
                "total_subtasks": len(captured_enumerated),
                "successful_generations": len(captured_enumerated),
                "failed_generations": 0,
                "skipped_existing": 0,
                "output_dir": str(output_dir),
                "errors": [],
            }
            summary_holder["summary"] = summary
            return summary

        generator.enumerate_single_subtasks = fake_enumerate
        generator.prepare_generated_subtasks = fake_prepare
        generator.write_flat_outputs = fake_write_flat
        self.addCleanup(lambda: setattr(generator, "enumerate_single_subtasks", original_enumerate))
        self.addCleanup(lambda: setattr(generator, "prepare_generated_subtasks", original_prepare))
        self.addCleanup(lambda: setattr(generator, "write_flat_outputs", original_write_flat))

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = generator.main(
                [
                    "--floor-plans",
                    "1",
                    "--limit",
                    "1",
                    "--bad-subtasks-config",
                    str(bad_subtasks_path),
                    "--no-valid-positions-config",
                    str(no_valid_positions_path),
                    "--output-dir",
                    str(Path(tmp_dir) / "generated"),
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            captured_enumerated,
            [generator.EnumeratedSubtask(floor_plan=1, subtask=good_subtask)],
        )
        self.assertEqual(
            summary_holder["summary"]["excluded_no_valid_position_subtasks"],
            1,
        )
        self.assertEqual(
            summary_holder["summary"]["no_valid_positions_config"],
            str(no_valid_positions_path),
        )

    def test_missing_no_valid_positions_config_is_empty_when_allowed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing_no_valid_positions.json"
            self.assertEqual(generator.load_no_valid_position_rules(missing_path), set())

    def test_no_valid_positions_config_rejects_malformed_json(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        path = Path(tmp_dir.name) / "no_valid_positions.json"
        path.write_text("{", encoding="utf-8")

        with self.assertRaises(ValueError):
            generator.load_no_valid_position_rules(path, missing_ok=False)

    def test_enumeration_uses_object_rules_without_robot_feasibility(self):
        original = data_engine.DataEngine._robot_can_complete_subtask

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("robot feasibility should not be checked")

        data_engine.DataEngine._robot_can_complete_subtask = fail_if_called
        self.addCleanup(lambda: setattr(data_engine.DataEngine, "_robot_can_complete_subtask", original))

        objects_path = self._fixture_properties()
        subtasks = generator.enumerate_single_subtasks_for_floor(
            1,
            [
                "Apple",
                "Bowl",
                "Bread",
                "Cabinet",
                "Egg",
                "Knife",
                "Microwave",
                "Pan",
                "Plate",
                "Sink",
                "StoveBurner",
                "Toaster",
            ],
            objects_path,
        )
        subtask_keys = {
            json.dumps(subtask, sort_keys=True)
            for subtask in subtasks
        }

        self.assertIn(
            json.dumps({"skill": "Open", "objects": ["Cabinet"]}, sort_keys=True),
            subtask_keys,
        )
        self.assertIn(
            json.dumps({"skill": "PutIn", "objects": ["Apple", "Bowl"]}, sort_keys=True),
            subtask_keys,
        )
        self.assertIn(
            json.dumps({"skill": "Wash", "objects": ["Plate"]}, sort_keys=True),
            subtask_keys,
        )

    def test_enumeration_excludes_non_receptacle_put_targets(self):
        objects_path = self._write_properties(
            [
                {"scene": "FloorPlan1", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": False},
                {"scene": "FloorPlan1", "objectType": "SinkBasin", "receptacle": True},
            ]
        )
        subtasks = generator.enumerate_single_subtasks_for_floor(
            1,
            ["Apple", "Sink", "SinkBasin"],
            objects_path,
        )
        subtask_keys = {
            json.dumps(subtask, sort_keys=True)
            for subtask in subtasks
        }

        self.assertNotIn(
            json.dumps({"skill": "PutOn", "objects": ["Apple", "Sink"]}, sort_keys=True),
            subtask_keys,
        )
        self.assertIn(
            json.dumps({"skill": "PutOn", "objects": ["Apple", "SinkBasin"]}, sort_keys=True),
            subtask_keys,
        )

    def test_action_templates_use_executor_argument_order(self):
        objects_path = self._fixture_properties()
        skill_sets = data_engine._build_object_skill_sets(1, objects_path)

        microwave_actions = generator.build_actions_for_subtask(
            {"skill": "RunMicrowave", "objects": ["Apple", "Microwave"]},
            skill_sets,
        )
        self.assertEqual(microwave_actions[-1]["action_type"], "RunMicrowave")
        self.assertEqual(
            microwave_actions[-1]["parameters"]["args"],
            ["Microwave", "Apple"],
        )

        prepare_egg_actions = generator.build_actions_for_subtask(
            {"skill": "PrepareEgg", "objects": ["Egg", "Pan"]},
            skill_sets,
        )
        self.assertEqual(
            self._action_pairs(prepare_egg_actions),
            [
                ("GoToObject", ["Egg"]),
                ("PickupObject", ["Egg"]),
                ("GoToObject", ["Pan"]),
                ("PutObject", ["Egg", "Pan"]),
                ("PrepareEgg", ["Egg"]),
            ],
        )
        self.assertNotIn(("PickupObject", ["Pan"]), self._action_pairs(prepare_egg_actions))

        cook_egg_actions = generator.build_actions_for_subtask(
            {"skill": "CookEgg", "objects": ["Egg", "Pan"]},
            skill_sets,
        )
        cook_egg_pairs = self._action_pairs(cook_egg_actions)
        self.assertEqual(
            cook_egg_pairs,
            [
                ("GoToObject", ["Egg"]),
                ("PickupObject", ["Egg"]),
                ("GoToObject", ["Pan"]),
                ("PutObject", ["Egg", "Pan"]),
                ("PrepareEgg", ["Egg"]),
                ("PickupObject", ["Pan"]),
                ("GoToObject", ["StoveBurner"]),
                ("CookByStoveBurner", ["StoveBurner", "Pan", "Egg"]),
            ],
        )
        self.assertGreater(
            cook_egg_pairs.index(("PickupObject", ["Pan"])),
            cook_egg_pairs.index(("PrepareEgg", ["Egg"])),
        )
        self.assertEqual(cook_egg_actions[-1]["action_type"], "CookByStoveBurner")
        self.assertEqual(
            cook_egg_actions[-1]["parameters"]["args"],
            ["StoveBurner", "Pan", "Egg"],
        )

    def test_prepare_generated_subtasks_constructs_task_text_without_data_engine_subtask_to_str(self):
        objects_path = self._fixture_properties()
        original = data_engine.DataEngine.subtask_to_str

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("subtask_to_str should not be used")

        data_engine.DataEngine.subtask_to_str = fail_if_called
        self.addCleanup(lambda: setattr(data_engine.DataEngine, "subtask_to_str", original))

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "CookEgg", "objects": ["Egg", "Pan"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(generated[0].task_text, "cook the egg in the pan")

    def test_pickup_target_inside_openable_container_opens_parent_first(self):
        objects_path = self._write_properties(
            [
                {
                    "scene": "FloorPlan1",
                    "objectType": "Plate",
                    "objectId": "Plate|+00.10|+00.20|+00.30",
                    "pickupable": True,
                    "dirtyable": True,
                    "parentReceptacles": ["Cabinet|+01.00|+00.00|+00.00"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Cabinet",
                    "objectId": "Cabinet|+01.00|+00.00|+00.00",
                    "openable": True,
                    "receptacle": True,
                },
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": True},
            ]
        )

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "Wash", "objects": ["Plate"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(
            self._action_pairs(generated[0].actions),
            [
                ("GoToObject", ["Cabinet|+01.00|+00.00|+00.00"]),
                ("OpenObject", ["Cabinet|+01.00|+00.00|+00.00"]),
                ("GoToObject", ["Plate"]),
                ("PickupObject", ["Plate"]),
                ("GoToObject", ["Sink"]),
                ("CleanObject", ["Plate"]),
            ],
        )
        self.assertEqual(
            self._action_pairs(generated[0].pre_task_actions),
            [("DirtyObject", ["Plate"])],
        )

    def test_break_target_inside_openable_container_opens_parent_first(self):
        objects_path = self._write_properties(
            [
                {
                    "scene": "FloorPlan1",
                    "objectType": "Vase",
                    "objectId": "Vase|+00.10|+00.20|+00.30",
                    "breakable": True,
                    "parentReceptacles": ["Box|-01.00|+00.00|+00.00"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Box",
                    "objectId": "Box|-01.00|+00.00|+00.00",
                    "openable": True,
                    "receptacle": True,
                },
            ]
        )

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "Break", "objects": ["Vase"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(
            self._action_pairs(generated[0].actions),
            [
                ("GoToObject", ["Box|-01.00|+00.00|+00.00"]),
                ("OpenObject", ["Box|-01.00|+00.00|+00.00"]),
                ("GoToObject", ["Vase"]),
                ("BreakObject", ["Vase"]),
            ],
        )

    def test_slice_target_inside_openable_container_opens_parent_first(self):
        objects_path = self._write_properties(
            [
                {
                    "scene": "FloorPlan1",
                    "objectType": "Egg",
                    "objectId": "Egg|+00.10|+00.20|+00.30",
                    "pickupable": True,
                    "sliceable": True,
                    "parentReceptacles": ["Fridge|-02.00|+00.00|+01.00"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Fridge",
                    "objectId": "Fridge|-02.00|+00.00|+01.00",
                    "openable": True,
                    "receptacle": True,
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Knife",
                    "objectId": "Knife|-01.70|+00.79|-00.22",
                    "pickupable": True,
                    "parentReceptacles": ["Drawer|-01.56|+00.84|-00.20"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Drawer",
                    "objectId": "Drawer|-01.56|+00.84|-00.20",
                    "openable": True,
                    "receptacle": True,
                },
            ]
        )

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "Slice", "objects": ["Egg"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(
            self._action_pairs(generated[0].actions),
            [
                ("GoToObject", ["Drawer|-01.56|+00.84|-00.20"]),
                ("OpenObject", ["Drawer|-01.56|+00.84|-00.20"]),
                ("GoToObject", ["Knife"]),
                ("PickupObject", ["Knife"]),
                ("GoToObject", ["Fridge|-02.00|+00.00|+01.00"]),
                ("OpenObject", ["Fridge|-02.00|+00.00|+01.00"]),
                ("GoToObject", ["Egg"]),
                ("SliceObject", ["Egg"]),
            ],
        )

    def test_reuses_already_open_parent_container_for_later_put(self):
        objects_path = self._write_properties(
            [
                {
                    "scene": "FloorPlan1",
                    "objectType": "Egg",
                    "objectId": "Egg|+00.10|+00.20|+00.30",
                    "pickupable": True,
                    "parentReceptacles": ["Fridge|-02.00|+00.00|+01.00"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "Fridge",
                    "objectId": "Fridge|-02.00|+00.00|+01.00",
                    "openable": True,
                    "receptacle": True,
                },
            ]
        )

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "ColdObject", "objects": ["Egg", "Fridge"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(
            self._action_pairs(generated[0].actions),
            [
                ("GoToObject", ["Fridge|-02.00|+00.00|+01.00"]),
                ("OpenObject", ["Fridge|-02.00|+00.00|+01.00"]),
                ("GoToObject", ["Egg"]),
                ("PickupObject", ["Egg"]),
                ("GoToObject", ["Fridge"]),
                ("PutObject", ["Egg", "Fridge"]),
                ("CloseObject", ["Fridge"]),
                ("ColdObject", ["Fridge", "Egg"]),
            ],
        )

    def test_pickup_target_on_non_openable_parent_does_not_open_parent(self):
        objects_path = self._write_properties(
            [
                {
                    "scene": "FloorPlan1",
                    "objectType": "Plate",
                    "objectId": "Plate|+00.10|+00.20|+00.30",
                    "pickupable": True,
                    "dirtyable": True,
                    "parentReceptacles": ["CounterTop|+01.00|+00.00|+00.00"],
                },
                {
                    "scene": "FloorPlan1",
                    "objectType": "CounterTop",
                    "objectId": "CounterTop|+01.00|+00.00|+00.00",
                    "receptacle": True,
                },
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": True},
            ]
        )

        generated = generator.prepare_generated_subtasks(
            [
                generator.EnumeratedSubtask(
                    floor_plan=1,
                    subtask={"skill": "Wash", "objects": ["Plate"]},
                )
            ],
            objects_path,
        )

        self.assertEqual(
            self._action_pairs(generated[0].actions),
            [
                ("GoToObject", ["Plate"]),
                ("PickupObject", ["Plate"]),
                ("GoToObject", ["Sink"]),
                ("CleanObject", ["Plate"]),
            ],
        )
        self.assertEqual(
            self._action_pairs(generated[0].pre_task_actions),
            [("DirtyObject", ["Plate"])],
        )

    def test_wash_bundle_adds_dirty_pre_task_without_counting_no_trans(self):
        subtask = {"skill": "Wash", "objects": ["Plate"]}
        actions = generator.build_actions_for_subtask(subtask, {})
        pre_task_actions = generator.build_pre_task_actions_for_subtask(subtask)

        bundle = generator.build_bundle_data(
            task_id="task",
            task_text="wash the plate",
            actions=actions,
            pre_task_actions=pre_task_actions,
        )

        self.assertEqual(bundle["no_trans"], len(actions))
        self.assertEqual(
            self._action_pairs(
                bundle["task_plan"]["pre_task_action_queues"]["robot1"]
            ),
            [("DirtyObject", ["Plate"])],
        )
        self.assertEqual(
            self._action_pairs(
                bundle["task_plan"]["stages"][0]["robot_action_queues"]["robot1"]
            ),
            self._action_pairs(actions),
        )

    def test_default_limit_generation_writes_only_flat_executables(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "generated"

            result = generator.main(
                [
                    "--floor-plans",
                    "1",
                    "--limit",
                    "3",
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result, 0)
            generated_names = sorted(path.name for path in output_dir.iterdir())
            self.assertEqual(
                generated_names,
                [
                    "1_00001_executable_plan.py",
                    "1_00002_executable_plan.py",
                    "1_00003_executable_plan.py",
                ],
            )
            self.assertFalse((output_dir / "dataset").exists())
            self.assertFalse((output_dir / "manifest.jsonl").exists())
            self.assertFalse((output_dir / "summary.json").exists())
            self.assertFalse((output_dir / "FloorPlan1").exists())
            self.assertFalse((output_dir / "__pycache__").exists())

            for filename in generated_names:
                executable = output_dir / filename
                executable_text = executable.read_text(encoding="utf-8")
                self.assertIn("FORCED_ROBOTS =", executable_text)
                self.assertIn("'name': 'robot1'", executable_text)
                self.assertIn("'mass_capacity': 100", executable_text)
                self.assertIn("EMBEDDED_TASK_RECORD = {", executable_text)
                self.assertIn("EMBEDDED_FLOOR_PLAN = '1'", executable_text)
                self.assertIn("TASK_FILE = None", executable_text)
                self.assertNotIn("build_robot_team(task_record", executable_text)
                self.assertNotIn("robot list", executable_text)
                generator.compile_python(executable)

            self.assertFalse((output_dir / "__pycache__").exists())

    def test_full_layout_writes_clean_records_and_forced_robot_script(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "generated"

            result = generator.main(
                [
                    "--floor-plans",
                    "1",
                    "--limit",
                    "3",
                    "--output-dir",
                    str(output_dir),
                    "--output-layout",
                    "full",
                ]
            )

            self.assertEqual(result, 0)
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["output_layout"], "full")
            self.assertEqual(summary["total_subtasks"], 3)
            self.assertEqual(summary["successful_generations"], 3)

            dataset_records = [
                json.loads(line)
                for line in (output_dir / "dataset" / "FloorPlan1.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(dataset_records), 3)
            for record in dataset_records:
                self.assertNotIn("robots", record)
                self.assertNotIn("robot list", record)

            manifest_records = [
                json.loads(line)
                for line in (output_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(manifest_records), 3)
            for record in manifest_records:
                self.assertNotIn("robots", record)
                self.assertNotIn("robot list", record)

            task_dirs = sorted((output_dir / "FloorPlan1").iterdir())
            self.assertEqual(len(task_dirs), 3)
            for task_dir in task_dirs:
                task_record = json.loads((task_dir / "task_record.json").read_text(encoding="utf-8"))
                self.assertNotIn("robots", task_record)
                self.assertNotIn("robot list", task_record)

                bundle = json.loads((task_dir / "plan_bundle.json").read_text(encoding="utf-8"))
                queues = bundle["task_plan"]["stages"][0]["robot_action_queues"]
                self.assertEqual(list(queues), ["robot1"])
                self.assertTrue(all(action["robot_id"] == "robot1" for action in queues["robot1"]))

                executable = task_dir / "executable_plan.py"
                executable_text = executable.read_text(encoding="utf-8")
                self.assertIn("FORCED_ROBOTS =", executable_text)
                self.assertIn("'name': 'robot1'", executable_text)
                self.assertIn("'mass_capacity': 100", executable_text)
                self.assertIn("EMBEDDED_TASK_RECORD = None", executable_text)
                self.assertNotIn("build_robot_team(task_record", executable_text)
                generator.compile_python(executable)

    def test_manifest_only_requires_full_layout(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                generator.parse_arguments(["--manifest-only"])
        self.assertEqual(error.exception.code, 2)

        args = generator.parse_arguments(["--manifest-only", "--output-layout", "full"])
        self.assertTrue(args.manifest_only)
        self.assertEqual(args.output_layout, "full")


if __name__ == "__main__":
    unittest.main()
