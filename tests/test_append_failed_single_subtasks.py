import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from append_failed_single_subtasks import main


def write_json(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_generated_script(path: Path, *, floor_plan="2", subtasks=None) -> None:
    if subtasks is None:
        subtasks = [{"skill": "Break", "objects": ["Bowl"]}]
    task_record = {"task": "test task", "subtasks": subtasks}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                f"EMBEDDED_TASK_RECORD = {task_record!r}",
                f"EMBEDDED_FLOOR_PLAN = {floor_plan!r}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def runner_result(script_path: Path, **overrides):
    result = {
        "executable_path": str(script_path),
        "returncode": 0,
        "robot_failures": [],
        "status": "success",
        "timed_out": False,
    }
    result.update(overrides)
    return result


class AppendFailedSingleSubtasksTest(unittest.TestCase):
    def test_appends_failed_generated_single_subtask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_path = root / "single_subtask.py"
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "bad_single_subtasks.json"
            write_generated_script(script_path)
            write_json(
                summary_path,
                {
                    "results": [
                        runner_result(
                            script_path,
                            robot_failures=[{"action_type": "PickupObject"}],
                        )
                    ]
                },
            )
            write_json(output_path, {"version": 1, "bad_subtasks": []})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(
                read_json(output_path),
                {
                    "version": 1,
                    "bad_subtasks": [
                        {
                            "floor_plan": 2,
                            "subtask": {"skill": "Break", "objects": ["Bowl"]},
                            "reason": "Failed in parallel runner",
                        }
                    ],
                },
            )

    def test_skips_duplicate_floor_plan_and_subtask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_path = root / "single_subtask.py"
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "bad_single_subtasks.json"
            existing = {
                "floor_plan": "FloorPlan2",
                "subtask": {"skill": "Break", "objects": ["Bowl"]},
                "reason": "Original reason",
            }
            write_generated_script(script_path)
            write_json(
                summary_path,
                {
                    "results": [
                        runner_result(
                            script_path,
                            robot_failures=[{"action_type": "PickupObject"}],
                        )
                    ]
                },
            )
            write_json(output_path, {"version": 1, "bad_subtasks": [existing]})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(read_json(output_path), {"version": 1, "bad_subtasks": [existing]})

    def test_failed_status_without_robot_failures_is_not_appended(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_path = root / "single_subtask.py"
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "bad_single_subtasks.json"
            write_generated_script(script_path)
            write_json(
                summary_path,
                {
                    "results": [
                        runner_result(
                            script_path,
                            returncode=7,
                            status="failed",
                            timed_out=False,
                        )
                    ]
                },
            )
            write_json(output_path, {"version": 1, "bad_subtasks": []})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(read_json(output_path), {"version": 1, "bad_subtasks": []})

    def test_timeout_without_robot_failures_is_not_appended(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_path = root / "single_subtask.py"
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "bad_single_subtasks.json"
            write_generated_script(
                script_path,
                floor_plan="FloorPlan5",
                subtasks=[{"skill": "PutOn", "objects": ["Mug", "CounterTop"]}],
            )
            write_json(
                summary_path,
                {
                    "results": [
                        runner_result(
                            script_path,
                            returncode=0,
                            status="success",
                            timed_out=True,
                        )
                    ]
                },
            )
            write_json(output_path, {"version": 1, "bad_subtasks": []})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--input",
                        str(summary_path),
                        "--output",
                        str(output_path),
                        "--reason",
                        "Timed out in runner",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(read_json(output_path), {"version": 1, "bad_subtasks": []})

    def test_non_list_robot_failures_is_not_appended(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script_path = root / "single_subtask.py"
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "bad_single_subtasks.json"
            write_generated_script(script_path)
            write_json(
                summary_path,
                {
                    "results": [
                        runner_result(
                            script_path,
                            robot_failures={"action_type": "PickupObject"},
                        )
                    ]
                },
            )
            write_json(output_path, {"version": 1, "bad_subtasks": []})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(read_json(output_path), {"version": 1, "bad_subtasks": []})

    def test_missing_output_file_is_created(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "nested" / "bad_single_subtasks.json"
            write_json(summary_path, {"results": []})

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(read_json(output_path), {"version": 1, "bad_subtasks": []})


if __name__ == "__main__":
    unittest.main()
