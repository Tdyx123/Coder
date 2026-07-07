import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import PDDLRagError, PDDLRagRetriever, RAG_PROMPT_TITLE, _tokenize_query
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

    def test_from_config_returns_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertIsNone(PDDLRagRetriever.from_config(RunConfig(tmp_dir)))

    def test_enabled_config_requires_clean_sources(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = RunConfig(tmp_dir, values={"decompose_rag": {"enabled": True}})

            with self.assertRaises(PDDLRagError):
                PDDLRagRetriever.from_config(config)

    def test_retrieve_builds_cache_filters_stage_quality_and_eligible_docs(self):
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
            self.assertEqual([example.doc_id for example in examples], ["decompose:rich", "decompose:weak"])
            self.assertTrue(all(example.stage == "decompose" for example in examples))
            self.assertTrue(all(example.quality == "success" for example in examples))
            self.assertTrue(all(example.retrieval_eligible for example in examples))

    def test_format_prompt_block_includes_safety_rules_and_truncates_content(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "rag" / "clean.jsonl"
            index_path = root / "rag" / "clean_index.json"
            write_jsonl(
                corpus_path,
                [
                    doc(
                        "problem_generation:one",
                        "problem_generation",
                        "Task: slice potato",
                        "A" * 80,
                    )
                ],
            )
            index_path.write_text("{}", encoding="utf-8")
            retriever = PDDLRagRetriever.from_config(
                RunConfig(
                    root,
                    values={
                        "decompose_rag": {
                            "enabled": True,
                            "corpus_path": str(corpus_path),
                            "index_path": str(index_path),
                            "runtime_db_path": str(root / "rag" / "runtime.sqlite"),
                            "max_example_chars": 20,
                        }
                    },
                )
            )

            examples = retriever.retrieve("problem_generation", "slice potato")
            block = retriever.format_prompt_block("problem_generation", examples)

            self.assertIn(RAG_PROMPT_TITLE, block)
            self.assertIn("Do not copy object names, robot tokens, floor-plan facts", block)
            self.assertIn("doc_id: problem_generation:one", block)
            self.assertIn("quality: success", block)
            self.assertIn("task: task problem_generation:one", block)
            self.assertIn("...[truncated]", block)


if __name__ == "__main__":
    unittest.main()
