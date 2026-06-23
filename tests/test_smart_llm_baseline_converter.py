import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "baselines" / "SMART-LLM.py"

spec = importlib.util.spec_from_file_location("smart_llm_converter", SCRIPT_PATH)
smart_llm_converter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smart_llm_converter
spec.loader.exec_module(smart_llm_converter)


class SmartLLMBaselineConverterTest(unittest.TestCase):
    def write_code_plan(self, input_root: Path, floor: str, task: str, content: str) -> Path:
        path = input_root / floor / task / "code_plan.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

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
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.parse_status, "syntax_error")
            self.assertIsNone(result.generated["code_plan"])
            self.assertFalse((output_root / "logs" / "2" / "bad_task" / "plan_to_code" / "code_plan.py").exists())
            self.assertTrue((output_root / "logs" / "2" / "bad_task" / "plan_to_code" / "conversion_summary.json").exists())

    def test_dry_run_writes_global_summary_without_code_plan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            self.write_code_plan(
                input_root,
                "2",
                "task",
                "Text\n```python\ndef open_drawer(robot):\n    OpenObject(robot, 'Drawer')\nopen_drawer(robots[0])\n```\n",
            )

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
            summary = json.loads((output_root / "smart_llm_conversion_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["converted"], 1)
            self.assertTrue(summary["dry_run"])
            self.assertFalse((output_root / "logs" / "2" / "task" / "plan_to_code" / "code_plan.py").exists())

    def test_successful_conversion_writes_clean_python(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_root = root / "input" / "logs"
            output_root = root / "output"
            self.write_code_plan(
                input_root,
                "2",
                "task",
                (
                    "Reasoning that should not be copied.\n"
                    "```python\n"
                    "def open_drawer(robot):\n"
                    "    OpenObject(robot, 'Drawer')\n\n"
                    "open_drawer(robots[0])\n"
                    "```\n"
                ),
            )

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
            converted_path = output_root / "logs" / "2" / "task" / "plan_to_code" / "code_plan.py"
            converted = converted_path.read_text(encoding="utf-8")
            self.assertIn("def open_drawer", converted)
            self.assertNotIn("```", converted)
            self.assertNotIn("Reasoning that should not be copied", converted)

            task_summary = json.loads(
                (output_root / "logs" / "2" / "task" / "plan_to_code" / "conversion_summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(task_summary["status"], "converted")
            self.assertEqual(task_summary["parse_status"], "parse_ok")


if __name__ == "__main__":
    unittest.main()
