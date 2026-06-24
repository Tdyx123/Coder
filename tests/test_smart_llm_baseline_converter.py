import ast
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "baselines" / "SMART-LLM.py"

spec = importlib.util.spec_from_file_location("smart_llm_converter", SCRIPT_PATH)
smart_llm_converter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smart_llm_converter
spec.loader.exec_module(smart_llm_converter)


class SmartLLMBaselineConverterTest(unittest.TestCase):
    @contextmanager
    def patched_repo_root(self, repo_root: Path):
        old_repo_root = smart_llm_converter.REPO_ROOT
        smart_llm_converter.REPO_ROOT = repo_root
        try:
            yield
        finally:
            smart_llm_converter.REPO_ROOT = old_repo_root

    def write_code_plan(self, input_root: Path, floor: str, task: str, content: str) -> Path:
        path = input_root / floor / task / "code_plan.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_log(
        self,
        source_path: Path,
        *,
        task_text: str = "open the drawer",
        floor: str = "2",
        test_set: str = "unit_set",
        robots=None,
        objects=None,
    ) -> None:
        robots = robots or [{"name": "robot1"}]
        objects = objects or [{"name": "Drawer"}]
        (source_path.parent / "log.txt").write_text(
            "\n".join(
                [
                    task_text,
                    "",
                    f"Floor Plan: {floor}",
                    "",
                    f"objects = {objects!r}",
                    f"robots = {robots!r}",
                    "trans = 0",
                    "max_trans = 0",
                    f"test-set: {test_set}",
                ]
            ),
            encoding="utf-8",
        )

    def write_dataset(
        self,
        repo_root: Path,
        *,
        floor: str = "2",
        test_set: str = "unit_set",
        tasks=None,
        robot_count: int = 1,
    ) -> Path:
        tasks = tasks or ["open the drawer"]
        path = repo_root / "data" / test_set / f"FloorPlan{floor}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "task": task,
                "robot list": list(range(1, robot_count + 1)),
                "object_states": [],
                "trans": 0,
                "min_trans": 0,
            }
            for task in tasks
        ]
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return path

    def bundle_from_executable(self, executable: str):
        tree = ast.parse(executable)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "BUNDLE_DATA" for target in node.targets):
                    return ast.literal_eval(node.value)
        self.fail("BUNDLE_DATA assignment not found")

    def test_extract_code_uses_longest_fenced_python_block(self):
        extracted = smart_llm_converter.extract_code(
            """
Reasoning text.
```python
def short(robot):
    pass
```
More text.
```python
def longer(robot):
    GoToObject(robot, 'Apple')
    PickupObject(robot, 'Apple')
```
"""
        )

        self.assertEqual(extracted.method, "fenced_python_block")
        self.assertEqual(extracted.block_count, 2)
        self.assertIn("def longer", extracted.code)
        self.assertNotIn("```", extracted.code)
        self.assertNotIn("Reasoning text", extracted.code)

    def test_extract_code_falls_back_to_first_code_line(self):
        extracted = smart_llm_converter.extract_code(
            """
Natural-language analysis first.

def open_drawer(robot):
    OpenObject(robot, 'Drawer')
"""
        )

        self.assertEqual(extracted.method, "fallback_first_code_line")
        self.assertEqual(extracted.code.splitlines()[0], "def open_drawer(robot):")

    def test_classify_schedule_types_from_ast(self):
        cases = [
            (
                "single_wrapper_sequential",
                "def task(robot):\n    pass\ntask(robots[0])\n",
            ),
            (
                "multi_function_sequential",
                "def a(robot):\n    pass\n\ndef b(robot):\n    pass\na(robots[0])\nb(robots[1])\n",
            ),
            (
                "threaded_parallel",
                (
                    "import threading\n"
                    "def a(robot):\n    pass\n"
                    "t = threading.Thread(target=a, args=(robots[0],))\n"
                    "t.start()\n"
                    "t.join()\n"
                ),
            ),
            (
                "staged_mixed",
                (
                    "import threading\n"
                    "def a(robot):\n    pass\n"
                    "def b(robot):\n    pass\n"
                    "a(robots[0])\n"
                    "t = threading.Thread(target=b, args=(robots[1],))\n"
                    "t.start()\n"
                    "t.join()\n"
                ),
            ),
        ]

        for expected, code in cases:
            with self.subTest(expected=expected):
                classification, tree = smart_llm_converter.classify_code(code)
                self.assertIsNotNone(tree)
                self.assertEqual(classification.parse_status, "parse_ok")
                self.assertEqual(classification.schedule_type, expected)

    def test_syntax_error_is_skipped_without_generated_code(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "bad_task",
                "Analysis\n```python\ndef broken(:\n    pass\n```\n",
            )

            result = smart_llm_converter.convert_one(
                source_path,
                input_root=input_root,
                output_root=output_root,
                dry_run=False,
                validate_code=True,
            )

            self.assertEqual(result.status, "skipped")
            self.assertFalse(result.success)
            self.assertEqual(result.parse_status, "syntax_error")
            self.assertEqual(result.skip_reason, "syntax_error")
            self.assertIsNone(result.generated["executable_plan"])
            self.assertFalse((output_root / "logs" / "2" / "bad_task" / "plan_to_code" / "executable_plan.py").exists())
            self.assertTrue((output_root / "logs" / "2" / "bad_task" / "plan_to_code" / "conversion_summary.json").exists())

    def test_dry_run_writes_plantocode_summary_without_executable_plan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "task",
                "Text\n```python\ndef open_drawer(robot):\n    OpenObject(robot, 'Drawer')\nopen_drawer(robots[0])\n```\n",
            )
            self.write_log(source_path)
            self.write_dataset(root)

            with self.patched_repo_root(root):
                with redirect_stdout(io.StringIO()):
                    status = smart_llm_converter.main(
                        [
                            "--input-root",
                            str(input_root),
                            "--output-root",
                            str(output_root),
                            "--dry-run",
                        ]
                    )

            self.assertEqual(status, 0)
            summary = json.loads((output_root / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((output_root / "plan_to_code_results.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["successful_generations"], 1)
            self.assertTrue(summary["dry_run"])
            self.assertEqual(details[0]["status"], "success")
            self.assertFalse((output_root / "logs" / "2" / "task" / "plan_to_code" / "executable_plan.py").exists())

    def test_successful_conversion_writes_clean_bundle_executable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "task",
                (
                    "Reasoning that should not be copied.\n"
                    "```python\n"
                    "def open_drawer(robot):\n"
                    "    GoToObject(robot, 'Drawer')\n"
                    "    OpenObject(robot, 'Drawer')\n\n"
                    "open_drawer(robots[0])\n"
                    "```\n"
                ),
            )
            self.write_log(source_path)
            self.write_dataset(root)

            with self.patched_repo_root(root):
                with redirect_stdout(io.StringIO()):
                    status = smart_llm_converter.main(
                        [
                            "--input-root",
                            str(input_root),
                            "--output-root",
                            str(output_root),
                        ]
                    )

            self.assertEqual(status, 0)
            executable_path = output_root / "logs" / "2" / "task" / "plan_to_code" / "executable_plan.py"
            executable = executable_path.read_text(encoding="utf-8")
            self.assertIn("BUNDLE_DATA =", executable)
            self.assertIn("run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__)", executable)
            self.assertNotIn("SMART_LLM_CODE", executable)
            self.assertNotIn("exec(compile(", executable)
            self.assertNotIn("```", executable)
            self.assertNotIn("Reasoning that should not be copied", executable)

            bundle = self.bundle_from_executable(executable)
            stages = bundle["task_plan"]["stages"]
            self.assertEqual(len(stages), 1)
            actions = stages[0]["robot_action_queues"]["robot1"]
            self.assertEqual([action["action_type"] for action in actions], ["GoToObject", "OpenObject"])
            self.assertEqual(actions[0]["parameters"]["args"], ["Drawer"])
            self.assertEqual(bundle["no_trans"], 2)

            task_summary = json.loads(
                (output_root / "logs" / "2" / "task" / "plan_to_code" / "conversion_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(task_summary["status"], "success")
            self.assertEqual(task_summary["action_count"], 2)
            self.assertEqual(task_summary["stage_count"], 1)
            self.assertEqual(task_summary["task_index"], 0)
            self.assertTrue(task_summary["task_file"].endswith("data/unit_set/FloorPlan2.jsonl"))

    def test_threaded_parallel_stage_is_encoded_as_one_stage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "threaded",
                (
                    "```python\n"
                    "import threading\n"
                    "def open_drawer(robot):\n"
                    "    OpenObject(robot, 'Drawer')\n"
                    "def pick_apple(robot):\n"
                    "    PickupObject(robot, 'Apple')\n"
                    "t1 = threading.Thread(target=open_drawer, args=(robots[0],))\n"
                    "t2 = threading.Thread(target=pick_apple, args=(robots[1],))\n"
                    "t1.start()\n"
                    "t2.start()\n"
                    "t1.join()\n"
                    "t2.join()\n"
                    "```\n"
                ),
            )
            self.write_log(
                source_path,
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                objects=[{"name": "Drawer"}, {"name": "Apple"}],
            )
            self.write_dataset(root, robot_count=2)

            with self.patched_repo_root(root):
                result = smart_llm_converter.convert_one(source_path, input_root, output_root, False, True)

            self.assertEqual(result.status, "success")
            executable = (output_root / "logs" / "2" / "threaded" / "plan_to_code" / "executable_plan.py").read_text(
                encoding="utf-8"
            )
            bundle = self.bundle_from_executable(executable)
            stages = bundle["task_plan"]["stages"]
            self.assertEqual(len(stages), 1)
            self.assertEqual(set(stages[0]["robot_action_queues"]), {"robot1", "robot2"})
            self.assertEqual(stages[0]["robot_action_queues"]["robot1"][0]["action_type"], "OpenObject")
            self.assertEqual(stages[0]["robot_action_queues"]["robot2"][0]["action_type"], "PickupObject")

    def test_time_sleep_becomes_wait(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "wait",
                (
                    "```python\n"
                    "import time\n"
                    "def toast(robot):\n"
                    "    SwitchOn(robot, 'Toaster')\n"
                    "    time.sleep(5)\n"
                    "    SwitchOff(robot, 'Toaster')\n"
                    "toast(robots[0])\n"
                    "```\n"
                ),
            )
            self.write_log(source_path, objects=[{"name": "Toaster"}])
            self.write_dataset(root)

            with self.patched_repo_root(root):
                result = smart_llm_converter.convert_one(source_path, input_root, output_root, False, True)

            self.assertEqual(result.status, "success")
            executable = (output_root / "logs" / "2" / "wait" / "plan_to_code" / "executable_plan.py").read_text(
                encoding="utf-8"
            )
            bundle = self.bundle_from_executable(executable)
            actions = bundle["task_plan"]["stages"][0]["robot_action_queues"]["robot1"]
            self.assertEqual([action["action_type"] for action in actions], ["SwitchOn", "Wait", "SwitchOff"])

    def test_single_element_robot_list_converts_and_multi_robot_team_skips(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            single_source = self.write_code_plan(
                input_root,
                "2",
                "single_team",
                (
                    "```python\n"
                    "def open_drawer(robot_list):\n"
                    "    OpenObject(robot_list[0], 'Drawer')\n"
                    "open_drawer([robots[0]])\n"
                    "```\n"
                ),
            )
            multi_source = self.write_code_plan(
                input_root,
                "2",
                "multi_team",
                (
                    "```python\n"
                    "def open_drawer(robot_list):\n"
                    "    OpenObject(robot_list[0], 'Drawer')\n"
                    "open_drawer([robots[0], robots[1]])\n"
                    "```\n"
                ),
            )
            self.write_log(single_source, robots=[{"name": "robot1"}, {"name": "robot2"}])
            self.write_log(multi_source, robots=[{"name": "robot1"}, {"name": "robot2"}])
            self.write_dataset(root, robot_count=2)

            with self.patched_repo_root(root):
                single_result = smart_llm_converter.convert_one(single_source, input_root, output_root, False, True)
                multi_result = smart_llm_converter.convert_one(multi_source, input_root, output_root, False, True)

            self.assertEqual(single_result.status, "success")
            self.assertEqual(multi_result.status, "skipped")
            self.assertEqual(multi_result.skip_reason, "multi_robot_team")
            self.assertFalse((output_root / "logs" / "2" / "multi_team" / "plan_to_code" / "executable_plan.py").exists())

    def test_missing_dataset_task_match_skips(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            source_path = self.write_code_plan(
                input_root,
                "2",
                "missing_task",
                (
                    "```python\n"
                    "def open_drawer(robot):\n"
                    "    OpenObject(robot, 'Drawer')\n"
                    "open_drawer(robots[0])\n"
                    "```\n"
                ),
            )
            self.write_log(source_path, task_text="open the drawer")
            self.write_dataset(root, tasks=["different task"])

            with self.patched_repo_root(root):
                result = smart_llm_converter.convert_one(source_path, input_root, output_root, False, True)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.skip_reason, "task_not_found")
            self.assertIsNone(result.generated["executable_plan"])


if __name__ == "__main__":
    unittest.main()
