import json
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_config import RunConfig
from run_pddlrun_llmseparate_parallel import (
    TaskJob,
    load_jobs,
    main as parallel_main,
    parse_args,
    prewarm_allocate_rag_if_configured,
    prewarm_decompose_rag_if_configured,
    run_single_job,
)


class ParallelRunnerTests(unittest.TestCase):
    def test_cli_defaults_to_decompose_rag_disabled(self):
        args = parse_args(["--floor-plans", "6"])

        self.assertFalse(args.decompose_rag)
        self.assertFalse(args.allocate_rag)

    def test_cli_decompose_rag_can_be_enabled_and_disabled(self):
        enabled_args = parse_args(["--floor-plans", "6", "--decompose-rag"])
        disabled_args = parse_args(["--floor-plans", "6", "--no-decompose-rag"])

        self.assertTrue(enabled_args.decompose_rag)
        self.assertFalse(disabled_args.decompose_rag)

    def test_cli_allocate_rag_can_be_enabled(self):
        args = parse_args(["--floor-plans", "6", "--allocate-rag"])

        self.assertTrue(args.allocate_rag)

    def test_prewarm_decompose_rag_if_configured_calls_single_runner_prewarm(self):
        config = RunConfig(ROOT, values={"decompose_rag": {"enabled": True, "prewarm_runtime_db": True}})

        with patch("pddlrun_llmseparate.prewarm_decompose_rag_runtime_db", return_value=True) as mock_prewarm:
            self.assertTrue(prewarm_decompose_rag_if_configured(config))

        mock_prewarm.assert_called_once_with(config)

    def test_prewarm_allocate_rag_if_configured_calls_single_runner_prewarm(self):
        config = RunConfig(ROOT, values={"allocate_rag": {"enabled": True, "prewarm_runtime_db": True}})

        with patch("pddlrun_llmseparate.prewarm_allocate_rag_runtime_db", return_value=True) as mock_prewarm:
            self.assertTrue(prewarm_allocate_rag_if_configured(config))

        mock_prewarm.assert_called_once_with(config)

    def test_main_prewarms_rag_before_submitting_floor_jobs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prewarm_state = {"decompose_called": False, "allocate_called": False}

            class FakeExecutor:
                def __init__(self, max_workers):
                    self.max_workers = max_workers

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def submit(self, fn, *args, **kwargs):
                    self_case.assertTrue(prewarm_state["decompose_called"])
                    self_case.assertTrue(prewarm_state["allocate_called"])
                    future = Future()
                    future.set_result(
                        {
                            "floor_plan": "6",
                            "task_count": 0,
                            "success_count": 0,
                            "failure_count": 0,
                            "all_pass_count": 0,
                            "pass_one_count": 0,
                            "results": [],
                        }
                    )
                    return future

            self_case = self
            args = SimpleNamespace(
                floor_plans=["6"],
                model="test-model",
                test_set="sample_set",
                max_floor_plan_workers=1,
                max_task_workers=1,
                output_root=str(root / "parallel"),
                prompt_decompse_set="pddl_train_task_decomposesep",
                prompt_allocation_set="pddl_train_task_allocationsep",
                decompose_rag=True,
                allocate_rag=True,
                disable_log_results=False,
            )
            config = RunConfig(root, values={"decompose_rag": {"enabled": False, "prewarm_runtime_db": True}})

            def fake_decompose_prewarm(config_arg):
                prewarm_state["decompose_called"] = True
                return True

            def fake_allocate_prewarm(config_arg):
                prewarm_state["allocate_called"] = True
                return True

            with patch("run_pddlrun_llmseparate_parallel.parse_args", return_value=args), \
                patch("run_pddlrun_llmseparate_parallel.load_run_config", return_value=config), \
                patch("run_pddlrun_llmseparate_parallel.prewarm_decompose_rag_if_configured", side_effect=fake_decompose_prewarm), \
                patch("run_pddlrun_llmseparate_parallel.prewarm_allocate_rag_if_configured", side_effect=fake_allocate_prewarm), \
                patch("run_pddlrun_llmseparate_parallel.ThreadPoolExecutor", FakeExecutor):
                parallel_main()

            self.assertTrue(prewarm_state["decompose_called"])
            self.assertTrue(prewarm_state["allocate_called"])

    def test_load_jobs_skips_truthy_invalid_records(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_file = root / "data" / "sample_set" / "FloorPlan6.jsonl"
            dataset_file.parent.mkdir(parents=True)
            records = [
                {"task": "valid task"},
                {"task": "lowercase invalid task", "invalid": True},
                {"task": "explicitly valid task", "invalid": False},
                {"task": "uppercase invalid task", "Invalid": True},
            ]
            dataset_file.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            jobs = load_jobs(RunConfig(root), "sample_set", "FloorPlan6")

            self.assertEqual([job.task_index for job in jobs], [0, 2])
            self.assertEqual(
                [job.record["task"] for job in jobs],
                ["valid task", "explicitly valid task"],
            )

    def test_run_single_job_passes_original_task_index(self):
        args = SimpleNamespace(
            model="test-model",
            prompt_decompse_set="pddl_train_task_decomposesep",
            prompt_allocation_set="pddl_train_task_allocationsep",
            test_set="sample_set",
        )
        job = TaskJob(
            floor_plan="6",
            task_index=17,
            record={"task": "sample task", "robot list": [1]},
        )

        with patch("pddlrun_llmseparate.run_single_floor_plan_task") as mock_run:
            mock_run.return_value = {
                "task_run_dir": "/tmp/sample-run",
                "tc": 1,
                "total": 1,
                "llm_token_usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            }
            summary = run_single_job(
                ROOT,
                RunConfig(ROOT),
                args,
                job,
                "objects=[]",
            )

        self.assertEqual(summary["task_index"], 17)
        self.assertEqual(
            summary["llm_token_usage"],
            {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
            },
        )
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["task_index"], 17)


if __name__ == "__main__":
    unittest.main()
