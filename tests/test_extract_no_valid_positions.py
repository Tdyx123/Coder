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

from extract_no_valid_positions import main


NO_VALID_POSITIONS = "No valid positions to place object found"


def write_json(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def no_valid_put_result(
    *,
    executable_path: str = "/tmp/bad_plan.py",
    floorplan: str = "FloorPlan2",
    held_object: str = "Pot",
    receptacle: str = "Fridge",
):
    error = (
        f"PutObject failed for agent 0 on {receptacle}|-01.76|+00.00|00.00: "
        f"{NO_VALID_POSITIONS}"
    )
    return {
        "exec_rate": 0.5,
        "executable_path": executable_path,
        "executed_actions": 5,
        "failed_actions": 1,
        "failure_action_ratio": 0.2,
        "returncode": 0,
        "robot_failures": [
            {
                "action_index": 4,
                "action_type": "PutObject",
                "error": error,
                "ignored_for_failure_ratio": False,
                "robot_id": "robot1",
                "stage_id": "Phase 1",
            }
        ],
        "status": "success",
        "stdout": "\n".join(
            [
                f"{floorplan} initialized with 1 physical agent(s).",
                (
                    "PutObject failed for agent 0; currently holding: "
                    f"{held_object}|+00.89|+00.90|-01.41."
                ),
                f"Tick 4: robot1 failed PutObject: {error}",
                "",
            ]
        ),
        "timed_out": False,
    }


class ExtractNoValidPositionsTest(unittest.TestCase):
    def test_main_extracts_and_prunes_flat_parallel_runner_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = root / "parallel_runner_summary.json"
            output_path = root / "no_valid_positions.json"
            good_result = {
                "executable_path": "/tmp/good_plan.py",
                "returncode": 0,
                "robot_failures": [],
                "status": "success",
                "timed_out": False,
            }
            failed_result = {
                "executable_path": "/tmp/failed_plan.py",
                "returncode": 7,
                "robot_failures": [],
                "status": "failed",
                "timed_out": False,
            }
            write_json(
                summary_path,
                {
                    "total_results": 3,
                    "success_count": 2,
                    "failure_count": 1,
                    "timeout_count": 0,
                    "total_run_time_seconds": 12.5,
                    "results": [
                        no_valid_put_result(executable_path="/tmp/bad_plan.py"),
                        good_result,
                        failed_result,
                    ],
                },
            )

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(
                read_json(output_path),
                [
                    {
                        "floorplan": "FloorPlan2",
                        "object": "Pot",
                        "receptacle": "Fridge",
                    }
                ],
            )

            summary = read_json(summary_path)
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(summary["timeout_count"], 0)
            self.assertEqual(summary["total_run_time_seconds"], 12.5)
            self.assertEqual(
                [result["executable_path"] for result in summary["results"]],
                ["/tmp/good_plan.py", "/tmp/failed_plan.py"],
            )

    def test_main_extracts_and_prunes_nested_parallel_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            summary_path = root / "summary.json"
            output_path = root / "no_valid_positions.json"
            pass_result = {
                "floor_plan": "2",
                "status": "success",
                "task_index": 1,
                "tc": 2,
                "total": 2,
            }
            failed_result = {
                "floor_plan": "2",
                "status": "error",
                "task_index": 2,
                "tc": 0,
                "total": 2,
            }
            write_json(
                summary_path,
                {
                    "created_at": "20260617_120000",
                    "success_count": 2,
                    "failure_count": 1,
                    "all_pass_count": 2,
                    "pass_one_count": 2,
                    "summaries": [
                        {
                            "floor_plan": "2",
                            "task_count": 3,
                            "success_count": 2,
                            "failure_count": 1,
                            "all_pass_count": 2,
                            "pass_one_count": 2,
                            "results": [
                                no_valid_put_result(
                                    executable_path="/tmp/nested_bad_plan.py",
                                    floorplan="FloorPlan2",
                                    held_object="Bowl",
                                    receptacle="Cabinet",
                                ),
                                pass_result,
                                failed_result,
                            ],
                        }
                    ],
                },
            )

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(["--input", str(summary_path), "--output", str(output_path)])

            self.assertEqual(result, 0)
            self.assertEqual(
                read_json(output_path),
                [
                    {
                        "floorplan": "FloorPlan2",
                        "object": "Bowl",
                        "receptacle": "Cabinet",
                    }
                ],
            )

            summary = read_json(summary_path)
            self.assertEqual(summary["created_at"], "20260617_120000")
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(summary["all_pass_count"], 1)
            self.assertEqual(summary["pass_one_count"], 1)

            floor_summary = summary["summaries"][0]
            self.assertEqual(floor_summary["task_count"], 2)
            self.assertEqual(floor_summary["success_count"], 1)
            self.assertEqual(floor_summary["failure_count"], 1)
            self.assertEqual(floor_summary["all_pass_count"], 1)
            self.assertEqual(floor_summary["pass_one_count"], 1)
            self.assertEqual(
                [result["task_index"] for result in floor_summary["results"]],
                [1, 2],
            )


if __name__ == "__main__":
    unittest.main()
