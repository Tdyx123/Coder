import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from compress_sft_user_content import (
    CompressionError,
    compress_allocate_content,
    compress_decompose_content,
    compress_problem_generation_content,
    compress_record,
    process_file,
)


class Encoding:
    def __init__(self, ids):
        self.ids = ids


class WhitespaceTokenizer:
    def encode(self, text, add_special_tokens=True):
        if add_special_tokens:
            raise AssertionError("content token counts must exclude special tokens")
        return Encoding(text.split())


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        if add_special_tokens:
            raise AssertionError("content token counts must exclude special tokens")
        return Encoding(list(text))


DECOMPOSE_CONTENT = """from pddl domain file with all possible actions:
(define (domain allactionrobot)
  (:requirements :strips :typing)
  (:types robot object)
  (:predicates (at ?r - robot ?o - object) (object-open ?o - object))
  (:action GoToObject
    :parameters (?r - robot ?o - object)
    :precondition ()
    :effect (at ?r ?o)
  )
  (:action OpenObject
    :parameters (?r - robot ?o - object)
    :precondition (at ?r ?o)
    :effect (object-open ?o)
  )
)

objects = ['Mug', 'Mug', 'Drawer']

# Task Description: Example task that must be removed.
# GENERAL TASK DECOMPOSITION
# SubTask 1: Example task.

# Task Description: Another example that must be removed.
# GENERAL TASK DECOMPOSITION
# SubTask 1: Another example.

# GENERAL TASK DECOMPOSITION
Decompose and parallel subtasks where ever possible.
# Task Description: Open the drawer.
"""


ALLOCATE_CONTENT = """
# EXAMPLE 1
# TASK ALLOCATION
robots = [{'name': 'example_robot'}]
objects = [{'name': 'ExampleObject', 'mass': 99}]
# Sequence of Operations:
Subtask 1: Robot 1;
# EXAMPLE 2
# TASK ALLOCATION
robots = [{'name': 'example_robot_2'}]
objects = [{'name': 'ExampleObject2', 'mass': 99}]
# Sequence of Operations:
Subtask 1: Robot 1;

**GENERAL TASK DECOMPOSITION**
1. **SubTask 1: Open the drawer.** (Skills Required: GoToObject, OpenObject)
2. **SubTask 2: Put the mug in the drawer.** (Skills Required: GoToObject, PickupObject, PutObject)
SubTask 2 depends on SubTask 1; no parallel execution is possible.

# TASK ALLOCATION
robots = [{'name': 'robot1', 'no_skills': 4, 'skills': ['GoToObject', 'OpenObject', 'PickupObject', 'BreakObject'], 'mass_capacity': 1.0}, {'name': 'robot2', 'no_skills': 4, 'skills': ['GoToObject', 'PickupObject', 'PutObject', 'BreakObject'], 'mass_capacity': 5.0}]
objects = [{'name': 'Drawer', 'mass': 10.0}, {'name': 'Mug', 'mass': 2.0}, {'name': 'Vase', 'mass': 4.0}]
# SOLUTION
"""


PROBLEM_CONTENT = """
#Example 1
Example prompt that must be removed.
Domain file content:(define (domain example) (:action FakeAction :parameters () :precondition () :effect ()))
#Problem file generation is done
#IMPORTANT stop generating after seeing Problem file generation is done.
Finish the tasks like example
Subtask examination from action perspective:#SubTask 1: Open the drawer

**Initial condition analyze:**
1. Robot not at drawer location
2. Drawer initially closed

**GoToObject:** Robot goes to the drawer
Parameters: ?robot, ?drawer
Preconditions: None.
Effects: (at ?robot ?drawer)

OpenObject: robot7 opens the drawer
Parameters: robot7, drawer
Preconditions: (at robot7 drawer)
Effects: (object-open drawer)

Domain file content:(define (domain robot7)
  (:requirements :strips :typing)
  (:types robot object)
  (:predicates (at ?r - robot ?o - object) (object-open ?o - object))
  (:action GoToObject
    :parameters (?r - robot ?o - object)
    :precondition ()
    :effect (at ?r ?o)
  )
  (:action OpenObject
    :parameters (?r - robot ?o - object)
    :precondition (at ?r ?o)
    :effect (object-open ?o)
  )
)
objects = [{'name': 'Drawer', 'mass': 0.0}, {'name': 'Vase', 'mass': 1.0}]
key_object_pddl_states = [
  {"object": "Drawer_1", "object_type": "object", "facts": ["(is-openable Drawer_1)"], "related_objects": [{"object": "CounterTop_1", "object_type": "object", "facts": ["(at-location Drawer_1 CounterTop_1)"]}]}
]
Task description: generate the problem file.
#IMPORTANT, strictly follow the structure.
"""


