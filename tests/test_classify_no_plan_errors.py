import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from classify_no_plan_errors import classify_record, main


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ClassifyNoPlanErrorsTest(unittest.TestCase):
    def test_extracts_keyerror_from_bytes_literal_traceback(self):
        result = classify_record(
            {
                "planner_stderr": "b'Traceback (most recent call last):\\nKeyError: \\'countertop\\'\\n'",
                "pddl": "(define (problem x))",
            }
        )

        self.assertEqual(result["error_phase"], "translate")
        self.assertEqual(result["symptom_category"], "translator_unknown_type")
        self.assertEqual(result["root_cause_category"], "pddl_unknown_type")
        self.assertEqual(result["classification_details"]["missing_type"], "countertop")

    def test_classifies_parse_error_root_causes(self):
        code_fence = classify_record(
            {
                "error_message": "Error: Could not parse task file: x\nReason: Expected '(', got '```lisp'.",
                "pddl": "```lisp\n(define (problem x))\n```",
            }
        )
        natural_language = classify_record(
            {
                "error_message": "Error: Could not parse task file: x\nReason: Expected '(', got 'WAIT'.",
                "pddl": "(define (problem x))\nWait no! That line leaked from my thought process.",
            }
        )
        malformed = classify_record(
            {
                "error_message": "Error: Could not parse task file: x\nReason: Tokens remaining after parsing: ) )",
                "pddl": "(define (problem x)))",
            }
        )

        self.assertEqual(code_fence["root_cause_category"], "markdown_fence_in_pddl")
        self.assertEqual(natural_language["root_cause_category"], "natural_language_leak")
        self.assertEqual(malformed["root_cause_category"], "malformed_pddl_syntax")
        self.assertEqual(code_fence["symptom_category"], "pddl_parse_error")

    def test_classifies_duplicate_object_and_initial_state_conflict(self):
        duplicate = classify_record(
            {
                "error_message": "error: duplicate object 'countertop'\nplease check :constants and :objects definitions",
                "pddl": "(define (problem x))",
            }
        )
        initial_state = classify_record(
            {
                "error_message": (
                    "Error in initial state specification\n"
                    "Reason: Atom at-location(tomato, countertop) is true and false."
                ),
                "pddl": "(define (problem x))",
            }
        )

        self.assertEqual(duplicate["symptom_category"], "pddl_duplicate_object")
        self.assertEqual(duplicate["classification_details"]["duplicate_object"], "countertop")
        self.assertEqual(initial_state["root_cause_category"], "initial_state_contradiction")
        self.assertEqual(
            initial_state["classification_details"]["conflicting_atom"],
            "Atom at-location(tomato, countertop) is true and false.",
        )

    def test_classifies_driver_and_planner_failures(self):
        unsupported = classify_record(
            {
                "planner_stderr": "This configuration does not support axioms!\nTerminating.",
                "pddl": "(define (problem x))",
            }
        )
        missing_sas = classify_record(
            {
                "planner_stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'output.sas'",
                "pddl": "(define (problem x))",
            }
        )
        invalid_sas = classify_record(
            {
                "planner_stderr": "Failed to match magic word 'begin_version'.\nGot ''.",
                "pddl": "(define (problem x))",
            }
        )

        self.assertEqual(unsupported["root_cause_category"], "unsupported_axioms_or_disjunction")
        self.assertEqual(missing_sas["root_cause_category"], "missing_or_invalid_sas_output")
        self.assertEqual(missing_sas["classification_details"]["missing_file"], "output.sas")
        self.assertEqual(invalid_sas["classification_details"]["sas_error"], "invalid_or_empty_sas_output")

    def test_classifies_unsolvable_variants(self):
        cases = [
            ("Simplified to trivially false goal! Generating unsolvable task...", "trivially_false_goal"),
            ("No relaxed solution! Generating unsolvable task...", "no_relaxed_solution"),
            ("[t=0.002s] Completely explored state space -- no solution!", "state_space_no_solution"),
        ]

        for message, expected_root in cases:
            with self.subTest(message=message):
                result = classify_record({"error_message": message, "pddl": "(define (problem x))"})
                self.assertEqual(result["symptom_category"], "planner_unsolvable")
                self.assertEqual(result["root_cause_category"], expected_root)

    def test_main_writes_classified_jsonl_and_summary_csv(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            stdout_path = root / "stdout" / "subtask_stdout.txt"
            stdout_path.parent.mkdir(parents=True)
            stdout_path.write_text(
                "Parsing...\n"
                "Error: Could not parse task file: sample.pddl\n"
                "Reason: Expected '(', got '```lisp'.\n"
                "translate exit code: 31\n",
                encoding="utf-8",
            )
            input_path = root / "input.jsonl"
            output_path = root / "classified.jsonl"
            summary_path = root / "summary.csv"
            records = [
                {
                    "floorplan": "6",
                    "task_index": 1,
                    "task": "parse sample",
                    "subtask": "subtask_01",
                    "error_message": "Driver aborting after translate",
                    "planner_stdout_path": str(stdout_path),
                    "pddl": "```lisp\n(define (problem x))",
                },
                {
                    "floorplan": "8",
                    "task_index": 2,
                    "task": "unsolvable sample",
                    "subtask": "subtask_02",
                    "error_message": "No relaxed solution! Generating unsolvable task...",
                    "pddl": "(define (problem x))",
                },
                {
                    "floorplan": "14",
                    "task_index": 3,
                    "task": "driver sample",
                    "subtask": "subtask_03",
                    "planner_stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'output.sas'",
                    "pddl": "(define (problem x))",
                },
            ]
            write_jsonl(input_path, records)

            result = main(
                [
                    "--input",
                    str(input_path),
                    "--output-jsonl",
                    str(output_path),
                    "--summary-csv",
                    str(summary_path),
                ]
            )

            self.assertEqual(result, 0)
            classified = read_jsonl(output_path)
            self.assertEqual(len(classified), len(records))
            self.assertEqual(classified[0]["root_cause_category"], "markdown_fence_in_pddl")

            with summary_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual(sum(int(row["count"]) for row in rows), len(records))


if __name__ == "__main__":
    unittest.main()
