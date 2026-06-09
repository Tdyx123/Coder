import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_config import RunConfig
from run_pddlrun_llmseparate_parallel import load_jobs


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


if __name__ == "__main__":
    unittest.main()
