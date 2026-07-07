import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ai2thor_object_cache import get_ai2_thor_objects_cache_path
from file_processor import FileProcessor as SharedFileProcessor
from file_processor import PDDLError as SharedPDDLError
try:
    import pddlrun_llmseparate_v2
except ModuleNotFoundError as exc:
    if exc.name != "pddlrun_llmseparate_v2":
        raise
    pddlrun_llmseparate_v2 = None
from pddlrun_llmseparate import (
    FileProcessor,
    PDDLPlanner,
    PDDLError,
    RunConfig,
    TaskManager,
    build_robot_domain_name_map,
    build_robot_team,
    load_run_config,
    parse_arguments,
    summarize_llm_token_usage,
)
from pddl_rag import PDDLRagTimeoutError, RAG_PROMPT_TITLE
from sft_generator import (
    check_allocate_assignment_count,
    check_decompose_subtask_count,
    check_planner_plan_count,
)
from migrate_intermediate_runs_layout import main as migrate_intermediate_runs_main
from parsing_utils import ParsingUtils
from run_config import DEFAULT_RUN_CONFIG as SHARED_DEFAULT_RUN_CONFIG
from run_config import RunConfig as SharedRunConfig
from run_config import apply_allocate_rag_cli_override
from run_config import apply_decompose_rag_cli_override


class FixedDatetime:
    @classmethod
    def now(cls):
        return datetime(2026, 5, 21, 9, 30, 15, 123456)


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def create_old_intermediate_run(root: Path, dirname: str, created_at: str, task: str) -> Path:
    run_dir = root / "logs" / "intermediate_runs" / dirname
    run_dir.mkdir(parents=True)
    (run_dir / "artifact.txt").write_text(dirname, encoding="utf-8")
    write_json(
        run_dir / "run_manifest.json",
        {
            "created_at": created_at,
            "task": task,
            "storage_base_dir": str(root / "logs" / "intermediate_runs"),
        },
    )
    return run_dir


def create_parallel_summary(
    root: Path,
    run_name: str,
    test_set,
    floor_plan: str,
    results,
) -> Path:
    parallel_run = root / "parallel_runs" / run_name
    floor_plan_dir = parallel_run / f"FloorPlan{str(floor_plan).replace('FloorPlan', '')}"
    floor_summary = {
        "floor_plan": floor_plan,
        "task_count": len(results),
        "success_count": len(results),
        "failure_count": 0,
        "all_pass_count": 0,
        "pass_one_count": 0,
        "results": results,
    }
    top_summary = {
        "created_at": "20260521_000000",
        "repo_root": str(root),
        "output_root": str(parallel_run),
        "test_set": test_set,
        "floor_plan_count": 1,
        "success_count": len(results),
        "failure_count": 0,
        "all_pass_count": 0,
        "pass_one_count": 0,
        "summaries": [floor_summary],
    }
    write_json(parallel_run / "summary.json", top_summary)
    write_json(floor_plan_dir / "summary.json", floor_summary)
    return parallel_run / "summary.json"


def rag_doc(doc_id: str, stage: str, query_text: str, content: str):
    return {
        "id": doc_id,
        "stage": stage,
        "query_text": query_text,
        "content": content,
        "metadata": {
            "task": f"task {doc_id}",
            "task_run_dir": f"/tmp/{doc_id}",
        },
        "quality": "success",
        "retrieval_eligible": True,
    }


def rag_config_values(root: Path, records, section: str = "decompose_rag"):
    rag_dir = root / "data" / "rag"
    prefix = "clean" if section == "decompose_rag" else section
    corpus_path = rag_dir / f"{prefix}.jsonl"
    index_path = rag_dir / f"{prefix}_index.json"
    write_jsonl(corpus_path, records)
    index_path.write_text("{}", encoding="utf-8")
    return {
        section: {
            "enabled": True,
            "corpus_path": str(corpus_path),
            "index_path": str(index_path),
            "runtime_db_path": str(rag_dir / f"{prefix}_runtime.sqlite"),
            "top_k": 1,
        }
    }