class CompressStageContentTest(unittest.TestCase):
    def test_decompose_keeps_last_task_unique_objects_and_action_signatures(self):
        compressed = compress_decompose_content(DECOMPOSE_CONTENT)

        self.assertIn("Task: Open the drawer.", compressed)
        self.assertIn("Available objects: Mug, Drawer", compressed)
        self.assertIn("GoToObject(?r - robot ?o - object)", compressed)
        self.assertIn("OpenObject(?r - robot ?o - object)", compressed)
        self.assertNotIn("Example task that must be removed", compressed)
        self.assertNotIn("Another example that must be removed", compressed)

    def test_allocate_filters_irrelevant_skills_and_objects(self):
        compressed = compress_allocate_content(ALLOCATE_CONTENT)

        self.assertIn("SubTask 1: Open the drawer", compressed)
        self.assertIn("SubTask 2 depends on SubTask 1", compressed)
        self.assertIn('"n":"robot1"', compressed)
        self.assertIn('"c":1.0', compressed)
        self.assertIn('"n":"Drawer","m":10.0', compressed)
        self.assertIn('"n":"Mug","m":2.0', compressed)
        self.assertNotIn("BreakObject", compressed)
        self.assertNotIn("Vase", compressed)
        self.assertNotIn("example_robot", compressed)

    def test_problem_accepts_markdown_and_plain_action_headings(self):
        compressed = compress_problem_generation_content(PROBLEM_CONTENT)

        self.assertIn("Domain: robot7", compressed)
        self.assertIn("GoToObject(?r - robot ?o - object)", compressed)
        self.assertIn("OpenObject(?r - robot ?o - object)", compressed)
        self.assertIn("pre=(at ?r ?o)", compressed)
        self.assertIn("effect=(object-open ?o)", compressed)
        self.assertIn("Drawer_1:object[(is-openable Drawer_1)]", compressed)
        self.assertIn("CounterTop_1:object[(at-location Drawer_1 CounterTop_1)]", compressed)
        self.assertNotIn("Example prompt that must be removed", compressed)
        self.assertNotIn('"name": "Vase"', compressed)

    def test_problem_without_actions_preserves_impossibility_constraint(self):
        content = PROBLEM_CONTENT.replace(
            "**GoToObject:** Robot goes to the drawer\nParameters: ?robot, ?drawer\nPreconditions: None.\nEffects: (at ?robot ?drawer)\n\nOpenObject: robot7 opens the drawer\nParameters: robot7, drawer\nPreconditions: (at robot7 drawer)\nEffects: (object-open drawer)",
            "Constraint: Drawer mass exceeds every robot capacity.\nTask Status: NOT POSSIBLE",
        )

        compressed = compress_problem_generation_content(content)

        self.assertIn("Constraint: Drawer mass exceeds every robot capacity.", compressed)
        self.assertIn("Task Status: NOT POSSIBLE", compressed)
        self.assertIn("Actions: none", compressed)


class CompressRecordAndFileTest(unittest.TestCase):
    def test_compress_record_changes_only_user_content_and_checks_budget(self):
        record = {
            "id": "sample",
            "messages": [
                {"role": "system", "content": "system text"},
                {"role": "user", "content": DECOMPOSE_CONTENT},
                {"role": "assistant", "content": "assistant text"},
            ],
            "metadata": {"keep": True},
        }

        compressed, token_count = compress_record(
            record,
            stage="decompose",
            tokenizer=WhitespaceTokenizer(),
            max_tokens=256,
        )

        self.assertEqual(compressed["id"], "sample")
        self.assertEqual(compressed["metadata"], {"keep": True})
        self.assertEqual(compressed["messages"][0], record["messages"][0])
        self.assertEqual(compressed["messages"][2], record["messages"][2])
        self.assertNotEqual(compressed["messages"][1]["content"], DECOMPOSE_CONTENT)
        self.assertEqual(token_count, len(compressed["messages"][1]["content"].split()))
        self.assertLessEqual(token_count, 256)
        self.assertEqual(record["messages"][1]["content"], DECOMPOSE_CONTENT)

    def test_compress_record_rejects_content_over_budget(self):
        record = {"messages": [{"role": "user", "content": DECOMPOSE_CONTENT}]}

        with self.assertRaisesRegex(CompressionError, "exceeds 1 tokens"):
            compress_record(
                record,
                stage="decompose",
                tokenizer=CharacterTokenizer(),
                max_tokens=1,
            )

    def test_process_file_skips_bad_rows_and_preserves_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "01_decompose.jsonl"
            output_path = root / "01_decompose_1280.jsonl"
            valid = {
                "messages": [
                    {"role": "user", "content": DECOMPOSE_CONTENT},
                    {"role": "assistant", "content": "answer"},
                ]
            }
            input_path.write_text(
                json.dumps(valid, ensure_ascii=False) + "\n{bad json\n",
                encoding="utf-8",
            )

            summary = process_file(
                input_path=input_path,
                output_path=output_path,
                stage="decompose",
                tokenizer=WhitespaceTokenizer(),
                max_tokens=256,
                overwrite=False,
            )

            rows = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            rejects = [
                json.loads(line)
                for line in summary["rejected_path"].read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["messages"][1], valid["messages"][1])
            self.assertEqual(summary["input_count"], 2)
            self.assertEqual(summary["output_count"], 1)
            self.assertEqual(summary["rejected_count"], 1)
            self.assertEqual(rejects[0]["line_number"], 2)
            self.assertEqual(rejects[0]["source"], str(input_path))
            self.assertIn("JSON", rejects[0]["reason"])

    def test_process_file_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "01_decompose.jsonl"
            output_path = root / "01_decompose_1280.jsonl"
            input_path.write_text("", encoding="utf-8")
            output_path.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                process_file(
                    input_path=input_path,
                    output_path=output_path,
                    stage="decompose",
                    tokenizer=WhitespaceTokenizer(),
                    max_tokens=256,
                    overwrite=False,
                )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
