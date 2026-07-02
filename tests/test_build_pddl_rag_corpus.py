import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_pddl_rag_corpus import main


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, content) -> None:
    write_text(path, json.dumps(content, ensure_ascii=False, indent=2))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def create_run(
    root: Path,
    name: str,
    *,
    return_codes,
    completion,
    include_key_artifacts=True,
    include_allocate=True,
) -> Path:
    run_dir = root / "logs" / "intermediate_runs" / "dataset___1" / name / "20260630_001"
    task = f"{name} task"
    artifacts = {
        "inputs": {"task_context": "inputs/task_context.json"},
        "decompose": {"output": "01_decompose/02_decompose_output.txt"},
        "problem_files": {
            "subtasks_index": "04_problem_files/03_subtasks.json",
            "generated_problem_files": "04_problem_files/04_generated_problem_files.json",
        },
        "validate": {"manifest": "07_validate/validation_manifest.json"},
        "planner": {"manifest": "08_planner/planner_manifest.json"},
    }
    if include_allocate:
        artifacts["allocate"] = {"output": "02_allocate/02_allocate_output.txt"}
        if include_key_artifacts:
            artifacts["allocate"]["key_objects"] = "02_allocate/00_key_objects.json"
    if include_key_artifacts:
        artifacts["problem_files"]["key_object_pddl_states"] = (
            "05_problem_generation/key_object_pddl_states.json"
        )

    write_json(
        run_dir / "run_manifest.json",
        {
            "task_index": 4,
            "task": task,
            "model": "model-a",
            "test_set": "dataset",
            "floor_plan": "1",
            "run_date": "20260630",
            "run_sequence": 1,
            "artifacts": artifacts,
            "completion": completion,
        },
    )
    write_json(
        run_dir / "inputs" / "task_context.json",
        {
            "task": task,
            "robots": [
                {
                    "name": "robot1",
                    "skills": ["GoToObject", "OpenObject"],
                    "mass_capacity": 10,
                }
            ],
            "objects_ai": "\n\nobjects = [{'name': 'Book', 'mass': 1.0}]",
        },
    )
    write_text(
        run_dir / "01_decompose" / "02_decompose_output.txt",
        "# SubTask 1: Open the book\nGoToObject then OpenObject",
    )
    if include_allocate:
        write_text(
            run_dir / "02_allocate" / "02_allocate_output.txt",
            "# SOLUTION\nRobot 1 can do it.\n# Sequence of Operations:\nSubtask 1: Robot 1;",
        )
    if include_key_artifacts:
        write_json(run_dir / "02_allocate" / "00_key_objects.json", [{"name": "Book", "mass": 1.0}])
        write_json(
            run_dir / "05_problem_generation" / "key_object_pddl_states.json",
            [{"object": "Book", "facts": ["(is-openable Book)"]}],
        )
    write_json(
        run_dir / "04_problem_files" / "03_subtasks.json",
        [{"index": 1, "path": "04_problem_files/subtasks/subtask_01.txt"}],
    )
    write_text(
        run_dir / "04_problem_files" / "subtasks" / "subtask_01.txt",
        "# SubTask 1: Open the book",
    )
    write_json(
        run_dir / "04_problem_files" / "04_generated_problem_files.json",
        [{"index": 1, "content": "(define (problem generated-book))"}],
    )
    write_text(
        run_dir / "05_problem_generation" / "outputs" / "subtask_01_problem.pddl",
        "(define (problem raw-book))",
    )
    write_json(
        run_dir / "07_validate" / "validation_manifest.json",
        [
            {
                "problem_file": "subtask_01_problem.pddl",
                "validated_problem_path": "07_validate/outputs/subtask_01_problem_validated.pddl",
                "status": "fake_validated",
            }
        ],
    )
    write_text(
        run_dir / "07_validate" / "outputs" / "subtask_01_problem_validated.pddl",
        "(define (problem validated-book))",
    )

    planner_records = []
    for idx, return_code in enumerate(return_codes, start=1):
        plan_path = run_dir / "08_planner" / "outputs" / f"subtask_{idx:02d}_problem_validated_plan.txt"
        if return_code == 0:
            write_text(plan_path, "(gotoobject robot1 book)\n(openobject robot1 book)\n")
        planner_records.append(
            {
                "problem_file": f"subtask_{idx:02d}_problem_validated.pddl",
                "return_code": return_code,
                "compatibility_output": str(plan_path),
                "domain_file": "resources/robot1.pddl",
            }
        )
    write_json(run_dir / "08_planner" / "planner_manifest.json", planner_records)
    return run_dir


