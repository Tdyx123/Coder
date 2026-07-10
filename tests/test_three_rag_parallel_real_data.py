import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import PDDLRagRetriever
from pddlrun_llmseparate import TaskManager
from run_config import RunConfig


RAG_FILE_KEYS = {
    "decompose_rag": (
        "corpus_path",
        "index_path",
        "runtime_db_path",
    ),
    "allocate_rag": (
        "corpus_path",
        "index_path",
        "runtime_db_path",
    ),
    "problem_rag": (
        "corpus_path",
        "index_path",
        "runtime_db_path",
    ),
}

RAG_CALLS = {
    "decompose": {
        "section": "decompose_rag",
        "manifest_section": "decompose_rag",
        "manifest_key": "decompose",
        "doc_prefix": "decompose:",
        "safety_phrase": "decomposition structure and reasoning style",
        "wrong_phrases": (
            "allocation reasoning style and output format",
            "PDDL problem structure",
        ),
        "query": "Task: Put apple in fridge and switch off the light",
    },
    "allocate": {
        "section": "allocate_rag",
        "manifest_section": "allocate_rag",
        "manifest_key": "allocate",
        "doc_prefix": "allocate:",
        "safety_phrase": "allocation reasoning style and output format",
        "wrong_phrases": (
            "decomposition structure and reasoning style",
            "PDDL problem structure",
        ),
        "query": (
            "Task: Put apple in fridge and switch off the light\n"
            "Required skills: GoToObject, PickupObject, OpenObject, PutObject, CloseObject, SwitchOff"
        ),
    },
    "problem_generation": {
        "section": "problem_rag",
        "manifest_section": "problem_rag",
        "manifest_key": "subtask_01",
        "doc_prefix": "problem_generation:",
        "safety_phrase": "PDDL problem structure",
        "wrong_phrases": (
            "decomposition structure and reasoning style",
            "allocation reasoning style and output format",
        ),
        "query": (
            "Task: break the bowl.\n"
            "Subtask 1: Break the bowl\n"
            "Domain actions: GoToObject, BreakObject\n"
            "Assigned Robot: robot4"
        ),
    },
}


class RetrievalConcurrencyProbe:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = []
        self.lock = threading.Lock()

    def enter(self, stage: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(stage)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


class ReadOnlyRecordingRetriever:
    runtime_db_path = None

    def __init__(self, retriever: PDDLRagRetriever, probe: RetrievalConcurrencyProbe):
        self.retriever = retriever
        self.probe = probe

    def query_tokens(self, query_text: str, stage=None):
        return self.retriever.query_tokens(query_text, stage=stage)

    def retrieve(self, stage: str, query_text: str):
        self.probe.enter(stage)
        try:
            time.sleep(0.02)
            return self.retriever.retrieve(stage, query_text)
        finally:
            self.probe.exit()

    def format_prompt_block(self, stage: str, examples):
        return self.retriever.format_prompt_block(stage, examples)


class ThreeRagParallelRealDataTest(unittest.TestCase):
    def test_three_real_rag_retrievers_can_be_submitted_in_parallel(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self._real_rag_config(Path(tmp_dir))
            self._assert_real_rag_files_exist(config)
            probe = RetrievalConcurrencyProbe()
            retrievers = {
                name: ReadOnlyRecordingRetriever(
                    self._read_only_real_retriever(config, call["section"]),
                    probe,
                )
                for name, call in RAG_CALLS.items()
            }
            manager = TaskManager(str(ROOT), "test-model", config=config)
            manager.current_task_manifest = {
                "artifacts": {},
                "task": "Put apple in fridge, switch off the light, and break the bowl",
            }
            manager.decompose_rag_retriever = retrievers["decompose"]
            manager.allocate_rag_retriever = retrievers["allocate"]
            manager.problem_rag_retriever = retrievers["problem_generation"]
            start_barrier = threading.Barrier(len(RAG_CALLS))

            def call_rag(name: str) -> str:
                start_barrier.wait(timeout=10)
                query = RAG_CALLS[name]["query"]
                if name == "decompose":
                    return manager._decompose_rag_prompt_block(query)
                if name == "allocate":
                    return manager._allocate_rag_prompt_block(query)
                return manager._problem_rag_prompt_block(query, "subtask_01")

            with ThreadPoolExecutor(max_workers=len(RAG_CALLS)) as executor:
                blocks = dict(zip(RAG_CALLS, executor.map(call_rag, RAG_CALLS)))

        self.assertEqual(probe.max_active, 1)
        self.assertCountEqual(probe.calls, ["decompose", "allocate", "problem_generation"])

        for name, call in RAG_CALLS.items():
            block = blocks[name]
            self.assertTrue(block.strip(), name)
            self.assertIn(call["safety_phrase"], block)
            for wrong_phrase in call["wrong_phrases"]:
                self.assertNotIn(wrong_phrase, block)

            retrieval = (
                manager.current_task_manifest[call["manifest_section"]]["retrievals"][call["manifest_key"]]
            )
            self.assertFalse(retrieval["timeout"])
            self.assertTrue(retrieval["query_tokens"])
            self.assertTrue(retrieval["examples"])
            self.assertTrue(
                retrieval["examples"][0]["doc_id"].startswith(call["doc_prefix"]),
                retrieval["examples"][0]["doc_id"],
            )

    def _real_rag_config(self, tmp_dir: Path) -> RunConfig:
        values = {
            "storage": {
                "base_dir": str(tmp_dir / "intermediate_runs"),
                "task_manager_runs_dir": str(tmp_dir / "task_manager_runs"),
            },
        }
        for call in RAG_CALLS.values():
            values[call["section"]] = {
                "enabled": True,
                "top_k": 1,
                "query_timeout_seconds": 10,
            }
        return RunConfig(ROOT, values=values)

    def _assert_real_rag_files_exist(self, config: RunConfig) -> None:
        missing = []
        for section, keys in RAG_FILE_KEYS.items():
            for key in keys:
                path = config.path(section, key)
                if not path.exists():
                    missing.append(str(path))

        self.assertEqual([], missing, "Missing real RAG data files:\n" + "\n".join(missing))

    def _read_only_real_retriever(self, config: RunConfig, section: str) -> PDDLRagRetriever:
        retriever = PDDLRagRetriever.from_config(config, section=section)

        def fail_runtime_db_build():
            raise AssertionError("real data parallel test must not rebuild runtime DB")

        retriever.build_runtime_db = fail_runtime_db_build
        retriever.ensure_runtime_db = fail_runtime_db_build
        return retriever


if __name__ == "__main__":
    unittest.main()
