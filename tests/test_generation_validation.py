"""Generation must reject unusable plans before writing executor scripts."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from baseline_converters import common, cot, kglamp, lammap, pddlrun, scale_plan, smart_llm
from baseline_converters.generation_validation import GenerationValidationError, validate_generation_plan
import resources.robots as robot_catalog


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class GenerationValidationIntegrationTests(unittest.TestCase):
    backends = (pddlrun, lammap, smart_llm, scale_plan, kglamp, cot)

    def fixture(self, root, backend, *, skills=None, mass=2.0, capacity=1.0, dry_run=False):
        task = "pick up the mug"
        run = root / "logs" / "unit" / "pickup" / "run1"
        run.mkdir(parents=True)
        record = {"task": task, "robot list": [5], "object_states": []}
        write_json(root / "data/unit/FloorPlan1.jsonl", record)
        robot = {
            "name": "robot5" if backend is scale_plan else "robot1",
            "skills": ["PickupObject"] if skills is None else skills,
            "mass_capacity": capacity,
        }
        objects = [{"name": "Mug", "mass": mass}] if mass is not None else [{"name": "Mug"}]
        context = {"task": task, "robots": [robot], "objects": objects,
                   "objects_ai": f"objects = {objects!r}", "scene_objects": objects}
        manifest = {"task": task, "test_set": "unit", "floor_plan": "1", "task_index": 0,
                    "repo_root": str(root), "status": "success"}
        write_json(run / "run_manifest.json", manifest)
        write_json(run / "inputs/task_context.json", context)
        executable = run / "plan_to_code/executable_plan.py"
        if backend is pddlrun:
            allocation = run / "02_allocate/02_allocate_output.txt"
            allocation.parent.mkdir()
            allocation.write_text("Subtask 1: Robot 1;\n")
            plan = run / "08_planner/outputs/subtask_01_plan.txt"
            plan.parent.mkdir(parents=True)
            plan.write_text("(pickupobject robot1 mug)\n")
            write_json(run / "08_planner/planner_manifest.json", [{"plan_file": str(plan)}])
            invoke = lambda: backend.process_task_run(run, validate_code=False)
        elif backend is lammap:
            plan = run / "08_final_match/02_final_plan.txt"
            plan.parent.mkdir()
            plan.write_text("(pickupobject robot1 mug)\n")
            metadata = {backend.task_run_key(run): {"test_set": "unit"}}
            invoke = lambda: backend.process_task_run(run, metadata, dry_run=dry_run, validate_code=False)
        elif backend is smart_llm:
            source = run / "code_plan.py"
            source.write_text("def pickup(robot):\n    PickupObject(robot, 'Mug')\npickup(robots[0])\n")
            (run / "log.txt").write_text(
                f"{task}\nFloor Plan: 1\nobjects = {objects!r}\nrobots = {[robot]!r}\ntest-set: unit\n"
            )
            output = root / "generated"
            executable, _, _ = backend.output_paths_for(source, root / "logs", output)
            invoke = lambda: asdict(backend.convert_one(source, root / "logs", output, dry_run, False))
        elif backend is scale_plan:
            write_json(run / "05_plan/03_final_plan.json", {"stages": [{"plans": [
                {"robot": "robot5", "plan": "(pickupobject robot5 mug)"}
            ]}]})
            indexed = backend.IndexedRun("unit", manifest, str(run), run)
            invoke = lambda: backend.process_indexed_run(indexed, dry_run=dry_run, validate_code=False)
        elif backend is kglamp:
            write_json(run / "00_inputs/task_context.json", context)
            plan = run / "09_replan/final_plan.txt"
            plan.parent.mkdir()
            plan.write_text("(pickupobject robot1 mug)\n")
            indexed = backend.SummaryRun(source_summary=root / "summary.json", metadata=manifest,
                                         raw_run_dir=str(run), task_run_dir=run)
            invoke = lambda: backend.process_task_run(indexed, dry_run=dry_run, validate_code=False)
        else:
            robot.update(symbol="robot1")
            write_json(run / "00_inputs/task_context.json", context)
            write_json(run / "02_plan/03_validation.json", {"valid": True})
            write_json(run / "02_plan/01_final_plan.json", {"plan": [
                {"action": "PickupObject", "arguments": ["robot5", "mug"], "reasoning_step": 1}
            ]})
            indexed = backend.SummaryRun(source_summary=root / "summary.json", floor_summary=root / "floor.json",
                                         parallel_run_root=root, metadata=manifest, raw_run_dir=str(run), task_run_dir=run)
            def invoke():
                # COT reads capabilities from the catalog, not task-context robots.
                with patch.dict(robot_catalog.robots[4], skills=robot['skills'], mass_capacity=capacity):
                    return backend.process_task_run(indexed, repo_root=root, dry_run=dry_run, validate_code=False)
        return invoke, executable

    def test_all_converters_reject_mass_skill_and_missing_data_without_compile_validation(self):
        for backend in self.backends:
            for skills, mass, expected in [
                (["PickupObject"], 2.0, "mass_exceeded"),
                ([], 2.0, "missing_skill"),
                (["PickupObject"], None, "validation_data_missing"),
            ]:
                with self.subTest(backend=backend.__name__, reason=expected), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    invoke, executable = self.fixture(root, backend, skills=skills, mass=mass)
                    executable.parent.mkdir(parents=True)
                    executable.write_text("# stale generated script\n")
                    with patch.object(backend, "REPO_ROOT", root):
                        result = invoke()
                    self.assertEqual(result["status"], "failed", result)
                    self.assertEqual(result.get("failure_reason"), expected, result)
                    self.assertEqual(result["validation_error"]["robot_id"], "robot5" if backend is cot else "robot1")
                    self.assertEqual(result["validation_error"]["action_type"], "PickupObject")
                    self.assertFalse(executable.exists())
                    if backend is smart_llm:
                        self.assertIsNone(result["initial_skip_reason"])
                        self.assertEqual(result["recovery_events"], [])

    def test_all_converters_accept_equal_capacity(self):
        for backend in self.backends:
            with self.subTest(backend=backend.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                invoke, executable = self.fixture(root, backend, mass=1.0)
                with patch.object(backend, "REPO_ROOT", root):
                    result = invoke()
                self.assertTrue(result["success"], result)
                self.assertTrue(executable.is_file())

    def test_missing_robot_list_uses_dataset_identity_or_fails_with_cleanup(self):
        for backend in self.backends:
            for known_team in (True, False):
                with self.subTest(backend=backend.__name__, known_team=known_team), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    invoke, executable = self.fixture(root, backend, mass=0.5)
                    if backend is smart_llm:
                        log = next((root / "logs").rglob("log.txt"))
                        log.write_text("\n".join(line for line in log.read_text().splitlines() if not line.startswith("robots =")))
                    else:
                        for context_path in (root / "logs").rglob("task_context.json"):
                            context = json.loads(context_path.read_text())
                            context["robots"] = []
                            write_json(context_path, context)
                    if not known_team:
                        dataset_path = root / "data/unit/FloorPlan1.jsonl"
                        record = json.loads(dataset_path.read_text())
                        record["robot list"] = []
                        write_json(dataset_path, record)
                    executable.parent.mkdir(parents=True)
                    executable.write_text("# stale\n")
                    with patch.object(backend, "REPO_ROOT", root):
                        result = invoke()
                    if known_team:
                        self.assertTrue(result["success"], result)
                        self.assertNotEqual(executable.read_text(), "# stale\n")
                    else:
                        self.assertEqual(result["status"], "failed", result)
                        self.assertEqual(result.get("failure_reason"), "validation_data_missing", result)
                        self.assertFalse(executable.exists())

    def test_dry_run_rejects_plan_without_removing_previous_script(self):
        for backend in self.backends[1:]:
            with self.subTest(backend=backend.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                invoke, executable = self.fixture(root, backend, dry_run=True)
                executable.parent.mkdir(parents=True)
                executable.write_text("# previous script\n")
                with patch.object(backend, "REPO_ROOT", root):
                    result = invoke()
                self.assertEqual(result.get("failure_reason"), "mass_exceeded", result)
                self.assertEqual(executable.read_text(), "# previous script\n")

    def test_reports_count_only_the_first_failure_per_task(self):
        for backend in self.backends:
            with self.subTest(backend=backend.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                results = []
                for index, (skills, mass) in enumerate([([], 2), (["PickupObject"], 2), (["PickupObject"], None), (["PickupObject"], 1)]):
                    fixture_root = root / str(index)
                    invoke, _ = self.fixture(fixture_root, backend, skills=skills, mass=mass)
                    with patch.object(backend, "REPO_ROOT", fixture_root):
                        results.append(invoke())
                output = root / "report"
                with redirect_stdout(StringIO()):
                    if backend is pddlrun:
                        backend.write_summary(results, output)
                    elif backend is lammap:
                        backend.write_global_summary(results, output, False)
                    elif backend is smart_llm:
                        entries = [backend.ConversionResult(**result) for result in results]
                        summary = backend.build_global_summary(entries, root, output, False, 0)
                        backend.write_global_summary(entries, output, summary)
                    else:
                        backend.write_plan_to_code_summary(results, output)
                summary = json.loads((output / "plan_to_code_summary.json").read_text())
                self.assertEqual(summary["failed_generations"], 3)
                self.assertEqual(summary["successful_generations"], 1)
                self.assertEqual(summary["mass_failed_generations"], 1)
                self.assertEqual(summary["skill_failed_generations"], 1)
                saved = json.loads((output / "plan_to_code_results.json").read_text())
                self.assertEqual([r["failure_reason"] for r in saved], ["missing_skill", "mass_exceeded", "validation_data_missing", None])

    def test_smart_recovered_plan_is_checked_and_failure_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoke, executable = self.fixture(root, smart_llm)
            source = next((root / "logs").rglob("code_plan.py"))
            source.write_text("def pickup(robot):\n    PickupObject(robot, 'Mug')\n")
            with patch.object(smart_llm, "REPO_ROOT", root):
                result = invoke()
            self.assertEqual(result["failure_reason"], "mass_exceeded", result)
            self.assertEqual(result["conversion_kind"], "failed")
            self.assertFalse(executable.exists())
            saved = json.loads(executable.with_name("conversion_summary.json").read_text())
            self.assertEqual(saved["failure_reason"], "mass_exceeded")

    def test_smart_recovery_keeps_invalid_metadata_for_ordered_validation(self):
        for field in ("mass", "mass_capacity", "skills"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                invoke, executable = self.fixture(root, smart_llm)
                source = next((root / "logs").rglob("code_plan.py"))
                source.write_text("def pickup(robot):\n    PickupObject(robot, 'Mug')\n")
                log = source.with_name("log.txt")
                if field == "mass":
                    log.write_text(log.read_text().replace("'mass': 2.0", "'mass': 'bad'"))
                elif field == "mass_capacity":
                    log.write_text(log.read_text().replace("'mass_capacity': 1.0", "'mass_capacity': 'bad'"))
                else:
                    log.write_text(log.read_text().replace("'skills': ['PickupObject']", "'skills': None"))
                executable.parent.mkdir(parents=True)
                executable.write_text("# stale\n")
                with patch.object(smart_llm, "REPO_ROOT", root):
                    result = invoke()
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result.get("failure_reason"), "validation_data_missing", result)
                self.assertFalse(executable.exists())

    def test_smart_malformed_assignments_cannot_be_replaced_by_defaults(self):
        for field in ("robots", "objects"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                invoke, executable = self.fixture(root, smart_llm, mass=0.5)
                log = next((root / "logs").rglob("log.txt"))
                text = log.read_text()
                if field == "robots":
                    text = text.replace("'mass_capacity': 1.0", "'mass_capacity': nan")
                else:
                    text = text.replace("'mass': 0.5", "'mass': nan")
                    write_json(root / "data/ai2thor_objects_cache/FloorPlan1.json", [{"name": "Mug", "mass": 0.5}])
                log.write_text(text)
                with patch.object(smart_llm, "REPO_ROOT", root):
                    result = invoke()
                self.assertEqual(result.get("failure_reason"), "validation_data_missing", result)
                self.assertFalse(executable.exists())

    def test_kglamp_uses_resolved_custom_dataset_for_robot_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoke, _ = self.fixture(root, kglamp, mass=0.5)
            dataset = root / "custom/tasks.jsonl"
            dataset.parent.mkdir()
            (root / "data/unit/FloorPlan1.jsonl").rename(dataset)
            manifest_path = next((root / "logs").rglob("run_manifest.json"))
            manifest = json.loads(manifest_path.read_text())
            manifest["dataset_file"] = str(dataset)
            write_json(manifest_path, manifest)
            context_path = next((root / "logs").rglob("00_inputs/task_context.json"))
            context = json.loads(context_path.read_text())
            context["robots"] = [{"name": "robot1"}]
            write_json(context_path, context)
            with patch.object(kglamp, "REPO_ROOT", root):
                result = invoke()
            self.assertTrue(result["success"], result)

    def test_cot_duplicate_dataset_robot_ids_fail_with_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoke, executable = self.fixture(root, cot)
            dataset_path = root / 'data/unit/FloorPlan1.jsonl'
            record = json.loads(dataset_path.read_text())
            record['robot list'] = [5, 5]
            write_json(dataset_path, record)
            executable.parent.mkdir(parents=True)
            executable.write_text("# stale\n")
            result = invoke()
            self.assertEqual(result.get("failure_reason"), "validation_data_missing", result)
            self.assertFalse(executable.exists())

    def test_smart_batch_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, smart_llm)
            output = root / "dry-output"
            with patch.object(smart_llm, "REPO_ROOT", root), redirect_stdout(StringIO()):
                smart_llm.convert(input_root=root / "logs", output_root=output, dry_run=True)
            self.assertFalse(output.exists())

    def test_pddlrun_batch_continues_after_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, mass in enumerate((2, 1)):
                self.fixture(root / str(index), pddlrun, mass=mass)
            with redirect_stdout(StringIO()):
                code = pddlrun.convert(logs_dir=root, output_dir=root / "summary", validate_code=False)
            self.assertEqual(code, 1)
            summary = json.loads((root / "summary/plan_to_code_summary.json").read_text())
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(summary["mass_failed_generations"], 1)

    def test_cot_later_missing_skill_does_not_hide_earlier_mass_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoke, _ = self.fixture(root, cot)
            plan_path = next((root / "logs").rglob("01_final_plan.json"))
            plan = json.loads(plan_path.read_text())
            plan["plan"].append({"action": "BreakObject", "arguments": ["robot5", "mug"], "reasoning_step": 2})
            write_json(plan_path, plan)
            self.assertEqual(invoke()["failure_reason"], "mass_exceeded")

    def test_pddlrun_allocation_fallback_still_checks_final_robot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoke, _ = self.fixture(root, pddlrun, skills=[])
            allocation = next((root / "logs").rglob("02_allocate_output.txt"))
            allocation.write_text("Subtask 1: Robot 99;\n")
            result = invoke()
            self.assertEqual(result["failure_reason"], "missing_skill")
            self.assertEqual(result["validation_error"]["robot_id"], "robot1")


def action(name, *args):
    return {"action_type": name, "parameters": {"args": list(args)}}


class GenerationValidationRuleTests(unittest.TestCase):
    def validate(self, actions, **overrides):
        options = dict(robots=[{"name": "robot1", "skills": ["PickupObject"], "mass_capacity": 1}],
                       objects=[{"name": "Mug", "mass": 0.5}], repo_root=Path("/nonexistent-fixture"), floor_plan=1)
        options.update(overrides)
        validate_generation_plan({"stages": [{"stage_id": "first", "robot_action_queues": {"robot1": actions}}]}, **options)

    def test_mass_limits_and_non_pickup_actions(self):
        self.validate([action("PickupObject", "Mug")])
        self.validate([action("OpenObject", "Cabinet"), action("RunMicrowave", "Microwave", "Mug")],
                      robots=[{"name": "robot1", "skills": ["OpenObject", "RunMicrowave"]}], objects=[])

    def test_skills_aliases_and_waits(self):
        for skill in ("PrepareEgg", "BreakEgg"):
            self.validate([action("PrepareEgg", "Egg", "Pan"), action("BreakEgg", "Egg"), action("WaitOneTick")],
                          robots=[{"name": "robot1", "skills": [skill]}], objects=[])

    def test_first_action_failure_precedes_later_skill_failure(self):
        with self.assertRaises(GenerationValidationError) as caught:
            self.validate([action("PickupObject", "Mug"), action("BreakObject", "Mug")], objects=[{"name": "Mug", "mass": 2}])
        self.assertEqual(caught.exception.reason, "mass_exceeded")
        self.assertEqual(caught.exception.details["action_index"], 0)

    def test_missing_robot_fields_use_explicit_catalog_identity(self):
        self.validate([action("PickupObject", "Mug")], robots=[{"name": "robot1", "source_id": 5}])
        self.validate([action("PickupObject", "Mug")], robots=[{"name": "robot1"}], task_record={"robot list": [5]})
        with self.assertRaises(GenerationValidationError) as caught:
            self.validate([action("PickupObject", "Mug")], robots=[{"name": "robot1"}])
        self.assertEqual(caught.exception.reason, "validation_data_missing")

    def test_invalid_metadata_is_not_replaced_by_catalog_defaults(self):
        for field, value in (("skills", None), ("skills", 7), ("mass_capacity", None), ("mass_capacity", -1), ("mass_capacity", True), ("mass_capacity", float("inf"))):
            robot = {"name": "robot1", "source_id": 5, "skills": ["PickupObject"], "mass_capacity": 1, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(GenerationValidationError) as caught:
                self.validate([action("PickupObject", "Mug")], robots=[robot])
            self.assertEqual(caught.exception.reason, "validation_data_missing")

    def test_invalid_and_ambiguous_object_mass_fails(self):
        for objects in ([{"name": "Mug", "mass": -1}], [{"name": "Mug", "mass": float("nan")}],
                        [{"name": "Mug", "mass": 0.1}, {"name": "Mug", "mass": 2}]):
            with self.subTest(objects=objects), self.assertRaises(GenerationValidationError) as caught:
                self.validate([action("PickupObject", "Mug")], objects=objects)
            self.assertEqual(caught.exception.reason, "validation_data_missing")

    def test_partial_run_masses_cannot_be_overwritten_by_type_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "data/ai2thor_objects_cache/FloorPlan1.json", [{"name": "Mug", "mass": 0.5}])
            with self.assertRaises(GenerationValidationError) as caught:
                self.validate([action("PickupObject", "Mug")], repo_root=root,
                              objects=[{"name": "Mug", "mass": 2}, {"name": "Mug"}])
            self.assertEqual(caught.exception.reason, "validation_data_missing")

    def test_mass_cache_fills_missing_metadata_and_run_mass_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "data/ai2thor_objects_cache/FloorPlan1.json", [{"name": "Mug", "mass": 2}])
            self.validate([action("PickupObject", "Mug")], repo_root=root)
            with self.assertRaises(GenerationValidationError) as caught:
                self.validate([action("PickupObject", "Mug")], repo_root=root, objects=[{"name": "Mug"}])
            self.assertEqual(caught.exception.reason, "mass_exceeded")

    def test_bound_object_ids_keep_coordinate_signs_and_select_one_instance(self):
        objects = [{"objectId": "Mug|+01.00|+00.00|+00.00", "objectType": "Mug", "mass": 2},
                   {"objectId": "Mug|-01.00|+00.00|+00.00", "objectType": "Mug", "mass": 0.5}]
        binding = {"object": "Mug_2", "object_type": "Mug", "number": 2,
                   "object_id": "Mug|-01.00|+00.00|+00.00"}
        self.validate([action("PickupObject", "Mug_2")], objects=objects, object_id_bindings=[binding])

    def test_bound_cache_instance_precedes_type_only_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "data/ai2thor_objects_cache/FloorPlan1.json", [
                {"objectId": "Mug|+01.00|+00.00|+00.00", "objectType": "Mug", "mass": 2}])
            with self.assertRaises(GenerationValidationError) as caught:
                self.validate([action("PickupObject", "Mug_1")], repo_root=root,
                              object_mappings={"Mug_1": "Mug|+01.00|+00.00|+00.00"})
            self.assertEqual(caught.exception.reason, "mass_exceeded")

    def test_missing_bound_instance_does_not_borrow_another_instances_mass(self):
        with self.assertRaises(GenerationValidationError) as caught:
            self.validate([action("PickupObject", "Mug|+02.00|+00.00|+00.00")], objects=[
                {"objectId": "Mug|+01.00|+00.00|+00.00", "objectType": "Mug", "mass": 0.1}])
        self.assertEqual(caught.exception.reason, "validation_data_missing")

    def test_stage_and_queue_order_define_first_failure(self):
        plan = {"stages": [
            {"stage_id": "first", "robot_action_queues": {
                "robot2": [action("PickupObject", "Mug")], "robot1": [action("BreakObject", "Mug")]}},
            {"stage_id": "second", "robot_action_queues": {"robot2": [action("BreakObject", "Mug")]}},
        ]}
        with self.assertRaises(GenerationValidationError) as caught:
            validate_generation_plan(plan, robots=[{"name": "robot1", "skills": []},
                {"name": "robot2", "skills": ["PickupObject"], "mass_capacity": 1}],
                objects=[{"name": "Mug", "mass": 2}], floor_plan=1, repo_root=Path("/nonexistent-fixture"))
        self.assertEqual(caught.exception.reason, "mass_exceeded")
        self.assertEqual(caught.exception.details["robot_id"], "robot2")
        self.assertEqual(caught.exception.details["stage_id"], "first")


if __name__ == "__main__":
    unittest.main()
