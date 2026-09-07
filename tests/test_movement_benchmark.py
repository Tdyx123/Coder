import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from benchmark_movement_modes import (
    acceptance_failures,
    build_benchmark_report,
    select_manifest,
)


def action(action_type):
    return {"action_type": action_type, "parameters": {}}


def stages_for_stratum(stratum):
    if stratum == "single_navigation":
        queues = {
            "robot1": [action("GoToObject")],
            "robot2": [action("OpenObject")],
        }
    elif stratum == "concurrent_goto":
        queues = {
            "robot1": [action("GoToObject")],
            "robot2": [action("GoToObject")],
        }
    elif stratum == "multiple_waves":
        queues = {
            "robot1": [
                action("GoToObject"),
                action("OpenObject"),
                action("GoToObject"),
            ],
            "robot2": [
                action("OpenObject"),
                action("OpenObject"),
                action("OpenObject"),
            ],
        }
    else:
        raise AssertionError(stratum)
    return [{"stage_id": "Phase 1", "robot_action_queues": queues}]


class MovementBenchmarkSelectionTest(unittest.TestCase):
    def write_candidate(self, root, floor_plan, stratum, ordinal):
        task_id = f"FloorPlan{floor_plan}_task_{ordinal:02d}"
        task_file = root / "data" / f"FloorPlan{floor_plan}_{stratum}.jsonl"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(
            json.dumps({"task": task_id, "robot list": [1, 2]}) + "\n",
            encoding="utf-8",
        )
        executable = (
            root
            / "logs"
            / f"floor_{floor_plan}"
            / stratum
            / "plan_to_code"
            / "executable_plan.py"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        bundle = {
            "task": task_id,
            "task_plan": {
                "task_id": task_id,
                "stages": stages_for_stratum(stratum),
            },
        }
        executable.write_text(
            "\n".join(
                [
                    "from executor_system.generated_plan_runtime import main as run_generated_plan",
                    f"BUNDLE_DATA = {bundle!r}",
                    f"TASK_FILE = {str(task_file)!r}",
                    "TASK_INDEX = 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return executable

    def test_selector_chooses_every_category_and_stratum(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            floors = [1, 201, 301, 401]
            strata = [
                "single_navigation",
                "concurrent_goto",
                "multiple_waves",
            ]
            for floor_plan in floors:
                for ordinal, stratum in enumerate(strata, start=1):
                    self.write_candidate(root, floor_plan, stratum, ordinal)

            manifest = select_manifest(root / "logs", repo_root=root)

        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(manifest["cases"]), 12)
        self.assertEqual(
            {
                (case["category"], case["stratum"])
                for case in manifest["cases"]
            },
            {
                (category, stratum)
                for category in ("kitchen", "living_room", "bedroom", "bathroom")
                for stratum in strata
            },
        )
        self.assertTrue(all(case["robot_count"] == 2 for case in manifest["cases"]))


class MovementBenchmarkThresholdTest(unittest.TestCase):
    def baseline_inputs(self):
        manifest = {
            "version": 1,
            "cases": [
                {
                    "category": "kitchen",
                    "stratum": "single_navigation",
                    "path": f"case_{index}.py",
                    "task_id": f"FloorPlan1_task_{index}",
                    "robot_count": 2,
                }
                for index in range(12)
            ],
        }
        results = []
        for case in manifest["cases"]:
            for mode in ("teleport", "step"):
                results.append(
                    {
                        **case,
                        "mode": mode,
                        "status": "success",
                        "timed_out": False,
                        "gcr": 0.80 if mode == "teleport" else 0.78,
                        "tc": 1.0,
                        "sr": 1,
                        "run_time_seconds": 1.0,
                        "navigation_metrics": {
                            "requests": 1,
                            "successes": 1,
                            "failures": 0,
                            "action_counts": {},
                            "planning_durations_seconds": (
                                [0.10] if mode == "step" else []
                            ),
                        },
                    }
                )
        return manifest, results

    def assert_failure(self, mutate, expected_code):
        manifest, results = self.baseline_inputs()
        mutate(results)
        report = build_benchmark_report(manifest, results)
        failures = acceptance_failures(report)
        self.assertTrue(
            any(failure.get("code") == expected_code for failure in failures),
            failures,
        )

    def test_acceptance_checks_each_required_threshold(self):
        mutations = {
            "step_navigation_success_rate": lambda results: [
                item["navigation_metrics"].update(successes=0, failures=1)
                for item in results
                if item["mode"] == "step" and item["path"] in {"case_0.py", "case_1.py"}
            ],
            "step_successful_navigation_cases": lambda results: [
                item["navigation_metrics"].update(successes=0, failures=1)
                for item in results
                if item["mode"] == "step" and int(item["path"].split("_")[1].split(".")[0]) >= 9
            ],
            "step_navigation_teleports": lambda results: next(
                item for item in results if item["mode"] == "step"
            )["navigation_metrics"]["action_counts"].update(Teleport=1),
            "success_gcr_gap": lambda results: [
                item.update(gcr=0.70)
                for item in results
                if item["mode"] == "step"
            ],
            "planner_fixture_p95": lambda results: [
                item["navigation_metrics"].update(
                    planning_durations_seconds=[0.30]
                )
                for item in results
                if item["mode"] == "step"
            ],
            "step_subprocess_timeout": lambda results: next(
                item for item in results if item["mode"] == "step"
            ).update(timed_out=True, status="timeout"),
        }
        for expected_code, mutate in mutations.items():
            with self.subTest(expected_code=expected_code):
                self.assert_failure(mutate, expected_code)

    def test_valid_synthetic_report_passes_all_thresholds(self):
        manifest, results = self.baseline_inputs()

        report = build_benchmark_report(manifest, copy.deepcopy(results))

        self.assertEqual(acceptance_failures(report), [])

    def test_report_keeps_v2_result_groups_separate_by_metric_contract(self):
        manifest, results = self.baseline_inputs()
        results[0].update(
            metrics_schema_version=2,
            evaluation_version="fixed_goals_v2",
            execution_policy="legacy",
            evaluation_status="valid",
            task_success=False,
        )

        report = build_benchmark_report(manifest, results)

        self.assertTrue(
            any(
                group["metrics_schema_version"] == 2
                and group["valid_evaluation_count"] == 1
                and group["task_success_count"] == 0
                for group in report["result_groups"]
            )
        )


if __name__ == "__main__":
    unittest.main()