class PDDLRunConfigTests(unittest.TestCase):
    def setUp(self):
        if pddlrun_llmseparate_v2 is None and self._testMethodName.startswith("test_v2_"):
            self.skipTest("pddlrun_llmseparate_v2 source module is not present")

    def test_summarize_llm_token_usage_sums_valid_usage_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            llm_calls = root / "llm_calls.jsonl"
            llm_calls.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "usage": {
                                    "prompt_tokens": 2,
                                    "completion_tokens": 3,
                                    "total_tokens": 5,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "usage": {
                                    "prompt_tokens": None,
                                    "completion_tokens": 4,
                                    "total_tokens": 4,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "usage": {
                                    "prompt_tokens": 7,
                                    "completion_tokens": "bad",
                                    "total_tokens": None,
                                }
                            }
                        ),
                        json.dumps({"messages": []}),
                        "not json",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                summarize_llm_token_usage(llm_calls),
                {
                    "prompt_tokens": 9,
                    "completion_tokens": 7,
                    "total_tokens": 9,
                },
            )

    def test_summarize_llm_token_usage_returns_zero_shape_for_missing_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(
                summarize_llm_token_usage(Path(tmp_dir) / "missing.jsonl"),
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )

    def test_apply_decompose_rag_cli_override_sets_enabled_flag(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = RunConfig(tmp_dir)

            self.assertFalse(config.get("decompose_rag", "enabled"))
            self.assertIs(apply_decompose_rag_cli_override(config, True), config)
            self.assertTrue(config.get("decompose_rag", "enabled"))
            apply_decompose_rag_cli_override(config, False)
            self.assertFalse(config.get("decompose_rag", "enabled"))

    def test_apply_allocate_rag_cli_override_sets_enabled_flag(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = RunConfig(tmp_dir)

            self.assertFalse(config.get("allocate_rag", "enabled"))
            self.assertIs(apply_allocate_rag_cli_override(config, True), config)
            self.assertTrue(config.get("allocate_rag", "enabled"))
            apply_allocate_rag_cli_override(config, False)
            self.assertFalse(config.get("allocate_rag", "enabled"))

    def test_single_runner_cli_defaults_to_decompose_rag_disabled(self):
        with patch("pddlrun_llmseparate.get_available_models", return_value=["deepseek-chat"]):
            args = parse_arguments(["--floor-plan", "6"])

        self.assertFalse(args.decompose_rag)
        self.assertFalse(args.allocate_rag)

    def test_single_runner_cli_decompose_rag_can_be_enabled_and_disabled(self):
        with patch("pddlrun_llmseparate.get_available_models", return_value=["deepseek-chat"]):
            enabled_args = parse_arguments(["--floor-plan", "6", "--decompose-rag"])
            disabled_args = parse_arguments(["--floor-plan", "6", "--no-decompose-rag"])

        self.assertTrue(enabled_args.decompose_rag)
        self.assertFalse(disabled_args.decompose_rag)

    def test_single_runner_cli_allocate_rag_can_be_enabled_and_disabled(self):
        with patch("pddlrun_llmseparate.get_available_models", return_value=["deepseek-chat"]):
            enabled_args = parse_arguments(["--floor-plan", "6", "--allocate-rag"])
            disabled_args = parse_arguments(["--floor-plan", "6", "--no-allocate-rag"])

        self.assertTrue(enabled_args.allocate_rag)
        self.assertFalse(disabled_args.allocate_rag)

    def test_load_run_config_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "run.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "storage": {"base_dir": "custom/intermediate"},
                        "data": {
                            "dataset_dir": "fixtures/data",
                            "ai2thor_objects_cache_dir": "cache/objects",
                            "ai2thor_objects_file": "cache/all_objects.json",
                        },
                        "planner": {"executable": "planner/fd.py"},
                    }
                ),
                encoding="utf-8",
            )

            config = load_run_config(root, config_path)

            self.assertEqual(config.storage_base_dir, root / "custom" / "intermediate")
            self.assertEqual(config.dataset_file("final_test", "FloorPlan15"), root / "fixtures" / "data" / "final_test" / "FloorPlan15.jsonl")
            self.assertEqual(config.ai2thor_objects_cache_dir, root / "cache" / "objects")
            self.assertEqual(config.ai2thor_objects_file, root / "cache" / "all_objects.json")
            self.assertEqual(config.planner_executable, root / "planner" / "fd.py")

    def test_available_test_sets_lists_floor_plan_dataset_dirs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_root = root / "fixtures" / "data"
            final_test = dataset_root / "final_test"
            new_test = dataset_root / "final_test_new_1"
            ignored = dataset_root / "notes"
            final_test.mkdir(parents=True)
            new_test.mkdir()
            ignored.mkdir()
            (final_test / "FloorPlan15.jsonl").write_text("{}\n", encoding="utf-8")
            (new_test / "FloorPlan6.jsonl").write_text("{}\n", encoding="utf-8")
            (ignored / "README.txt").write_text("not a dataset\n", encoding="utf-8")
            config_path = root / "run.yaml"
            config_path.write_text(
                yaml.safe_dump({"data": {"dataset_dir": "fixtures/data"}}),
                encoding="utf-8",
            )

            config = load_run_config(root, config_path)

            self.assertEqual(config.available_test_sets(), ["final_test", "final_test_new_1"])
            self.assertEqual(
                config.dataset_file("final_test_new_1", "FloorPlan6"),
                dataset_root / "final_test_new_1" / "FloorPlan6.jsonl",
            )

    def test_missing_config_uses_current_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = load_run_config(root, root / "missing.yaml")

            self.assertEqual(config.storage_base_dir, root / "logs" / "intermediate_runs")
            self.assertEqual(config.task_manager_runs_dir, root / "logs" / "task_manager_runs")
            self.assertEqual(config.dataset_file("final_test", 6), root / "data" / "final_test" / "FloorPlan6.jsonl")
            self.assertEqual(config.ai2thor_objects_cache_dir, root / "data" / "ai2thor_objects_cache")
            self.assertEqual(config.ai2thor_objects_file, root / "data" / "all_ai2thor_objects.json")

    def test_prepare_task_run_dir_uses_dataset_floorplan_layout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(root, values={"storage": {"base_dir": "runs"}})
            manager = TaskManager(
                str(root),
                "test-model",
                config=config,
                test_set="final_test",
                floor_plan="FloorPlan6",
            )

            with patch("pddlrun_llmseparate.datetime", FixedDatetime):
                manager._prepare_task_run_dir(
                    0,
                    "Pick up the apple",
                    robots=[],
                    objects_ai="objects=[]",
                    domain_content="(define (domain test))",
                )

            expected_run_dir = root / "runs" / "final_test___6" / "Pick_up_the_apple" / "20260521_001"
            self.assertEqual(Path(manager.current_task_run_dir), expected_run_dir)
            self.assertEqual(
                Path(manager.current_generated_subtask_dir),
                expected_run_dir / "06_split" / "generated_subtask",
            )
            manifest = json.loads((expected_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["task_index"], 0)
            self.assertEqual(manifest["test_set"], "final_test")
            self.assertEqual(manifest["floor_plan"], "6")
            self.assertEqual(manifest["run_date"], "20260521")
            self.assertEqual(manifest["run_sequence"], 1)
            self.assertEqual(manifest["task_run_dir"], str(expected_run_dir))

    def test_prepare_task_run_dir_uses_manifest_task_index_override(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(root, values={"storage": {"base_dir": "runs"}})
            manager = TaskManager(
                str(root),
                "test-model",
                config=config,
                test_set="final_test",
                floor_plan="FloorPlan6",
            )

            with patch("pddlrun_llmseparate.datetime", FixedDatetime):
                manager._prepare_task_run_dir(
                    0,
                    "Pick up the apple",
                    robots=[],
                    objects_ai="objects=[]",
                    domain_content="(define (domain test))",
                    manifest_task_index=42,
                )

            expected_run_dir = root / "runs" / "final_test___6" / "Pick_up_the_apple" / "20260521_001"
            manifest = json.loads((expected_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["task_index"], 42)

    def test_prepare_task_run_dir_increments_sequence_and_normalizes_floorplan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(root, values={"storage": {"base_dir": "runs"}})
            first_manager = TaskManager(
                str(root),
                "test-model",
                config=config,
                test_set="final_test",
                floor_plan="FloorPlan6",
            )
            second_manager = TaskManager(
                str(root),
                "test-model",
                config=config,
                test_set="final_test",
                floor_plan="6",
            )

            with patch("pddlrun_llmseparate.datetime", FixedDatetime):
                first_manager._prepare_task_run_dir(0, "Pick up the apple", [], "objects=[]", "domain")
                second_manager._prepare_task_run_dir(0, "Pick up the apple", [], "objects=[]", "domain")

            first_run_dir = Path(first_manager.current_task_run_dir)
            second_run_dir = Path(second_manager.current_task_run_dir)
            self.assertEqual(first_run_dir.parent, second_run_dir.parent)
            self.assertEqual(first_run_dir.name, "20260521_001")
            self.assertEqual(second_run_dir.name, "20260521_002")
            self.assertEqual(first_run_dir.parent.parent.name, "final_test___6")

    def test_v2_prepare_task_run_dir_uses_dataset_floorplan_layout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = pddlrun_llmseparate_v2.RunConfig(root, values={"storage": {"base_dir": "runs"}})
            manager = pddlrun_llmseparate_v2.TaskManager(
                str(root),
                "test-model",
                config=config,
                test_set="sample_set",
                floor_plan="FloorPlan12",
            )

            with patch("pddlrun_llmseparate_v2.datetime", FixedDatetime):
                manager._prepare_task_run_dir(0, "Open the fridge", [], "objects=[]", "domain")

            expected_run_dir = root / "runs" / "sample_set___12" / "Open_the_fridge" / "20260521_001"
            self.assertEqual(Path(manager.current_task_run_dir), expected_run_dir)
            manifest = json.loads((expected_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["test_set"], "sample_set")
            self.assertEqual(manifest["floor_plan"], "12")
            self.assertEqual(manifest["run_sequence"], 1)

    def test_migrate_intermediate_runs_dry_run_and_apply_updates_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task = "Pick up the apple"
            first_old_run = create_old_intermediate_run(
                root,
                "001_pick_up_the_apple_20260521_010101_a_20260521_010102",
                "20260521_010102",
                task,
            )
            second_old_run = create_old_intermediate_run(
                root,
                "001_pick_up_the_apple_20260521_020201_b_20260521_020202",
                "20260521_020202",
                task,
            )
            summary_path = create_parallel_summary(
                root,
                "pddlrun_llmseparate_20260521_000000",
                "sample_set",
                "FloorPlan6",
                [
                    {
                        "floor_plan": "FloorPlan6",
                        "model": "test-model",
                        "task_index": 0,
                        "task": task,
                        "status": "success",
                        "duration_seconds": 1.0,
                        "task_run_dir": str(first_old_run),
                    },
                    {
                        "floor_plan": "6",
                        "model": "test-model",
                        "task_index": 1,
                        "task": task,
                        "status": "success",
                        "duration_seconds": 1.0,
                        "task_run_dir": str(second_old_run),
                    },
                ],
            )

            migrate_intermediate_runs_main(["--summary", str(summary_path)], base_path=root)

            expected_parent = root / "logs" / "intermediate_runs" / "sample_set___6" / "Pick_up_the_apple"
            self.assertTrue(first_old_run.exists())
            self.assertTrue(second_old_run.exists())
            self.assertFalse((expected_parent / "20260521_001").exists())
            self.assertFalse((summary_path.with_name("summary.json.bak")).exists())

            migrate_intermediate_runs_main(["--summary", str(summary_path), "--apply"], base_path=root)

            first_new_run = expected_parent / "20260521_001"
            second_new_run = expected_parent / "20260521_002"
            self.assertFalse(first_old_run.exists())
            self.assertFalse(second_old_run.exists())
            self.assertEqual((first_new_run / "artifact.txt").read_text(encoding="utf-8"), first_old_run.name)
            self.assertEqual((second_new_run / "artifact.txt").read_text(encoding="utf-8"), second_old_run.name)

            top_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            top_results = top_summary["summaries"][0]["results"]
            self.assertEqual(top_results[0]["task_run_dir"], str(first_new_run))
            self.assertEqual(top_results[1]["task_run_dir"], str(second_new_run))
            floor_summary_path = summary_path.parent / "FloorPlan6" / "summary.json"
            floor_summary = json.loads(floor_summary_path.read_text(encoding="utf-8"))
            self.assertEqual(floor_summary["results"][0]["task_run_dir"], str(first_new_run))
            self.assertEqual(floor_summary["results"][1]["task_run_dir"], str(second_new_run))
            self.assertTrue((summary_path.parent / "summary.json.bak").exists())
            self.assertTrue((floor_summary_path.parent / "summary.json.bak").exists())

            first_manifest = json.loads((first_new_run / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(first_manifest["task_run_dir"], str(first_new_run))
            self.assertEqual(first_manifest["test_set"], "sample_set")
            self.assertEqual(first_manifest["floor_plan"], "6")
            self.assertEqual(first_manifest["run_date"], "20260521")
            self.assertEqual(first_manifest["run_sequence"], 1)
            self.assertTrue((first_new_run / "run_manifest.json.bak").exists())

    def test_migrate_intermediate_runs_uses_fallback_test_set_when_summary_missing_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task = "Open the fridge"
            old_run = create_old_intermediate_run(
                root,
                "001_open_the_fridge_20260521_010101_a_20260521_010102",
                "20260521_010102",
                task,
            )
            summary_path = create_parallel_summary(
                root,
                "pddlrun_llmseparate_20260521_010000",
                None,
                "6",
                [
                    {
                        "floor_plan": "6",
                        "model": "test-model",
                        "task_index": 0,
                        "task": task,
                        "status": "success",
                        "duration_seconds": 1.0,
                        "task_run_dir": str(old_run),
                    }
                ],
            )

            migrate_intermediate_runs_main(["--summary", str(summary_path), "--apply"], base_path=root)

            expected_run = root / "logs" / "intermediate_runs" / "final_test_new_0___6" / "Open_the_fridge" / "20260521_001"
            self.assertTrue(old_run.exists())
            self.assertFalse(expected_run.exists())

            migrate_intermediate_runs_main(
                [
                    "--summary",
                    str(summary_path),
                    "--apply",
                    "--fallback-test-set",
                    "final_test_new_0",
                ],
                base_path=root,
            )

            self.assertFalse(old_run.exists())
            self.assertTrue(expected_run.exists())
            migrated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(
                migrated_summary["summaries"][0]["results"][0]["task_run_dir"],
                str(expected_run),
            )

    def test_run_config_is_shared_and_keeps_v2_artifact_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SharedRunConfig(root)

            self.assertIs(RunConfig, SharedRunConfig)
            self.assertEqual(config.dataset_file("final_test", "FloorPlan15"), root / "data" / "final_test" / "FloorPlan15.jsonl")
            self.assertEqual(config.artifact("allocate_subtasks", "missing"), "02_allocate/subtasks.json")
            self.assertEqual(
                SHARED_DEFAULT_RUN_CONFIG["artifacts"]["allocate_subtasks"],
                "02_allocate/subtasks.json",
            )

    def test_file_processor_is_shared_and_raises_shared_pddl_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            processor = SharedFileProcessor(str(root))

            self.assertIs(FileProcessor, SharedFileProcessor)
            self.assertIs(PDDLError, SharedPDDLError)
            with self.assertRaises(SharedPDDLError):
                processor.read_file(str(root / "missing.pddl"))

    def test_ai2thor_cache_path_accepts_configured_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "objects"

            path = get_ai2_thor_objects_cache_path(21, cache_dir=cache_dir)

            self.assertEqual(path, cache_dir / "FloorPlan21.json")

    def test_planner_uses_configured_command_alias_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(
                root,
                values={
                    "planner": {
                        "executable": "custom/fast-downward.py",
                        "alias": "custom-alias",
                        "timeout_seconds": 17,
                    }
                },
            )
            file_processor = FileProcessor(str(root), config=config)
            planner = PDDLPlanner(str(root), file_processor, config=config)
            problem_file = root / "problem.pddl"
            problem_file.write_text("(define (problem test))", encoding="utf-8")

            def fake_run(command, stdout, stderr, text, timeout):
                self.assertEqual(command[0], str(root / "custom" / "fast-downward.py"))
                self.assertEqual(command[1:3], ["--alias", "custom-alias"])
                self.assertEqual(command[-1], str(problem_file))
                self.assertEqual(timeout, 17)

                class Result:
                    stdout = "Solution found!"
                    stderr = ""

                return Result()

            with patch("pddlrun_llmseparate.subprocess.run", side_effect=fake_run):
                planner.run_plan(str(root / "domain.pddl"), str(problem_file))

            self.assertEqual(problem_file.with_name("problem_plan.txt").read_text(encoding="utf-8"), "Solution found!")

    def test_run_planners_passes_plan_file_before_alias_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(
                root,
                values={
                    "planner": {
                        "executable": "custom/fast-downward.py",
                        "alias": "custom-alias",
                        "timeout_seconds": 17,
                    }
                },
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "task_run")
            manager.current_task_manifest = {"artifacts": {}}
            Path(manager.current_task_run_dir).mkdir(parents=True)

            resources_dir = root / "resources"
            resources_dir.mkdir(parents=True, exist_ok=True)
            domain_file = resources_dir / "robot1.pddl"
            domain_file.write_text("(define (domain robot1))", encoding="utf-8")

            validated_problem_dir = Path(manager._get_validated_problem_file_path())
            validated_problem_dir.mkdir(parents=True, exist_ok=True)
            problem_file = validated_problem_dir / "task.pddl"
            problem_file.write_text("(define (problem task) (:domain robot1 ))", encoding="utf-8")
            output_file = Path(manager._get_plan_file_path()) / "task_plan.txt"
            captured = {}

            def fake_run(command, stdout, stderr, text, timeout):
                captured["command"] = command
                self.assertEqual(timeout, 17)

                class Result:
                    stdout = "planner stdout"
                    stderr = ""
                    returncode = 0

                return Result()

            with patch("pddlrun_llmseparate.subprocess.run", side_effect=fake_run), \
                    patch.object(manager.file_processor, "write_file", wraps=manager.file_processor.write_file) as write_file:
                manager.run_planners()

            command = captured["command"]
            self.assertEqual(command[0], str(root / "custom" / "fast-downward.py"))
            self.assertEqual(command[1:3], ["--plan-file", str(output_file)])
            self.assertLess(command.index("--plan-file"), command.index("--alias"))
            self.assertEqual(command[3:5], ["--alias", "custom-alias"])
            self.assertEqual(command[-2:], [str(domain_file), str(problem_file)])
            self.assertFalse(output_file.exists())
            planner_manifest = (
                Path(manager.current_task_run_dir)
                / manager.current_task_manifest["artifacts"]["planner"]["manifest"]
            )
            planner_records = json.loads(planner_manifest.read_text(encoding="utf-8"))
            self.assertEqual(planner_records[0]["compatibility_output"], str(output_file))
            self.assertNotIn((str(output_file), "planner stdout"), [call.args for call in write_file.call_args_list])
            self.assertTrue(
                any(str(call.args[0]).endswith("08_planner/stdout/task_stdout.txt") and call.args[1] == "planner stdout"
                    for call in write_file.call_args_list)
            )

    def test_run_fake_validator_copies_raw_problem_without_llm_call(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "task_run")
            manager.current_task_manifest = {"artifacts": {}}
            Path(manager.current_task_run_dir).mkdir(parents=True)

            raw_problem_dir = Path(manager._get_raw_problem_file_path())
            raw_problem_dir.mkdir(parents=True, exist_ok=True)
            raw_problem = raw_problem_dir / "subtask_01_problem.pddl"
            raw_content = "\n".join([
                "(define (problem subtask_01_problem)",
                "  (:domain robot1)",
                "  (:objects robot1 apple)",
                "  (:init (available apple))",
                "  (:goal (and (available apple)))",
                ")",
            ])
            raw_problem.write_text(raw_content, encoding="utf-8")

            with patch.object(manager.llm, "query_model") as query_model:
                manager.run_fake_validator()

            query_model.assert_not_called()
            validated_problem = (
                Path(manager._get_validated_problem_file_path())
                / "subtask_01_problem_validated.pddl"
            )
            raw_validated = (
                Path(manager.current_task_run_dir)
                / "07_validate/outputs/subtask_01_problem_validated.raw.txt"
            )
            validate_input = (
                Path(manager.current_task_run_dir)
                / "07_validate/inputs/subtask_01_problem_input.pddl"
            )
            validate_prompt = (
                Path(manager.current_task_run_dir)
                / "07_validate/prompts/subtask_01_problem_prompt.txt"
            )

            self.assertEqual(validated_problem.read_text(encoding="utf-8"), raw_content)
            self.assertEqual(raw_validated.read_text(encoding="utf-8"), raw_content)
            self.assertEqual(validate_input.read_text(encoding="utf-8"), raw_content)
            self.assertFalse(validate_prompt.exists())

            validation_manifest = (
                Path(manager.current_task_run_dir)
                / manager.current_task_manifest["artifacts"]["validate"]["manifest"]
            )
            validation_records = json.loads(validation_manifest.read_text(encoding="utf-8"))
            self.assertEqual(validation_records[0]["status"], "fake_validated")
            self.assertEqual(validation_records[0]["validated_problem_path"], "07_validate/outputs/subtask_01_problem_validated.pddl")

    def test_validate_and_plan_uses_fake_validator_call_site(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            with patch.object(manager, "run_fake_validator") as run_fake_validator, \
                    patch.object(manager, "run_llmvalidator") as run_llmvalidator, \
                    patch.object(manager, "run_planners") as run_planners:
                manager._validate_and_plan()

            run_fake_validator.assert_called_once_with()
            run_llmvalidator.assert_not_called()
            run_planners.assert_called_once_with()

    def test_v2_validate_and_plan_uses_validated_allaction_problem(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = pddlrun_llmseparate_v2.RunConfig(
                root,
                values={
                    "planner": {
                        "executable": "custom/fast-downward.py",
                        "alias": "custom-alias",
                        "timeout_seconds": 17,
                    }
                },
            )
            manager = pddlrun_llmseparate_v2.TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "task_run")
            manager.current_task_manifest = {"artifacts": {}}
            Path(manager.current_task_run_dir).mkdir(parents=True)

            resources_dir = root / "resources"
            resources_dir.mkdir(parents=True, exist_ok=True)
            domain_file = resources_dir / "allactionrobot.pddl"
            domain_file.write_text("(define (domain allactionrobot))", encoding="utf-8")

            raw_problem_dir = Path(manager._get_raw_problem_file_path())
            raw_problem_dir.mkdir(parents=True, exist_ok=True)
            raw_problem = raw_problem_dir / "subtask_01_problem.pddl"
            raw_problem.write_text(
                "\n".join([
                    "(define (problem subtask_01_problem)",
                    "  (:domain allactionrobot)",
                    "  (:objects robot1 apple)",
                    "  (:init (not (inaction robot1)) (available apple))",
                    "  (:goal (and (available apple)))",
                    ")",
                ]),
                encoding="utf-8",
            )

            call_order = []
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                call_order.append("validator")
                captured["validator_prompt"] = messages[-1]["content"]
                return {}, "\n".join([
                    "```pddl",
                    "(define (problem subtask_01_problem)",
                    "  (:domain wrongdomain)",
                    "  (:objects robot1 apple)",
                    "  (:init (not (inaction robot1)) (available apple))",
                    "  (:goal (and (available apple)))",
                    ")",
                    "```",
                ])

            def fake_run(command, stdout, stderr, text, timeout):
                call_order.append("planner")
                captured["planner_command"] = command
                self.assertEqual(timeout, 17)
                Path(command[2]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[2]).write_text("(GoToObject robot1 apple)\n", encoding="utf-8")

                class Result:
                    stdout = "planner stdout"
                    stderr = ""
                    returncode = 0

                return Result()

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model), \
                    patch("pddlrun_llmseparate_v2.subprocess.run", side_effect=fake_run):
                planner_records = manager._validate_and_plan()

            self.assertEqual(call_order, ["validator", "planner"])
            self.assertIn("Domain Description", captured["validator_prompt"])
            self.assertIn("Problem Description", captured["validator_prompt"])

            validated_problem = Path(manager._get_validated_problem_file_path()) / "subtask_01_problem_validated.pddl"
            self.assertTrue(validated_problem.exists())
            validated_content = validated_problem.read_text(encoding="utf-8")
            self.assertIn("(:domain allactionrobot)", validated_content)
            self.assertNotIn("wrongdomain", validated_content)

            command = captured["planner_command"]
            self.assertEqual(command[0], str(root / "custom" / "fast-downward.py"))
            self.assertEqual(command[1:3], ["--plan-file", str(Path(manager._get_plan_file_path()) / "subtask_01_problem_validated_plan.txt")])
            self.assertEqual(command[3:5], ["--alias", "custom-alias"])
            self.assertEqual(command[-2:], [str(domain_file), str(validated_problem)])
            self.assertNotEqual(command[-1], str(raw_problem))

            validation_manifest = Path(manager.current_task_run_dir) / "07_validate/validation_manifest.json"
            validation_records = json.loads(validation_manifest.read_text(encoding="utf-8"))
            self.assertEqual(validation_records[0]["status"], "validated")
            self.assertEqual(validation_records[0]["validated_problem_path"], "07_validate/outputs/subtask_01_problem_validated.pddl")

            self.assertEqual(planner_records[0]["problem_file"], "subtask_01_problem_validated.pddl")
            self.assertEqual(manager._subtask_id_from_filename(planner_records[0]["problem_file"]), 1)
            requirements = manager._build_subtask_requirements(
                subtasks=["#SubTask 1: Go to apple"],
                decomposed_plan="#SubTask 1: Go to apple",
                problem_pddl=[validated_content],
                planner_records=planner_records,
                objects_ai="objects=[]",
            )
            self.assertEqual(requirements[0]["subtask_id"], 1)
            self.assertEqual(requirements[0]["required_skills"], ["GoToObject"])

    def test_v2_extracts_required_robot_skills_from_normalized_action_names(self):
        manager = pddlrun_llmseparate_v2.TaskManager.__new__(pddlrun_llmseparate_v2.TaskManager)
        plan_text = "\n".join(
            [
                "(run_microwave robot1 microwave apple)",
                "(run-coffee-machine robot1 coffee_machine mug)",
                "(runtoaster robot1 toaster bread)",
                "(cook_by_stove_burner robot1 stove_burner egg)",
                "(heat-by-stove-burner robot1 stove_burner kettle)",
                "(fill_water robot1 sink mug)",
                "(cold_object robot1 fridge apple)",
                "(prepare_egg robot1 egg pan)",
            ]
        )

        actions = manager._parse_plan_actions(plan_text)

        self.assertEqual(
            manager._extract_required_skills(actions),
            [
                "RunMicrowave",
                "RunCoffeeMachine",
                "RunToaster",
                "CookByStoveBurner",
                "HeatByStoveBurner",
                "FillWater",
                "ColdObject",
                "BreakEgg",
            ],
        )

    def test_v2_pairwise_precedence_extracts_predecessors(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v2"
            prompt_dir.mkdir(parents=True)
            prompt_dir.joinpath("pddl_train_task_precedence_pairwise.txt").write_text(
                "Task:{task}\nPlan:{decomposed_plan}\nA:{subtask_a_id}\n{subtask_a}\nB:{subtask_b_id}\n{subtask_b}",
                encoding="utf-8",
            )
            config = SharedRunConfig(root, values={"data": {"prompt_template_dir": "prompts/v2"}})
            manager = pddlrun_llmseparate_v2.TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "task_run")
            Path(manager.current_task_run_dir).mkdir(parents=True)
            manager.current_task_manifest = {"artifacts": {}}
            responses = [
                ({}, '{"subtask_a_id": 1, "subtask_b_id": 2, "relation": "NO_ORDER", "reason": "independent"}'),
                ({}, '{"subtask_a_id": 1, "subtask_b_id": 3, "relation": "A_BEFORE_B", "reason": "first prepares an ingredient"}'),
                ({}, '{"subtask_a_id": 2, "subtask_b_id": 3, "relation": "A_BEFORE_B", "reason": "second prepares an ingredient"}'),
            ]
            captured_prompts = []

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured_prompts.append(messages[-1]["content"])
                return responses.pop(0)

            subtasks = [
                "#SubTask 1: Slice lettuce",
                "#SubTask 2: Slice tomato",
                "#SubTask 3: Assemble sandwich",
            ]
            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                predecessors = manager._generate_pairwise_predecessors(
                    task="Make a sandwich",
                    decomposed_plan="\n".join(subtasks),
                    subtasks=subtasks,
                )

            self.assertEqual(predecessors, {1: [], 2: [], 3: [1, 2]})
            self.assertEqual(len(captured_prompts), 3)
            self.assertIn("Make a sandwich", captured_prompts[0])
            self.assertIn("#SubTask 1: Slice lettuce", captured_prompts[0])
            output_path = Path(manager.current_task_run_dir) / "02_precedence/predecessors.json"
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), {"1": [], "2": [], "3": [1, 2]})

    def test_v2_pairwise_precedence_skips_llm_for_single_subtask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = pddlrun_llmseparate_v2.TaskManager(str(root), "test-model", config=SharedRunConfig(root))
            manager.current_task_run_dir = str(root / "task_run")
            Path(manager.current_task_run_dir).mkdir(parents=True)
            manager.current_task_manifest = {"artifacts": {}}

            with patch.object(manager.llm, "query_model") as query_model:
                predecessors = manager._generate_pairwise_predecessors(
                    task="Slice potato",
                    decomposed_plan="#SubTask 1: Slice potato",
                    subtasks=["#SubTask 1: Slice potato"],
                )

            self.assertEqual(predecessors, {1: []})
            query_model.assert_not_called()

    def test_v2_pairwise_precedence_falls_back_on_cycle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v2"
            prompt_dir.mkdir(parents=True)
            prompt_dir.joinpath("pddl_train_task_precedence_pairwise.txt").write_text(
                "A:{subtask_a_id}\nB:{subtask_b_id}\n{task}\n{decomposed_plan}\n{subtask_a}\n{subtask_b}",
                encoding="utf-8",
            )
            config = SharedRunConfig(root, values={"data": {"prompt_template_dir": "prompts/v2"}})
            manager = pddlrun_llmseparate_v2.TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "task_run")
            Path(manager.current_task_run_dir).mkdir(parents=True)
            manager.current_task_manifest = {"artifacts": {}}
            responses = [
                ({}, '{"subtask_a_id": 1, "subtask_b_id": 2, "relation": "A_BEFORE_B", "reason": "1 before 2"}'),
                ({}, '{"subtask_a_id": 1, "subtask_b_id": 3, "relation": "B_BEFORE_A", "reason": "3 before 1"}'),
                ({}, '{"subtask_a_id": 2, "subtask_b_id": 3, "relation": "A_BEFORE_B", "reason": "2 before 3"}'),
            ]

            with patch.object(manager.llm, "query_model", side_effect=lambda *args, **kwargs: responses.pop(0)):
                predecessors = manager._generate_pairwise_predecessors(
                    task="Cyclic bad response",
                    decomposed_plan="plan",
                    subtasks=["one", "two", "three"],
                )

            self.assertIsNone(predecessors)
            manifest_path = Path(manager.current_task_run_dir) / "02_precedence/pairwise_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "fallback")

    def test_v2_build_requirements_prefers_pairwise_predecessors(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = pddlrun_llmseparate_v2.TaskManager(str(root), "test-model", config=SharedRunConfig(root))
            plan_dir = root / "plans"
            plan_dir.mkdir()
            plan_1 = plan_dir / "subtask_01_plan.txt"
            plan_2 = plan_dir / "subtask_02_plan.txt"
            plan_1.write_text("(gotoobject robot1 apple)\n", encoding="utf-8")
            plan_2.write_text("(gotoobject robot1 fridge)\n", encoding="utf-8")

            requirements = manager._build_subtask_requirements(
                subtasks=["#SubTask 1: Prepare apple", "#SubTask 2: Store apple"],
                decomposed_plan="#SubTask 1 and #SubTask 2 can run in parallel.",
                problem_pddl=["", ""],
                planner_records=[
                    {"problem_file": "subtask_01_problem_validated.pddl", "compatibility_output": str(plan_1)},
                    {"problem_file": "subtask_02_problem_validated.pddl", "compatibility_output": str(plan_2)},
                ],
                objects_ai="objects=[]",
                preferred_predecessors={1: [], 2: [1]},
            )

            self.assertEqual(requirements[0]["predecessor_ids"], [])
            self.assertEqual(requirements[1]["predecessor_ids"], [1])

    def test_combine_all_plans_reads_recorded_plan_output_files_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "task_run")
            Path(manager.current_task_run_dir).mkdir(parents=True)

            plan_output_dir = Path(manager._get_plan_file_path())
            plan_output_dir.mkdir(parents=True, exist_ok=True)
            recorded_plan = plan_output_dir / "recorded_validated_plan.txt"
            recorded_plan.write_text("move robot apple (1)", encoding="utf-8")
            unrelated_plan = Path(manager.file_processor.validated_subtask_path) / "unrelated_plan.txt"
            unrelated_plan.write_text("drop robot banana (1)", encoding="utf-8")

            manager.current_task_manifest = {
                "artifacts": {},
                "planner": {
                    "plan_output_files": [str(recorded_plan)]
                },
            }
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                return {}, "combined plan"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._combine_all_plans("initial decomposed plan", ["Subtask 1: Robot 1;"])

            self.assertEqual(result, "combined plan")
            self.assertIn("move robot apple (1)", captured["prompt"])
            self.assertNotIn("drop robot banana (1)", captured["prompt"])

    def test_build_robot_team_keeps_source_metadata_out_of_robot_dict(self):
        team = build_robot_team([15, 6])

        self.assertEqual(team[0]["name"], "robot1")
        self.assertNotIn("source_robot_id", team[0])
        self.assertNotIn("source_robot_name", team[0])
        self.assertEqual(team[1]["name"], "robot2")
        self.assertNotIn("source_robot_id", team[1])
        self.assertNotIn("source_robot_name", team[1])

    def test_build_robot_domain_name_map_preserves_source_robot_identity(self):
        self.assertEqual(
            build_robot_domain_name_map([15, 6]),
            {"robot1": "robot15", "robot2": "robot6"},
        )

    def test_extract_subtasks_accepts_markdown_bold_headers_without_summary_bullets(self):
        decomposed_plan = (
            "Based on the provided domain and the task description, here is the decomposition.\n\n"
            "**GENERAL TASK DECOMPOSITION**\n"
            "Independent subtasks:\n"
            "- SubTask 1: Put the pot on the countertop.\n"
            "- SubTask 2: Put the houseplant on the countertop.\n"
            "- SubTask 3: Put the saltshaker on the sinkbasin.\n"
            "We can parallelize SubTask 1 and SubTask 2.\n\n"
            "**action description from domain for tasks required**\n\n"
            "**SubTask 1: Put the pot on the countertop**\n\n"
            "**Initial condition analyze due to previous subtask:**\n"
            "1. Robot not at pot location.\n"
            "2. Robot not holding pot.\n\n"
            "GoToObject: Robot goes to the pot.\n"
            "Parameters: ?robot, ?pot\n"
            "Preconditions: (not (inaction ?robot))\n"
            "Effects: (at ?robot ?pot), (not (inaction ?robot))\n\n"
            "PutObject: Robot puts the pot on the countertop.\n"
            "Parameters: ?robot, ?pot, ?countertop\n"
            "Preconditions: (holding ?robot ?pot)\n"
            "Effects: (at-location ?pot ?countertop)\n\n"
            "**SubTask 2: Put the houseplant on the countertop**\n\n"
            "**Initial condition analyze due to previous subtask:**\n"
            "1. Robot not at houseplant location.\n"
            "2. Robot not holding houseplant.\n\n"
            "GoToObject: Robot goes to the houseplant.\n"
            "Parameters: ?robot, ?houseplant\n"
            "Preconditions: (not (inaction ?robot))\n"
            "Effects: (at ?robot ?houseplant), (not (inaction ?robot))\n\n"
            "PutObject: Robot puts the houseplant on the countertop.\n"
            "Parameters: ?robot, ?houseplant, ?countertop\n"
            "Preconditions: (holding ?robot ?houseplant)\n"
            "Effects: (at-location ?houseplant ?countertop)\n\n"
            "**SubTask 3: Put the saltshaker on the sinkbasin**\n\n"
            "**Initial condition analyze due to previous subtask:**\n"
            "1. Robot not at saltshaker location.\n"
            "2. Robot not holding saltshaker.\n\n"
            "GoToObject: Robot goes to the saltshaker.\n"
            "Parameters: ?robot, ?saltshaker\n"
            "Preconditions: (not (inaction ?robot))\n"
            "Effects: (at ?robot ?saltshaker), (not (inaction ?robot))\n\n"
            "PutObject: Robot puts the saltshaker on the sinkbasin.\n"
            "Parameters: ?robot, ?saltshaker, ?sinkbasin\n"
            "Preconditions: (holding ?robot ?saltshaker)\n"
            "Effects: (at-location ?saltshaker ?sinkbasin)\n\n"
            "**Task put the pot on the countertop, put the houseplant on the countertop, "
            "and put the saltshaker on the sinkbasin is done.**"
        )

        subtasks = ParsingUtils.extract_subtasks(decomposed_plan)

        self.assertEqual(len(subtasks), 3)
        self.assertTrue(subtasks[0].startswith("#SubTask 1: Put the pot on the countertop"))
        self.assertIn("Robot goes to the pot", subtasks[0])
        self.assertNotIn("houseplant", subtasks[0])
        self.assertTrue(subtasks[1].startswith("#SubTask 2: Put the houseplant on the countertop"))
        self.assertIn("Robot goes to the houseplant", subtasks[1])
        self.assertNotIn("saltshaker", subtasks[1])
        self.assertTrue(subtasks[2].startswith("#SubTask 3: Put the saltshaker on the sinkbasin"))
        self.assertIn("Robot goes to the saltshaker", subtasks[2])
        self.assertNotIn("is done", subtasks[2])

    def test_extract_subtasks_keeps_existing_hash_header_formats(self):
        body = "\n".join(f"Action detail line {idx}" for idx in range(1, 10))
        decomposed_plan = (
            "#Subtask 1 Put an Egg in the Fridge\n"
            f"{body}\n"
            "# SubTask 2: Slice the Tomato.\n"
            f"{body}\n"
        )

        subtasks = ParsingUtils.extract_subtasks(decomposed_plan)

        self.assertEqual(len(subtasks), 2)
        self.assertTrue(subtasks[0].startswith("#Subtask 1 Put an Egg in the Fridge"))
        self.assertTrue(subtasks[1].startswith("# SubTask 2: Slice the Tomato."))

    def test_extract_subtasks_does_not_use_bullets_or_sentences_as_headers(self):
        decomposed_plan = (
            "Independent subtasks:\n"
            "- SubTask 1: Put the pot on the countertop.\n"
            "- SubTask 2: Put the houseplant on the countertop.\n"
            "We can parallelize SubTask 1 and SubTask 2 because they do not depend on each other.\n"
        )

        self.assertEqual(ParsingUtils.extract_subtasks(decomposed_plan), [decomposed_plan.strip()])

    def test_parsing_utils_matches_supported_header_variants(self):
        self.assertTrue(ParsingUtils.is_subtask_header_line("**SubTask 1: Put the pot on the table**"))
        self.assertTrue(ParsingUtils.is_subtask_header_line("#Subtask 1 Put an Egg in the Fridge"))
        self.assertTrue(
            ParsingUtils.is_subtask_header_line(
                "SubTask 1: Put the pot on the table",
                allow_bare=True,
            )
        )
        self.assertFalse(ParsingUtils.is_subtask_header_line("- SubTask 1: Put the pot on the table"))
        self.assertEqual(
            ParsingUtils.normalize_subtask_header_line("**SubTask 2: Open the Drawer**"),
            "#SubTask 2: Open the Drawer",
        )

    def test_split_and_store_tasks_accepts_markdown_bold_headers_without_bullet_headers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            processor = FileProcessor(tmp_dir)
            content = (
                "**SubTask 1: Put the pot on the countertop**\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: pot, countertop\n\n"
                "**SubTask 2: Put the houseplant on the countertop**\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: houseplant, countertop\n"
            )
            bullet_only_content = (
                "Independent subtasks:\n"
                "- SubTask 1: Put the pot on the countertop.\n"
                "- SubTask 2: Put the houseplant on the countertop.\n"
            )

            subtasks, _ = processor.split_and_store_tasks(content)
            bullet_subtasks, _ = processor.split_and_store_tasks(bullet_only_content)

            self.assertEqual(len(subtasks), 2)
            self.assertIn("pot", subtasks[0])
            self.assertIn("houseplant", subtasks[1])
            self.assertEqual(bullet_subtasks, [bullet_only_content.strip()])

    def test_extract_sequence_operations_accepts_robot_ids_without_space_without_filling_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            allocated_plan = (
                "# Sequence of Operations:\n"
                "Subtask 1: Robot2;Subtask 2: Robot2;\n"
                "Subtask 3: Robot2;\n"
            )

            sequence_operations = manager._extract_sequence_operations(allocated_plan)
            assignments = manager._extract_robot_assignments(sequence_operations)

            self.assertEqual(assignments, {1: 2, 2: 2, 3: 2})
            self.assertNotIn("Subtask 4: Robot 1;", sequence_operations)

    def test_extract_sequence_operations_accepts_markdown_and_header_without_colon(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            markdown_sequence = manager._extract_sequence_operations(
                "**Sequence of Operations:**\nSubTask1:robot2;",
            )
            no_colon_sequence = manager._extract_sequence_operations(
                "# Sequence of Operations\nSubtask 1: robot4;",
            )

            self.assertEqual(manager._extract_robot_assignments(markdown_sequence), {1: 2})
            self.assertEqual(manager._extract_robot_assignments(no_colon_sequence), {1: 4})

    def test_extract_sequence_operations_selects_best_sequence_block(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            most_complete_plan = (
                "Sequence of Operations:\n"
                "Subtask 1: Robot 2;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 3;Subtask 2: Robot 4;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 5;\n"
            )
            tied_plan = (
                "Sequence of Operations:\n"
                "Subtask 1: Robot 2;Subtask 2: Robot 3;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 4;Subtask 2: Robot 5;\n"
            )

            most_complete_sequence = manager._extract_sequence_operations(most_complete_plan)
            tied_sequence = manager._extract_sequence_operations(tied_plan)

            self.assertEqual(
                manager._extract_robot_assignments(most_complete_sequence),
                {1: 3, 2: 4},
            )
            self.assertEqual(
                manager._extract_robot_assignments(tied_sequence),
                {1: 4, 2: 5},
            )

    def test_extract_sequence_operations_does_not_guess_non_numeric_assignments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            allocated_plan = (
                "# Sequence of Operations:\n"
                "Subtask A: Robot2;Subtask;Robot;\n"
                "Subtask 2: Robot ?;\n"
                "Subtask 3: Robot A;\n"
            )

            sequence_operations = manager._extract_sequence_operations(allocated_plan)
            assignments = manager._extract_robot_assignments(sequence_operations)

            self.assertEqual(assignments, {})

    def test_extract_sequence_operations_accepts_real_run_variants(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            robot_lowercase_plan = (
                "# Sequence of Operations:\n"
                "Subtask 1: robot4;Subtask 2: robot1;\n"
                "Subtask 3: robot1;\n"
            )
            compact_and_described_plan = (
                "# Sequence of Operations:\n"
                "SubTask1:robot3;\n"
                "SubTask2\n"
                "- Robot2\n"
                "SubTask 3 (Open cabinet): Robot 4;\n"
            )

            robot_lowercase_sequence = manager._extract_sequence_operations(robot_lowercase_plan)
            compact_sequence = manager._extract_sequence_operations(compact_and_described_plan)

            self.assertEqual(
                manager._extract_robot_assignments(robot_lowercase_sequence),
                {1: 4, 2: 1, 3: 1},
            )
            self.assertEqual(
                manager._extract_robot_assignments(compact_sequence),
                {1: 3, 2: 2, 3: 4},
            )

    def test_check_decompose_subtask_count_checks_only_flat_index(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "subtasks": [{"skill": "Open"}, {"skill": "Break"}]}) + "\n"
                + json.dumps({"task": "second", "subtasks": [{"skill": "Open"}]}) + "\n",
                encoding="utf-8",
            )

            first_run = root / "runs" / "first"
            second_run = root / "runs" / "second_missing_output"
            (first_run / "01_decompose").mkdir(parents=True)
            second_run.mkdir(parents=True)
            body = "\n".join(f"Action detail line {idx}" for idx in range(1, 10))
            (first_run / "01_decompose" / "02_decompose_output.txt").write_text(
                "#Subtask 1: Open the Drawer\n" + body + "\n",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_run),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = check_decompose_subtask_count(str(summary_path), index=0)

            self.assertFalse(result["matched"])
            self.assertEqual(result["decompose_count"], 1)
            self.assertEqual(result["jsonl_count"], 2)
            self.assertEqual(result["flat_index"], 0)

            with self.assertRaises(IndexError):
                check_decompose_subtask_count(str(summary_path), index=2)

    def test_check_allocate_assignment_count_compares_against_assigned_robots(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "assigned_robots": [2, 2]}) + "\n"
                + json.dumps({"task": "second", "assigned_robots": [4, 4]}) + "\n"
                + json.dumps({"task": "third", "assigned_robots": [1]}) + "\n",
                encoding="utf-8",
            )

            first_run = root / "runs" / "first"
            second_run = root / "runs" / "second"
            invalid_run = root / "runs" / "invalid"
            for run_dir in (first_run, second_run, invalid_run):
                (run_dir / "02_allocate").mkdir(parents=True)

            (first_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 2;Subtask 2: Robot 2;\n",
                encoding="utf-8",
            )
            (second_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 4;\n",
                encoding="utf-8",
            )
            (invalid_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot;\n",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 2,
                                        "task": "third",
                                        "task_run_dir": str(invalid_run),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            matched = check_allocate_assignment_count(str(summary_path), index=0)
            mismatched = check_allocate_assignment_count(str(summary_path), index=1)
            invalid = check_allocate_assignment_count(str(summary_path), index=2)

            self.assertTrue(matched["matched"])
            self.assertEqual(matched["allocate_count"], 2)
            self.assertEqual(matched["jsonl_count"], 2)
            self.assertEqual(matched["assignments"], {1: 2, 2: 2})
            self.assertEqual(matched["flat_index"], 0)

            self.assertFalse(mismatched["matched"])
            self.assertEqual(mismatched["allocate_count"], 1)
            self.assertEqual(mismatched["jsonl_count"], 2)

            self.assertFalse(invalid["matched"])
            self.assertEqual(invalid["allocate_count"], 0)
            self.assertEqual(invalid["jsonl_count"], 1)
            self.assertEqual(invalid["assignments"], {})
            self.assertEqual(invalid["sequence_operations"], [])

    def test_check_planner_plan_count_compares_against_subtasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "subtasks": [{"skill": "Open"}, {"skill": "Break"}]}) + "\n"
                + json.dumps({"task": "second", "subtasks": [{"skill": "Put"}, {"skill": "Close"}]}) + "\n",
                encoding="utf-8",
            )

            first_outputs = root / "runs" / "first" / "08_planner" / "outputs"
            second_outputs = root / "runs" / "second" / "08_planner" / "outputs"
            first_outputs.mkdir(parents=True)
            second_outputs.mkdir(parents=True)

            first_plan_1 = first_outputs / "subtask_01_problem_validated_plan.txt"
            first_plan_2 = first_outputs / "subtask_02_problem_validated_plan.txt"
            first_plan_1.write_text("plan 1", encoding="utf-8")
            first_plan_2.write_text("plan 2", encoding="utf-8")
            (first_outputs / "subtask_01_problem_validated_stdout.txt").write_text(
                "ignored",
                encoding="utf-8",
            )
            (second_outputs / "subtask_01_problem_validated_plan.txt").write_text(
                "plan 1",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_outputs.parents[1]),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_outputs.parents[1]),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            matched = check_planner_plan_count(str(summary_path), index=0)
            mismatched = check_planner_plan_count(str(summary_path), index=1)

            self.assertTrue(matched["matched"])
            self.assertEqual(matched["planner_count"], 2)
            self.assertEqual(matched["jsonl_count"], 2)
            self.assertEqual(matched["flat_index"], 0)
            self.assertEqual(matched["plan_paths"], [str(first_plan_1), str(first_plan_2)])

            self.assertFalse(mismatched["matched"])
            self.assertEqual(mismatched["planner_count"], 1)
            self.assertEqual(mismatched["jsonl_count"], 2)
            self.assertEqual(mismatched["flat_index"], 1)

    def test_problemextracting_reads_real_domain_and_keeps_real_robot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources_dir = root / "resources"
            prompt_dir = root / "prompts" / "v1"
            resources_dir.mkdir(parents=True)
            prompt_dir.mkdir(parents=True)
            (resources_dir / "robot15.pddl").write_text(
                "(define (domain robot15)\n"
                "  (:types robot object)\n"
                "  (:predicates\n"
                "    (ready ?robot - robot)\n"
                "    (near ?robot - robot)\n"
                "    (object-open ?object - object)\n"
                "    (at-location ?object - object ?location - object)\n"
                "  )\n"
                "  (:action UseRobot15\n"
                "    :parameters (?robot - robot)\n"
                "    :precondition (and (ready robot15) (near robot150))\n"
                "  )\n"
                ")",
                encoding="utf-8",
            )
            (prompt_dir / "pddl_train_task_allocationsep_problem.txt").write_text("# example\n", encoding="utf-8")
            config = RunConfig(root)
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_robot_domain_names = {"robot1": "robot15"}

            captured = {}

            class FakeLLM:
                def query_model(self, messages, model, max_tokens=None, frequency_penalty=0):
                    captured["prompt"] = messages[-1]["content"]
                    return {}, (
                        "(define (problem generated)\n"
                        "  (:domain robot1)\n"
                        "  (:objects robot1 robot150 robot1_extra - robot)\n"
                        "  (:init (ready robot1) (near robot150) (ready robot1_extra))\n"
                        ")"
                    )

            subtask = (
                "#SubTask 1: Test\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: Apple\n"
            )

            result = manager.problemextracting(
                subtasks=[subtask],
                robot_assignments={1: 1},
                llm=FakeLLM(),
                model="test-model",
                file_processor=manager.file_processor,
                objects_ai="\n\nobjects = []",
                prompt_allocation_set="pddl_train_task_allocationsep",
                key_object_pddl_states=[
                    {
                        "object": "Drawer",
                        "object_type": "Drawer",
                        "object_id": "Drawer|+01.00|+00.20|-00.30",
                        "facts": [
                            "(object-open Drawer)",
                            "(at-location Drawer CounterTop)",
                            "(switch-on Drawer)",
                        ],
                    }
                ],
            )

            self.assertIn("(:domain robot15)", result[0])
            self.assertIn("robot15 robot150 robot1_extra - robot", result[0])
            self.assertIn("(ready robot15)", result[0])
            self.assertIn("(near robot150)", result[0])
            self.assertIn("(ready robot1_extra)", result[0])
            self.assertNotIn("(:domain robot1)", result[0])

            self.assertIn("(define (domain robot15)", captured["prompt"])
            self.assertIn("(ready robot15)", captured["prompt"])
            self.assertIn("(near robot150)", captured["prompt"])
            self.assertNotIn("(define (domain robot1)", captured["prompt"])
            self.assertIn("key_object_pddl_states = [", captured["prompt"])
            self.assertIn("(object-open Drawer)", captured["prompt"])
            self.assertIn("(at-location Drawer CounterTop)", captured["prompt"])
            self.assertNotIn("(switch-on Drawer)", captured["prompt"])
            self.assertNotIn("object_id", captured["prompt"])

    def test_problemextracting_uses_subtask_specific_key_object_states(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources_dir = root / "resources"
            prompt_dir = root / "prompts" / "v1"
            resources_dir.mkdir(parents=True)
            prompt_dir.mkdir(parents=True)
            (resources_dir / "robot1.pddl").write_text(
                "(define (domain robot1)\n"
                "  (:types robot object)\n"
                "  (:predicates\n"
                "    (object-open ?object - object)\n"
                "    (switch-on ?object - object)\n"
                "  )\n"
                ")",
                encoding="utf-8",
            )
            (prompt_dir / "pddl_train_task_allocationsep_problem.txt").write_text("# example\n", encoding="utf-8")
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            captured_prompts = []

            class FakeLLM:
                def query_model(self, messages, model, max_tokens=None, frequency_penalty=0):
                    captured_prompts.append(messages[-1]["content"])
                    return {}, (
                        "(define (problem generated)\n"
                        "  (:domain robot1)\n"
                        "  (:objects robot1 - robot)\n"
                        "  (:init)\n"
                        ")"
                    )

            manager.problemextracting(
                subtasks=[
                    "#SubTask 1: Open the drawer",
                    "#SubTask 2: Switch on the light",
                ],
                robot_assignments={1: 1, 2: 1},
                llm=FakeLLM(),
                model="test-model",
                file_processor=manager.file_processor,
                objects_ai="\n\nobjects = []",
                prompt_allocation_set="pddl_train_task_allocationsep",
                key_object_pddl_states=[
                    {
                        "object": "Drawer",
                        "object_type": "object",
                        "facts": ["(object-open Drawer)"],
                    },
                    {
                        "object": "LightSwitch",
                        "object_type": "object",
                        "facts": ["(switch-on LightSwitch)"],
                    },
                ],
                key_object_pddl_states_by_subtask={
                    1: [
                        {
                            "object": "Drawer",
                            "object_type": "object",
                            "facts": ["(object-open Drawer)"],
                        }
                    ],
                    2: [
                        {
                            "object": "LightSwitch",
                            "object_type": "object",
                            "facts": ["(switch-on LightSwitch)"],
                        }
                    ],
                },
            )

            self.assertEqual(len(captured_prompts), 2)
            self.assertIn("(object-open Drawer)", captured_prompts[0])
            self.assertNotIn("(switch-on LightSwitch)", captured_prompts[0])
            self.assertIn("(switch-on LightSwitch)", captured_prompts[1])
            self.assertNotIn("(object-open Drawer)", captured_prompts[1])

    def test_key_objects_match_floorplan_objects_in_decomposition(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            objects_ai = (
                "\n\nobjects = ["
                "{'name': 'LightSwitch', 'mass': 0.0}, "
                "{'name': 'Apple', 'mass': 0.2}, "
                "{'name': 'Pan', 'mass': 0.6}"
                "]"
            )
            decomposed_plan = (
                "#SubTask 1: Turn off the light switch\n"
                "Skills Required: GoToObject, SwitchOff\n"
                "The robot should go to the light switch and switch it off.\n"
                "This sentence says parallel, but short object names must not match inside other words."
            )

            key_objects = manager._extract_key_objects_from_decomposition(decomposed_plan, objects_ai)

            self.assertEqual([item["name"] for item in key_objects], ["LightSwitch"])

    def test_key_objects_split_by_subtask_without_cross_contamination(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            objects_ai = (
                "\n\nobjects = ["
                "{'name': 'LightSwitch', 'mass': 0.0}, "
                "{'name': 'Apple', 'mass': 0.2}, "
                "{'name': 'Pan', 'mass': 0.6}"
                "]"
            )
            subtasks = [
                "#SubTask 1: Turn off the light switch\n"
                "The robot should go to the light switch and switch it off.",
                "#SubTask 2: Pick up the apple\n"
                "The robot should pick up the apple.",
                "#SubTask 3: Move near the counter\n"
                "This subtask does not name a floorplan object.",
            ]

            key_objects_by_subtask = manager._extract_key_objects_by_subtask(subtasks, objects_ai)
            key_objects = manager._combine_key_objects_by_subtask(key_objects_by_subtask)

            self.assertEqual(
                {
                    subtask_idx: [item["name"] for item in subtask_key_objects]
                    for subtask_idx, subtask_key_objects in key_objects_by_subtask.items()
                },
                {
                    1: ["LightSwitch"],
                    2: ["Apple"],
                    3: [],
                },
            )
            self.assertEqual([item["name"] for item in key_objects], ["LightSwitch", "Apple"])

    def test_key_object_pddl_states_filter_floorplan_and_convert_supported_facts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources_dir = root / "resources"
            resources_dir.mkdir(parents=True)
            (resources_dir / "allactionrobot.pddl").write_text(
                "(define (domain allactionrobot)\n"
                "  (:types robot object knife microwave toaster coffee_machine fridge stove_burner sink mug bread egg - object)\n"
                "  (:predicates\n"
                "    (at-location ?object - object ?location - object)\n"
                "    (object-open ?object - object)\n"
                "    (switch-on ?object - object)\n"
                "    (placable_on_stove_burner ?object - object)\n"
                "    (cookable-by-microwave ?object - object)\n"
                "    (cookable-by-stove_burner ?object - object)\n"
                "  )\n"
                ")",
                encoding="utf-8",
            )
            metadata_path = root / "data" / "all_ai2thor_objects.json"
            write_json(
                metadata_path,
                [
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Drawer",
                        "objectId": "Drawer|+01.00|+00.20|-00.30",
                        "openable": True,
                        "isOpen": True,
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Drawer",
                        "objectId": "Drawer|+01.00|+00.60|-00.30",
                        "openable": True,
                        "isOpen": False,
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "LightSwitch",
                        "objectId": "LightSwitch|+00.10|+01.20|+00.30",
                        "toggleable": True,
                        "isToggled": True,
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Apple",
                        "objectId": "Apple|+00.10|+01.15|+00.30",
                        "parentReceptacles": ["CounterTop|+00.00|+01.00|+00.00"],
                        "isSliced": False,
                        "isCooked": True,
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "CounterTop",
                        "objectId": "CounterTop|+00.00|+01.00|+00.00",
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Mug",
                        "objectId": "Mug|+00.20|+01.15|+00.30",
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Bread",
                        "objectId": "Bread|+00.20|+01.15|+00.30",
                        "isSliced": True,
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Pot",
                        "objectId": "Pot|+00.40|+01.15|+00.30",
                    },
                    {
                        "scene": "FloorPlan1",
                        "objectType": "Potato",
                        "objectId": "Potato|+00.50|+01.15|+00.30",
                    },
                    {
                        "scene": "FloorPlan2",
                        "objectType": "Drawer",
                        "objectId": "Drawer|+09.00|+00.20|-00.30",
                        "openable": True,
                        "isOpen": True,
                    },
                ],
            )
            manager = TaskManager(
                str(root),
                "test-model",
                config=RunConfig(root),
                floor_plan="FloorPlan1",
            )
            domain = (
                "(define (domain robot1)\n"
                "  (:predicates\n"
                "    (at-location ?object - object ?location - object)\n"
                "    (object-open ?object - object)\n"
                "    (switch-on ?object - object)\n"
                "    (placable_on_stove_burner ?object - object)\n"
                "    (cookable-by-microwave ?object - object)\n"
                "    (cookable-by-stove_burner ?object - object)\n"
                "  )\n"
                ")"
            )

            key_object_inputs = [
                {"name": "Drawer"},
                {"name": "LightSwitch"},
                {"name": "Apple"},
                {"name": "Mug"},
                {"name": "Pot"},
                {"name": "Potato"},
            ]
            states = manager._build_key_object_pddl_states(key_object_inputs, domain)
            context = manager._build_key_object_pddl_context(key_object_inputs, domain)

            self.assertEqual(context["states"], states)
            self.assertEqual([item["object"] for item in states if item["object_type"] == "object" and item["object"].startswith("Drawer")], ["Drawer_1", "Drawer_2"])
            self.assertIn("LightSwitch", [item["object"] for item in states])
            self.assertNotIn("Bread", [item["object"] for item in states])
            for item in states:
                self.assertNotIn("object_id", item)
                for related_object in item.get("related_objects", []):
                    self.assertNotIn("object_id", related_object)

            bindings_by_object = {
                item["object"]: item
                for item in context["object_id_bindings"]
            }
            self.assertEqual(
                bindings_by_object["Drawer_1"]["object_id"],
                "Drawer|+01.00|+00.20|-00.30",
            )
            self.assertEqual(bindings_by_object["Drawer_1"]["object_type"], "Drawer")
            self.assertEqual(bindings_by_object["Drawer_1"]["number"], 1)
            self.assertEqual(bindings_by_object["Drawer_1"]["count"], 2)
            self.assertTrue(bindings_by_object["Drawer_1"]["multiple"])
            self.assertEqual(bindings_by_object["Drawer_1"]["roles"], ["key_object"])
            self.assertEqual(
                bindings_by_object["Drawer_2"]["object_id"],
                "Drawer|+01.00|+00.60|-00.30",
            )
            self.assertEqual(bindings_by_object["Drawer_2"]["number"], 2)
            self.assertFalse(bindings_by_object["LightSwitch"]["multiple"])
            self.assertEqual(bindings_by_object["LightSwitch"]["count"], 1)
            self.assertEqual(bindings_by_object["LightSwitch"]["roles"], ["key_object"])
            self.assertEqual(
                bindings_by_object["CounterTop"]["object_id"],
                "CounterTop|+00.00|+01.00|+00.00",
            )
            self.assertEqual(bindings_by_object["CounterTop"]["roles"], ["parentReceptacle"])
            self.assertNotIn("Bread", bindings_by_object)

            facts_by_object = {
                item["object"]: "\n".join(item["facts"])
                for item in states
            }
            types_by_object = {item["object"]: item["object_type"] for item in states}

            self.assertEqual(types_by_object["Mug"], "mug")
            self.assertEqual(types_by_object["Apple"], "object")
            self.assertEqual(types_by_object["Drawer_1"], "object")
            self.assertIn("(switch-on LightSwitch)", facts_by_object["LightSwitch"])
            self.assertIn("(at-location Apple CounterTop)", facts_by_object["Apple"])
            self.assertIn("(cookable-by-microwave Apple)", facts_by_object["Apple"])
            self.assertIn("(placable_on_stove_burner Pot)", facts_by_object["Pot"])
            self.assertIn("(cookable-by-stove_burner Potato)", facts_by_object["Potato"])
            self.assertNotIn("(sliced Apple)", facts_by_object["Apple"])
            self.assertNotIn("(cooked Apple)", facts_by_object["Apple"])

            open_drawer = next(item for item in states if "(object-open" in "\n".join(item["facts"]))
            closed_drawer = next(item for item in states if item["object"] == "Drawer_2")
            self.assertIn("(object-open Drawer_1)", "\n".join(open_drawer["facts"]))
            self.assertNotIn("(object-open", "\n".join(closed_drawer["facts"]))

    def test_allocation_prompt_includes_key_objects_without_pddl_domain(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_solution.txt").write_text(
                "# allocation example\n",
                encoding="utf-8",
            )

            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                return {}, "# Sequence of Operations:\nSubtask 1: Robot 1;"

            decomposed_plan = (
                "#SubTask 1: Heat the apple in the microwave\n"
                "Skills Required: GoToObject, RunMicrowave\n"
            )
            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_allocation_plan(
                    decomposed_plan,
                    robots=[{
                        "name": "robot1",
                        "skills": ["GoToObject", "RunMicrowave"],
                        "mass_capacity": 100,
                    }],
                    objects_ai="\n\nobjects = [{'name': 'Microwave', 'mass': 7.0}, {'name': 'Toaster', 'mass': 1.0}]",
                    key_objects=[{"name": "Microwave", "mass": 7.0}],
                    key_objects_by_subtask={1: [{"name": "Microwave", "mass": 7.0}]},
                )

            prompt = result["prompt"]
            self.assertIn("objects = [{'name': 'Microwave', 'mass': 7.0}]", prompt)
            self.assertNotIn("'Toaster'", prompt)
            self.assertNotIn("key_objects =", prompt)
            self.assertNotIn("key_objects_by_subtask =", prompt)
            self.assertNotIn("key_object_pddl_states =", prompt)
            self.assertIn("# - Use robots and objects as allocation context.", prompt)
            self.assertIn(
                "# - Judge robot capability using robot skills, and only consider mass capacity for objects that the subtask requires a robot to pick up.",
                prompt,
            )
            self.assertIn("# - Mass capacity only matters for objects that must be picked up.", prompt)
            self.assertNotIn("# CROPPED ROBOT PDDL DOMAINS FOR ALLOCATION", prompt)
            self.assertNotIn("# Robot allocation name: robot1", prompt)
            self.assertNotIn("Real PDDL domain file", prompt)
            self.assertNotIn("Domain unavailable for allocation prompt", prompt)
            self.assertNotIn("cropped robot PDDL domains", prompt)
            self.assertNotIn("cropped domain", prompt)
            self.assertNotIn("(:action RunMicrowave", prompt)
            self.assertNotIn("(:action RunToaster", prompt)
            self.assertNotIn(
                "cropped_robot_domains",
                manager.current_task_manifest["artifacts"].get("allocate", {}),
            )
            self.assertFalse((root / "run" / "02_allocate" / "00_cropped_robot_domains.txt").exists())
            self.assertFalse((root / "run" / "02_allocate" / "01_allocate_prompt.txt").exists())
            self.assertFalse((root / "run" / "02_allocate" / "02_allocate_output.txt").exists())

            manager._write_allocation_generation_artifacts(result)

            self.assertIn(
                "# allocation example",
                (root / "run" / "02_allocate" / "01_allocate_prompt.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "# Sequence of Operations:\nSubtask 1: Robot 1;",
                (root / "run" / "02_allocate" / "02_allocate_output.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "02_allocate/01_allocate_prompt.txt",
                manager.current_task_manifest["artifacts"]["allocate"]["prompt"],
            )
            self.assertEqual(
                "02_allocate/02_allocate_output.txt",
                manager.current_task_manifest["artifacts"]["allocate"]["output"],
            )

    def test_decompose_rag_is_disabled_by_library_default_for_decompose_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# decompose example\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the fridge"}
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                captured["max_tokens"] = max_tokens
                return {}, "#SubTask 1: Open the fridge"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                manager._generate_decomposed_plan(
                    "Open the fridge",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Fridge', 'mass': 5.0}]",
            )

            prompt = captured["prompt"]
            self.assertNotIn(RAG_PROMPT_TITLE, prompt)
            self.assertIn("objects = ['Fridge']", prompt)
            self.assertNotIn("objects = [{'name': 'Fridge', 'mass': 5.0}]", prompt)
            self.assertNotIn("'mass':", prompt)
            self.assertNotIn("robots =", prompt)
            self.assertNotIn("robot's skills meet", prompt)
            self.assertNotIn("mass_capacity must be strictly greater", prompt)
            self.assertIn("# decompose example", prompt)
            self.assertEqual(1300, captured["max_tokens"])
            self.assertIn(
                "# decompose example",
                (root / "run" / "01_decompose" / "01_decompose_prompt.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "#SubTask 1: Open the fridge",
                (root / "run" / "01_decompose" / "02_decompose_output.txt").read_text(encoding="utf-8"),
            )
            self.assertNotIn("decompose_rag", manager.current_task_manifest)

    def test_decompose_prompt_preserves_duplicate_object_names(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# decompose example\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the cabinet"}
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                return {}, "#SubTask 1: Open the cabinet"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                manager._generate_decomposed_plan(
                    "Open the cabinet",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Cabinet', 'mass': 0.0}, {'name': 'Cabinet', 'mass': 1.0}]",
                    write_artifacts=False,
                )

            prompt = captured["prompt"]
            self.assertIn("objects = ['Cabinet', 'Cabinet']", prompt)
            self.assertNotIn("'mass':", prompt)

    def test_generate_decomposed_plan_can_skip_artifact_writes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# decompose example\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the fridge"}
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                captured["max_tokens"] = max_tokens
                return {}, "#SubTask 1: Open the fridge"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_decomposed_plan(
                    "Open the fridge",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Fridge', 'mass': 5.0}]",
                    write_artifacts=False,
                )

            self.assertEqual("#SubTask 1: Open the fridge", result)
            self.assertIn("# decompose example", captured["prompt"])
            self.assertEqual(1300, captured["max_tokens"])
            self.assertFalse((root / "run" / "01_decompose" / "01_decompose_prompt.txt").exists())
            self.assertFalse((root / "run" / "01_decompose" / "02_decompose_output.txt").exists())
            self.assertEqual({}, manager.current_task_manifest["artifacts"])

    def test_decompose_rag_prompt_block_serializes_concurrent_retrievals(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_manifest = {"artifacts": {}, "task": "Concurrent RAG"}

            class Example:
                def to_manifest_record(self):
                    return {"doc_id": "example"}

            class SlowRetriever:
                def __init__(self, runtime_db_path: Path):
                    self.runtime_db_path = runtime_db_path
                    self.active = 0
                    self.max_active = 0
                    self.lock = threading.Lock()

                def retrieve(self, stage: str, query_text: str):
                    with self.lock:
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                    try:
                        time.sleep(0.05)
                        return [Example()]
                    finally:
                        with self.lock:
                            self.active -= 1

                def format_prompt_block(self, stage: str, examples):
                    return f"block {stage} {len(examples)}"

            retriever = SlowRetriever(root / "rag" / "runtime.sqlite")
            manager.decompose_rag_retriever = retriever
            start_barrier = threading.Barrier(4)

            def call_rag(index: int) -> str:
                start_barrier.wait(timeout=5)
                return manager._decompose_rag_prompt_block(f"query {index}", f"key_{index}")

            with ThreadPoolExecutor(max_workers=4) as executor:
                blocks = list(executor.map(call_rag, range(4)))

            self.assertEqual(retriever.max_active, 1)
            self.assertEqual(blocks, ["\nblock decompose 1\n"] * 4)
            self.assertEqual(
                set(manager.current_task_manifest["decompose_rag"]["retrievals"]),
                {"key_0", "key_1", "key_2", "key_3"},
            )

    def test_decompose_rag_prompt_block_records_timeout_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_manifest = {"artifacts": {}, "task": "Timeout RAG"}

            class TimeoutRetriever:
                runtime_db_path = root / "rag" / "runtime.sqlite"

                def query_tokens(self, query_text: str):
                    return ["open", "fridge"]

                def retrieve(self, stage: str, query_text: str):
                    raise PDDLRagTimeoutError("RAG query exceeded 5s")

            manager.decompose_rag_retriever = TimeoutRetriever()

            block = manager._decompose_rag_prompt_block("open fridge", "decompose")

            self.assertEqual(block, "")
            retrieval = manager.current_task_manifest["decompose_rag"]["retrievals"]["decompose"]
            self.assertEqual(retrieval["examples"], [])
            self.assertEqual(retrieval["query_tokens"], ["open", "fridge"])
            self.assertTrue(retrieval["timeout"])
            self.assertIn("RAG query exceeded", retrieval["error"])

    def test_decompose_rag_zero_hit_records_and_uses_static_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# static decompose zero-hit fallback\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the fridge"}
            captured = {}

            class EmptyRetriever:
                runtime_db_path = root / "rag" / "runtime.sqlite"

                def query_tokens(self, query_text: str, stage=None):
                    captured["query_stage"] = stage
                    return ["open", "fridge"]

                def retrieve(self, stage: str, query_text: str):
                    captured["retrieve_stage"] = stage
                    return []

            manager.decompose_rag_retriever = EmptyRetriever()

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                return {}, "#SubTask 1: Open the fridge"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                manager._generate_decomposed_plan(
                    "Open the fridge",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Fridge', 'mass': 5.0}]",
                    write_artifacts=False,
                )

            self.assertIn("# static decompose zero-hit fallback", captured["prompt"])
            self.assertEqual(captured["query_stage"], "decompose")
            self.assertEqual(captured["retrieve_stage"], "decompose")
            retrieval = manager.current_task_manifest["decompose_rag"]["retrievals"]["decompose"]
            self.assertEqual(retrieval["examples"], [])
            self.assertEqual(retrieval["query_tokens"], ["open", "fridge"])
            self.assertFalse(retrieval["timeout"])

    def test_decompose_rag_timeout_generation_uses_static_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# static decompose timeout fallback\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the fridge"}
            captured = {}

            class TimeoutRetriever:
                runtime_db_path = root / "rag" / "runtime.sqlite"

                def query_tokens(self, query_text: str):
                    return ["open", "fridge"]

                def retrieve(self, stage: str, query_text: str):
                    raise PDDLRagTimeoutError("RAG query exceeded 5s")

            manager.decompose_rag_retriever = TimeoutRetriever()

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                return {}, "#SubTask 1: Open the fridge"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                manager._generate_decomposed_plan(
                    "Open the fridge",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Fridge', 'mass': 5.0}]",
                    write_artifacts=False,
                )

            self.assertIn("# static decompose timeout fallback", captured["prompt"])
            retrieval = manager.current_task_manifest["decompose_rag"]["retrievals"]["decompose"]
            self.assertEqual(retrieval["examples"], [])
            self.assertEqual(retrieval["query_tokens"], ["open", "fridge"])
            self.assertTrue(retrieval["timeout"])
            self.assertIn("RAG query exceeded", retrieval["error"])

    def test_decompose_prompt_includes_enabled_rag_examples(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_decomposesep.txt").write_text(
                "# static decompose example should be replaced\n",
                encoding="utf-8",
            )
            config = RunConfig(
                root,
                values=rag_config_values(
                    root,
                    [
                        rag_doc(
                            "decompose:fridge",
                            "decompose",
                            "Task: open fridge",
                            "# Historical decomposition for opening a fridge",
                        )
                    ],
                ),
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the fridge"}
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                captured["max_tokens"] = max_tokens
                return {}, "#SubTask 1: Open the fridge"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                manager._generate_decomposed_plan(
                    "Open the fridge",
                    "(define (domain allactionrobot))",
                    [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "\n\nobjects = [{'name': 'Fridge', 'mass': 5.0}]",
                )

            prompt = captured["prompt"]
            self.assertNotIn(RAG_PROMPT_TITLE, prompt)
            self.assertNotIn("# End Retrieved Task Decomposition Examples (RAG)", prompt)
            self.assertIn("Do not copy object names, robot tokens, floor-plan facts", prompt)
            self.assertIn("# Example", prompt)
            self.assertNotIn("doc_id: decompose:fridge", prompt)
            self.assertIn("# Historical decomposition for opening a fridge", prompt)
            self.assertNotIn("# static decompose example should be replaced", prompt)
            self.assertEqual(1300, captured["max_tokens"])
            self.assertEqual(
                manager.current_task_manifest["decompose_rag"]["retrievals"]["decompose"]["examples"][0]["doc_id"],
                "decompose:fridge",
            )

    def test_allocation_prompt_uses_static_examples_when_allocate_rag_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_solution.txt").write_text(
                "# static allocation example\n",
                encoding="utf-8",
            )
            config = RunConfig(
                root,
                values=rag_config_values(
                    root,
                    [
                        rag_doc(
                            "allocate:microwave",
                            "allocate",
                            "Task: heat apple microwave",
                            "# Historical allocation for microwave heating",
                        )
                    ],
                ),
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Heat the apple"}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                return {}, "# Sequence of Operations:\nSubtask 1: Robot 1;"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_allocation_plan(
                    "#SubTask 1: Heat the apple in the microwave",
                    robots=[{"name": "robot1", "skills": ["GoToObject"], "mass_capacity": 100}],
                    objects_ai="\n\nobjects = [{'name': 'Microwave', 'mass': 7.0}]",
                    key_objects=[{"name": "Microwave", "mass": 7.0}],
                    key_objects_by_subtask={1: [{"name": "Microwave", "mass": 7.0}]},
                )

            prompt = result["prompt"]
            self.assertNotIn(RAG_PROMPT_TITLE, prompt)
            self.assertNotIn("doc_id: allocate:microwave", prompt)
            self.assertIn("# static allocation example", prompt)
            self.assertNotIn("decompose_rag", manager.current_task_manifest)
            self.assertNotIn("allocate_rag", manager.current_task_manifest)

    def test_allocation_prompt_includes_enabled_allocate_rag_examples(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_solution.txt").write_text(
                "# static allocation example should be replaced\n",
                encoding="utf-8",
            )
            config = RunConfig(
                root,
                values=rag_config_values(
                    root,
                    [
                        rag_doc(
                            "allocate:microwave",
                            "allocate",
                            "Task: heat apple microwave",
                            "# Historical allocation for microwave heating",
                        )
                    ],
                    section="allocate_rag",
                ),
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Heat the apple"}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                return {}, "# Sequence of Operations:\nSubtask 1: Robot 1;"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_allocation_plan(
                    "#SubTask 1: Heat the apple in the microwave",
                    robots=[{"name": "robot1", "skills": ["GoToObject"], "mass_capacity": 100}],
                    objects_ai="\n\nobjects = [{'name': 'Microwave', 'mass': 7.0}]",
                    key_objects=[{"name": "Microwave", "mass": 7.0}],
                    key_objects_by_subtask={1: [{"name": "Microwave", "mass": 7.0}]},
                )

            prompt = result["prompt"]
            self.assertNotIn(RAG_PROMPT_TITLE, prompt)
            self.assertIn("allocation reasoning style and output format", prompt)
            self.assertNotIn("decomposition structure and reasoning style", prompt)
            self.assertIn("Do not copy object names, robot tokens, floor-plan facts", prompt)
            self.assertIn("# Example", prompt)
            self.assertIn("# Historical allocation for microwave heating", prompt)
            self.assertNotIn("doc_id: allocate:microwave", prompt)
            self.assertNotIn("# static allocation example should be replaced", prompt)
            self.assertEqual(
                manager.current_task_manifest["allocate_rag"]["retrievals"]["allocate"]["examples"][0]["doc_id"],
                "allocate:microwave",
            )
            self.assertIn(
                "gotoobject",
                manager.current_task_manifest["allocate_rag"]["retrievals"]["allocate"]["query_tokens"],
            )

    def test_allocation_rag_zero_hit_records_and_uses_static_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_solution.txt").write_text(
                "# static allocation zero-hit fallback\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Heat the apple"}
            captured = {}

            class EmptyRetriever:
                runtime_db_path = root / "rag" / "runtime.sqlite"

                def query_tokens(self, query_text: str, stage=None):
                    captured["query_stage"] = stage
                    return ["heat", "apple", "gotoobject"]

                def retrieve(self, stage: str, query_text: str):
                    captured["retrieve_stage"] = stage
                    return []

            manager.allocate_rag_retriever = EmptyRetriever()

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                return {}, "# Sequence of Operations:\nSubtask 1: Robot 1;"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_allocation_plan(
                    "#SubTask 1: Heat the apple in the microwave\nSkills Required: GoToObject, RunMicrowave",
                    robots=[{"name": "robot1", "skills": ["GoToObject"], "mass_capacity": 100}],
                    objects_ai="\n\nobjects = [{'name': 'Microwave', 'mass': 7.0}]",
                    key_objects=[{"name": "Microwave", "mass": 7.0}],
                    key_objects_by_subtask={1: [{"name": "Microwave", "mass": 7.0}]},
                )

            self.assertIn("# static allocation zero-hit fallback", result["prompt"])
            self.assertEqual(captured["query_stage"], "allocate")
            self.assertEqual(captured["retrieve_stage"], "allocate")
            retrieval = manager.current_task_manifest["allocate_rag"]["retrievals"]["allocate"]
            self.assertEqual(retrieval["examples"], [])
            self.assertEqual(retrieval["query_tokens"], ["heat", "apple", "gotoobject"])
            self.assertFalse(retrieval["timeout"])

    def test_allocation_rag_timeout_records_and_uses_static_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_solution.txt").write_text(
                "# static allocation fallback\n",
                encoding="utf-8",
            )
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Heat the apple"}

            class TimeoutRetriever:
                runtime_db_path = root / "rag" / "runtime.sqlite"

                def query_tokens(self, query_text: str):
                    return ["heat", "apple"]

                def retrieve(self, stage: str, query_text: str):
                    raise PDDLRagTimeoutError("RAG query exceeded 5s")

            manager.allocate_rag_retriever = TimeoutRetriever()

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                return {}, "# Sequence of Operations:\nSubtask 1: Robot 1;"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._generate_allocation_plan(
                    "#SubTask 1: Heat the apple in the microwave",
                    robots=[{"name": "robot1", "skills": ["GoToObject"], "mass_capacity": 100}],
                    objects_ai="\n\nobjects = [{'name': 'Microwave', 'mass': 7.0}]",
                    key_objects=[{"name": "Microwave", "mass": 7.0}],
                    key_objects_by_subtask={1: [{"name": "Microwave", "mass": 7.0}]},
                )

            self.assertIn("# static allocation fallback", result["prompt"])
            retrieval = manager.current_task_manifest["allocate_rag"]["retrievals"]["allocate"]
            self.assertEqual(retrieval["examples"], [])
            self.assertEqual(retrieval["query_tokens"], ["heat", "apple"])
            self.assertTrue(retrieval["timeout"])
            self.assertIn("RAG query exceeded", retrieval["error"])

    def test_problem_generation_prompt_does_not_include_decompose_rag_examples(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_dir = root / "prompts" / "v1"
            resources_dir = root / "resources"
            prompt_dir.mkdir(parents=True)
            resources_dir.mkdir(parents=True)
            (prompt_dir / "pddl_train_task_allocationsep_problem.txt").write_text(
                "# problem example\n",
                encoding="utf-8",
            )
            (resources_dir / "robot1.pddl").write_text(
                "(define (domain robot1) (:predicates (object-open ?object - object)))",
                encoding="utf-8",
            )
            config = RunConfig(
                root,
                values=rag_config_values(
                    root,
                    [
                        rag_doc(
                            "problem_generation:drawer",
                            "problem_generation",
                            "Task: open drawer",
                            "(define (problem historical_open_drawer))",
                        )
                    ],
                ),
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "run")
            manager.current_task_manifest = {"artifacts": {}, "task": "Open the drawer"}
            captured = {}

            class FakeLLM:
                def query_model(self, messages, model, max_tokens=None, frequency_penalty=0.0):
                    captured["prompt"] = messages[-1]["content"]
                    return {}, (
                        "(define (problem open_drawer)\n"
                        "  (:domain robot1)\n"
                        "  (:objects robot1 Drawer - object)\n"
                        "  (:init)\n"
                        ")"
                    )

            manager.problemextracting(
                subtasks=["#SubTask 1: Open the drawer"],
                robot_assignments={1: 1},
                llm=FakeLLM(),
                model="test-model",
                file_processor=manager.file_processor,
                objects_ai="\n\nobjects = [{'name': 'Drawer', 'mass': 5.0}]",
                prompt_allocation_set="pddl_train_task_allocationsep",
                key_object_pddl_states=[
                    {
                        "object": "Drawer",
                        "object_type": "object",
                        "facts": ["(object-open Drawer)"],
                    }
                ],
            )

            prompt = captured["prompt"]
            self.assertNotIn(RAG_PROMPT_TITLE, prompt)
            self.assertNotIn("doc_id: problem_generation:drawer", prompt)
            self.assertNotIn("decompose_rag", manager.current_task_manifest)

    def test_problem_generation_fewshot_includes_key_object_pddl_states(self):
        prompt = (ROOT / "prompts" / "v1" / "pddl_train_task_allocationsep_problem.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("key_object_pddl_states = [", prompt)
        self.assertIn("(at-location Potato CounterTop_1)", prompt)
        self.assertIn("(cookable-by-stove_burner Potato)", prompt)
        self.assertIn("CounterTop_1 CounterTop_2 - object", prompt)
        self.assertNotIn("object_id", prompt)
        self.assertIn("Copy its facts into (:init)", prompt)
        self.assertIn("do not output raw AI2-THOR fields", prompt)

    def test_domain_robot_replacement_does_not_replace_partial_tokens(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            replaced = manager._replace_domain_robot_name(
                "(domain robot10) robot10 robot100 robot10_extra",
                "robot10",
                "robot1",
            )

            self.assertEqual(replaced, "(domain robot1) robot1 robot100 robot10_extra")


if __name__ == "__main__":
    unittest.main()
