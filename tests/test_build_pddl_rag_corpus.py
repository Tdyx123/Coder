import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_pddl_rag_corpus import main, parse_args


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


def run_corpus_builder(root: Path, output_dir: Path, stage: str) -> int:
    return main(
        [
            "--base-path",
            str(root),
            "--logs-root",
            str(root / "logs" / "intermediate_runs"),
            "--output-dir",
            str(output_dir),
            "--stage",
            stage,
        ]
    )


def create_run(
    root: Path,
    name: str,
    *,
    return_codes,
    completion,
    include_key_artifacts=True,
    include_allocate=True,
    allocate_output=None,
    robots=None,
    subtask_texts=None,
    domain_file="resources/robot1.pddl",
) -> Path:
    run_dir = root / "logs" / "intermediate_runs" / "dataset___1" / name / "20260630_001"
    task = f"{name} task"
    if robots is None:
        robots = [
            {
                "name": "robot1",
                "skills": ["GoToObject", "OpenObject"],
                "mass_capacity": 10,
            }
        ]
    if subtask_texts is None:
        subtask_texts = ["# SubTask 1: Open the book"]
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
            "robots": robots,
            "objects_ai": "\n\nobjects = [{'name': 'Book', 'mass': 1.0}]",
        },
    )
    write_text(
        run_dir / "01_decompose" / "02_decompose_output.txt",
        "\n\n".join(subtask_texts),
    )
    if include_allocate:
        if allocate_output is None:
            sequence = "".join(
                f"Subtask {index}: Robot 1;"
                for index in range(1, len(subtask_texts) + 1)
            )
            allocate_output = f"# SOLUTION\nRobot 1 can do it.\n# Sequence of Operations:\n{sequence}"
        write_text(
            run_dir / "02_allocate" / "02_allocate_output.txt",
            allocate_output,
        )
    if include_key_artifacts:
        write_json(run_dir / "02_allocate" / "00_key_objects.json", [{"name": "Book", "mass": 1.0}])
        write_json(
            run_dir / "05_problem_generation" / "key_object_pddl_states.json",
            [{"object": "Book", "facts": ["(is-openable Book)"]}],
        )
    write_json(
        run_dir / "04_problem_files" / "03_subtasks.json",
        [
            {
                "index": index,
                "path": f"04_problem_files/subtasks/subtask_{index:02d}.txt",
            }
            for index in range(1, len(subtask_texts) + 1)
        ],
    )
    for index, subtask_text in enumerate(subtask_texts, start=1):
        write_text(
            run_dir / "04_problem_files" / "subtasks" / f"subtask_{index:02d}.txt",
            subtask_text,
        )
    write_json(
        run_dir / "04_problem_files" / "04_generated_problem_files.json",
        [
            {"index": index, "content": f"(define (problem generated-book-{index}))"}
            for index in range(1, len(subtask_texts) + 1)
        ],
    )
    for index in range(1, len(subtask_texts) + 1):
        write_text(
            run_dir / "05_problem_generation" / "outputs" / f"subtask_{index:02d}_problem.pddl",
            f"(define (problem raw-book-{index}))",
        )
    write_json(
        run_dir / "07_validate" / "validation_manifest.json",
        [
            {
                "problem_file": f"subtask_{index:02d}_problem.pddl",
                "validated_problem_path": f"07_validate/outputs/subtask_{index:02d}_problem_validated.pddl",
                "status": "fake_validated",
            }
            for index in range(1, len(subtask_texts) + 1)
        ],
    )
    for index in range(1, len(subtask_texts) + 1):
        write_text(
            run_dir / "07_validate" / "outputs" / f"subtask_{index:02d}_problem_validated.pddl",
            f"(define (problem validated-book-{index}))",
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
                "domain_file": domain_file,
            }
        )
    write_json(run_dir / "08_planner" / "planner_manifest.json", planner_records)
    return run_dir


