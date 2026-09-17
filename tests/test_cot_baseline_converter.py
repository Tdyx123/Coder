import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "baselines" / "COT.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.parallel_runner import is_runner_compatible_executable


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_cot_module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError(f"COT baseline entrypoint is missing: {SCRIPT_PATH}")
    module_name = "test_cot_baseline_converter_module"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_bundle_data(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "BUNDLE_DATA"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("BUNDLE_DATA assignment not found")


def write_dataset(repo_root: Path, task_count: int = 1) -> Path:
    dataset_path = repo_root / "data" / "unit_set" / "FloorPlan1.jsonl"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(task_count):
        records.append(
            {
                "task": "fill the mug with water" if index == 0 else "failed task",
                "robot list": [1, 9],
                "object_states": [
                    {"name": "Mug", "contains": [], "states": ["FILLED"]}
                ],
                "trans": 0,
                "min_trans": 10,
            }
        )
    dataset_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return dataset_path


def write_success_run(parallel_run: Path) -> tuple[Path, dict]:
    relative_run = Path(
        "FloorPlan1/task_001/unit_set/fill_the_mug_with_water/20260829_001"
    )
    task_run_dir = parallel_run / relative_run
    task = "fill the mug with water"
    robots = [
        {
            "symbol": "robot1",
            "skills": ["GoToObject", "OpenObject", "FillWater"],
        },
        {
            "symbol": "robot2",
            "skills": ["GoToObject", "PickupObject", "CleanObject"],
        },
    ]
    write_json(
        task_run_dir / "00_inputs" / "task_context.json",
        {
            "task": task,
            "floor_plan": "FloorPlan1",
            "task_index": 0,
            "test_set": "unit_set",
            "goal_states": [
                {"name": "Mug", "contains": [], "states": ["FILLED"]}
            ],
            "robots": robots,
            "objects": [
                {"symbol": "drawer", "label": "Drawer"},
                {"symbol": "mug", "label": "Mug", "mass": 1.0},
                {"symbol": "sinkbasin", "label": "SinkBasin"},
            ],
        },
    )
    write_json(
        task_run_dir / "02_plan" / "01_final_plan.json",
        {
            "plan": [
                {
                    "action": "GoToObject",
                    "arguments": ["robot1", "drawer"],
                    "reasoning_step": 1,
                },
                {
                    "action": "OpenObject",
                    "arguments": ["robot1", "drawer"],
                    "reasoning_step": 1,
                },
                {
                    "action": "GoToObject",
                    "arguments": ["robot9", "mug"],
                    "reasoning_step": 2,
                },
                {
                    "action": "PickupObject",
                    "arguments": ["robot9", "mug", "countertop"],
                    "reasoning_step": 2,
                },
                {
                    "action": "CleanObject",
                    "arguments": ["robot9", "mug", "sinkbasin"],
                    "reasoning_step": 2,
                },
                {
                    "action": "GoToObject",
                    "arguments": ["robot1", "sinkbasin"],
                    "reasoning_step": 2,
                },
                {
                    "action": "FillWater",
                    "arguments": ["robot1", "sinkbasin", "mug"],
                    "reasoning_step": 2,
                },
            ]
        },
    )
    write_json(task_run_dir / "02_plan" / "03_validation.json", {"valid": True, "diagnostics": []})
    manifest = {
        "schema_version": "1.0",
        "framework": "COT direct planner",
        "status": "success",
        "task": task,
        "floor_plan": "FloorPlan1",
        "task_index": 0,
        "test_set": "unit_set",
        "model": "fixture-model",
        "resources": {
            "dataset": "/stale/repository/data/unit_set/FloorPlan1.jsonl"
        },
        "artifacts": {
            "task_context": "00_inputs/task_context.json",
            "validation": "02_plan/03_validation.json",
            "final_plan_json": "02_plan/01_final_plan.json",
        },
    }
    write_json(task_run_dir / "run_manifest.json", manifest)
    return task_run_dir, {
        "floor_plan": "FloorPlan1",
        "task_index": 0,
        "task": task,
        "status": "success",
        "run_dir": str(relative_run),
        "manifest": manifest,
    }


def write_parallel_summary(
    parallel_run: Path,
    task_rows: list[dict],
    floor_plan: str = "FloorPlan1",
) -> None:
    status_counts = {}
    for row in task_rows:
        status = row["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    floor_summary = {
        "floor_plan": floor_plan,
        "test_set": "unit_set",
        "model": "fixture-model",
        "total_tasks": len(task_rows),
        "status_counts": status_counts,
        "llm_token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
            "missing_usage_count": 0,
        },
        "tasks": task_rows,
        "summary_path": f"{floor_plan}/summary.json",
    }
    write_json(parallel_run / floor_plan / "summary.json", floor_summary)
    write_json(
        parallel_run / "summary.json",
        {
            "repo_root": "/stale/repository",
            "output_root": "/stale/repository/parallel_runs/pddlrun_cot_fixture",
            "test_set": "unit_set",
            "model": "fixture-model",
            "floor_plans": [floor_plan],
            "total_tasks": len(task_rows),
            "status_counts": status_counts,
            "llm_token_usage": floor_summary["llm_token_usage"],
            "summaries": [
                {"floor_plan": floor_plan, "summary": f"{floor_plan}/summary.json"}
            ],
        },
    )


