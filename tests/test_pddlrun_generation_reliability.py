"""Generation/publication and zero-action execution contract regressions."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_plantocode_demo_bundle import (
    load_bundle_data_from_executable,
    write_json,
    write_parallel_run_fixture_task,
    write_parallel_run_summary,
)
from tests import test_evaluation_contract, test_pddlrun_executor_adapter
from baseline_converters import pddlrun
from executor_system import generated_plan_runtime as generated, parallel_runner
from executor_system.movement import MovementConfig


class PddlRunGenerationReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.run = write_parallel_run_fixture_task(self.root, "6", 0)
        self.output = self.run / "plan_to_code"
        self.executable = self.output / "executable_plan.py"
        self.plan = self.run / "08_planner/outputs/subtask_01_problem_validated_plan.txt"

    def generate(self, validate_code=True):
        return pddlrun.process_task_run(self.run, validate_code)

    def seed_executable(self):
        result = self.generate()
        self.assertTrue(result["success"], result)

    def assert_no_outputs(self):
        self.assertFalse(self.executable.exists())
        self.assertEqual([p for p in self.output.rglob("*") if p.is_file()], [])
        self.assertEqual(parallel_runner.discover_executable_plans([], str(self.run)), [])

    def write_noops(self, count=1):
        plans = []
        for subtask in range(1, count + 1):
            path = self.plan if subtask == 1 else self.plan.parent / f"subtask_{subtask:02d}_plan.txt"
            path.write_text("; cost = 0\n", encoding="utf-8")
            plans.append((subtask, path, "(object-open cabinet)"))
        (self.run / "02_allocate/02_allocate_output.txt").write_text(
            "".join(f"Subtask {sid}: Robot 1;\n" for sid, _, _ in plans), encoding="utf-8",
        )
        test_pddlrun_executor_adapter.PddlRunExecutorAdapterTest().write_current_noop_artifacts(
            self.run, plans,
        )

    def test_old_entry_is_removed_before_inputs_are_read(self):
        self.seed_executable()
        load_inputs = pddlrun.load_run_inputs

        def inspect_inputs(path):
            self.assertFalse(self.executable.exists())
            return load_inputs(path)

        with patch.object(pddlrun, "load_run_inputs", side_effect=inspect_inputs):
            self.assertTrue(self.generate()["success"])

    def test_parse_failure_removes_old_entry_from_parallel_discovery(self):
        self.seed_executable()
        self.plan.write_text("(unknownaction robot1 cabinet)\n", encoding="utf-8")
        result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("Unsupported PDDL action", result["error"])
        self.assert_no_outputs()
        summary_root = self.root / "parallel"
        write_parallel_run_summary(self.root, summary_root, [self.run])
        with self.assertRaisesRegex(RuntimeError, "No runner-compatible"):
            parallel_runner.discover_parallel_run_executable_plans(str(summary_root))

    def test_compile_failure_removes_old_entry_and_temporary_files(self):
        self.seed_executable()
        with patch.object(pddlrun, "compile_python", side_effect=SyntaxError("bad compile")):
            result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("bad compile", result["error"])
        self.assert_no_outputs()

    def test_cleanup_error_does_not_replace_original_failure(self):
        self.seed_executable()
        unlink = Path.unlink

        def refuse_temporary(path, *args, **kwargs):
            if path.suffix == ".tmp" and path.exists():
                raise PermissionError("cannot remove temporary")
            return unlink(path, *args, **kwargs)

        with patch.object(pddlrun, "compile_python", side_effect=SyntaxError("bad compile")), \
             patch.object(Path, "unlink", new=refuse_temporary), redirect_stderr(io.StringIO()):
            result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("bad compile", result["error"])
        self.assertIn("cannot remove temporary", result["cleanup_error"])
        self.assertFalse(self.executable.exists())
        self.assertEqual(parallel_runner.discover_executable_plans([], str(self.run)), [])

    def test_capability_failures_invalidate_previous_script(self):
        context_path = self.run / "inputs/task_context.json"
        original_context = json.loads(context_path.read_text())
        original_plan = self.plan.read_text()
        for reason in ("missing_skill", "mass_exceeded"):
            with self.subTest(reason=reason):
                write_json(context_path, original_context)
                self.plan.write_text(original_plan, encoding="utf-8")
                self.seed_executable()
                context = json.loads(context_path.read_text())
                if reason == "missing_skill":
                    context["robots"][0]["skills"] = []
                else:
                    context["robots"][0].update(skills=["PickupObject"], mass_capacity=0.5)
                    context["objects_ai"] = "objects = [{'name': 'Cabinet', 'mass': 1.0}]"
                    self.plan.write_text("(pickupobject robot1 cabinet)\n", encoding="utf-8")
                write_json(context_path, context)
                result = self.generate(validate_code=False)
                self.assertEqual(result["failure_reason"], reason, result)
                self.assert_no_outputs()

    def test_partial_write_failure_cleans_temporary_file(self):
        self.seed_executable()
        write = Path.write_text

        def fail_write(path, text, *args, **kwargs):
            if path.parent == self.output:
                write(path, text[:20], *args, **kwargs)
                raise OSError("disk full")
            return write(path, text, *args, **kwargs)

        with patch.object(Path, "write_text", new=fail_write):
            result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("disk full", result["error"])
        self.assert_no_outputs()

    def test_replace_failure_cleans_compiled_temporary_file(self):
        self.seed_executable()
        with patch.object(os, "replace", side_effect=OSError("publish failed")):
            result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("publish failed", result["error"])
        self.assert_no_outputs()

    def test_success_is_published_only_after_temporary_compile(self):
        self.seed_executable()
        compile_python = pddlrun.compile_python
        compiled = []

        def inspect_compile(path, **kwargs):
            self.assertFalse(self.executable.exists())
            self.assertEqual(path.suffix, ".tmp")
            self.assertEqual(path.parent, self.output)
            self.assertEqual(list(self.output.glob("*.py")), [])
            compile_python(path, **kwargs)
            compiled.append(path)

        with patch.object(pddlrun, "compile_python", side_effect=inspect_compile):
            result = self.generate()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(compiled), 1)
        self.assertFalse(compiled[0].exists())
        compile(self.executable.read_text(), str(self.executable), "exec")
        bundle = generated.build_hardcoded_bundle(load_bundle_data_from_executable(self.executable))
        self.assertEqual(bundle.no_trans, 2)
        self.assertEqual([p.name for p in self.output.rglob("*") if p.is_file()], ["executable_plan.py"])

    def test_invalidation_failure_aborts_without_reading_inputs(self):
        self.seed_executable()
        original = self.executable.read_bytes()
        unlink = Path.unlink

        def refuse_entry(path, *args, **kwargs):
            if path == self.executable:
                raise PermissionError("cannot invalidate")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=refuse_entry), patch.object(
            pddlrun, "load_run_inputs", side_effect=AssertionError("must not read inputs"),
        ):
            result = self.generate()
        self.assertFalse(result["success"])
        self.assertIn("cannot invalidate", result["cleanup_error"])
        self.assertEqual(self.executable.read_bytes(), original)

    def test_interrupts_clean_outputs_and_propagate(self):
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception_type=exception_type):
                self.seed_executable()
                with patch.object(pddlrun, "compile_python", side_effect=exception_type("stop")):
                    with self.assertRaises(exception_type):
                        self.generate()
                self.assert_no_outputs()

    def test_uncaught_input_error_still_invalidates_old_entry(self):
        self.seed_executable()
        (self.run / "inputs/task_context.json").write_text("{broken json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.generate()
        self.assert_no_outputs()

    def test_unverified_empty_plan_fails_even_without_compile_validation(self):
        self.seed_executable()
        self.plan.write_text("; cost = 0\n", encoding="utf-8")
        result = self.generate(validate_code=False)
        self.assertFalse(result["success"])
        self.assertIn("zero-action", result["error"])
        self.assert_no_outputs()

    def test_invalid_current_noop_artifacts_cannot_publish(self):
        for case in ("forged", "stale_plan", "failed_planner", "failed_val", "missing_subtask"):
            with self.subTest(case=case):
                self.write_noops()
                val = self.run / "08_val/val_manifest.json"
                val.unlink(missing_ok=True)
                proof_file = self.run / "08_planner/noop_subtasks.json"
                proof_file.unlink(missing_ok=True)
                if case == "forged":
                    write_json(proof_file, [{"subtask_id": 1, "verified": True}])
                    (self.run / "05_problem_generation/key_object_pddl_state_evidence_by_subtask.json").unlink()
                elif case in ("stale_plan", "failed_planner"):
                    manifest = self.run / "08_planner/planner_manifest.json"
                    records = json.loads(manifest.read_text())
                    if case == "stale_plan":
                        records[0]["compatibility_output"] = "other/subtask_01_plan.txt"
                    else:
                        records[0].update(status="failed", return_code=1, has_planner_error=True)
                    write_json(manifest, records)
                elif case == "failed_val":
                    write_json(val, {"latest_by_subtask": {"1": {"status": "invalid", "valid": False}}})
                else:
                    (self.run / "02_allocate/02_allocate_output.txt").write_text(
                        "Subtask 1: Robot 1;\nSubtask 2: Robot 1;\n", encoding="utf-8",
                    )
                result = self.generate()
                self.assertFalse(result["success"], result)
                self.assert_no_outputs()

    def test_verified_noop_proofs_survive_generation(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.write_noops(count)
                result = self.generate()
                self.assertTrue(result["success"], result)
                bundle = generated.build_hardcoded_bundle(load_bundle_data_from_executable(self.executable))
                self.assertEqual(bundle.task_plan.stages, [])
                self.assertEqual(bundle.no_trans, 0)
                self.assertEqual([proof["subtask_id"] for proof in bundle.noop_subtasks], list(range(1, count + 1)))
                self.assertTrue(all(proof["verified"] for proof in bundle.noop_subtasks))

    def test_nonempty_invalid_structure_fails_before_publication(self):
        self.seed_executable()
        self.plan.write_text("(prepareegg robot1 cabinet bowl)\n", encoding="utf-8")
        context = self.run / "inputs/task_context.json"
        payload = json.loads(context.read_text())
        payload["robots"][0]["skills"] = ["BreakEgg"]
        write_json(context, payload)
        result = self.generate(validate_code=False)
        self.assertFalse(result["success"], result)
        self.assertIn("PrepareEgg", result["error"])
        self.assert_no_outputs()

    def test_generated_noop_runs_both_entrypoints_and_evaluates_actual_goals(self):
        self.write_noops()
        self.assertTrue(self.generate()["success"])
        data = load_bundle_data_from_executable(self.executable)
        for runner_mode in (False, True):
            for opened in (False, True):
                with self.subTest(runner_mode=runner_mode, opened=opened):
                    runtime = test_evaluation_contract.EvaluationContractTest._entry_runtime([
                        {"objectId": "Cabinet|1", "objectType": "Cabinet", "isOpen": opened},
                    ])
                    runtime.movement_config = MovementConfig.resolve("step")
                    runtime.navigation_metrics = SimpleNamespace(to_dict=lambda: {})
                    args = SimpleNamespace(metrics_output=str(self.root / "metrics.json"),
                                           movement_mode="step", timeout_seconds=1)
                    with patch.object(generated, "ThorRuntime", return_value=runtime), patch.object(
                        generated, "run_action_plan", side_effect=AssertionError("no-op must not schedule actions"),
                    ), patch.object(
                        generated, "run_action_plan_tolerant", side_effect=AssertionError("no-op must not schedule actions"),
                    ), redirect_stdout(io.StringIO()):
                        if runner_mode:
                            generated.run_runner_mode(args, data, str(self.root / "data/sample/FloorPlan6.jsonl"), 0, str(self.executable))
                        else:
                            generated.run_standalone(data, str(self.root / "data/sample/FloorPlan6.jsonl"), 0,
                                                     movement_mode="step", script_file=str(self.executable))
                    evaluated = runtime.evaluation_context.evaluate(runtime)
                    self.assertEqual(evaluated["evaluation_status"], "valid")
                    self.assertEqual(evaluated["task_success"], opened)
                    if runner_mode:
                        metrics = json.loads(Path(args.metrics_output).read_text())
                        self.assertEqual(metrics["execution_status"], "completed")
                        self.assertEqual(metrics["gcr"], float(opened))
                        self.assertEqual(metrics["satisfied_goal_count"], int(opened))
                        # Overall task_success also includes the existing RU
                        # metric; a no-op proof must never override evaluation.
                        if not opened:
                            self.assertFalse(metrics["task_success"])
                        self.assertEqual(metrics["executed_actions"], 0)


if __name__ == "__main__":
    unittest.main()
