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
    check_problem_output_count,
    get_model,
    output_latest_unique_pass_one_tasks,
    select_latest_unique_task_runs,
)


class TestSftGeneratorModelCheck(unittest.TestCase):
    def write_summary(self, root: Path, content) -> Path:
        summary_path = root / "summary.json"
        summary_path.write_text(
            json.dumps(content, ensure_ascii=False),
            encoding="utf-8",
        )
        return summary_path

    def write_dataset(self, root: Path, test_set: str, floor_plan: str, records) -> Path:
        dataset_path = root / "data" / test_set / f"FloorPlan{floor_plan}.jsonl"
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        dataset_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        return dataset_path

    def make_valid_task_run(self, root: Path, name: str, decompose_output: str = "do task") -> Path:
        task_run_dir = root / "task_runs" / name

        (task_run_dir / "01_decompose").mkdir(parents=True)
        (task_run_dir / "01_decompose" / "01_decompose_prompt.txt").write_text(
            f"decompose prompt {name}",
            encoding="utf-8",
        )
        (task_run_dir / "01_decompose" / "02_decompose_output.txt").write_text(
            decompose_output,
            encoding="utf-8",
        )

        (task_run_dir / "02_allocate").mkdir(parents=True)
        (task_run_dir / "02_allocate" / "01_allocate_prompt.txt").write_text(
            f"allocate prompt {name}",
            encoding="utf-8",
        )
        (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
            "Subtask 1: Robot 1;",
            encoding="utf-8",
        )

        (task_run_dir / "05_problem_generation" / "prompts").mkdir(parents=True)
        (task_run_dir / "05_problem_generation" / "outputs").mkdir(parents=True)
        (task_run_dir / "05_problem_generation" / "prompts" / "subtask_01_prompt.txt").write_text(
            f"problem prompt {name}",
            encoding="utf-8",
        )
        (task_run_dir / "05_problem_generation" / "outputs" / "subtask_01_problem.pddl").write_text(
            f"problem output {name}",
            encoding="utf-8",
        )

        (task_run_dir / "08_planner" / "outputs").mkdir(parents=True)
        (task_run_dir / "08_planner" / "outputs" / "subtask_01_plan.txt").write_text(
            f"planner output {name}",
            encoding="utf-8",
        )

        return task_run_dir

    def write_pddlrun_summary(self, root: Path, pddlrun_name: str, results, test_set: str = "unit_set") -> Path:
        summary_dir = root / "parallel_runs" / pddlrun_name
        summary_dir.mkdir(parents=True)
        summary_path = summary_dir / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "repo_root": str(root),
                    "test_set": test_set,
                    "summaries": [
                        {
                            "floor_plan": "1",
                            "results": results,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
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
                check_problem_output_count,
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

    def test_select_latest_unique_task_runs_keeps_newest_pddlrun_for_duplicate_task_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [{"subtasks": ["do task"], "assigned_robots": [1]}],
            )
            old_task_run_dir = self.make_valid_task_run(root, "old")
            new_task_run_dir = self.make_valid_task_run(root, "new")

            old_summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260501_000000",
                [
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 0,
                        "task": "same task",
                        "task_run_dir": str(old_task_run_dir),
                        "tc": 1,
                        "total": 1,
                    }
                ],
            )
            new_summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "FloorPlan1",
                        "model": "test-model",
                        "task_index": 0,
                        "task": "same task",
                        "task_run_dir": str(new_task_run_dir),
                        "tc": 1,
                        "total": 1,
                    }
                ],
            )

            selected = select_latest_unique_task_runs([
                str(old_summary_path),
                str(new_summary_path),
            ])

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["task_key"], ("unit_set", "1", 0))
            self.assertEqual(selected[0]["task_run_dir"], str(new_task_run_dir))
            self.assertEqual(selected[0]["pddlrun_timestamp"], "20260502_000000")

    def test_select_latest_unique_task_runs_requires_all_subtasks_to_pass(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [
                    {"subtasks": ["failed task"], "assigned_robots": [1]},
                    {"subtasks": ["partial task"], "assigned_robots": [1]},
                    {"subtasks": ["passed task"], "assigned_robots": [1]},
                    {"subtasks": ["empty task"], "assigned_robots": [1]},
                ],
            )
            failed_task_run_dir = self.make_valid_task_run(root, "failed", decompose_output="failed task")
            partial_task_run_dir = self.make_valid_task_run(root, "partial", decompose_output="partial task")
            passed_task_run_dir = self.make_valid_task_run(root, "passed", decompose_output="passed task")
            empty_task_run_dir = self.make_valid_task_run(root, "empty", decompose_output="empty task")
            summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "1",
                        "task_index": 0,
                        "task_run_dir": str(failed_task_run_dir),
                        "tc": 0,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 1,
                        "task_run_dir": str(partial_task_run_dir),
                        "tc": 1,
                        "total": 2,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 2,
                        "task_run_dir": str(passed_task_run_dir),
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 3,
                        "task_run_dir": str(empty_task_run_dir),
                        "tc": 0,
                        "total": 0,
                    },
                ],
            )

            selected = select_latest_unique_task_runs([str(summary_path)])

            self.assertEqual(
                [candidate["task_key"] for candidate in selected],
                [("unit_set", "1", 2)],
            )

    def test_output_latest_unique_pass_one_tasks_writes_partial_and_full_passes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [
                    {"subtasks": ["failed task"], "assigned_robots": [1]},
                    {"subtasks": ["partial task"], "assigned_robots": [1]},
                    {"subtasks": ["full task"], "assigned_robots": [1]},
                    {"subtasks": ["no total task"], "assigned_robots": [1]},
                ],
            )
            task_run_dirs = [
                self.make_valid_task_run(root, name, decompose_output=f"{name} task")
                for name in ("failed", "partial", "full", "no total")
            ]
            summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "1",
                        "task_index": 0,
                        "task_run_dir": str(task_run_dirs[0]),
                        "tc": 0,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 1,
                        "task_run_dir": str(task_run_dirs[1]),
                        "tc": 1,
                        "total": 2,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 2,
                        "task_run_dir": str(task_run_dirs[2]),
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "task_index": 3,
                        "task_run_dir": str(task_run_dirs[3]),
                        "tc": 1,
                    },
                ],
            )
            output_path = root / "sft"
            selected = output_latest_unique_pass_one_tasks(
                [str(summary_path)],
                output_path_base=str(output_path),
            )

            self.assertEqual(
                [candidate["task_key"] for candidate in selected],
                [("unit_set", "1", 1), ("unit_set", "1", 2), ("unit_set", "1", 3)],
            )
            for filename in (
                "01_decompose.jsonl",
                "02_allocate.jsonl",
                "05_problem_generation.jsonl",
            ):
                records = (output_path / filename).read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(records), 3)

    def test_output_latest_unique_pass_one_tasks_rejects_invalid_tc_values(self):
        for case_name, tc in (
            ("missing", None),
            ("boolean", True),
            ("string", "1"),
            ("zero", 0),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                self.write_dataset(
                    root,
                    "unit_set",
                    "1",
                    [{"subtasks": ["do task"], "assigned_robots": [1]}],
                )
                task_run_dir = self.make_valid_task_run(root, case_name)
                result = {
                    "floor_plan": "1",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                    "total": 1,
                }
                if tc is not None:
                    result["tc"] = tc
                summary_path = self.write_pddlrun_summary(
                    root,
                    "pddlrun_llmseparate_20260502_000000",
                    [result],
                )

                selected = output_latest_unique_pass_one_tasks(
                    [str(summary_path)],
                    output_path_base=str(root / "sft"),
                )

                self.assertEqual(selected, [])

    def test_output_latest_unique_pass_one_tasks_keeps_latest_qualifying_run(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [{"subtasks": ["do task"], "assigned_robots": [1]}],
            )
            old_task_run_dir = self.make_valid_task_run(root, "old")
            new_task_run_dir = self.make_valid_task_run(root, "new")
            old_summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260501_000000",
                [
                    {
                        "floor_plan": "1",
                        "task_index": 0,
                        "task_run_dir": str(old_task_run_dir),
                        "tc": 1,
                        "total": 2,
                    }
                ],
            )
            new_summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "1",
                        "task_index": 0,
                        "task_run_dir": str(new_task_run_dir),
                        "tc": 0,
                        "total": 1,
                    }
                ],
            )

            selected = output_latest_unique_pass_one_tasks(
                [str(old_summary_path), str(new_summary_path)],
                output_path_base=str(root / "sft"),
            )

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["task_run_dir"], str(old_task_run_dir))
            self.assertEqual(selected[0]["pddlrun_timestamp"], "20260501_000000")

    def test_select_latest_unique_task_runs_keeps_distinct_keys_with_same_task_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [
                    {"subtasks": ["do first task"], "assigned_robots": [1]},
                    {"subtasks": ["do second task"], "assigned_robots": [1]},
                ],
            )
            first_task_run_dir = self.make_valid_task_run(root, "first", decompose_output="do first task")
            second_task_run_dir = self.make_valid_task_run(root, "second", decompose_output="do second task")

            summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 0,
                        "task": "same visible task text",
                        "task_run_dir": str(first_task_run_dir),
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 1,
                        "task": "same visible task text",
                        "task_run_dir": str(second_task_run_dir),
                        "tc": 1,
                        "total": 1,
                    },
                ],
            )

            selected = select_latest_unique_task_runs([str(summary_path)])

            self.assertEqual(len(selected), 2)
            self.assertEqual(
                [candidate["task_key"] for candidate in selected],
                [("unit_set", "1", 0), ("unit_set", "1", 1)],
            )
            self.assertEqual(
                [candidate["task_run_dir"] for candidate in selected],
                [str(first_task_run_dir), str(second_task_run_dir)],
            )

    def test_select_latest_unique_task_runs_skips_invalid_source_task_records(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.write_dataset(
                root,
                "unit_set",
                "1",
                [
                    {"subtasks": ["do task"], "assigned_robots": [1]},
                    {"subtasks": ["do task"], "assigned_robots": [1], "invalid": True},
                    {"subtasks": ["do task"], "assigned_robots": [1], "invalid": False},
                    {"subtasks": ["do task"], "assigned_robots": [1], "Invalid": True},
                ],
            )
            task_run_dirs = [
                self.make_valid_task_run(root, f"task_{idx}")
                for idx in range(4)
            ]

            summary_path = self.write_pddlrun_summary(
                root,
                "pddlrun_llmseparate_20260502_000000",
                [
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 0,
                        "task": "summary marked invalid but source valid",
                        "task_run_dir": str(task_run_dirs[0]),
                        "invalid": True,
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 1,
                        "task": "source lowercase invalid",
                        "task_run_dir": str(task_run_dirs[1]),
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 2,
                        "task": "source explicitly valid",
                        "task_run_dir": str(task_run_dirs[2]),
                        "tc": 1,
                        "total": 1,
                    },
                    {
                        "floor_plan": "1",
                        "model": "test-model",
                        "task_index": 3,
                        "task": "source uppercase invalid",
                        "task_run_dir": str(task_run_dirs[3]),
                        "Invalid": False,
                        "tc": 1,
                        "total": 1,
                    },
                ],
            )

            selected = select_latest_unique_task_runs([str(summary_path)])

            self.assertEqual(
                [candidate["task_key"] for candidate in selected],
                [("unit_set", "1", 0), ("unit_set", "1", 2)],
            )
            self.assertEqual(
                [candidate["task_run_dir"] for candidate in selected],
                [str(task_run_dirs[0]), str(task_run_dirs[2])],
            )


if __name__ == "__main__":
    unittest.main()
