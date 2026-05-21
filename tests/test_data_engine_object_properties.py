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
                {"scene": "FloorPlan1", "objectType": "Cabinet", "openable": True, "receptacle": True},
                {"scene": "FloorPlan1", "objectType": "Plate", "receptacle": True, "dirtyable": True},
                {"scene": "FloorPlan2", "objectType": "Egg", "breakable": False, "sliceable": False, "pickupable": True},
                {"scene": "FloorPlan2", "objectType": "Cabinet", "openable": False, "receptacle": True},
            ]
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

    def test_get_applicable_skills_uses_explicit_skill_sets(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        skills = engine.get_applicable_skills("Egg", ["Egg", "Knife", "Sink"], skill_sets)
        skill_names = {skill["skill"] for skill in skills}

        self.assertIn("Break", skill_names)
        self.assertIn("Slice", skill_names)

    def test_openable_receptacles_can_be_putin_targets(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        skills = engine.get_applicable_skills("Cabinet", ["Cabinet", "Apple"], skill_sets)

        self.assertIn(
            {
                "skill": "PutIn",
                "type": "double",
                "role": "obj2",
                "needed_set": "pickupable_objects",
            },
            skills,
        )

    def test_openable_receptacles_are_not_puton_surfaces(self):
        engine = data_engine.DataEngine.__new__(data_engine.DataEngine)
        skill_sets = data_engine._build_object_skill_sets(1, self._skill_fixture_path())

        skills = engine.get_applicable_skills("Cabinet", ["Cabinet", "Apple"], skill_sets)

        self.assertNotIn(
            {
                "skill": "PutOn",
                "type": "double",
                "role": "obj2",
                "needed_set": "pickupable_objects",
            },
            skills,
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
