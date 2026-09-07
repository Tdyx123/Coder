import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "execute_plan.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


class ExecutePlanCompatibilityTests(unittest.TestCase):
    def test_package_exports_canonical_plan_and_runtime_service_types(self):
        import executor_system
        from executor_system.action_registry import ActionSpec
        from executor_system.controller_client import ControllerClient
        from executor_system.object_interactor import ObjectInteractor
        from executor_system.object_resolver import ObjectResolver
        from executor_system.plan_types import Action, TaskPlan
        from executor_system.reachable_map import ReachableMapCache
        from executor_system.runtime_artifacts import RuntimeArtifacts
        from executor_system.runtime_metrics import RuntimeMetrics

        self.assertIs(executor_system.Action, Action)
        self.assertIs(executor_system.TaskPlan, TaskPlan)
        self.assertIs(executor_system.ActionSpec, ActionSpec)
        self.assertIs(executor_system.ObjectResolver, ObjectResolver)
        self.assertIs(executor_system.ObjectInteractor, ObjectInteractor)
        self.assertIs(executor_system.ControllerClient, ControllerClient)
        self.assertIs(executor_system.RuntimeArtifacts, RuntimeArtifacts)
        self.assertIs(executor_system.RuntimeMetrics, RuntimeMetrics)
        self.assertIs(executor_system.ReachableMapCache, ReachableMapCache)

    def run_cli(self, working_directory, *arguments):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(ROOT / "scripts"), str(ROOT), environment.get("PYTHONPATH", ""))
            if value
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=working_directory,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_import_has_no_cli_side_effects(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.util; "
                f"spec=importlib.util.spec_from_file_location('execute_plan', {str(SCRIPT)!r}); "
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_verified_generated_runtime_is_preferred_and_return_code_is_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_dir = root / "logs" / "task-run"
            generated = command_dir / "plan_to_code" / "executable_plan.py"
            generated.parent.mkdir(parents=True)
            task_file = root / "FloorPlan9.jsonl"
            task_file.write_text('{}\n', encoding="utf-8")
            generated.write_text(
                "\n".join(
                    [
                        "from executor_system.generated_plan_runtime import main as run_generated_plan",
                        "BUNDLE_DATA = {'task_plan': {'task_id': 'fixture', 'stages': []}, 'gcr': []}",
                        f"TASK_FILE = {str(task_file)!r}",
                        "TASK_INDEX = 0",
                        "if __name__ == '__main__':",
                        "    raise SystemExit(23)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            # An incomplete legacy log must never shadow a generated runtime.
            (command_dir / "log.txt").write_text("robots = []\n", encoding="utf-8")

            completed = self.run_cli(root, "--command", "task-run")

        self.assertEqual(completed.returncode, 23, completed.stderr)

    def test_generated_runtime_arguments_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_dir = root / "selected"
            generated = command_dir / "executable_plan.py"
            command_dir.mkdir()
            task_file = root / "FloorPlan8.jsonl"
            task_file.write_text('{}\n', encoding="utf-8")
            generated.write_text(
                "\n".join(
                    [
                        "import sys",
                        "from executor_system.generated_plan_runtime import main",
                        "BUNDLE_DATA = {'task_plan': {'task_id': 'fixture', 'stages': []}, 'gcr': []}",
                        f"TASK_FILE = {str(task_file)!r}",
                        "TASK_INDEX = 0",
                        "if __name__ == '__main__':",
                        "    raise SystemExit(0 if sys.argv[1:] == ['--execution-policy', 'strict'] else 24)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            completed = self.run_cli(
                root,
                "--command",
                str(command_dir),
                "--execution-policy",
                "strict",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_incomplete_legacy_context_fails_without_fabricating_an_executable(self):
        assignments = {
            "floor_no": "floor_no = 9\n",
            "robots": "robots = [{'name': 'robot1'}]\n",
            "ground_truth": "ground_truth = [{'name': 'Mug'}]\n",
        }
        for omitted in assignments:
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                command_dir = root / "legacy"
                command_dir.mkdir()
                (command_dir / "log.txt").write_text(
                    "".join(value for name, value in assignments.items() if name != omitted),
                    encoding="utf-8",
                )
                (command_dir / "code_plan.py").write_text("Pass(robot1)\n", encoding="utf-8")

                completed = self.run_cli(root, "--command", str(command_dir))

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(omitted, completed.stderr)
                self.assertIn("plantocode.py", completed.stderr)
                self.assertFalse((command_dir / "executable_plan.py").exists())

    def test_legacy_transition_metrics_are_not_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            command_dir = Path(directory)
            (command_dir / "log.txt").write_text(
                "floor_no = 9\n"
                "robots = [{'name': 'robot1'}]\n"
                "ground_truth = [{'name': 'Mug'}]\n",
                encoding="utf-8",
            )
            (command_dir / "code_plan.py").write_text("Pass(robot1)\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "from scripts.execute_plan import compile_aithor_exec_file; "
                    "compile_aithor_exec_file(Path(__import__('sys').argv[1]))",
                    str(command_dir),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no_trans_gt", completed.stderr)
            self.assertIn("max_trans", completed.stderr)
            self.assertFalse((command_dir / "executable_plan.py").exists())


if __name__ == "__main__":
    unittest.main()
