import os
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.run_results import (
    ActionLedger,
    RunResultStore,
    task_key_for_executable,
    validate_result,
)


class ActionLedgerContractTest(unittest.TestCase):
    def test_retries_do_not_inflate_logical_success(self):
        ledger = ActionLedger()
        ledger.record_started("0:robot1:0")
        for _ in range(3):
            ledger.record_attempt()
        ledger.record_terminal("0:robot1:0", "failed", ignored_for_legacy=True)

        result = ledger.freeze()

        self.assertEqual(result["action_counts"]["failed"], 1)
        self.assertEqual(result["action_counts"]["attempts"], 3)
        self.assertEqual(result["raw_action_sr"], 0.0)
        self.assertEqual(result["ignored_failure_count"], 1)

    def test_empty_or_deferred_actions_never_fabricate_a_success_rate(self):
        ledger = ActionLedger(["0:robot1:0"])
        ledger.record_started("0:robot1:0")
        ledger.record_attempt()

        result = ledger.freeze()

        self.assertEqual(result["action_counts"]["planned"], 1)
        self.assertEqual(result["action_counts"]["started"], 1)
        self.assertEqual(result["action_counts"]["unexecuted"], 1)
        self.assertIsNone(result["raw_action_sr"])

    def test_freeze_rejects_late_writes_and_a_duplicate_terminal(self):
        ledger = ActionLedger()
        ledger.record_started("0:robot1:0")
        ledger.record_terminal("0:robot1:0", "succeeded")
        with self.assertRaises(RuntimeError):
            ledger.record_terminal("0:robot1:0", "failed")
        ledger.freeze()
        with self.assertRaises(RuntimeError):
            ledger.record_attempt()


class ResultValidationContractTest(unittest.TestCase):
    def test_v2_identity_must_match_parent_environment(self):
        result = {
            "metrics_schema_version": 2,
            "evaluation_version": "fixed_goals_v2",
            "execution_policy": "legacy",
            "run_id": "wrong-run",
            "task_key": "task-a",
            "attempt": 2,
        }
        environment = {
            "LAMMAP_RUN_ID": "run-a",
            "LAMMAP_TASK_KEY": "task-a",
            "LAMMAP_ATTEMPT": "2",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ValueError):
                validate_result(result, returncode=0)

    def test_nonzero_returncode_overrides_child_success_fields(self):
        result = {
            "metrics_schema_version": 2,
            "evaluation_version": "fixed_goals_v2",
            "execution_policy": "legacy",
            "run_id": "run-a",
            "task_key": "task-a",
            "attempt": 1,
            "process_status": "completed",
            "execution_status": "completed",
            "task_success": True,
        }
        environment = {
            "LAMMAP_RUN_ID": "run-a",
            "LAMMAP_TASK_KEY": "task-a",
            "LAMMAP_ATTEMPT": "1",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            checked = validate_result(result, returncode=1)

        self.assertEqual(checked["process_status"], "failed")
        self.assertEqual(checked["execution_status"], "failed")
        self.assertIsNone(checked["task_success"])
        self.assertEqual(checked["status"], "failed")
        self.assertFalse(checked["timed_out"])

    def test_timeout_returncode_overrides_child_status_and_marks_retryable_timeout(self):
        result = {
            "metrics_schema_version": 2,
            "evaluation_version": "fixed_goals_v2",
            "execution_policy": "legacy",
            "run_id": "run-a",
            "task_key": "task-a",
            "attempt": 1,
            "status": "success",
            "timed_out": False,
        }
        environment = {
            "LAMMAP_RUN_ID": "run-a",
            "LAMMAP_TASK_KEY": "task-a",
            "LAMMAP_ATTEMPT": "1",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            checked = validate_result(result, returncode=124)

        self.assertEqual(checked["process_status"], "timeout")
        self.assertEqual(checked["status"], "timeout")
        self.assertTrue(checked["timed_out"])

    def test_legacy_result_keeps_legacy_version_and_parent_identity(self):
        environment = {
            "LAMMAP_RUN_ID": "run-a",
            "LAMMAP_TASK_KEY": "task-a",
            "LAMMAP_ATTEMPT": "3",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            checked = validate_result({"gcr": 1.0}, returncode=0)

        self.assertEqual(checked["evaluation_version"], "legacy_v1")
        self.assertEqual(checked["run_id"], "run-a")
        self.assertEqual(checked["attempt"], 3)

    def test_store_rebuild_skips_missing_or_invalid_attempt_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RunResultStore(Path(temp_dir), "run-a")
            task_key = sha256(b"/tmp/executable_plan.py").hexdigest()
            store.record_attempt(
                task_key,
                1,
                {
                    "metrics_schema_version": 2,
                    "evaluation_version": "fixed_goals_v2",
                    "execution_policy": "legacy",
                    "run_id": "run-a",
                    "task_key": task_key,
                    "attempt": 1,
                    "process_status": "completed",
                    "execution_status": "completed",
                    "evaluation_status": "valid",
                    "task_success": False,
                    "gcr": 0.0, "tc": 0, "sr": 0, "ru": 1.0,
                    "original_goal_count": 1, "satisfied_goal_count": 0,
                    "action_counts": dict(planned=0, started=0, succeeded=0, failed=0,
                                          skipped=0, cancelled=0, unexecuted=0, attempts=0),
                    "raw_action_sr": None, "ignored_failure_count": 0,
                    "movement_mode": "step",
                },
            )
            broken = store.start_attempt(task_key, 2) / "result.json"
            broken.write_text("{", encoding="utf-8")

            summary = store.rebuild_summary()

        self.assertEqual(summary["total_results"], 1)
        self.assertEqual(summary["success_count"], 1)  # Legacy process-success counter.
        self.assertEqual(summary["groups"][0]["task_success_count"], 0)
        self.assertEqual(summary["groups"][0]["valid_evaluation_count"], 1)
        self.assertEqual(summary["interrupted_attempts"][0]["attempt"], 2)

    def test_task_keys_hash_resolved_executable_paths_and_store_rejects_unsafe_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "nested" / "plan.py"
            expected = sha256(str(executable.resolve()).encode("utf-8")).hexdigest()
            self.assertEqual(task_key_for_executable(executable), expected)

            store = RunResultStore(Path(temp_dir) / "results", "run-a")
            with self.assertRaises(ValueError):
                store.record_attempt(
                    "../../escape",
                    1,
                    {"run_id": "run-a", "task_key": "../../escape", "attempt": 1},
                )


if __name__ == "__main__":
    unittest.main()
