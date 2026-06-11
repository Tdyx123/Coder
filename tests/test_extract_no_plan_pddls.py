import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from extract_no_plan_pddls import main


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, content) -> None:
    write_text(path, json.dumps(content, ensure_ascii=False, indent=2))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ExtractNoPlanPDDLsTest(unittest.TestCase):
    def test_extracts_no_plan_records_into_error_buckets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = root / "logs" / "intermediate_runs" / "sample_task" / "20260506_001"
            outputs_dir = task_run_dir / "08_planner" / "outputs"
            stdout_dir = task_run_dir / "08_planner" / "stdout"
            stderr_dir = task_run_dir / "08_planner" / "stderr"

            write_text(
                task_run_dir / "07_validate" / "outputs" / "subtask_01_problem_validated.pddl",
                "(define (problem failed-one))",
            )
            write_text(
                task_run_dir / "07_validate" / "outputs" / "subtask_02_problem_validated.pddl",
                "(define (problem silent-one))",
            )
            write_text(
                task_run_dir / "07_validate" / "outputs" / "subtask_03_problem_validated.pddl",
                "(define (problem solved-one))",
            )
            write_text(
                task_run_dir / "04_problem_files" / "subtasks" / "subtask_01.txt",
                "#Subtask 1: fail to plan",
            )
            write_text(
                task_run_dir / "04_problem_files" / "subtasks" / "subtask_02.txt",
                "#Subtask 2: silently missing plan",
            )
            write_text(
                task_run_dir / "04_problem_files" / "subtasks" / "subtask_03.txt",
                "#Subtask 3: solved",
            )

            write_text(stdout_dir / "subtask_01_problem_validated_stdout.txt", "search exit code: 12\n")
            write_text(stderr_dir / "subtask_01_problem_validated_stderr.txt", "")
            write_text(stdout_dir / "subtask_02_problem_validated_stdout.txt", "planner stopped\n")
            write_text(stderr_dir / "subtask_02_problem_validated_stderr.txt", "")
            write_text(stdout_dir / "subtask_03_problem_validated_stdout.txt", "Solution found!\n")
            write_text(stderr_dir / "subtask_03_problem_validated_stderr.txt", "")
            write_text(outputs_dir / "subtask_03_problem_validated_plan.txt", "(gotoobject robot1 drawer)\n")

            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "stdout_path": "08_planner/stdout/subtask_01_problem_validated_stdout.txt",
                        "stderr_path": "08_planner/stderr/subtask_01_problem_validated_stderr.txt",
                        "return_code": 12,
                    },
                    {
                        "problem_file": "subtask_02_problem_validated.pddl",
                        "stdout_path": "08_planner/stdout/subtask_02_problem_validated_stdout.txt",
                        "stderr_path": "08_planner/stderr/subtask_02_problem_validated_stderr.txt",
                        "return_code": 0,
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "stdout_path": "08_planner/stdout/subtask_03_problem_validated_stdout.txt",
                        "stderr_path": "08_planner/stderr/subtask_03_problem_validated_stderr.txt",
                        "return_code": 0,
                    },
                ],
            )
            summary_path = root / "parallel_runs" / "sample" / "summary.json"
            write_json(
                summary_path,
                {
                    "summaries": [
                        {
                            "floor_plan": "6",
                            "results": [
                                {
                                    "floor_plan": "6",
                                    "task_index": 4,
                                    "task": "sample task",
                                    "task_run_dir": str(task_run_dir),
                                }
                            ],
                        }
                    ]
                },
            )
            output_dir = root / "out"

            result = main(["--summary", str(summary_path), "--output-dir", str(output_dir)])

            self.assertEqual(result, 0)
            with_errors = read_jsonl(output_dir / "no_plan_with_errors.jsonl")

            self.assertEqual(len(with_errors), 1)
            self.assertFalse((output_dir / "no_plan_without_errors.jsonl").exists())
            self.assertEqual(with_errors[0]["floorplan"], "6")
            self.assertEqual(with_errors[0]["floor_plan"], "6")
            self.assertEqual(with_errors[0]["task_index"], 4)
            self.assertEqual(with_errors[0]["task"], "sample task")
            self.assertEqual(with_errors[0]["subtask_index"], 1)
            self.assertEqual(with_errors[0]["subtask"], "subtask_01")
            self.assertEqual(with_errors[0]["subtask_text"], "#Subtask 1: fail to plan")
            self.assertEqual(with_errors[0]["pddl"], "(define (problem failed-one))")
            self.assertIsNone(with_errors[0]["plan"])
            self.assertTrue(with_errors[0]["has_error"])
            self.assertEqual(with_errors[0]["error_message"], "planner return_code: 12")
            self.assertNotIn("planner_stdout", with_errors[0])
            self.assertTrue(with_errors[0]["planner_stdout_path"].endswith("subtask_01_problem_validated_stdout.txt"))

            emitted_subtasks = {record["subtask"] for record in with_errors}
            self.assertNotIn("subtask_02", emitted_subtasks)
            self.assertNotIn("subtask_03", emitted_subtasks)


if __name__ == "__main__":
    unittest.main()
