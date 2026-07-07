import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import RAG_PROMPT_TITLE
from pddlrun_llmseparate import (
    PDDLUtils,
    TaskManager,
    build_robot_domain_name_map,
    build_robot_team,
)
from llm_logger import get_llm_logger
from run_config import RunConfig


MODEL = "deepseek-v4-pro"
TEST_SET = "final_test_new_0630_1"
LOG_DIR = Path(__file__).resolve().parent / "logs"
WITH_RAG_LOG_FILE = LOG_DIR / "test_decompose_rag_live_comparison_withrag.txt"
NO_RAG_LOG_FILE = LOG_DIR / "test_decompose_rag_live_comparison_norag.txt"

SAMPLES: List[Dict[str, Any]] = [
    {
        "test_set": TEST_SET,
        "floor_plan": "303",
        "task_index": 17,
        "task": "open the box, then break the mug, then put the mug on the shelf.",
        "robot_list": [16, 15],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "302",
        "task_index": 3,
        "task": (
            "put the CD on the shelf, put the teddy bear and credit card on the desk, "
            "then open the drawer."
        ),
        "robot_list": [18, 22],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "415",
        "task_index": 29,
        "task": (
            "put the soap bottle on the shelf, switch on the lightswitch, "
            "put the toilet paper in the garbage can, and switch on the faucet."
        ),
        "robot_list": [24, 1, 5, 17],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "217",
        "task_index": 15,
        "task": "open the box, then put the credit card on the sofa, then open the drawer.",
        "robot_list": [24, 17],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "9",
        "task_index": 13,
        "task": "put the egg on the sinkbasin, then open the cabinet and the drawer.",
        "robot_list": [8, 19, 9, 20],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "13",
        "task_index": 18,
        "task": (
            "wash the cup, wash the pot, fill the pot with water, "
            "heat the pot on the stoveburner, then open the cabinet."
        ),
        "robot_list": [25, 7],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "420",
        "task_index": 6,
        "task": "switch on the candle, then break the window, then open the drawer.",
        "robot_list": [25, 9, 23, 28],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "422",
        "task_index": 10,
        "task": (
            "switch on the candle, then switch on the lightswitch, "
            "then put the soapbottle in the garbagecan."
        ),
        "robot_list": [21, 3],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "204",
        "task_index": 0,
        "task": "break the vase, then put the pillow on the sofa, then open the drawer.",
        "robot_list": [14, 3, 4, 6],
    },
    {
        "test_set": TEST_SET,
        "floor_plan": "203",
        "task_index": 6,
        "task": "break the window, then open the laptop, break the laptop, and break the vase.",
        "robot_list": [8, 26, 25, 6],
    },
]


def make_config(*, decompose_rag_enabled: bool) -> RunConfig:
    return RunConfig(
        ROOT,
        values={
            "decompose_rag": {
                "enabled": decompose_rag_enabled,
                "corpus_path": "data/rag/task_decompose_corpus.jsonl",
                "index_path": "data/rag/task_decompose_index.json",
                "runtime_db_path": "data/rag/task_decompose_runtime.sqlite",
                "quality": ["success"],
                "retrieval_eligible_only": True,
                "top_k": 2,
                "prewarm_runtime_db": True,
            }
        },
    )


class LiveDecomposeRagComparisonTest(unittest.TestCase):
    maxDiff = None

    def test_live_decomposition_with_and_without_rag(self):
        self._reset_logs()
        rag_config = make_config(decompose_rag_enabled=True)
        no_rag_config = make_config(decompose_rag_enabled=False)

        for sample_number, sample in enumerate(SAMPLES, start=1):
            with self.subTest(
                floor_plan=sample["floor_plan"],
                task_index=sample["task_index"],
            ):
                record = self._load_and_validate_sample(rag_config, sample)
                with_rag = self._run_decomposition(sample, record, rag_config)
                without_rag = self._run_decomposition(sample, record, no_rag_config)

                self.assertTrue(with_rag["text"].strip())
                self.assertTrue(without_rag["text"].strip())

                rag_manifest = with_rag["manifest"]
                rag_retrievals = (
                    rag_manifest.get("decompose_rag", {})
                    .get("retrievals", {})
                )
                self.assertIn("decompose", rag_retrievals)

                self.assertNotIn(RAG_PROMPT_TITLE, without_rag["prompt"])

                self._append_decomposition_log(
                    WITH_RAG_LOG_FILE,
                    sample_number,
                    sample,
                    "WITH RAG",
                    with_rag,
                )
                self._append_decomposition_log(
                    NO_RAG_LOG_FILE,
                    sample_number,
                    sample,
                    "WITHOUT RAG",
                    without_rag,
                )

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

    def _run_decomposition(
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
        domain_content = manager.file_processor.read_file(str(config.allaction_domain_path()))
        robot_team = build_robot_team(record["robot list"])
        manager.current_robot_domain_names = build_robot_domain_name_map(record["robot list"])
        floor_plan_number = int(PDDLUtils.extract_floor_plan_number(sample["floor_plan"]))
        objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(floor_plan_number, config)}"

        get_llm_logger().clear_context()
        try:
            result = manager._run_decompose_generation(
                record["task"],
                domain_content,
                robot_team,
                objects_ai,
            )
        finally:
            get_llm_logger().clear_context()
        return {
            "prompt": result["prompt"],
            "text": result["text"],
            "manifest": manager.current_task_manifest,
        }

    def _append_decomposition_log(
        self,
        log_file: Path,
        sample_number: int,
        sample: Dict[str, Any],
        mode: str,
        result: Dict[str, Any],
    ) -> None:
        lines = [
            "",
            "=" * 88,
            f"TASK {sample_number}/{len(SAMPLES)}",
            f"mode: {mode}",
            f"floor_plan: {sample['floor_plan']}",
            f"task_index: {sample['task_index']}",
            f"task: {sample['task']}",
            "",
            "prompt:",
            str(result["prompt"]),
            "",
            "decomposition:",
            str(result["text"]),
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
