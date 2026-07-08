import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import ALLOCATE_RAG_SAFETY_RULES
from pddlrun_llmseparate import (
    PDDLUtils,
    TaskManager,
    build_robot_domain_name_map,
    build_robot_team,
    prewarm_allocate_rag_runtime_db,
)
from llm_logger import get_llm_logger
from run_config import RunConfig


MODEL = "deepseek-v4-pro"
LOG_DIR = Path(__file__).resolve().parent / "logs"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "live_comparison"
DECOMPOSITIONS_FILE = FIXTURE_DIR / "decompositions.json"
KEY_OBJECTS_FILE = FIXTURE_DIR / "key_objects.json"
WITH_RAG_LOG_FILE = LOG_DIR / "test_allocate_rag_live_comparison_withrag.txt"
NO_RAG_LOG_FILE = LOG_DIR / "test_allocate_rag_live_comparison_norag.txt"


def make_config(*, allocate_rag_enabled: bool) -> RunConfig:
    return RunConfig(
        ROOT,
        values={
            "allocate_rag": {
                "enabled": allocate_rag_enabled,
                "corpus_path": "data/rag/task_allocate_corpus.jsonl",
                "index_path": "data/rag/task_allocate_index.json",
                "runtime_db_path": "data/rag/task_allocate_runtime.sqlite",
                "quality": ["success"],
                "retrieval_eligible_only": True,
                "top_k": 2,
                "prewarm_runtime_db": True,
            }
        },
    )


