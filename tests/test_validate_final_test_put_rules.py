import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_final_test_put_rules as validator


class ValidateFinalTestPutRulesTests(unittest.TestCase):
    def setUp(self):
        self.skill_sets = {
            "pickupable_objects": ["Apple", "Book", "Mug"],
            "put_in_receptacles": ["Bowl", "Drawer"],
            "placement_restrictions": {
                "Apple": ["Bowl", "CounterTop"],
                "Book": ["Chair", "Drawer"],
                "Mug": ["CounterTop"],
            },
        }

    def _record(self, *subtasks):
        return {"task": "test task", "subtasks": list(subtasks)}

    def _is_valid(self, record):
        valid, _ = validator.validate_task_record(record, self.skill_sets)
        return valid

    def test_valid_puton_is_marked_true(self):
        record = self._record({"skill": "PutOn", "objects": ["Apple", "CounterTop"]})

        self.assertTrue(self._is_valid(record))

    def test_puton_disallowed_by_placement_restriction_is_marked_false(self):
        record = self._record({"skill": "PutOn", "objects": ["Apple", "Drawer"]})

        self.assertFalse(self._is_valid(record))

    def test_valid_putin_is_marked_true(self):
        record = self._record({"skill": "PutIn", "objects": ["Apple", "Bowl"]})

        self.assertTrue(self._is_valid(record))

    def test_putin_requires_receptacle_whitelist_and_placement_restriction(self):
        non_whitelisted = self._record({"skill": "PutIn", "objects": ["Mug", "CounterTop"]})
        disallowed_pair = self._record({"skill": "PutIn", "objects": ["Apple", "Drawer"]})

        self.assertFalse(self._is_valid(non_whitelisted))
        self.assertFalse(self._is_valid(disallowed_pair))

    def test_tasks_with_only_non_put_skills_are_marked_true(self):
        record = self._record(
            {"skill": "Break", "objects": ["Window"]},
            {"skill": "SwitchOn", "objects": ["LightSwitch"]},
        )

        self.assertTrue(self._is_valid(record))

    def test_malformed_put_subtask_is_marked_false(self):
        missing_object = self._record({"skill": "PutOn", "objects": ["Apple"]})
        non_string_object = self._record({"skill": "PutIn", "objects": ["Apple", 1]})
        missing_subtasks = {"task": "no subtasks"}

        self.assertFalse(self._is_valid(missing_object))
        self.assertFalse(self._is_valid(non_string_object))
        self.assertFalse(self._is_valid(missing_subtasks))

    def test_dry_run_does_not_modify_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(Path(tmp_dir))
            original = path.read_text(encoding="utf-8")

            stats = validator.run_validation(
                Path(tmp_dir),
                dry_run=True,
                skill_set_loader=lambda floor_plan: self.skill_sets,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(stats.files, 1)
            self.assertEqual(stats.rows, 2)
            self.assertEqual(stats.valid, 1)
            self.assertEqual(stats.invalid, 1)
            self.assertEqual(stats.changed_files, 1)

    def test_in_place_write_adds_invalid_only_and_preserves_row_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_task_file(Path(tmp_dir))

            stats = validator.run_validation(
                Path(tmp_dir),
                skill_set_loader=lambda floor_plan: self.skill_sets,
            )

            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(stats.changed_files, 1)
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["task"] for row in rows], ["valid task", "invalid task"])
            self.assertNotIn("valid", rows[0])
            self.assertNotIn("invalid", rows[0])
            self.assertNotIn("valid", rows[1])
            self.assertEqual(rows[1]["invalid"], True)

    def _write_task_file(self, tmp_root):
        data_dir = tmp_root / "final_test_new_unit"
        data_dir.mkdir()
        path = data_dir / "FloorPlan1.jsonl"
        records = [
            {
                "task": "valid task",
                "valid": True,
                "invalid": True,
                "subtasks": [{"skill": "PutOn", "objects": ["Apple", "CounterTop"]}],
            },
            {
                "task": "invalid task",
                "valid": True,
                "subtasks": [{"skill": "PutIn", "objects": ["Apple", "Drawer"]}],
            },
        ]
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
