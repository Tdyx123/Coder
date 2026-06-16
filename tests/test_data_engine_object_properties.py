import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import data_engine
from special_task_skills import SPECIAL_TASK_SKILLS


class DataEngineObjectPropertiesTests(unittest.TestCase):
    def _write_objects(self, objects):
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "objects.json"
        path.write_text(json.dumps(objects), encoding="utf-8")
        self.addCleanup(tmp_dir.cleanup)
        return path

    def _skill_fixture_path(self):
        return self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Egg", "breakable": True, "sliceable": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Knife", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Bread", "pickupable": True, "sliceable": True},
                {"scene": "FloorPlan1", "objectType": "Book", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Candle", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Cabinet", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "CoffeeMachine", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Drawer", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Box", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Bowl", "receptacle": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Cup", "receptacle": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Fridge", "openable": True, "receptacle": True, "toggleable": True},
                {"scene": "FloorPlan1", "objectType": "Microwave", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Mirror", "dirtyable": True},
                {"scene": "FloorPlan1", "objectType": "Mug", "pickupable": True, "canFillWithLiquid": True},
                {"scene": "FloorPlan1", "objectType": "Pan", "receptacle": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Pot", "receptacle": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Potato", "pickupable": True, "cookable": True},
                {"scene": "FloorPlan1", "objectType": "CounterTop", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Floor", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Chair", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Plate", "receptacle": True, "dirtyable": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "StoveBurner", "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Toaster"},
                {"scene": "FloorPlan2", "objectType": "Egg", "breakable": False, "sliceable": False, "pickupable": True},
                {"scene": "FloorPlan2", "objectType": "Cabinet", "openable": False, "receptacle": True},
            ]
        )

    def test_all_generated_skills_are_declared_in_skill_configs(self):
        expected_generated_skills = {
            "Open",
            "SwitchOn",
            "Wash",
            "Break",
            "Slice",
            "PutOn",
            "PutIn",
            "RunMicrowave",
            "RunCoffeeMachine",
            "RunToaster",
            "CookByStoveBurner",
            "PrepareEgg",
            "CookEgg",
            "HeatByStoveBurner",
            "FillWater",
            "ColdObject",
        }

        self.assertLessEqual(expected_generated_skills, set(data_engine.SKILL_CONFIGS))
        for skill in expected_generated_skills:
            with self.subTest(skill=skill):
                self.assertGreater(data_engine.SKILL_CONFIGS[skill].generation_probability, 0)

        self.assertEqual(data_engine.SKILL_CONFIGS["Close"].generation_probability, 0)
        self.assertEqual(data_engine.SKILL_CONFIGS["SwitchOff"].generation_probability, 0)

    def test_skill_text_final_state_and_robot_requirements_use_config(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        subtask = {"skill": "RunMicrowave", "objects": ["Potato", "Microwave"]}
        config = data_engine.SKILL_CONFIGS["RunMicrowave"]

        self.assertEqual(engine.subtask_to_str(subtask), config.text_builder(subtask["objects"]))
        self.assertEqual(
            engine.get_subtask_final_state(subtask),
            config.final_state_builder(subtask["objects"]),
        )
        self.assertEqual(
            data_engine._required_robot_skills_for_subtask(subtask),
            list(config.robot_skills),
        )

    def test_temporary_skill_config_can_drive_lookup_and_generation(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())
        skill_name = "Polish"
        original_config = data_engine.SKILL_CONFIGS.get(skill_name)

        def restore_config():
            if original_config is None:
                data_engine.SKILL_CONFIGS.pop(skill_name, None)
            else:
                data_engine.SKILL_CONFIGS[skill_name] = original_config

        self.addCleanup(restore_config)
        data_engine.SKILL_CONFIGS[skill_name] = data_engine.SkillConfig(
            name=skill_name,
            arity=1,
            primary_set="pickupable_objects",
            target_set=None,
            roles=("obj1",),
            relation="single",
            needed_set_by_role={},
            robot_skills=("GoToObject", "PickupObject"),
            required_pickup=(0,),
            text_builder=lambda objs: f"polish the {objs[0].lower()}",
            final_state_builder=lambda objs: [
                {"name": objs[0], "contains": [], "states": ["POLISHED"]}
            ],
            generation_probability=1.0,
        )

        self.assertIn(
            {"skill": skill_name, "type": "single"},
            engine.get_applicable_skills("Apple", ["Apple"], skill_sets),
        )
        self.assertEqual(
            engine.subtask_to_str({"skill": skill_name, "objects": ["Apple"]}),
            "polish the apple",
        )
        self.assertEqual(
            engine.get_subtask_final_state({"skill": skill_name, "objects": ["Apple"]}),
            [{"name": "Apple", "contains": [], "states": ["POLISHED"]}],
        )
        self.assertEqual(
            engine.generate_task(["Apple"], 1, skill_sets, seed=1),
            [{"skill": skill_name, "objects": ["Apple"]}],
        )

    def test_normalize_scene_name_accepts_common_floor_plan_forms(self):
        self.assertEqual(data_engine._normalize_scene_name(1), "FloorPlan1")
        self.assertEqual(data_engine._normalize_scene_name("1"), "FloorPlan1")
        self.assertEqual(data_engine._normalize_scene_name("FloorPlan1"), "FloorPlan1")

        with self.assertRaises(ValueError):
            data_engine._normalize_scene_name("FloorPlanA")

    def test_load_object_type_properties_filters_scene_and_uses_any_true_aggregation(self):
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Cup", "breakable": False, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Cup", "breakable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Box", "openable": True, "receptacle": True},
                {"scene": "FloorPlan2", "objectType": "Cup", "openable": True},
                {"scene": "FloorPlan2", "objectType": "Plate", "dirtyable": True},
            ]
        )

        properties = data_engine._load_ai2thor_object_type_properties("FloorPlan1", path)

        self.assertTrue(properties["Cup"]["breakable"])
        self.assertTrue(properties["Cup"]["receptacle"])
        self.assertFalse(properties["Cup"]["openable"])
        self.assertNotIn("Plate", properties)

    def test_load_object_type_properties_requires_matching_scene(self):
        path = self._write_objects(
            [{"scene": "FloorPlan1", "objectType": "Cup", "breakable": True}]
        )

        with self.assertRaisesRegex(ValueError, "FloorPlan999"):
            data_engine._load_ai2thor_object_type_properties(999, path)

    def test_build_object_skill_sets_uses_floor_plan_specific_properties(self):
        path = self._skill_fixture_path()

        floor_plan_1_sets = data_engine._build_object_skill_sets(1, path)
        floor_plan_2_sets = data_engine._build_object_skill_sets("FloorPlan2", path)

        self.assertIn("Egg", floor_plan_1_sets["breakable_objects"])
        self.assertIn("Egg", floor_plan_1_sets["sliceable_objects"])
        self.assertNotIn("Egg", floor_plan_2_sets["breakable_objects"])
        self.assertIn("Cabinet", floor_plan_1_sets["openable_containers"])
        self.assertNotIn("Cabinet", floor_plan_1_sets["has_placing_surface_objects"])
        self.assertIn("Cabinet", floor_plan_2_sets["has_placing_surface_objects"])
        self.assertNotIn("put_on_receptacles", floor_plan_1_sets)
        self.assertIn("Cabinet", floor_plan_1_sets["put_in_receptacles"])
        self.assertIn("Bowl", floor_plan_1_sets["put_in_receptacles"])
        self.assertIn("Cabinet", floor_plan_1_sets["must_open_to_place_receptacles"])
        self.assertNotIn("Bowl", floor_plan_1_sets["must_open_to_place_receptacles"])
        self.assertIn("Bowl", floor_plan_1_sets["placement_restrictions"]["Apple"])

    def test_puton_target_must_be_receptacle(self):
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Apple", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Sink", "receptacle": False},
                {"scene": "FloorPlan1", "objectType": "SinkBasin", "receptacle": True},
            ]
        )
        skill_sets = data_engine._build_object_skill_sets(1, path)

        self.assertFalse(
            data_engine._can_place_with_skill("Apple", "Sink", "PutOn", skill_sets)
        )
        self.assertTrue(
            data_engine._can_place_with_skill("Apple", "SinkBasin", "PutOn", skill_sets)
        )

    def test_washable_objects_require_pickupable(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        self.assertIn("Plate", skill_sets["washable_objects"])
        self.assertNotIn("Mirror", skill_sets["washable_objects"])

        plate_skills = engine.get_applicable_skills("Plate", ["Plate", "Sink"], skill_sets)
        mirror_skills = engine.get_applicable_skills("Mirror", ["Mirror", "Sink"], skill_sets)

        self.assertIn({"skill": "Wash", "type": "single"}, plate_skills)
        self.assertNotIn("Wash", {skill["skill"] for skill in mirror_skills})

    def test_get_applicable_skills_uses_explicit_skill_sets(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        skills = engine.get_applicable_skills("Egg", ["Egg", "Knife", "Sink"], skill_sets)
        skill_names = {skill["skill"] for skill in skills}

        self.assertIn("Break", skill_names)
        self.assertIn("Slice", skill_names)

    def test_placement_restrictions_can_be_puton_targets_in_both_roles(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        apple_skills = engine.get_applicable_skills("Apple", ["Apple", "CounterTop"], skill_sets)
        countertop_skills = engine.get_applicable_skills("CounterTop", ["CounterTop", "Apple"], skill_sets)

        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj1",
            },
            apple_skills,
        )
        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
            },
            countertop_skills,
        )

    def test_in_receptacles_can_be_putin_targets_and_puton_when_restrictions_allow(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        book_skills = engine.get_applicable_skills("Book", ["Book", "Cabinet"], skill_sets)
        cabinet_skills = engine.get_applicable_skills("Cabinet", ["Cabinet", "Book"], skill_sets)

        self.assertIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj1",
                "needed_set": "put_in_receptacles",
            },
            book_skills,
        )
        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj1",
            },
            book_skills,
        )
        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
            },
            cabinet_skills,
        )
        self.assertIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj2",
                "needed_set": "pickupable_objects",
            },
            cabinet_skills,
        )

    def test_bottom_four_in_receptacles_do_not_require_open_or_closed_final_state(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        skills = engine.get_applicable_skills("Bowl", ["Bowl", "Apple"], skill_sets)

        self.assertIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj2",
                "needed_set": "pickupable_objects",
            },
            skills,
        )
        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
            },
            skills,
        )
        self.assertEqual(
            engine.get_subtask_final_state({"skill": "PutIn", "objects": ["Apple", "Bowl"]}),
            [{"name": "Bowl", "contains": ["Apple"], "states": []}],
        )
        self.assertEqual(
            data_engine._required_robot_skills_for_subtask(
                {"skill": "PutIn", "objects": ["Apple", "Bowl"]}
            ),
            ["GoToObject", "PickupObject", "PutObject"],
        )

    def test_must_open_in_receptacles_do_not_require_closed_final_state(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_subtask_final_state({"skill": "PutIn", "objects": ["Book", "Cabinet"]}),
            [{"name": "Cabinet", "contains": ["Book"], "states": []}],
        )
        self.assertEqual(
            data_engine._required_robot_skills_for_subtask(
                {"skill": "PutIn", "objects": ["Book", "Cabinet"]}
            ),
            ["GoToObject", "OpenObject", "CloseObject", "PickupObject", "PutObject"],
        )

    def test_special_task_skills_always_require_same_named_robot_skill(self):
        original_robots = data_engine.robots
        data_engine.robots = []
        self.addCleanup(lambda: setattr(data_engine, "robots", original_robots))

        for skill in SPECIAL_TASK_SKILLS:
            with self.subTest(skill=skill):
                required = data_engine._required_robot_skills_for_subtask(
                    {"skill": skill, "objects": ["Apple", "Microwave"]}
                )
                self.assertIn(skill, required)

    def test_robot_can_complete_special_task_only_with_same_named_skill(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        subtask = {"skill": "RunMicrowave", "objects": ["Apple", "Microwave"]}
        obj_mass_map = {"Apple": 0.1}
        setup_skills = list(data_engine.ACTION_SKILL_CORE_REQUIREMENTS["RunMicrowave"])

        base_only_robot = {
            "skills": setup_skills,
            "mass_capacity": 1,
        }
        specialist_robot = {
            "skills": setup_skills + ["RunMicrowave"],
            "mass_capacity": 1,
        }

        self.assertFalse(engine._robot_can_complete_subtask(base_only_robot, subtask, obj_mass_map))
        self.assertTrue(engine._robot_can_complete_subtask(specialist_robot, subtask, obj_mass_map))

    def test_special_task_requirements_include_allaction_setup_skills(self):
        expected = {
            "RunMicrowave": [
                "GoToObject",
                "PickupObject",
                "PutObject",
                "OpenObject",
                "CloseObject",
                "RunMicrowave",
            ],
            "RunCoffeeMachine": [
                "GoToObject",
                "PickupObject",
                "PutObject",
                "RunCoffeeMachine",
            ],
            "RunToaster": [
                "GoToObject",
                "PickupObject",
                "SliceObject",
                "RunToaster",
            ],
            "ColdObject": [
                "GoToObject",
                "PickupObject",
                "PutObject",
                "OpenObject",
                "CloseObject",
                "SwitchOn",
                "ColdObject",
            ],
            "PrepareEgg": [
                "GoToObject",
                "PickupObject",
                "PutObject",
                "PrepareEgg",
            ],
        }

        for skill, required_skills in expected.items():
            with self.subTest(skill=skill):
                self.assertEqual(
                    data_engine._required_robot_skills_for_subtask(
                        {"skill": skill, "objects": ["Apple", "Microwave"]}
                    ),
                    required_skills,
                )

    def test_placement_restrictions_filter_invalid_pairs(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        apple_skills = engine.get_applicable_skills("Apple", ["Apple", "Cabinet"], skill_sets)
        cabinet_skills = engine.get_applicable_skills("Cabinet", ["Cabinet", "Apple"], skill_sets)
        chair_skills = engine.get_applicable_skills("Chair", ["Chair", "Book"], skill_sets)
        invalid_chair_skills = engine.get_applicable_skills("Chair", ["Chair", "Apple"], skill_sets)

        self.assertNotIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj1",
                "needed_set": "put_in_receptacles",
            },
            apple_skills,
        )
        self.assertNotIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj2",
                "needed_set": "pickupable_objects",
            },
            cabinet_skills,
        )
        self.assertIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
            },
            chair_skills,
        )
        self.assertNotIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
            },
            invalid_chair_skills,
        )

    def test_sample_second_object_uses_pair_filters(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        self.assertEqual(
            engine.sample_second_object(
                ["Apple", "Cabinet", "Bowl"],
                "put_in_receptacles",
                skill_sets,
                exclude="Apple",
                skill="PutIn",
                role="obj1",
            ),
            "Bowl",
        )
        self.assertEqual(
            engine.sample_second_object(
                ["Book", "Chair"],
                skill_sets=skill_sets,
                exclude="Book",
                skill="PutOn",
                role="obj1",
            ),
            "Chair",
        )

    def test_run_toaster_requires_knife_in_scene(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        without_knife = engine.get_applicable_skills("Bread", ["Bread", "Toaster"], skill_sets)
        with_knife = engine.get_applicable_skills("Bread", ["Bread", "Toaster", "Knife"], skill_sets)

        self.assertNotIn("RunToaster", {skill["skill"] for skill in without_knife})
        self.assertIn(
            {
                "skill": "RunToaster",
                "type": "double",
                "role": "obj1",
                "needed_set": "toaster_objects",
            },
            with_knife,
        )

    def test_run_microwave_only_uses_microwave_placeable_objects(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        apple_skills = engine.get_applicable_skills("Apple", ["Apple", "Microwave"], skill_sets)
        book_skills = engine.get_applicable_skills("Book", ["Book", "Microwave"], skill_sets)

        self.assertIn(
            {
                "skill": "RunMicrowave",
                "type": "double",
                "role": "obj1",
                "needed_set": "microwave_objects",
            },
            apple_skills,
        )
        self.assertNotIn("RunMicrowave", {skill["skill"] for skill in book_skills})

    def test_prepare_egg_requires_stove_placeable_container_that_can_contain_egg(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())
        all_objects = ["Egg", "Pan", "Pot", "StoveBurner"]

        egg_skills = engine.get_applicable_skills("Egg", all_objects, skill_sets)
        pan_skills = engine.get_applicable_skills("Pan", all_objects, skill_sets)

        self.assertEqual(skill_sets["egg_objects"], ["Egg"])
        self.assertEqual(skill_sets["prepare_egg_container_objects"], ["Pan", "Pot"])
        self.assertIn(
            {
                "skill": "PrepareEgg",
                "type": "double",
                "role": "obj1",
                "needed_set": "prepare_egg_container_objects",
            },
            egg_skills,
        )
        self.assertIn(
            {
                "skill": "PrepareEgg",
                "type": "double",
                "role": "obj2",
                "needed_set": "egg_objects",
            },
            pan_skills,
        )
        self.assertTrue(
            data_engine._can_generate_action_skill(
                "PrepareEgg",
                all_objects,
                skill_sets,
            )
        )
        self.assertEqual(
            engine.subtask_to_str({"skill": "PrepareEgg", "objects": ["Egg", "Pan"]}),
            "prepare the egg in the pan",
        )
        self.assertEqual(
            engine.get_subtask_final_state({"skill": "PrepareEgg", "objects": ["Egg", "Pan"]}),
            [{"name": "Egg", "contains": [], "states": ["BROKEN"]}],
        )
        self.assertEqual(
            data_engine._required_robot_skills_for_subtask(
                {"skill": "PrepareEgg", "objects": ["Egg", "Pan"]}
            ),
            ["GoToObject", "PickupObject", "PutObject", "PrepareEgg"],
        )

    def test_prepare_egg_rejects_stove_placeable_object_that_cannot_contain_egg(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Egg", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Kettle", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "StoveBurner", "receptacle": True},
            ]
        )
        skill_sets = data_engine._build_object_skill_sets(1, path)
        all_objects = ["Egg", "Kettle", "StoveBurner"]

        self.assertIn("Kettle", skill_sets["stove_burner_placeable_objects"])
        self.assertNotIn("Kettle", skill_sets["prepare_egg_container_objects"])
        self.assertFalse(
            data_engine._can_generate_action_skill(
                "PrepareEgg",
                all_objects,
                skill_sets,
            )
        )
        self.assertNotIn(
            "PrepareEgg",
            {skill["skill"] for skill in engine.get_applicable_skills("Egg", all_objects, skill_sets)},
        )
        self.assertNotIn(
            "PrepareEgg",
            {skill["skill"] for skill in engine.get_applicable_skills("Kettle", all_objects, skill_sets)},
        )

    def test_cook_egg_extends_prepare_egg_and_requires_stove_burner(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())
        all_objects = ["Egg", "Pan", "Pot", "StoveBurner"]

        egg_skills = engine.get_applicable_skills("Egg", all_objects, skill_sets)
        pan_skills = engine.get_applicable_skills("Pan", all_objects, skill_sets)

        self.assertIn(
            {
                "skill": "CookEgg",
                "type": "double",
                "role": "obj1",
                "needed_set": "prepare_egg_container_objects",
            },
            egg_skills,
        )
        self.assertIn(
            {
                "skill": "CookEgg",
                "type": "double",
                "role": "obj2",
                "needed_set": "egg_objects",
            },
            pan_skills,
        )
        self.assertTrue(
            data_engine._can_generate_action_skill(
                "CookEgg",
                all_objects,
                skill_sets,
            )
        )
        self.assertEqual(
            engine.subtask_to_str({"skill": "CookEgg", "objects": ["Egg", "Pan"]}),
            "cook the egg in the pan",
        )
        self.assertEqual(
            engine.get_subtask_final_state({"skill": "CookEgg", "objects": ["Egg", "Pan"]}),
            [{"name": "Egg", "contains": [], "states": ["BROKEN", "COOKED"]}],
        )
        self.assertEqual(
            data_engine._required_robot_skills_for_subtask(
                {"skill": "CookEgg", "objects": ["Egg", "Pan"]}
            ),
            ["GoToObject", "PickupObject", "PutObject", "PrepareEgg"],
        )

    def test_cook_egg_rejects_scene_without_stove_burner(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())
        all_objects = ["Egg", "Pan"]

        self.assertFalse(
            data_engine._can_generate_action_skill(
                "CookEgg",
                all_objects,
                skill_sets,
            )
        )
        self.assertFalse(
            data_engine._can_pair_with_action_skill(
                "Egg",
                "Pan",
                "CookEgg",
                skill_sets,
                all_objects,
            )
        )
        self.assertNotIn(
            "CookEgg",
            {skill["skill"] for skill in engine.get_applicable_skills("Egg", all_objects, skill_sets)},
        )

    def test_cook_by_stove_burner_requires_valid_pickupable_container_for_food(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())
        all_objects = ["Potato", "Pan", "StoveBurner"]

        potato_skills = engine.get_applicable_skills("Potato", all_objects, skill_sets)
        stove_skills = engine.get_applicable_skills("StoveBurner", all_objects, skill_sets)

        self.assertIn(
            {
                "skill": "CookByStoveBurner",
                "type": "double",
                "role": "obj1",
                "needed_set": "stove_burner_objects",
            },
            potato_skills,
        )
        self.assertIn(
            {
                "skill": "CookByStoveBurner",
                "type": "double",
                "role": "obj2",
                "needed_set": "cookable_objects",
            },
            stove_skills,
        )
        self.assertTrue(
            data_engine._can_generate_action_skill(
                "CookByStoveBurner",
                all_objects,
                skill_sets,
            )
        )

    def test_cook_by_stove_burner_rejects_stove_placeable_object_that_cannot_contain_food(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Potato", "pickupable": True, "cookable": True},
                {"scene": "FloorPlan1", "objectType": "Kettle", "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "StoveBurner", "receptacle": True},
            ]
        )
        skill_sets = data_engine._build_object_skill_sets(1, path)
        all_objects = ["Potato", "Kettle", "StoveBurner"]

        self.assertIn("Kettle", skill_sets["stove_burner_placeable_objects"])
        self.assertNotIn("Kettle", skill_sets["put_in_receptacles"])
        self.assertFalse(
            data_engine._can_generate_action_skill(
                "CookByStoveBurner",
                all_objects,
                skill_sets,
            )
        )
        self.assertNotIn(
            "CookByStoveBurner",
            {skill["skill"] for skill in engine.get_applicable_skills("Potato", all_objects, skill_sets)},
        )
        self.assertNotIn(
            "CookByStoveBurner",
            {skill["skill"] for skill in engine.get_applicable_skills("StoveBurner", all_objects, skill_sets)},
        )

    def test_cook_by_stove_burner_rejects_scene_without_stove_placeable_container(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Potato", "pickupable": True, "cookable": True},
                {"scene": "FloorPlan1", "objectType": "StoveBurner", "receptacle": True},
            ]
        )
        skill_sets = data_engine._build_object_skill_sets(1, path)
        all_objects = ["Potato", "StoveBurner"]

        self.assertEqual(skill_sets["stove_burner_placeable_objects"], [])
        self.assertFalse(
            data_engine._can_generate_action_skill(
                "CookByStoveBurner",
                all_objects,
                skill_sets,
            )
        )
        self.assertNotIn(
            "CookByStoveBurner",
            {skill["skill"] for skill in engine.get_applicable_skills("Potato", all_objects, skill_sets)},
        )

    def test_task_final_state_open_closed_states_are_mutually_exclusive(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "Open", "objects": ["Cabinet"]},
                    {"skill": "Close", "objects": ["Cabinet"]},
                    {"skill": "Open", "objects": ["Cabinet"]},
                ]
            ),
            [{"name": "Cabinet", "contains": [], "states": ["OPENED"]}],
        )

    def test_task_final_state_on_off_states_are_mutually_exclusive(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "SwitchOn", "objects": ["Lamp"]},
                    {"skill": "SwitchOff", "objects": ["Lamp"]},
                    {"skill": "SwitchOn", "objects": ["Lamp"]},
                ]
            ),
            [{"name": "Lamp", "contains": [], "states": ["ON"]}],
        )

    def test_task_final_state_temperature_and_liquid_states_are_mutually_exclusive(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "HeatByStoveBurner", "objects": ["Apple", "StoveBurner"]},
                    {"skill": "ColdObject", "objects": ["Apple", "Fridge"]},
                ]
            ),
            [{"name": "Apple", "contains": [], "states": ["COLD"]}],
        )
        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "FillWater", "objects": ["Mug", "Sink"]},
                    {"skill": "RunCoffeeMachine", "objects": ["Mug", "CoffeeMachine"]},
                ]
            ),
            [{"name": "Mug", "contains": [], "states": ["FILLEDWITHCOFFEE"]}],
        )
        self.assertEqual(
            engine.get_subtask_final_state({"skill": "FillWater", "objects": ["Mug", "Sink"]}),
            [{"name": "Mug", "contains": [], "states": ["FILLEDWITHWATER"]}],
        )

    def test_task_final_state_accumulates_nonexclusive_states_and_ignores_duplicates(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "RunToaster", "objects": ["Bread", "Toaster"]},
                    {"skill": "CookByStoveBurner", "objects": ["Bread", "StoveBurner"]},
                    {"skill": "Slice", "objects": ["Bread"]},
                ]
            ),
            [{"name": "Bread", "contains": [], "states": ["HOT", "COOKED", "SLICED"]}],
        )

    def test_task_final_state_accumulates_contains_and_keeps_empty_states(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertEqual(
            engine.get_task_final_state(
                [
                    {"skill": "PutIn", "objects": ["Apple", "Bowl"]},
                    {"skill": "PutIn", "objects": ["Apple", "Bowl"]},
                    {"skill": "PutIn", "objects": ["Book", "Bowl"]},
                ]
            ),
            [{"name": "Bowl", "contains": ["Apple", "Book"], "states": []}],
        )

    def test_check_subtasks_only_treats_must_open_putin_as_container_order_conflict(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertTrue(
            engine.check_subtasks(
                [
                    {"skill": "Open", "objects": ["Bowl"]},
                    {"skill": "PutIn", "objects": ["Apple", "Bowl"]},
                ]
            )
        )
        self.assertFalse(
            engine.check_subtasks(
                [
                    {"skill": "Open", "objects": ["Cabinet"]},
                    {"skill": "PutIn", "objects": ["Book", "Cabinet"]},
                ]
            )
        )

    def test_check_subtasks_treats_prepare_egg_as_breaking_the_egg(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

        self.assertFalse(
            engine.check_subtasks(
                [
                    {"skill": "Break", "objects": ["Egg"]},
                    {"skill": "PrepareEgg", "objects": ["Egg", "Pan"]},
                ]
            )
        )
        self.assertFalse(
            engine.check_subtasks(
                [
                    {"skill": "PrepareEgg", "objects": ["Egg", "Pan"]},
                    {"skill": "Break", "objects": ["Egg"]},
                ]
            )
        )
        self.assertFalse(
            engine.check_subtasks(
                [
                    {"skill": "PrepareEgg", "objects": ["Egg", "Pan"]},
                    {"skill": "CookEgg", "objects": ["Egg", "Pan"]},
                ]
            )
        )
        self.assertFalse(
            engine.check_subtasks(
                [
                    {"skill": "Break", "objects": ["Egg"]},
                    {"skill": "CookEgg", "objects": ["Egg", "Pan"]},
                ]
            )
        )

    def test_check_subtasks_rejects_non_pickupable_required_objects(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        self.assertFalse(
            engine.check_subtasks(
                [{"skill": "Wash", "objects": ["Mirror"]}],
                skill_sets,
            )
        )
        self.assertTrue(
            engine.check_subtasks(
                [{"skill": "Wash", "objects": ["Plate"]}],
                skill_sets,
            )
        )
        self.assertTrue(
            engine.check_subtasks(
                [{"skill": "Break", "objects": ["Mirror"]}],
                skill_sets,
            )
        )

    def test_slice_requires_pickupable_knife(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        path = self._write_objects(
            [
                {"scene": "FloorPlan1", "objectType": "Bread", "sliceable": True, "pickupable": True},
                {"scene": "FloorPlan1", "objectType": "Knife", "pickupable": False},
            ]
        )
        skill_sets = data_engine._build_object_skill_sets(1, path)

        skills = engine.get_applicable_skills("Bread", ["Bread", "Knife"], skill_sets)

        self.assertNotIn("Slice", {skill["skill"] for skill in skills})
        self.assertFalse(
            engine.check_subtasks(
                [{"skill": "Slice", "objects": ["Bread"]}],
                skill_sets,
            )
        )

    def test_unknown_objects_have_no_skills(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        self.assertEqual(
            engine.get_applicable_skills("NotAThorObject", ["NotAThorObject"], skill_sets),
            [],
        )


if __name__ == "__main__":
    unittest.main()