class LiveAllocateRagComparisonTest(unittest.TestCase):
    maxDiff = None

    def test_live_allocation_with_and_without_rag(self):
        samples = self._load_samples()
        self._reset_logs()
        rag_config = make_config(allocate_rag_enabled=True)
        no_rag_config = make_config(allocate_rag_enabled=False)
        self._assert_allocate_rag_source_files_exist(rag_config)
        self.assertTrue(prewarm_allocate_rag_runtime_db(rag_config))

        for sample_number, sample in enumerate(samples, start=1):
            with self.subTest(
                floor_plan=sample["floor_plan"],
                task_index=sample["task_index"],
            ):
                record = self._load_and_validate_sample(rag_config, sample)
                with_rag = self._run_allocation(sample, record, rag_config)
                without_rag = self._run_allocation(sample, record, no_rag_config)

                self.assertTrue(with_rag["text"].strip())
                self.assertTrue(without_rag["text"].strip())

                rag_retrievals = (
                    with_rag["manifest"]
                    .get("allocate_rag", {})
                    .get("retrievals", {})
                )
                self.assertIn("allocate", rag_retrievals)
                rag_retrieval = rag_retrievals["allocate"]
                self.assertEqual("allocate", rag_retrieval.get("stage"))
                self.assertIn("query_tokens", rag_retrieval)
                self.assertIn("timeout", rag_retrieval)
                self.assertNotIn("allocate_rag", without_rag["manifest"])
                self.assertNotIn(ALLOCATE_RAG_SAFETY_RULES, without_rag["prompt"])

                rag_examples = rag_retrieval.get("examples", [])
                self.assertIsInstance(rag_examples, list)
                if rag_examples:
                    self.assertIn(ALLOCATE_RAG_SAFETY_RULES, with_rag["prompt"])
                    self.assertIn("# Example\n", with_rag["prompt"])
                else:
                    self.assertNotIn(ALLOCATE_RAG_SAFETY_RULES, with_rag["prompt"])

                self._append_allocation_log(
                    WITH_RAG_LOG_FILE,
                    sample_number,
                    len(samples),
                    sample,
                    "WITH RAG",
                    with_rag,
                )
                self._append_allocation_log(
                    NO_RAG_LOG_FILE,
                    sample_number,
                    len(samples),
                    sample,
                    "WITHOUT RAG",
                    without_rag,
                )

    def _load_samples(self) -> List[Dict[str, Any]]:
        self.assertTrue(
            DECOMPOSITIONS_FILE.exists(),
            f"Fixture file missing: {DECOMPOSITIONS_FILE}",
        )
        self.assertTrue(
            KEY_OBJECTS_FILE.exists(),
            f"Fixture file missing: {KEY_OBJECTS_FILE}",
        )
        with DECOMPOSITIONS_FILE.open("r", encoding="utf-8") as handle:
            samples = json.load(handle)
        with KEY_OBJECTS_FILE.open("r", encoding="utf-8") as handle:
            key_object_records = json.load(handle)
        self.assertIsInstance(samples, list)
        self.assertIsInstance(key_object_records, list)
        self.assertEqual(10, len(samples))
        self.assertEqual(len(samples), len(key_object_records))
        for sample, key_object_record in zip(samples, key_object_records):
            self.assertTrue(str(sample.get("decomposition", "")).strip())
            for key in ("test_set", "floor_plan", "task_index", "task", "robot_list"):
                self.assertEqual(sample.get(key), key_object_record.get(key))
            key_objects = key_object_record.get("key_objects")
            self.assertIsInstance(key_objects, list)
            sample["key_objects"] = key_objects
        return samples

    def _assert_allocate_rag_source_files_exist(self, config: RunConfig) -> None:
        required_paths = [
            config.path("allocate_rag", "corpus_path"),
            config.path("allocate_rag", "index_path"),
        ]
        missing = [str(path) for path in required_paths if not path.exists()]
        self.assertEqual([], missing, f"Allocate RAG file(s) missing: {missing}")

    def _reset_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        WITH_RAG_LOG_FILE.write_text("", encoding="utf-8")
        NO_RAG_LOG_FILE.write_text("", encoding="utf-8")

    def _load_and_validate_sample(
        self,
        config: RunConfig,
        sample: Dict[str, Any],
    ) -> Dict[str, Any]:
        dataset_file = config.dataset_file(sample["test_set"], sample["floor_plan"])
        self.assertTrue(dataset_file.exists(), f"Dataset file missing: {dataset_file}")

        record = None
        with dataset_file.open("r", encoding="utf-8") as handle:
            for idx, raw_line in enumerate(handle):
                if idx != sample["task_index"]:
                    continue
                record = json.loads(raw_line)
                break

        self.assertIsNotNone(
            record,
            f"Task index {sample['task_index']} missing from {dataset_file}",
        )
        self.assertFalse(record.get("invalid") or record.get("Invalid"))
        self.assertEqual(sample["task"], record["task"])
        self.assertEqual(sample["robot_list"], record["robot list"])
        return record

    def _run_allocation(
        self,
        sample: Dict[str, Any],
        record: Dict[str, Any],
        config: RunConfig,
    ) -> Dict[str, Any]:
        manager = TaskManager(
            base_path=str(ROOT),
            model=MODEL,
            config=config,
            test_set=sample["test_set"],
            floor_plan=sample["floor_plan"],
        )
        manager.current_task_manifest = {
            "artifacts": {},
            "task": record["task"],
            "task_index": sample["task_index"],
        }
        robot_team = build_robot_team(record["robot list"])
        manager.current_robot_domain_names = build_robot_domain_name_map(record["robot list"])
        floor_plan_number = int(PDDLUtils.extract_floor_plan_number(sample["floor_plan"]))
        objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(floor_plan_number, config)}"

        get_llm_logger().clear_context()
        try:
            result = manager._generate_allocation_plan(
                sample["decomposition"],
                robot_team,
                objects_ai,
                key_objects=sample["key_objects"],
            )
        finally:
            get_llm_logger().clear_context()

        return {
            "prompt": result["prompt"],
            "text": result["text"],
            "manifest": manager.current_task_manifest,
        }

    def _append_allocation_log(
        self,
        log_file: Path,
        sample_number: int,
        sample_count: int,
        sample: Dict[str, Any],
        mode: str,
        result: Dict[str, Any],
    ) -> None:
        retrievals = (
            result["manifest"]
            .get("allocate_rag", {})
            .get("retrievals", {})
        )
        lines = [
            "",
            "=" * 88,
            f"TASK {sample_number}/{sample_count}",
            f"mode: {mode}",
            f"floor_plan: {sample['floor_plan']}",
            f"task_index: {sample['task_index']}",
            f"task: {sample['task']}",
            "",
            "key_objects:",
            json.dumps(sample.get("key_objects", []), ensure_ascii=False, indent=2),
            "",
            "prompt:",
            str(result["prompt"]),
            "",
            "allocation:",
            str(result["text"]),
            "",
            "allocate_rag_retrievals:",
            json.dumps(retrievals, ensure_ascii=False, indent=2, sort_keys=True),
            "",
            "artifacts:",
            "artifacts disabled; core method result in memory",
            "=" * 88,
        ]
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")


if __name__ == "__main__":
    unittest.main()
