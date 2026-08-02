import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from grpo_generator import (
    generate_grpo_examples,
    resolve_summary_path,
    write_grpo_jsonl,
)


class GrpoGeneratorTest(unittest.TestCase):
    def write_allocate_run(
        self,
        root: Path,
        name: str,
        prompt: str = "allocate prompt",
        output: str = "allocate output",
        write_prompt: bool = True,
        write_output: bool = True,
    ) -> Path:
        task_run_dir = root / "task_runs" / name
        allocate_dir = task_run_dir / "02_allocate"
        allocate_dir.mkdir(parents=True)
        if write_prompt:
            (allocate_dir / "01_allocate_prompt.txt").write_text(prompt, encoding="utf-8")
        if write_output:
            (allocate_dir / "02_allocate_output.txt").write_text(output, encoding="utf-8")
        return task_run_dir

    def result(
        self,
        task_run_dir: Path,
        tc=1,
        floor_plan="1",
        task_index=0,
        task="Open the drawer",
    ):
        return {
            "floor_plan": floor_plan,
            "task_index": task_index,
            "task": task,
            "tc": tc,
            "total": 99,
            "task_run_dir": str(task_run_dir),
        }

    def write_summary(
        self,
        root: Path,
        run_name: str,
        results,
        test_set="sample_set",
        top_level=False,
    ) -> Path:
        run_dir = root / "parallel_runs" / run_name
        run_dir.mkdir(parents=True)
        content = {
            "repo_root": str(root),
            "test_set": test_set,
        }
        if top_level:
            content["results"] = results
        else:
            content["summaries"] = [{"floor_plan": "1", "results": results}]
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        return summary_path

    def make_pair(self, root: Path, first_results, second_results, first_set="sample_set", second_set="sample_set"):
        first = self.write_summary(root, "first", first_results, test_set=first_set)
        second = self.write_summary(root, "second", second_results, test_set=second_set)
        return first, second

    def test_second_higher_tc_emits_second_allocate_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_run_dir = self.write_allocate_run(root, "first_task", "first prompt", "first output")
            second_run_dir = self.write_allocate_run(
                root,
                "second_task",
                "分配提示词\n第二行",
                "完整推理\nSubtask 1: Robot 2;",
            )
            first, second = self.make_pair(
                root,
                [self.result(first_run_dir, tc=1)],
                [self.result(second_run_dir, tc=2)],
            )

            examples, stats = generate_grpo_examples(str(first), str(second), repo_root=root)

            self.assertEqual(examples, [{
                "messages": [{"role": "user", "content": "分配提示词\n第二行"}],
                "solution": "完整推理\nSubtask 1: Robot 2;",
            }])
            self.assertEqual(stats.matched_tasks, 1)
            self.assertEqual(stats.better_tasks, 1)
            self.assertEqual(stats.examples_written, 1)

    def test_equal_or_lower_second_tc_is_not_emitted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dirs = [self.write_allocate_run(root, f"run_{index}") for index in range(4)]
            first, second = self.make_pair(
                root,
                [
                    self.result(run_dirs[0], tc=2, task_index=0, task="task zero"),
                    self.result(run_dirs[1], tc=3, task_index=1, task="task one"),
                ],
                [
                    self.result(run_dirs[2], tc=2, task_index=0, task="task zero"),
                    self.result(run_dirs[3], tc=1, task_index=1, task="task one"),
                ],
            )

            examples, stats = generate_grpo_examples(str(first), str(second), repo_root=root)

            self.assertEqual(examples, [])
            self.assertEqual(stats.skipped_not_better, 2)

    def test_multiple_tasks_keep_second_summary_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_dirs = [self.write_allocate_run(root, f"first_{index}") for index in range(2)]
            second_a = self.write_allocate_run(root, "second_a", "prompt A", "output A")
            second_b = self.write_allocate_run(root, "second_b", "prompt B", "output B")
            first, second = self.make_pair(
                root,
                [
                    self.result(first_dirs[0], tc=0, task_index=0, task="task A"),
                    self.result(first_dirs[1], tc=0, task_index=1, task="task B"),
                ],
                [
                    self.result(second_b, tc=1, task_index=1, task=" TASK   B "),
                    self.result(second_a, tc=1, task_index=0, task="Task A"),
                ],
            )

            examples, _ = generate_grpo_examples(str(first), str(second), repo_root=root)

            self.assertEqual([example["solution"] for example in examples], ["output B", "output A"])

    def test_top_level_results_and_floor_plan_normalization_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_dir = self.write_allocate_run(root, "first_task")
            second_dir = self.write_allocate_run(root, "second_task", "prompt", "solution")
            first = self.write_summary(
                root,
                "first",
                [self.result(first_dir, tc="1", floor_plan="FloorPlan7", task_index="0")],
                top_level=True,
            )
            second = self.write_summary(
                root,
                "second",
                [self.result(second_dir, tc="2", floor_plan="7", task_index=0)],
                top_level=True,
            )

            examples, stats = generate_grpo_examples(str(first), str(second), repo_root=root)

            self.assertEqual(len(examples), 1)
            self.assertEqual(stats.matched_tasks, 1)

    def test_mismatches_missing_values_and_artifact_errors_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            normal_dirs = [self.write_allocate_run(root, f"normal_{index}") for index in range(8)]
            missing_prompt = self.write_allocate_run(root, "missing_prompt", write_prompt=False)
            empty_output = self.write_allocate_run(root, "empty_output", output=" \n")
            missing_task_dir = root / "task_runs" / "does_not_exist"
            first_results = [
                self.result(normal_dirs[index], tc=0, task_index=index, task=f"task {index}")
                for index in range(8)
            ]
            second_results = [
                self.result(normal_dirs[0], tc=1, task_index=0, task="different task"),
                self.result(normal_dirs[1], tc=None, task_index=1, task="task 1"),
                self.result(missing_prompt, tc=1, task_index=2, task="task 2"),
                self.result(empty_output, tc=1, task_index=3, task="task 3"),
                self.result(missing_task_dir, tc=1, task_index=4, task="task 4"),
                self.result(normal_dirs[5], tc=1, task_index=99, task="missing first"),
                self.result(normal_dirs[6], tc=1, task_index=None, task="invalid key"),
                self.result(normal_dirs[7], tc="not-a-number", task_index=7, task="task 7"),
            ]
            first, second = self.make_pair(root, first_results, second_results)

            examples, stats = generate_grpo_examples(str(first), str(second), repo_root=root)

            self.assertEqual(examples, [])
            self.assertEqual(stats.skipped_task_text_mismatch, 1)
            self.assertEqual(stats.skipped_missing_tc, 2)
            self.assertEqual(stats.skipped_missing_artifact, 1)
            self.assertEqual(stats.skipped_empty_artifact, 1)
            self.assertEqual(stats.skipped_missing_task_run_dir, 1)
            self.assertEqual(stats.skipped_missing_first_task, 1)
            self.assertEqual(stats.skipped_invalid_task_key, 1)

    def test_run_name_directory_and_summary_path_are_resolved(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary = self.write_summary(root, "named_run", [
                self.result(self.write_allocate_run(root, "task"))
            ])

            self.assertEqual(resolve_summary_path("named_run", root), summary.resolve())
            self.assertEqual(resolve_summary_path(str(summary.parent), root), summary.resolve())
            self.assertEqual(resolve_summary_path(str(summary), root), summary.resolve())

    def test_different_test_sets_raise(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_dir = self.write_allocate_run(root, "first_task")
            second_dir = self.write_allocate_run(root, "second_task")
            first, second = self.make_pair(
                root,
                [self.result(first_dir)],
                [self.result(second_dir)],
                first_set="set_a",
                second_set="set_b",
            )

            with self.assertRaisesRegex(ValueError, "different test_set"):
                generate_grpo_examples(str(first), str(second), repo_root=root)

    def test_duplicate_task_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first_dir = self.write_allocate_run(root, "first_task")
            second_dir = self.write_allocate_run(root, "second_task")
            duplicate = self.result(first_dir, tc=1)
            first, second = self.make_pair(
                root,
                [duplicate, dict(duplicate)],
                [self.result(second_dir, tc=2)],
            )

            with self.assertRaisesRegex(ValueError, "duplicate task key"):
                generate_grpo_examples(str(first), str(second), repo_root=root)

    def test_invalid_summary_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            invalid_dir = root / "parallel_runs" / "invalid"
            invalid_dir.mkdir(parents=True)
            invalid_summary = invalid_dir / "summary.json"
            invalid_summary.write_text("[]", encoding="utf-8")
            valid = self.write_summary(root, "valid", [
                self.result(self.write_allocate_run(root, "task"))
            ])

            with self.assertRaisesRegex(ValueError, "JSON object"):
                generate_grpo_examples(str(invalid_summary), str(valid), repo_root=root)

    def test_write_jsonl_preserves_unicode_multiline_and_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_path = root / "nested" / "train.jsonl"
            output_path.parent.mkdir(parents=True)
            output_path.write_text("old data\n", encoding="utf-8")
            examples = [{
                "messages": [{"role": "user", "content": "中文提示\n下一行"}],
                "solution": "答案\nRobot 1",
            }]

            written_path = write_grpo_jsonl(examples, str(output_path), repo_root=root)

            self.assertEqual(written_path, output_path)
            lines = output_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), examples[0])
            self.assertIn("中文提示", lines[0])

            write_grpo_jsonl([], str(output_path), repo_root=root)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