class BuildPDDLRagCorpusTest(unittest.TestCase):
    def test_parse_args_accepts_supported_single_stage(self):
        self.assertEqual(parse_args(["--stage", "decompose"]).stage, "decompose")
        self.assertEqual(parse_args(["--stage", "allocate"]).stage, "allocate")
        self.assertEqual(parse_args(["--stage", "problem_generation"]).stage, "problem_generation")

    def test_parse_args_rejects_unknown_stage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                parse_args(["--stage", "unknown"])

        self.assertEqual(context.exception.code, 2)

    def test_parse_args_requires_stage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                parse_args(["--base-path", str(ROOT)])

        self.assertEqual(context.exception.code, 2)

    def test_parse_args_rejects_old_stages_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                parse_args(["--stages", "decompose"])

        self.assertEqual(context.exception.code, 2)

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
                    "--stage",
                    "decompose",
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_decompose_corpus.jsonl")
            self.assertEqual({doc["stage"] for doc in docs}, {"decompose"})
            self.assertTrue(all(doc["quality"] == "success" for doc in docs))
            self.assertTrue(all(doc["retrieval_eligible"] for doc in docs))
            self.assertTrue(
                all(
                    set(doc.keys())
                    == {"id", "stage", "query_text", "content", "metadata", "quality", "retrieval_eligible"}
                    for doc in docs
                )
            )

            decompose_doc = docs[0]
            self.assertIn("# Task", decompose_doc["content"])
            self.assertIn("# Decomposition Output", decompose_doc["content"])
            self.assertIn("# SubTask 1: Open the book", decompose_doc["content"])
            self.assertEqual(decompose_doc["query_text"], "Task: success task")
            self.assertNotIn("# Robots", decompose_doc["content"])
            self.assertNotIn("# Key Objects", decompose_doc["content"])

            index = read_json(output_dir / "task_decompose_index.json")
            self.assertEqual(index["document_count"], 1)
            self.assertIn("book", index["postings"])

            summary = read_json(output_dir / "task_decompose_summary.json")
            self.assertEqual(summary["document_count"], 1)
            self.assertEqual(summary["retrieval_eligible_count"], 1)
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
                    "--stage",
                    "decompose",
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_decompose_corpus.jsonl")
            self.assertTrue(docs)
            self.assertTrue(all(doc["quality"] == "failed" for doc in docs))
            self.assertTrue(all(not doc["retrieval_eligible"] for doc in docs))
            summary = read_json(output_dir / "task_decompose_summary.json")
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
                    "--stage",
                    "decompose",
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_decompose_corpus.jsonl")
            self.assertEqual(len(docs), 1)
            decompose_doc = next(doc for doc in docs if doc["stage"] == "decompose")
            self.assertEqual(decompose_doc["metadata"]["key_object_count"], 0)

    def test_problem_generation_stage_keeps_problem_context_documents(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "problem-generation",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                domain_file="resources/robot25.pddl",
            )

            result = run_corpus_builder(root, output_dir, "problem_generation")

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_problem_generation_corpus.jsonl")
            self.assertEqual(len(docs), 1)
            doc = docs[0]
            self.assertEqual(doc["stage"], "problem_generation")
            self.assertEqual(doc["quality"], "success")
            self.assertTrue(doc["retrieval_eligible"])
            self.assertIn("Subtask 1: # SubTask 1: Open the book", doc["query_text"])
            self.assertIn("Assigned Robot: robot25", doc["query_text"])
            self.assertNotIn("Assigned robot: robot1", doc["query_text"])
            self.assertNotIn("Assigned PDDL robot/domain", doc["query_text"])
            self.assertIn("# Assigned Robot", doc["content"])
            self.assertIn("# Assigned Robot\nrobot25\n\n# Generated Problem", doc["content"])
            self.assertNotIn("Assigned robot number", doc["content"])
            self.assertNotIn("Assigned robot: robot1", doc["content"])
            self.assertNotIn("Assigned PDDL robot/domain", doc["content"])
            self.assertIn("# Generated Problem", doc["content"])
            self.assertIn("(define (problem raw-book-1))", doc["content"])
            self.assertNotIn("# Validated Problem", doc["content"])
            self.assertNotIn("(define (problem validated-book-1))", doc["content"])
            self.assertNotIn("# Planner Record", doc["content"])
            self.assertNotIn("# Plan", doc["content"])
            self.assertNotIn("(gotoobject robot1 book)", doc["content"])
            self.assertNotIn("(openobject robot1 book)", doc["content"])
            self.assertNotIn("assigned_robot_number", doc["metadata"])
            self.assertNotIn("assigned_robot", doc["metadata"])
            self.assertEqual(doc["metadata"]["assigned_pddl_robot"], "robot25")
            self.assertEqual(
                doc["metadata"]["plan_path"],
                "08_planner/outputs/subtask_01_problem_validated_plan.txt",
            )
            self.assertEqual(doc["metadata"]["planner_return_code"], 0)
            self.assertEqual(doc["metadata"]["domain_file"], "resources/robot25.pddl")
            index = read_json(output_dir / "task_problem_generation_index.json")
            self.assertEqual(index["document_count"], 1)
            summary = read_json(output_dir / "task_problem_generation_summary.json")
            self.assertEqual(summary["by_stage_quality"]["problem_generation"]["success"], 1)

    def test_problem_generation_stage_requires_nonempty_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            run_dir = create_run(
                root,
                "missing-plan",
                return_codes=[0, 0],
                completion={"successful_subtasks": 2, "total_subtasks": 2},
                subtask_texts=[
                    "# SubTask 1: Open the book",
                    "# SubTask 2: Open the drawer",
                ],
            )
            write_text(
                run_dir / "08_planner" / "outputs" / "subtask_01_problem_validated_plan.txt",
                "",
            )
            (run_dir / "08_planner" / "outputs" / "subtask_02_problem_validated_plan.txt").unlink()

            result = run_corpus_builder(root, output_dir, "problem_generation")

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_problem_generation_corpus.jsonl")
            self.assertEqual(docs, [])
            index = read_json(output_dir / "task_problem_generation_index.json")
            self.assertEqual(index["document_count"], 0)
            summary = read_json(output_dir / "task_problem_generation_summary.json")
            self.assertEqual(summary["document_count"], 0)
            self.assertEqual(summary["skip_reasons"]["filtered_problem_generation_missing_plan"], 2)
            self.assertNotIn("missing_problem_generation_outputs", summary["skip_reasons"])

    def test_allocate_stage_is_supported_and_skips_missing_allocate_output(self):
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
                    "--stage",
                    "allocate",
                ]
            )

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_allocate_corpus.jsonl")
            self.assertEqual(docs, [])
            index = read_json(output_dir / "task_allocate_index.json")
            self.assertEqual(index["document_count"], 0)
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["missing_allocate_output"], 1)

    def test_allocate_stage_keeps_valid_success_documents(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "valid-allocate",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            docs = read_jsonl(output_dir / "task_allocate_corpus.jsonl")
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["stage"], "allocate")
            self.assertEqual(docs[0]["quality"], "success")
            self.assertTrue(docs[0]["retrieval_eligible"])
            self.assertIn("Subtask 1: Robot 1;", docs[0]["content"])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["document_count"], 1)
            self.assertEqual(summary["retrieval_eligible_count"], 1)
            self.assertEqual(summary["skip_reasons"], {})

    def test_allocate_stage_filters_non_success_quality(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "failed-allocate",
                return_codes=[12],
                completion={"successful_subtasks": 0, "total_subtasks": 1},
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_non_success_quality"], 1)

    def test_allocate_stage_filters_missing_sequence_block(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "missing-sequence",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                allocate_output="# SOLUTION\nRobot 1 can do it.",
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_missing_sequence"], 1)

    def test_allocate_stage_filters_missing_subtask_assignment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "missing-subtask-assignment",
                return_codes=[0, 0],
                completion={"successful_subtasks": 2, "total_subtasks": 2},
                subtask_texts=[
                    "# SubTask 1: Open the book",
                    "# SubTask 2: Close the book",
                ],
                allocate_output="# SOLUTION\n# Sequence of Operations:\nSubtask 1: Robot 1;",
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_missing_subtask_assignment"], 1)

    def test_allocate_stage_filters_extra_subtask_assignment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "extra-subtask-assignment",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                allocate_output=(
                    "# SOLUTION\n# Sequence of Operations:\n"
                    "Subtask 1: Robot 1;Subtask 2: Robot 1;"
                ),
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_extra_subtask_assignment"], 1)

    def test_allocate_stage_filters_invalid_robot_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "invalid-robot-id",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                allocate_output="# SOLUTION\n# Sequence of Operations:\nSubtask 1: Robot 2;",
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_invalid_robot_id"], 1)

    def test_allocate_stage_filters_unparseable_sequence_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "rag"
            create_run(
                root,
                "unparseable-sequence",
                return_codes=[0],
                completion={"successful_subtasks": 1, "total_subtasks": 1},
                allocate_output=(
                    "# SOLUTION\n# Sequence of Operations:\n"
                    "Subtask 1: Robot 1;\nSubtask;Robot;"
                ),
            )

            result = run_corpus_builder(root, output_dir, "allocate")

            self.assertEqual(result, 0)
            self.assertEqual(read_jsonl(output_dir / "task_allocate_corpus.jsonl"), [])
            summary = read_json(output_dir / "task_allocate_summary.json")
            self.assertEqual(summary["skip_reasons"]["filtered_allocate_unparseable_sequence_line"], 1)


if __name__ == "__main__":
    unittest.main()
