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


if __name__ == "__main__":
    unittest.main()
