import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sft_generator import (
    check_allocate_assignment_count,
    check_decompose_subtask_count,
    check_model,
    check_planner_plan_count,
    check_validate_output_count,
    get_model,
)


class TestSftGeneratorModelCheck(unittest.TestCase):
    def write_summary(self, root: Path, content) -> Path:
        summary_path = root / "summary.json"
        summary_path.write_text(
            json.dumps(content, ensure_ascii=False),
            encoding="utf-8",
        )
        return summary_path

    def test_check_model_matches_first_nested_result(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(
                root,
                {
                    "summaries": [
                        {
                            "floor_plan": "6",
                            "results": [
                                {
                                    "floor_plan": "6",
                                    "model": "test-model",
                                    "task_index": 3,
                                    "task_run_dir": str(root / "run"),
                                }
                            ],
                        }
                    ]
                },
            )

            result = check_model(str(summary_path), "test-model")

            self.assertTrue(result["matched"])
            self.assertEqual(result["model"], "test-model")
            self.assertEqual(result["expected_model"], "test-model")
            self.assertEqual(result["flat_index"], 0)
            self.assertEqual(result["floor_plan"], "6")
            self.assertEqual(result["task_index"], 3)
            self.assertEqual(result["task_run_dir"], str(root / "run"))

    def test_get_model_returns_first_nested_result_model(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(
                root,
                {
                    "summaries": [
                        {
                            "results": [
                                {"model": "first-model"},
                                {"model": "second-model"},
                            ],
                        }
                    ]
                },
            )

            self.assertEqual(get_model(str(summary_path)), "first-model")

    def test_check_model_mismatches_first_top_level_result(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(
                root,
                {
                    "results": [
                        {"model": "actual-model", "task_index": 0},
                        {"model": "expected-model", "task_index": 1},
                    ]
                },
            )

            result = check_model(str(summary_path), "expected-model")

            self.assertFalse(result["matched"])
            self.assertEqual(result["model"], "actual-model")
            self.assertEqual(result["expected_model"], "expected-model")
            self.assertEqual(result["flat_index"], 0)

    def test_get_model_returns_first_top_level_result_model(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(
                root,
                {
                    "results": [
                        {"model": "top-level-first"},
                        {"model": "top-level-second"},
                    ]
                },
            )

            self.assertEqual(get_model(str(summary_path)), "top-level-first")

    def test_check_model_raises_for_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(root, {"summaries": []})

            with self.assertRaisesRegex(ValueError, "No summary results found"):
                check_model(str(summary_path), "test-model")
            with self.assertRaisesRegex(ValueError, "No summary results found"):
                get_model(str(summary_path))

    def test_check_model_raises_for_missing_first_model(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(root, {"results": [{"task_index": 0}]})

            with self.assertRaisesRegex(ValueError, "Missing model for flat index 0"):
                check_model(str(summary_path), "test-model")
            with self.assertRaisesRegex(ValueError, "Missing model for flat index 0"):
                get_model(str(summary_path))

    def test_check_model_raises_for_empty_expected_model(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(root, {"results": [{"model": "test-model"}]})

            with self.assertRaisesRegex(ValueError, "Expected model must be non-empty"):
                check_model(str(summary_path), " ")

    def test_check_count_functions_return_unmatched_for_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = self.write_summary(
                root,
                {
                    "results": [
                        {
                            "task_run_dir": str(root / "missing_run"),
                            "floor_plan": "6",
                            "task_index": 0,
                        }
                    ]
                },
            )

            checks = [
                check_decompose_subtask_count,
                check_allocate_assignment_count,
                check_validate_output_count,
                check_planner_plan_count,
            ]

            for check in checks:
                with self.subTest(check=check.__name__):
                    result = check(str(summary_path), 0)
                    self.assertIs(result, False)

    def test_check_decompose_subtask_count_returns_false_for_missing_task_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            results = [
                {
                    "task_run_dir": str(root / f"run_{idx}"),
                    "floor_plan": "6",
                    "task_index": idx,
                }
                for idx in range(32)
            ]
            results.append({"floor_plan": "6", "task_index": 32})
            summary_path = self.write_summary(root, {"results": results})

            result = check_decompose_subtask_count(str(summary_path), 32)

            self.assertIs(result, False)


if __name__ == "__main__":
    unittest.main()
