import json
import py_compile
import sys
import tempfile
import unittest
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

        cook_egg_actions = generator.build_actions_for_subtask(
            {"skill": "CookEgg", "objects": ["Egg", "Pan"]},
            skill_sets,
        )
        self.assertEqual(cook_egg_actions[-1]["action_type"], "CookByStoveBurner")
        self.assertEqual(
            cook_egg_actions[-1]["parameters"]["args"],
            ["StoveBurner", "Pan", "Egg"],
        )

    def test_limit_generation_writes_clean_records_and_forced_robot_script(self):
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
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
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
                self.assertNotIn("build_robot_team(task_record", executable_text)
                py_compile.compile(str(executable), doraise=True)


if __name__ == "__main__":
    unittest.main()
