import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import (
    PDDLRagError,
    PDDLRagExample,
    PDDLRagRetriever,
    PROBLEM_GENERATION_RAG_SAFETY_RULES,
    _tokenize_query,
)
from run_config import RunConfig


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def doc(
    doc_id: str,
    stage: str,
    query_text: str,
    content: str,
    *,
    quality: str = "success",
    retrieval_eligible: bool = True,
):
    return {
        "id": doc_id,
        "stage": stage,
        "query_text": query_text,
        "content": content,
        "metadata": {
            "task": f"task {doc_id}",
            "task_run_dir": f"/tmp/{doc_id}",
        },
        "quality": quality,
        "retrieval_eligible": retrieval_eligible,
    }


def rag_example(doc_id: str, content: str) -> PDDLRagExample:
    return PDDLRagExample(
        doc_id=doc_id,
        stage="decompose",
        quality="success",
        retrieval_eligible=True,
        task=f"task {doc_id}",
        query_text=f"query {doc_id}",
        content=content,
        metadata={"task_run_dir": f"/tmp/{doc_id}"},
        score=0.0,
    )


class PDDLRagRetrieverTest(unittest.TestCase):
    def test_query_token_filter_removes_structure_noise_and_limits_width(self):
        query = (
            "Task: break the window, then cool the pot and the bottle in the fridge.\n"
            "Robots: [{'name': 'robot1', 'skills': ['GoToObject', 'OpenObject'], "
            "'mass_capacity': 100}, {'name': 'robot2', 'skills': ['BreakObject', 'ColdObject']}]\n"
            "Objects: [{'name': 'Fridge'}, {'name': 'Bottle'}, {'name': 'Window'}]"
        )

        tokens = _tokenize_query(query, limit=8)

        self.assertLessEqual(len(tokens), 8)
        for noisy_token in [
            "task",
            "the",
            "then",
            "and",
            "robots",
            "name",
            "skills",
            "mass_capacity",
            "robot1",
            "gotoobject",
            "openobject",
            "breakobject",
        ]:
            self.assertNotIn(noisy_token, tokens)
        for useful_token in ["break", "window", "cool", "pot", "bottle", "fridge"]:
            self.assertIn(useful_token, tokens)

    def test_allocate_query_tokens_keep_robot_action_skills(self):
        query = (
            "Task: heat the apple in the microwave.\n"
            "Required skills: GoToObject, PickupObject, RunMicrowave\n"
            "Robot skill coverage: robot1 skills GoToObject, OpenObject, RunMicrowave"
        )

        decompose_tokens = _tokenize_query(query, limit=12)
        allocate_tokens = _tokenize_query(query, limit=12, stage="allocate")

        self.assertNotIn("gotoobject", decompose_tokens)
        self.assertIn("gotoobject", allocate_tokens)
        self.assertIn("runmicrowave", allocate_tokens)
        self.assertIn("apple", allocate_tokens)

    def test_problem_generation_query_tokens_keep_action_symbols(self):
        query = (
            "Subtask 1: heat the apple in the microwave.\n"
            "Domain actions: GoToObject, PickupObject, RunMicrowave\n"
            "Domain predicates: at-location, pickupable, heated"
        )

        tokens = _tokenize_query(query, limit=12, stage="problem_generation")

        self.assertIn("gotoobject", tokens)
        self.assertIn("pickupobject", tokens)
        self.assertIn("runmicrowave", tokens)
        self.assertIn("apple", tokens)

    def test_from_config_returns_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertIsNone(PDDLRagRetriever.from_config(RunConfig(tmp_dir)))

    def test_enabled_config_requires_clean_sources(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = RunConfig(tmp_dir, values={"decompose_rag": {"enabled": True}})

            with self.assertRaises(PDDLRagError):
                PDDLRagRetriever.from_config(config)

    def test_from_config_defaults_to_top_three(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "rag" / "clean.jsonl"
            index_path = root / "rag" / "clean_index.json"
            corpus_path.parent.mkdir(parents=True, exist_ok=True)
            corpus_path.write_text("", encoding="utf-8")
            index_path.write_text("{}", encoding="utf-8")
            config = RunConfig(
                root,
                values={
                    "decompose_rag": {
                        "enabled": True,
                        "corpus_path": str(corpus_path),
                        "index_path": str(index_path),
                        "runtime_db_path": str(root / "rag" / "runtime.sqlite"),
                    }
                },
            )

            retriever = PDDLRagRetriever.from_config(config)

            self.assertEqual(retriever.top_k, 3)

    def test_retrieve_builds_cache_filters_quality_and_eligible_docs_without_stage_filter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "rag" / "clean.jsonl"
            index_path = root / "rag" / "clean_index.json"
            db_path = root / "rag" / "runtime.sqlite"
            write_jsonl(
                corpus_path,
                [
                    doc(
                        "decompose:rich",
                        "decompose",
                        "Task: open fridge apple",
                        "open fridge apple fridge apple fridge",
                    ),
                    doc(
                        "decompose:weak",
                        "decompose",
                        "Task: open container",
                        "open fridge",
                    ),
                    doc(
                        "decompose:partial",
                        "decompose",
                        "Task: open fridge apple",
                        "open fridge apple",
                        quality="partial",
                    ),
                    doc(
                        "decompose:ineligible",
                        "decompose",
                        "Task: open fridge apple",
                        "open fridge apple",
                        retrieval_eligible=False,
                    ),
                    doc(
                        "allocate:other-stage",
                        "allocate",
                        "Task: open fridge apple",
                        "open fridge apple",
                    ),
                ],
            )
            index_path.write_text("{}", encoding="utf-8")
            config = RunConfig(
                root,
                values={
                    "decompose_rag": {
                        "enabled": True,
                        "corpus_path": str(corpus_path),
                        "index_path": str(index_path),
                        "runtime_db_path": str(db_path),
                        "top_k": 5,
                    }
                },
            )

            retriever = PDDLRagRetriever.from_config(config)
            examples = retriever.retrieve("decompose", "please open the fridge and move apple")

            self.assertTrue(db_path.exists())
            self.assertEqual(
                [example.doc_id for example in examples],
                ["decompose:rich", "allocate:other-stage", "decompose:weak"],
            )
            self.assertTrue(all(example.quality == "success" for example in examples))
            self.assertTrue(all(example.retrieval_eligible for example in examples))

    def test_retrieve_keeps_mixed_stage_docs_for_allocate_section(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "rag" / "clean.jsonl"
            index_path = root / "rag" / "clean_index.json"
            db_path = root / "rag" / "runtime.sqlite"
            write_jsonl(
                corpus_path,
                [
                    doc(
                        "decompose:other-stage",
                        "decompose",
                        "Task: heat apple microwave",
                        "heat apple microwave",
                    ),
                    doc(
                        "allocate:rich",
                        "allocate",
                        "Task: heat apple microwave",
                        "assign robot microwave apple microwave",
                    ),
                ],
            )
            index_path.write_text("{}", encoding="utf-8")
            config = RunConfig(
                root,
                values={
                    "allocate_rag": {
                        "enabled": True,
                        "corpus_path": str(corpus_path),
                        "index_path": str(index_path),
                        "runtime_db_path": str(db_path),
                        "top_k": 5,
                    }
                },
            )

            retriever = PDDLRagRetriever.from_config(config, section="allocate_rag")
            examples = retriever.retrieve("allocate", "heat the apple in microwave")

            self.assertCountEqual(
                [example.doc_id for example in examples],
                ["decompose:other-stage", "allocate:rich"],
            )
            self.assertCountEqual([example.stage for example in examples], ["decompose", "allocate"])

    def test_format_prompt_block_includes_top_three_full_contents_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            retriever = PDDLRagRetriever(
                root / "rag" / "clean.jsonl",
                root / "rag" / "clean_index.json",
                root / "rag" / "runtime.sqlite",
                max_example_chars=20,
                max_block_chars=60,
            )
            examples = [
                rag_example("decompose:one", "A" * 80),
                rag_example("decompose:two", "B" * 80),
                rag_example("decompose:three", "C" * 80),
                rag_example("decompose:four", "D" * 80),
            ]
            block = retriever.format_prompt_block("problem_generation", examples)

            self.assertIn(PROBLEM_GENERATION_RAG_SAFETY_RULES, block)
            self.assertIn("PDDL problem structure", block)
            self.assertNotIn("decomposition structure and reasoning style", block)
            self.assertNotIn("allocation reasoning style and output format", block)
            self.assertEqual(block.count("# Example"), 3)
            self.assertNotIn("# Retrieved Task Decomposition Examples (RAG)", block)
            self.assertNotIn("# End Retrieved Task Decomposition Examples (RAG)", block)
            self.assertNotIn("# Retrieved Example", block)
            self.assertNotIn("# Stage:", block)
            self.assertNotIn("doc_id:", block)
            self.assertNotIn("quality:", block)
            self.assertNotIn("task:", block)
            self.assertNotIn("score:", block)
            self.assertIn("A" * 80, block)
            self.assertIn("B" * 80, block)
            self.assertIn("C" * 80, block)
            self.assertNotIn("D" * 80, block)
            self.assertNotIn("...[truncated]", block)

    def test_format_prompt_block_uses_allocation_safety_rules(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            retriever = PDDLRagRetriever(
                root / "rag" / "clean.jsonl",
                root / "rag" / "clean_index.json",
                root / "rag" / "runtime.sqlite",
            )

            block = retriever.format_prompt_block("allocate", [rag_example("allocate:one", "allocation output")])

            self.assertIn("allocation reasoning style and output format", block)
            self.assertNotIn("decomposition structure and reasoning style", block)
            self.assertIn("# Example", block)
            self.assertIn("allocation output", block)


if __name__ == "__main__":
    unittest.main()
