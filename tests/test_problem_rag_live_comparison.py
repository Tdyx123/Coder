import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_rag import PROBLEM_GENERATION_RAG_SAFETY_RULES
from pddlrun_llmseparate import (
    PDDLUtils,
    TaskManager,
    build_robot_domain_name_map,
    prewarm_problem_rag_runtime_db,
)
from llm_logger import get_llm_logger
from run_config import RunConfig


MODEL = "deepseek-v4-pro"
LOG_DIR = Path(__file__).resolve().parent / "logs"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "live_comparison"
PROBLEM_INPUTS_FILE = FIXTURE_DIR / "problem_generation_inputs.json"
WITH_RAG_LOG_FILE = LOG_DIR / "test_problem_rag_live_comparison_withrag.txt"
NO_RAG_LOG_FILE = LOG_DIR / "test_problem_rag_live_comparison_norag.txt"


def make_config(*, problem_rag_enabled: bool) -> RunConfig:
    return RunConfig(
        ROOT,
        values={
            "problem_rag": {
                "enabled": problem_rag_enabled,
                "corpus_path": "data/rag/task_problem_generation_corpus.jsonl",
                "index_path": "data/rag/task_problem_generation_index.json",
                "runtime_db_path": "data/rag/task_problem_generation_runtime.sqlite",
                "quality": ["success"],
                "retrieval_eligible_only": True,
                "top_k": 2,
                "prewarm_runtime_db": True,
            }
        },
    )