class BuildPDDLRagCorpusTest(unittest.TestCase):
    def test_builds_success_documents_and_lexical_index(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_root = root / "logs" / "intermediate_runs"
            output_dir = root / "rag"
            create_run(
                root,
                "success",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
            )

            result = main(
                [
                    "--base-path",
                    str(root),
                    "--logs-root",
                    str(logs_root),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "pddlrun_corpus.jsonl")
            self.assertEqual({doc["stage"] for doc in docs}, {"decompose", "allocate", "problem_generation"})
            self.assertTrue(all(doc["quality"] == "success" for doc in docs))
            self.assertTrue(all(doc["retrieval_eligible"] for doc in docs))
            self.assertTrue(
                all(
                    set(doc.keys())
                    == {"id", "stage", "query_text", "content", "metadata", "quality", "retrieval_eligible"}
                    for doc in docs
                )
            )

            problem_doc = next(doc for doc in docs if doc["stage"] == "problem_generation")
            self.assertIn("(define (problem validated-book))", problem_doc["content"])
            self.assertEqual(problem_doc["metadata"]["planner_return_code"], 0)

            index = read_json(output_dir / "pddlrun_index.json")
            self.assertEqual(index["document_count"], 3)
            self.assertIn("book", index["postings"])

            summary = read_json(output_dir / "pddlrun_summary.json")
            self.assertEqual(summary["document_count"], 3)
            self.assertEqual(summary["retrieval_eligible_count"], 3)
            self.assertEqual(summary["by_stage_quality"]["decompose"]["success"], 1)

    def test_keeps_failed_documents_but_marks_them_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_root = root / "logs" / "intermediate_runs"
            output_dir = root / "rag"
            create_run(
                root,
                "failed",
                return_codes=[12],
                completion={"successful_subtasks": 0, "total_subtasks": 1},
            )

            result = main(
                [
                    "--base-path",
                    str(root),
                    "--logs-root",
                    str(logs_root),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "pddlrun_corpus.jsonl")
            self.assertTrue(docs)
            self.assertTrue(all(doc["quality"] == "failed" for doc in docs))
            self.assertTrue(all(not doc["retrieval_eligible"] for doc in docs))
            summary = read_json(output_dir / "pddlrun_summary.json")
            self.assertEqual(summary["retrieval_eligible_count"], 0)

    def test_supports_old_manifest_missing_optional_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_root = root / "logs" / "intermediate_runs"
            output_dir = root / "rag"
            create_run(
                root,
                "old",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                include_key_artifacts=False,
            )

            result = main(
                [
                    "--base-path",
                    str(root),
                    "--logs-root",
                    str(logs_root),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "pddlrun_corpus.jsonl")
            self.assertEqual(len(docs), 3)
            decompose_doc = next(doc for doc in docs if doc["stage"] == "decompose")
            self.assertEqual(decompose_doc["metadata"]["key_object_count"], 0)

    def test_records_skip_reason_for_missing_stage_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_root = root / "logs" / "intermediate_runs"
            output_dir = root / "rag"
            create_run(
                root,
                "missing-allocate",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                include_allocate=False,
            )

            result = main(
                [
                    "--base-path",
                    str(root),
                    "--logs-root",
                    str(logs_root),
                    "--output-dir",
                    str(output_dir),
                    "--stages",
                    "allocate",
                ]
            )

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "pddlrun_corpus.jsonl"), [])
            summary = read_json(output_dir / "pddlrun_summary.json")
            self.assertEqual(summary["skip_reasons"]["missing_allocate_output"], 1)


if __name__ == "__main__":
    unittest.main()
