import importlib.util
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "generate_pddlrun_completion_commands.py"
RUNNER_MODULE_PATH = REPO_ROOT / "scripts" / "run_pddlrun_llmseparate_parallel.py"


def load_generator_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"completion command generator is missing: {MODULE_PATH}")
    module_name = "generate_pddlrun_completion_commands_under_test"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_runner_module():
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    module_name = "run_pddlrun_llmseparate_parallel_contract_test"
    spec = importlib.util.spec_from_file_location(module_name, RUNNER_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def create_dataset(repo_root, test_set, floors):
    dataset_dir = repo_root / "data" / test_set
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for floor in floors:
        (dataset_dir / f"FloorPlan{floor}.jsonl").write_text("{}\n", encoding="utf-8")
    return dataset_dir


def create_run(
    repo_root,
    name,
    test_set,
    completed_floors,
    *,
    model="model-main",
    allocate_model=None,
    statuses=None,
):
    allocate_model = model if allocate_model is None else allocate_model
    statuses = statuses or {}
    run_dir = repo_root / "parallel_runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    for floor in completed_floors:
        task_run_dir = repo_root / "task runs" / name / f"FloorPlan{floor}"
        write_json(
            task_run_dir / "run_manifest.json",
            {
                "test_set": test_set,
                "model": model,
                "allocate_model": allocate_model,
            },
        )
        write_json(
            run_dir / f"FloorPlan{floor}" / "summary.json",
            {
                "floor_plan": str(floor),
                "task_count": 1,
                "success_count": int(statuses.get(floor, "success") == "success"),
                "failure_count": int(statuses.get(floor, "success") != "success"),
                "all_pass_count": 0,
                "pass_one_count": 0,
                "results": [
                    {
                        "floor_plan": str(floor),
                        "model": model,
                        "allocate_model": allocate_model,
                        "task_index": 0,
                        "status": statuses.get(floor, "success"),
                        "task_run_dir": str(task_run_dir),
                    }
                ],
            },
        )
    return run_dir


def run_generator(module, repo_root):
    stdout = io.StringIO()
    stderr = io.StringIO()
    return_code = module.run(repo_root, stdout=stdout, stderr=stderr)
    return return_code, stdout.getvalue(), stderr.getvalue()


def command_tokens(stdout):
    lines = [line for line in stdout.splitlines() if line and not line.startswith("#")]
    return [shlex.split(line) for line in lines]


def option_values(tokens, option):
    index = tokens.index(option)
    values = []
    for token in tokens[index + 1 :]:
        if token.startswith("--"):
            break
        values.append(token)
    return values


class CompletionCommandGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.module = load_generator_module()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="completion repo ")
        self.repo_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_repo_root_is_derived_from_script_location(self):
        self.assertEqual(self.module.default_repo_root(), REPO_ROOT)

    def test_strict_majority_counts_failed_floor_and_sorts_missing_floors_numerically(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3, 10, 11])
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_majority",
            "set-a",
            [1, 3, 11],
            statuses={3: "error"},
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(command_tokens(stdout)), 1)
        tokens = command_tokens(stdout)[0]
        self.assertEqual(option_values(tokens, "--floor-plans"), ["2", "10"])
        self.assertEqual(option_values(tokens, "--model"), ["model-main"])
        self.assertEqual(option_values(tokens, "--test-set"), ["set-a"])
        self.assertEqual(option_values(tokens, "--output-root"), [str(run_dir)])
        self.assertIn("--merge-existing-floor-summaries", tokens)
        self.assertNotIn("--allocate-model", tokens)
        self.assertIn("# pddlrun_llmseparate_majority", stdout)
        self.assertIn("3/5", stdout)

    def test_exact_half_complete_top_summary_v2_and_empty_runs_are_silent(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3, 4])
        create_run(
            self.repo_root,
            "pddlrun_llmseparate_exact_half",
            "set-a",
            [1, 2],
        )
        create_run(
            self.repo_root,
            "pddlrun_llmseparate_complete",
            "set-a",
            [1, 2, 3, 4],
        )
        top_summary_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_has_top_summary",
            "set-a",
            [1, 2, 3],
        )
        write_json(top_summary_run / "summary.json", {"already": "complete"})
        create_run(
            self.repo_root,
            "pddlrun_llmseparate_old_v2_variant",
            "set-a",
            [1, 2, 3],
        )
        (self.repo_root / "parallel_runs" / "pddlrun_llmseparate_empty").mkdir(
            parents=True
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual((return_code, stdout, stderr), (0, "", ""))

    def test_shell_quotes_values_and_includes_different_allocate_model(self):
        test_set = "set with spaces"
        create_dataset(self.repo_root, test_set, [1, 2, 10])
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_quoted",
            test_set,
            [1, 10],
            model="model with spaces",
            allocate_model="allocator's model",
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        tokens = command_tokens(stdout)[0]
        self.assertEqual(option_values(tokens, "--floor-plans"), ["2"])
        self.assertEqual(option_values(tokens, "--model"), ["model with spaces"])
        self.assertEqual(option_values(tokens, "--allocate-model"), ["allocator's model"])
        self.assertEqual(option_values(tokens, "--test-set"), [test_set])
        self.assertEqual(option_values(tokens, "--output-root"), [str(run_dir)])
        self.assertIn("'model with spaces'", stdout)
        self.assertIn("'\"'\"'", stdout)

    def test_only_parseable_numeric_floor_summaries_count_as_completed(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_parseable",
            "set-a",
            [1, 3],
        )
        malformed_numeric = run_dir / "FloorPlan2" / "summary.json"
        malformed_numeric.parent.mkdir(parents=True)
        malformed_numeric.write_text("{not json", encoding="utf-8")
        malformed_nonnumeric = run_dir / "FloorPlanX" / "summary.json"
        malformed_nonnumeric.parent.mkdir(parents=True)
        malformed_nonnumeric.write_text("{also not json", encoding="utf-8")

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(option_values(command_tokens(stdout)[0], "--floor-plans"), ["2"])

    def test_structurally_incompatible_floor_summary_fails_only_its_run_closed(self):
        cases = {
            "non-object": [],
            "directory mismatch": {"floor_plan": "9"},
            "missing count": {"remove": "task_count"},
            "boolean count": {"success_count": True},
            "negative count": {"failure_count": -1},
            "non-integer count": {"all_pass_count": "0"},
        }

        for index, (case_name, mutation) in enumerate(cases.items()):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp_dir:
                repo_root = Path(tmp_dir)
                create_dataset(repo_root, "set-a", [1, 2, 3])
                bad_run = create_run(
                    repo_root,
                    f"pddlrun_llmseparate_a_bad_{index}",
                    "set-a",
                    [1, 2],
                )
                valid_run = create_run(
                    repo_root,
                    f"pddlrun_llmseparate_z_valid_{index}",
                    "set-a",
                    [1, 2],
                )
                summary_path = bad_run / "FloorPlan1" / "summary.json"
                if case_name == "non-object":
                    write_json(summary_path, mutation)
                else:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    if "remove" in mutation:
                        del summary[mutation["remove"]]
                    else:
                        summary.update(mutation)
                    write_json(summary_path, summary)

                return_code, stdout, stderr = run_generator(self.module, repo_root)

                self.assertEqual(return_code, 1)
                self.assertEqual(len(command_tokens(stdout)), 1)
                self.assertIn(str(valid_run), command_tokens(stdout)[0])
                self.assertNotIn(str(bad_run), stdout)
                self.assertIn(bad_run.name, stderr)
                self.assertEqual(len(stderr.splitlines()), 1)

    def test_invalid_utf8_floor_summary_is_missing_but_manifest_error_is_isolated(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        missing_floor_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_a_invalid_floor_utf8",
            "set-a",
            [1, 3],
        )
        invalid_floor_path = missing_floor_run / "FloorPlan2" / "summary.json"
        invalid_floor_path.parent.mkdir(parents=True)
        invalid_floor_path.write_bytes(b"\xff\xfe")

        bad_manifest_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_b_invalid_manifest_utf8",
            "set-a",
            [1, 2],
        )
        invalid_manifest_path = (
            self.repo_root
            / "task runs"
            / bad_manifest_run.name
            / "FloorPlan1"
            / "run_manifest.json"
        )
        invalid_manifest_path.write_bytes(b"\xff\xfe")
        valid_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_z_valid_after_utf8",
            "set-a",
            [1, 2],
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(len(command_tokens(stdout)), 2)
        emitted_runs = "\n".join(" ".join(tokens) for tokens in command_tokens(stdout))
        self.assertIn(str(missing_floor_run), emitted_runs)
        self.assertIn(str(valid_run), emitted_runs)
        self.assertNotIn(str(bad_manifest_run), stdout)
        self.assertIn(bad_manifest_run.name, stderr)
        self.assertIn(str(invalid_manifest_path), stderr)
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_metadata_conflict_reports_run_error_but_keeps_valid_command(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        valid_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_valid",
            "set-a",
            [1, 2],
        )
        bad_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_bad_model",
            "set-a",
            [1, 2],
        )
        bad_manifest = (
            self.repo_root
            / "task runs"
            / bad_run.name
            / "FloorPlan1"
            / "run_manifest.json"
        )
        manifest = json.loads(bad_manifest.read_text(encoding="utf-8"))
        manifest["model"] = "conflicting-model"
        write_json(bad_manifest, manifest)

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(len(command_tokens(stdout)), 1)
        self.assertIn(str(valid_run), command_tokens(stdout)[0])
        self.assertNotIn(str(bad_run), stdout)
        self.assertIn("pddlrun_llmseparate_bad_model", stderr)
        self.assertIn("model", stderr)
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_missing_referenced_metadata_fails_closed_for_each_bad_run(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        missing_field = create_run(
            self.repo_root,
            "pddlrun_llmseparate_missing_test_set",
            "set-a",
            [1, 2],
        )
        manifest_path = (
            self.repo_root
            / "task runs"
            / missing_field.name
            / "FloorPlan1"
            / "run_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["test_set"]
        write_json(manifest_path, manifest)

        missing_record_model = create_run(
            self.repo_root,
            "pddlrun_llmseparate_missing_record_model",
            "set-a",
            [1, 2],
        )
        summary_path = missing_record_model / "FloorPlan1" / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        del summary["results"][0]["allocate_model"]
        write_json(summary_path, summary)

        missing_manifest = create_run(
            self.repo_root,
            "pddlrun_llmseparate_missing_manifest",
            "set-a",
            [1, 2],
        )
        (
            self.repo_root
            / "task runs"
            / missing_manifest.name
            / "FloorPlan1"
            / "run_manifest.json"
        ).unlink()

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        for run_dir in (missing_field, missing_record_model, missing_manifest):
            self.assertIn(run_dir.name, stderr)
        self.assertEqual(len(stderr.splitlines()), 3)

    def test_dataset_errors_and_completed_floor_outside_target_fail_closed(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        missing_dataset = create_run(
            self.repo_root,
            "pddlrun_llmseparate_missing_dataset",
            "unknown-set",
            [1, 2],
        )
        outside_target = create_run(
            self.repo_root,
            "pddlrun_llmseparate_outside_target",
            "set-a",
            [1, 4],
        )
        decoy_set = self.repo_root / "data" / "decoy-only"
        decoy_set.mkdir(parents=True)
        (decoy_set / "FloorPlan1.jsonl.bak").write_text("{}\n", encoding="utf-8")
        (decoy_set / "FloorPlanX.jsonl").write_text("{}\n", encoding="utf-8")
        nested = decoy_set / "nested"
        nested.mkdir()
        (nested / "FloorPlan1.jsonl").write_text("{}\n", encoding="utf-8")
        no_exact_targets = create_run(
            self.repo_root,
            "pddlrun_llmseparate_no_exact_targets",
            "decoy-only",
            [1],
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        for run_dir in (missing_dataset, outside_target, no_exact_targets):
            self.assertIn(run_dir.name, stderr)
        self.assertIn("FloorPlan4", stderr)
        self.assertEqual(len(stderr.splitlines()), 3)

    def test_absolute_test_set_cannot_enumerate_dataset_outside_data_root(self):
        outside_dataset = self.repo_root / "absolute dataset"
        outside_dataset.mkdir()
        for floor in (1, 2, 3):
            (outside_dataset / f"FloorPlan{floor}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_absolute_test_set",
            str(outside_dataset),
            [1, 2],
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(run_dir.name, stderr)
        self.assertIn("test_set", stderr)

    def test_traversing_test_set_cannot_enumerate_dataset_outside_data_root(self):
        (self.repo_root / "data").mkdir()
        outside_dataset = self.repo_root / "escaped-set"
        outside_dataset.mkdir()
        for floor in (1, 2, 3):
            (outside_dataset / f"FloorPlan{floor}.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_traversing_test_set",
            "../escaped-set",
            [1, 2],
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(run_dir.name, stderr)
        self.assertIn("test_set", stderr)

    def test_dataset_symlink_loop_error_does_not_suppress_valid_run_command(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        loop_path = self.repo_root / "data" / "loop-set"
        loop_path.symlink_to("loop-set", target_is_directory=True)
        bad_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_a_symlink_loop",
            "loop-set",
            [1, 2],
        )
        valid_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_z_valid_after_loop",
            "set-a",
            [1, 2],
        )

        try:
            return_code, stdout, stderr = run_generator(self.module, self.repo_root)
        except RuntimeError as exc:
            self.fail(
                f"scanner leaked RuntimeError and suppressed valid output: {exc}"
            )

        self.assertEqual(return_code, 1)
        self.assertEqual(len(command_tokens(stdout)), 1)
        self.assertIn(str(valid_run), command_tokens(stdout)[0])
        self.assertNotIn(str(bad_run), stdout)
        self.assertIn(bad_run.name, stderr)
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_dataset_file_symlink_escape_and_loop_do_not_suppress_valid_run(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        outside_file = self.repo_root / "outside.jsonl"
        outside_file.write_text("{}\n", encoding="utf-8")

        escape_dataset = create_dataset(self.repo_root, "escape-set", [1, 2])
        (escape_dataset / "FloorPlan3.jsonl").symlink_to(outside_file)
        escape_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_a_file_escape",
            "escape-set",
            [1, 2],
        )

        loop_dataset = create_dataset(self.repo_root, "loop-file-set", [1, 2])
        (loop_dataset / "FloorPlan3.jsonl").symlink_to("FloorPlan3.jsonl")
        loop_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_b_file_loop",
            "loop-file-set",
            [1, 2],
        )
        valid_run = create_run(
            self.repo_root,
            "pddlrun_llmseparate_z_valid_after_file_links",
            "set-a",
            [1, 2],
        )

        return_code, stdout, stderr = run_generator(self.module, self.repo_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(len(command_tokens(stdout)), 1)
        self.assertIn(str(valid_run), command_tokens(stdout)[0])
        self.assertNotIn(str(escape_run), stdout)
        self.assertNotIn(str(loop_run), stdout)
        self.assertIn(escape_run.name, stderr)
        self.assertIn(loop_run.name, stderr)
        self.assertEqual(len(stderr.splitlines()), 2)

    def test_noncanonical_dataset_floor_names_fail_closed_without_deduplication(self):
        for case_name, canonical_present in (("alias-only", False), ("duplicate", True)):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp_dir:
                repo_root = Path(tmp_dir)
                dataset_dir = create_dataset(repo_root, "set-a", [2, 3])
                if canonical_present:
                    (dataset_dir / "FloorPlan1.jsonl").write_text(
                        "{}\n", encoding="utf-8"
                    )
                (dataset_dir / "FloorPlan01.jsonl").write_text(
                    "{}\n", encoding="utf-8"
                )
                bad_run = create_run(
                    repo_root,
                    "pddlrun_llmseparate_bad_alias",
                    "set-a",
                    [2, 3],
                )

                return_code, stdout, stderr = run_generator(self.module, repo_root)

                self.assertEqual(return_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn(bad_run.name, stderr)
                self.assertIn("FloorPlan01.jsonl", stderr)

    def test_generator_accepted_summaries_satisfy_runner_merge_contract(self):
        create_dataset(self.repo_root, "set-a", [1, 2, 3])
        run_dir = create_run(
            self.repo_root,
            "pddlrun_llmseparate_contract",
            "set-a",
            [1, 2],
        )

        plan = self.module.evaluate_run(self.repo_root, run_dir)
        loaded = load_runner_module().load_floor_plan_summaries(run_dir)

        self.assertIsNotNone(plan)
        self.assertEqual([summary["floor_plan"] for summary in loaded], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