class LiveProblemRagComparisonTest(unittest.TestCase):
    maxDiff = None

    def test_live_problem_generation_with_and_without_rag(self):
        samples = self._load_samples()
        self._reset_logs()
        rag_config = make_config(problem_rag_enabled=True)
        no_rag_config = make_config(problem_rag_enabled=False)
        self._assert_problem_rag_source_files_exist(rag_config)
        self.assertTrue(prewarm_problem_rag_runtime_db(rag_config))

        for sample_number, sample in enumerate(samples, start=1):
            with self.subTest(
                floor_plan=sample["floor_plan"],
                task_index=sample["task_index"],
            ):
                record = self._load_and_validate_sample(rag_config, sample)
                with_rag = self._run_problem_generation(sample, record, rag_config)
                without_rag = self._run_problem_generation(sample, record, no_rag_config)

                self.assertEqual(len(sample["subtasks"]), len(with_rag["problems"]))
                self.assertEqual(len(sample["subtasks"]), len(without_rag["problems"]))
                self.assertTrue(all(problem.strip() for problem in with_rag["problems"]))
                self.assertTrue(all(problem.strip() for problem in without_rag["problems"]))

                rag_retrievals = (
                    with_rag["manifest"]
                    .get("problem_rag", {})
                    .get("retrievals", {})
                )
                self.assertNotIn("problem_rag", without_rag["manifest"])

                for subtask_index, prompt in enumerate(with_rag["prompts"], start=1):
                    retrieval_key = f"subtask_{subtask_index:02d}"
                    self.assertIn(retrieval_key, rag_retrievals)
                    retrieval = rag_retrievals[retrieval_key]
                    self.assertEqual("problem_generation", retrieval.get("stage"))
                    self.assertIn("query_tokens", retrieval)
                    self.assertIn("timeout", retrieval)

                    examples = retrieval.get("examples", [])
                    self.assertIsInstance(examples, list)
                    if examples:
                        self.assertIn(PROBLEM_GENERATION_RAG_SAFETY_RULES, prompt)
                        self.assertIn("# Example\n", prompt)
                    else:
                        self.assertNotIn(PROBLEM_GENERATION_RAG_SAFETY_RULES, prompt)

                for prompt in without_rag["prompts"]:
                    self.assertNotIn(PROBLEM_GENERATION_RAG_SAFETY_RULES, prompt)

                self._append_problem_log(
                    WITH_RAG_LOG_FILE,
                    sample_number,
                    len(samples),
                    sample,
                    "WITH RAG",
                    with_rag,
                )
                self._append_problem_log(
                    NO_RAG_LOG_FILE,
                    sample_number,
                    len(samples),
                    sample,
                    "WITHOUT RAG",
                    without_rag,
                )

    def _load_samples(self) -> List[Dict[str, Any]]:
        self.assertTrue(
            PROBLEM_INPUTS_FILE.exists(),
            f"Fixture file missing: {PROBLEM_INPUTS_FILE}",
        )
        with PROBLEM_INPUTS_FILE.open("r", encoding="utf-8") as handle:
            samples = json.load(handle)

        self.assertIsInstance(samples, list)
        self.assertEqual(10, len(samples))
        required_keys = {
            "test_set",
            "floor_plan",
            "task_index",
            "task",
            "robot_list",
            "decomposition",
            "subtasks",
            "allocation",
            "sequence_operations",
            "robot_assignments",
            "key_objects",
            "key_objects_by_subtask",
            "key_object_pddl_states",
            "key_object_pddl_states_by_subtask",
            "key_object_id_bindings",
            "key_object_id_bindings_by_subtask",
        }
        for sample in samples:
            self.assertTrue(required_keys.issubset(sample.keys()))
            self.assertTrue(str(sample["decomposition"]).strip())
            self.assertTrue(str(sample["allocation"]).strip())
            self.assertIsInstance(sample["subtasks"], list)
            self.assertTrue(sample["subtasks"])
            self.assertIsInstance(sample["sequence_operations"], list)
            self.assertTrue(sample["sequence_operations"])
            self.assertIsInstance(sample["robot_assignments"], dict)
            self.assertTrue(sample["robot_assignments"])
            self.assertIsInstance(sample["key_objects"], list)
            self.assertIsInstance(sample["key_objects_by_subtask"], dict)
            self.assertIsInstance(sample["key_object_pddl_states"], list)
            self.assertIsInstance(sample["key_object_pddl_states_by_subtask"], dict)
            self.assertIsInstance(sample["key_object_id_bindings"], list)
            self.assertIsInstance(sample["key_object_id_bindings_by_subtask"], dict)
        return samples

    def _assert_problem_rag_source_files_exist(self, config: RunConfig) -> None:
        required_paths = [
            config.path("problem_rag", "corpus_path"),
            config.path("problem_rag", "index_path"),
        ]
        missing = [str(path) for path in required_paths if not path.exists()]
        self.assertEqual([], missing, f"Problem RAG file(s) missing: {missing}")

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

    def _run_problem_generation(
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
        manager.current_robot_domain_names = build_robot_domain_name_map(record["robot list"])
        floor_plan_number = int(PDDLUtils.extract_floor_plan_number(sample["floor_plan"]))
        objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(floor_plan_number, config)}"
        robot_assignments = self._int_key_map(sample["robot_assignments"])
        key_object_pddl_states_by_subtask = {
            int(key): value
            for key, value in sample["key_object_pddl_states_by_subtask"].items()
        }
        static_problem_prompt = manager.file_processor.read_file(
            str(config.prompt_file(f"{manager.prompt_allocation_set}_problem.txt"))
        ) or ""
        domain_contents_by_robot = self._domain_contents_by_robot(manager, robot_assignments, len(sample["subtasks"]))

        get_llm_logger().clear_context()
        try:
            results = manager._run_problem_generation(
                subtasks=sample["subtasks"],
                robot_assignments=robot_assignments,
                llm=manager.llm,
                model=MODEL,
                objects_ai=objects_ai,
                domain_contents_by_robot=domain_contents_by_robot,
                static_problem_prompt=static_problem_prompt,
                key_object_pddl_states=sample["key_object_pddl_states"],
                key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
            )
        finally:
            get_llm_logger().clear_context()

        return {
            "prompts": [result.prompt for result in results],
            "problems": [result.problem for result in results],
            "manifest": manager.current_task_manifest,
        }

    def _int_key_map(self, value: Dict[str, Any]) -> Dict[int, int]:
        return {int(key): int(item) for key, item in value.items()}

    def _domain_contents_by_robot(
        self,
        manager: TaskManager,
        robot_assignments: Dict[int, int],
        subtask_count: int,
    ) -> Dict[str, str]:
        domain_contents = {}
        for subtask_index in range(1, subtask_count + 1):
            robot_num = robot_assignments.get(subtask_index, 1)
            normalized_robot_name = f"robot{robot_num}"
            real_robot_name = manager.current_robot_domain_names.get(
                normalized_robot_name,
                normalized_robot_name,
            )
            if real_robot_name in domain_contents:
                continue
            domain_path = manager.config.robot_domain_path(f"{real_robot_name}.pddl")
            domain_contents[real_robot_name] = manager.file_processor.read_file(str(domain_path)) or ""
        return domain_contents

    def _append_problem_log(
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
            .get("problem_rag", {})
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
            "sequence_operations:",
            json.dumps(sample["sequence_operations"], ensure_ascii=False, indent=2),
            "",
            "robot_assignments:",
            json.dumps(sample["robot_assignments"], ensure_ascii=False, indent=2, sort_keys=True),
            "",
            "key_object_pddl_states:",
            json.dumps(sample["key_object_pddl_states"], ensure_ascii=False, indent=2),
        ]
        for subtask_index, (subtask, prompt, problem) in enumerate(
            zip(sample["subtasks"], result["prompts"], result["problems"]),
            start=1,
        ):
            retrieval_key = f"subtask_{subtask_index:02d}"
            lines.extend(
                [
                    "",
                    "-" * 88,
                    f"SUBTASK {subtask_index}/{len(sample['subtasks'])}",
                    "",
                    "subtask:",
                    str(subtask),
                    "",
                    "prompt:",
                    str(prompt),
                    "",
                    "problem:",
                    str(problem),
                    "",
                    "problem_rag_retrieval:",
                    json.dumps(
                        retrievals.get(retrieval_key, {}),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                ]
            )
        lines.extend(
            [
                "",
                "artifacts:",
                "temporary run directory removed after core method result was captured",
                "=" * 88,
            ]
        )
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")


if __name__ == "__main__":
    unittest.main()