def failed_task_row() -> dict:
    return {
        "floor_plan": "FloorPlan1",
        "task_index": 1,
        "task": "failed task",
        "status": "llm_error",
        "run_dir": "FloorPlan1/task_002/unit_set/failed_task/20260829_002",
        "manifest": {
            "schema_version": "1.0",
            "framework": "COT direct planner",
            "status": "llm_error",
            "task": "failed task",
            "floor_plan": "FloorPlan1",
            "task_index": 1,
            "test_set": "unit_set",
            "model": "fixture-model",
            "error": {"type": "llm_error", "message": "Rate limit exceeded"},
        },
    }


class CotBaselineConverterTests(unittest.TestCase):
    def test_successful_plan_generates_ordered_runner_bundle(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            dataset_path = write_dataset(repo_root)
            task_run_dir, task_row = write_success_run(parallel_run)
            task_row["run_dir"] = f"/stale/repository/{task_row['run_dir']}"
            write_parallel_summary(parallel_run, [task_row])

            result_code = cot.main(["--root", str(baseline_root)])

            self.assertEqual(result_code, 0)
            executable_path = task_run_dir / "plan_to_code" / "executable_plan.py"
            self.assertTrue(executable_path.is_file())
            self.assertTrue(is_runner_compatible_executable(executable_path))
            bundle = load_bundle_data(executable_path)
            self.assertEqual(bundle["task"], "fill the mug with water")
            self.assertEqual(bundle["gcr"], [{"name": "Mug", "contains": [], "states": ["FILLED"]}])
            self.assertEqual(bundle["no_trans"], 7)
            self.assertEqual(
                [stage["stage_id"] for stage in bundle["task_plan"]["stages"]],
                ["COT Step 1 Segment 1", "COT Step 2 Segment 2", "COT Step 2 Segment 3"],
            )
            queues = [stage["robot_action_queues"] for stage in bundle["task_plan"]["stages"]]
            self.assertEqual(list(queues[0]), ["robot1"])
            self.assertEqual(list(queues[1]), ["robot9"])
            self.assertEqual(list(queues[2]), ["robot1"])
            self.assertEqual(
                queues[1]["robot9"][-1]["parameters"]["args"],
                ["Mug"],
            )
            self.assertEqual(
                queues[2]["robot1"][-1]["parameters"]["args"],
                ["SinkBasin", "Mug"],
            )

            executable_text = executable_path.read_text(encoding="utf-8")
            self.assertIn(f"TASK_FILE = {str(dataset_path)!r}", executable_text)
            summary = json.loads(
                (baseline_root / "plan_to_code_results" / "plan_to_code_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(summary["skipped_generations"], 0)
            self.assertEqual(summary["error_generations"], 0)

    def test_llm_error_is_reported_as_skipped_without_generated_code(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            write_dataset(repo_root, task_count=2)
            _task_run_dir, success_row = write_success_run(parallel_run)
            skipped_row = failed_task_row()
            write_parallel_summary(parallel_run, [success_row, skipped_row])

            result_code = cot.main(["--root", str(baseline_root)])

            self.assertEqual(result_code, 0)
            skipped_output = (
                parallel_run
                / skipped_row["run_dir"]
                / "plan_to_code"
                / "executable_plan.py"
            )
            self.assertFalse(skipped_output.exists())
            output_root = baseline_root / "plan_to_code_results"
            summary = json.loads(
                (output_root / "plan_to_code_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(summary["skipped_generations"], 1)
            self.assertEqual(summary["error_generations"], 0)
            results = json.loads(
                (output_root / "plan_to_code_results.json").read_text(encoding="utf-8")
            )
            self.assertEqual([result["status"] for result in results], ["success", "skipped"])
            self.assertEqual(results[1]["skip_reason"], "source status: llm_error")
            self.assertNotIn("generated", results[1])

    def test_skipped_task_still_requires_a_local_run_dir(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            skipped_row = failed_task_row()
            skipped_row["run_dir"] = ""
            write_parallel_summary(parallel_run, [skipped_row])

            result_code = cot.main(["--root", str(baseline_root)])

            self.assertEqual(result_code, 1)
            self.assertFalse((baseline_root / "plan_to_code_results").exists())

    def test_unsupported_action_fails_conversion_without_writing_code(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            write_dataset(repo_root)
            task_run_dir, task_row = write_success_run(parallel_run)
            write_parallel_summary(parallel_run, [task_row])
            write_json(
                task_run_dir / "02_plan" / "01_final_plan.json",
                {
                    "plan": [
                        {
                            "action": "InventedAction",
                            "arguments": ["robot1", "mug"],
                            "reasoning_step": 1,
                        }
                    ]
                },
            )

            result_code = cot.main(["--root", str(baseline_root)])

            self.assertEqual(result_code, 1)
            self.assertFalse(
                (task_run_dir / "plan_to_code" / "executable_plan.py").exists()
            )
            results = json.loads(
                (
                    baseline_root
                    / "plan_to_code_results"
                    / "plan_to_code_results.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(results[0]["status"], "failed")
            self.assertIn("Unsupported COT action", results[0]["error"])

    def test_context_skills_do_not_override_catalog_skills(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            write_dataset(repo_root)
            task_run_dir, task_row = write_success_run(parallel_run)
            write_parallel_summary(parallel_run, [task_row])
            context_path = task_run_dir / "00_inputs" / "task_context.json"
            task_context = json.loads(context_path.read_text(encoding="utf-8"))
            task_context["robots"][0]["skills"] = 7
            write_json(context_path, task_context)

            result_code = cot.main(["--root", str(baseline_root)])

            self.assertEqual(result_code, 0)
            results = json.loads(
                (
                    baseline_root
                    / "plan_to_code_results"
                    / "plan_to_code_results.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(results[0]["status"], "success")

    def test_dry_run_with_limit_does_not_write_code_or_summaries(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            write_dataset(repo_root, task_count=2)
            task_run_dir, success_row = write_success_run(parallel_run)
            write_parallel_summary(parallel_run, [success_row, failed_task_row()])

            result_code = cot.main(
                ["--root", str(baseline_root), "--limit", "1", "--dry-run"]
            )

            self.assertEqual(result_code, 0)
            self.assertFalse(
                (task_run_dir / "plan_to_code" / "executable_plan.py").exists()
            )
            self.assertFalse((baseline_root / "plan_to_code_results").exists())

    def test_floor_filter_can_select_no_tasks(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            baseline_root = repo_root / "baselines" / "COT"
            parallel_run = baseline_root / "parallel_runs" / "pddlrun_cot_fixture"
            write_dataset(repo_root)
            task_run_dir, success_row = write_success_run(parallel_run)
            write_parallel_summary(parallel_run, [success_row])

            result_code = cot.main(
                ["--root", str(baseline_root), "--floor-plan", "FloorPlan2"]
            )

            self.assertEqual(result_code, 0)
            self.assertFalse(
                (task_run_dir / "plan_to_code" / "executable_plan.py").exists()
            )
            summary = json.loads(
                (
                    baseline_root
                    / "plan_to_code_results"
                    / "plan_to_code_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 0)
            self.assertEqual(summary["successful_generations"], 0)
            self.assertEqual(summary["skipped_generations"], 0)
            self.assertEqual(summary["error_generations"], 0)

    def test_multiple_source_summaries_sort_by_floor_then_task(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary_root = Path(tmp_dir) / "parallel_runs"
            rows = []
            for run_name, floor_plan in (
                ("a_source", "FloorPlan2"),
                ("z_source", "FloorPlan1"),
            ):
                row = failed_task_row()
                row["floor_plan"] = floor_plan
                row["task_index"] = 0
                row["run_dir"] = f"{floor_plan}/task_001/run"
                row["manifest"]["floor_plan"] = floor_plan
                row["manifest"]["task_index"] = 0
                write_parallel_summary(
                    summary_root / run_name,
                    [row],
                    floor_plan=floor_plan,
                )
                rows.append(row)

            runs = cot.collect_summary_runs(
                cot.discover_top_level_summaries(summary_root)
            )

            self.assertEqual(
                [run.metadata["floor_plan"] for run in runs],
                ["FloorPlan1", "FloorPlan2"],
            )

    def test_dataset_path_rejects_nonlocal_components(self):
        cot = load_cot_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)

            with self.assertRaises(cot.CotConversionError):
                cot.dataset_path_for_run(repo_root, "1", "../../outside")
            with self.assertRaises(cot.CotConversionError):
                cot.dataset_path_for_run(repo_root, "../1", "unit_set")


if __name__ == "__main__":
    unittest.main()
