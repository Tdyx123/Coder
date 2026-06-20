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
import relabel_final_test_tasks as relabeler


class RelabelFinalTestTasksTests(unittest.TestCase):
    def setUp(self):
        self.skill_sets = {
            "breakable_objects": ["Vase"],
            "pickupable_objects": ["Apple", "Mug", "Plate"],
            "washable_objects": ["Plate"],
            "openable_objects": ["Drawer"],
            "switchable_objects": [],
            "receptacle_objects": ["CounterTop", "Drawer", "Sink"],
            "put_in_receptacles": ["Drawer"],
            "placement_restrictions": {
                "Apple": ["CounterTop"],
                "Mug": ["CounterTop"],
                "Plate": ["CounterTop"],
            },
            "fillable_objects": ["Mug"],
            "sink_objects": ["Sink"],
        }
        self.mass_map = {
            "Apple": 0.2,
            "CounterTop": 0.0,
            "Drawer": 0.0,
            "Mug": 0.3,
            "Plate": 0.5,
            "Sink": 0.0,
            "Vase": 0.4,
        }
        self.floor_objects = [
            {"objectType": "Mug", "isFilledWithWater": True},
            {"objectType": "Plate"},
        ]

    def _context(self, *, bad_rules=None, no_valid_position_rules=None):
        return relabeler.RelabelContext(
            skill_set_loader=lambda _floor_plan: self.skill_sets,
            floor_objects_loader=lambda _floor_plan: self.floor_objects,
            object_mass_loader=lambda _floor_plan: self.mass_map,
            bad_subtask_rules=bad_rules or [],
            no_valid_position_rules=no_valid_position_rules or set(),
        )

    def _task_record(self, subtasks, *, robot_list=None, assigned_robots=None, **extra):
        record = {
            "task": "unit task",
            "robot list": robot_list or [1, 2],
            "object_states": [],
            "trans": 0,
            "max_trans": 0,
            "subtasks": subtasks,
            "assigned_robots": assigned_robots or [1 for _ in subtasks],
        }
        record.update(extra)
        return record

    def _write_task_file(self, tmp_root, records):
        data_dir = Path(tmp_root) / "final_test_new_unit"
        data_dir.mkdir()
        path = data_dir / "FloorPlan1.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def _read_rows(self, path):
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_relabel_removes_stale_invalid_and_adds_wash_pre_task_actions(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(
                tmp_dir,
                [
                    self._task_record(
                        [{"skill": "PutOn", "objects": ["Apple", "CounterTop"]}],
                        invalid=True,
                        valid=True,
                    ),
                    self._task_record(
                        [{"skill": "Wash", "objects": ["Plate"]}],
                    ),
                ],
            )

            stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                context=self._context(),
            )

            rows = self._read_rows(path)
            self.assertEqual(stats.rows, 2)
            self.assertEqual(stats.invalid_removed, 1)
            self.assertNotIn("invalid", rows[0])
            self.assertNotIn("valid", rows[0])
            self.assertEqual(rows[0]["pre_task_actions"], [])
            self.assertEqual(
                rows[1]["pre_task_actions"],
                [
                    {
                        "action_type": "DirtyObject",
                        "parameters": {"args": ["Plate"]},
                        "robot_id": "robot1",
                    }
                ],
            )

    def test_fillwater_adds_empty_liquid_pre_task_action(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(
                tmp_dir,
                [
                    self._task_record(
                        [{"skill": "FillWater", "objects": ["Mug", "Sink"]}],
                    ),
                ],
            )

            stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                context=self._context(),
            )

            rows = self._read_rows(path)
            self.assertEqual(stats.valid, 1)
            self.assertEqual(
                rows[0]["pre_task_actions"],
                [
                    {
                        "action_type": "EmptyLiquid",
                        "parameters": {"args": ["Mug"]},
                        "robot_id": "robot1",
                    }
                ],
            )

    def test_invalid_causes_are_marked(self):
        bad_subtask = {"skill": "Break", "objects": ["Vase"]}
        bad_rules = [
            data_engine.BadSubtaskRule(
                floor_plan=1,
                subtask_key=data_engine._subtask_key(bad_subtask),
            )
        ]
        context = self._context(
            bad_rules=bad_rules,
            no_valid_position_rules={(1, "Apple", "CounterTop")},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(
                tmp_dir,
                [
                    self._task_record(
                        [{"skill": "PutOn", "objects": ["Apple", "Drawer"]}],
                    ),
                    self._task_record([bad_subtask]),
                    self._task_record(
                        [{"skill": "PutOn", "objects": ["Apple", "CounterTop"]}],
                    ),
                    self._task_record(
                        [{"skill": "Wash", "objects": ["Plate"]}],
                        robot_list=[11, 1],
                        assigned_robots=[11],
                    ),
                ],
            )

            stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                context=context,
            )

            rows = self._read_rows(path)
            self.assertEqual(stats.invalid, 4)
            self.assertTrue(all(row.get("invalid") is True for row in rows))
            reasons = [detail.reason for detail in stats.invalid_details]
            self.assertIn("object pair is not valid for skill", reasons)
            self.assertIn("matches bad subtask rule", reasons)
            self.assertIn("matches no-valid-position rule", reasons)
            self.assertIn("assigned robot cannot complete subtask", reasons)

    def test_dry_run_does_not_modify_file_and_written_result_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(
                tmp_dir,
                [
                    self._task_record(
                        [{"skill": "PutOn", "objects": ["Apple", "CounterTop"]}],
                        invalid=True,
                    ),
                ],
            )
            original = path.read_text(encoding="utf-8")

            dry_stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                dry_run=True,
                context=self._context(),
            )
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(dry_stats.changed_files, 1)

            write_stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                context=self._context(),
            )
            self.assertEqual(write_stats.changed_files, 1)

            second_dry_stats = relabeler.run_relabel(
                Path(tmp_dir),
                target_dirs=["final_test_new_unit"],
                dry_run=True,
                context=self._context(),
            )
            self.assertEqual(second_dry_stats.changed_files, 0)
            self.assertEqual(second_dry_stats.invalid_removed, 0)
            self.assertEqual(second_dry_stats.pre_task_changed, 0)


if __name__ == "__main__":
    unittest.main()
