import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_config import RunConfig
from run_pddlrun_llmseparate_parallel import TaskJob, load_jobs, run_single_job


class ParallelRunnerTests(unittest.TestCase):
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
            }
            summary = run_single_job(
                ROOT,
                RunConfig(ROOT),
                args,
                job,
                "objects=[]",
            )

        self.assertEqual(summary["task_index"], 17)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["task_index"], 17)


if __name__ == "__main__":
    unittest.main()
