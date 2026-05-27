import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from dpo_generator import format_dpo_example, generate_dpo_examples, write_dpo_jsonl


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_dataset(root: Path, test_set: str = "sample_set", floor_plan: str = "6", subtask_count: int = 2):
    dataset_dir = root / "data" / test_set
    dataset_dir.mkdir(parents=True)
    record = {
        "task": "open the drawer, then break the cup.",
        "subtasks": [{"skill": "Open"} for _ in range(subtask_count)],
    }
    (dataset_dir / f"FloorPlan{floor_plan}.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_decompose_run(root: Path, name: str, prompt: str, output: str) -> Path:
    run_dir = root / "runs" / name
    stage_dir = run_dir / "01_decompose"
    stage_dir.mkdir(parents=True)
    (stage_dir / "01_decompose_prompt.txt").write_text(prompt, encoding="utf-8")
    (stage_dir / "02_decompose_output.txt").write_text(output, encoding="utf-8")
    return run_dir


def write_problem_generation_run(
    root: Path,
    name: str,
    slot: str,
    prompt: str,
    output: str,
    return_code: int,
) -> Path:
    run_dir = root / "runs" / name
    prompt_dir = run_dir / "05_problem_generation" / "prompts"
    output_dir = run_dir / "05_problem_generation" / "outputs"
    prompt_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (prompt_dir / f"{slot}_prompt.txt").write_text(prompt, encoding="utf-8")
    (output_dir / f"{slot}_problem.pddl").write_text(output, encoding="utf-8")
    write_json(
        run_dir / "08_planner" / "planner_manifest.json",
        [
            {
                "problem_file": f"{slot}_problem_validated.pddl",
                "return_code": return_code,
            }
        ],
    )
    return run_dir


def write_summary(
    root: Path,
    name: str,
    run_dir: Path,
    tc: int,
    total: int,
    test_set: str = "sample_set",
    floor_plan: str = "6",
    task_index: int = 0,
    task: str = "open the drawer, then break the cup.",
) -> Path:
    summary_path = root / "parallel_runs" / name / "summary.json"
    write_json(
        summary_path,
        {
            "repo_root": str(root),
            "test_set": test_set,
            "summaries": [
                {
                    "floor_plan": floor_plan,
                    "results": [
                        {
                            "floor_plan": floor_plan,
                            "task_index": task_index,
                            "task": task,
                            "model": name,
                            "status": "success",
                            "task_run_dir": str(run_dir),
                            "tc": tc,
                            "total": total,
                        }
                    ],
                }
            ],
        },
    )
    return summary_path


class DpoGeneratorTest(unittest.TestCase):
    def test_format_ms_swift_without_system_message(self):
        example = {
            "prompt": "怎么学习 Python？",
            "chosen": "建议从基础语法、练习题和小项目开始。",
            "rejected": "去网上随便看看就行。",
        }

        formatted = format_dpo_example(example, "ms_swift")

        self.assertEqual(formatted, {
            "messages": [
                {"role": "user", "content": "怎么学习 Python？"},
                {"role": "assistant", "content": "建议从基础语法、练习题和小项目开始。"},
            ],
            "rejected_response": "去网上随便看看就行。",
        })

    def test_format_ms_swift_with_system_message(self):
        example = {
            "prompt": "解释一下 DPO",
            "chosen": "DPO 是一种直接用偏好对训练模型的方法。",
            "rejected": "DPO 就是普通 SFT。",
        }

        formatted = format_dpo_example(example, "ms_swift", system_message="你是一个有帮助的助手")

        self.assertEqual(formatted, {
            "messages": [
                {"role": "system", "content": "你是一个有帮助的助手"},
                {"role": "user", "content": "解释一下 DPO"},
                {"role": "assistant", "content": "DPO 是一种直接用偏好对训练模型的方法。"},
            ],
            "rejected_response": "DPO 就是普通 SFT。",
        })

    def test_format_trl_keeps_prompt_chosen_rejected(self):
        example = {
            "prompt": "prompt",
            "chosen": "chosen",
            "rejected": "rejected",
        }

        self.assertEqual(format_dpo_example(example, "trl"), example)

    def test_invalid_output_format_raises(self):
        with self.assertRaises(ValueError):
            format_dpo_example({"prompt": "p", "chosen": "c", "rejected": "r"}, "unknown")

    def test_write_dpo_jsonl_uses_requested_format(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "train_dpo.jsonl"
            write_dpo_jsonl(
                [{"prompt": "p", "chosen": "c", "rejected": "r"}],
                str(output_path),
                output_format="ms_swift",
                system_message="s",
            )

            [line] = output_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(line), {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "p"},
                    {"role": "assistant", "content": "c"},
                ],
                "rejected_response": "r",
            })

    def test_similar_prompt_same_task_generates_preference(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=2)
            better_run = write_decompose_run(
                root,
                "better",
                "Task: open the drawer, then break the cup.\nObjects: drawer cup\n",
                "good decomposition",
            )
            worse_run = write_decompose_run(
                root,
                "worse",
                "Task: open the drawer, then break the cup. \nObjects: drawer cup\n",
                "bad decomposition",
            )
            better_summary = write_summary(root, "better_summary", better_run, tc=2, total=2)
            worse_summary = write_summary(root, "worse_summary", worse_run, tc=1, total=2)

            examples, stats = generate_dpo_examples(
                [str(better_summary), str(worse_summary)],
                stages_to_include=["01_decompose"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(stats.examples_written, 1)
            self.assertEqual(examples, [
                {
                    "prompt": "Task: open the drawer, then break the cup.\nObjects: drawer cup\n",
                    "chosen": "good decomposition",
                    "rejected": "bad decomposition",
                }
            ])

    def test_dissimilar_prompt_does_not_pair(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=2)
            first_run = write_decompose_run(root, "first", "Task: open drawer", "better")
            second_run = write_decompose_run(
                root,
                "second",
                "Completely different kitchen planning request with unrelated objects",
                "worse",
            )
            first_summary = write_summary(root, "first_summary", first_run, tc=2, total=2)
            second_summary = write_summary(root, "second_summary", second_run, tc=1, total=2)

            examples, stats = generate_dpo_examples(
                [str(first_summary), str(second_summary)],
                stages_to_include=["01_decompose"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(examples, [])
            self.assertEqual(stats.prompt_groups_considered, 0)

    def test_different_task_key_does_not_pair(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, test_set="set_a", subtask_count=2)
            write_dataset(root, test_set="set_b", subtask_count=2)
            first_run = write_decompose_run(root, "first", "Task: open drawer", "better")
            second_run = write_decompose_run(root, "second", "Task: open drawer", "worse")
            first_summary = write_summary(root, "first_summary", first_run, tc=2, total=2, test_set="set_a")
            second_summary = write_summary(root, "second_summary", second_run, tc=1, total=2, test_set="set_b")

            examples, stats = generate_dpo_examples(
                [str(first_summary), str(second_summary)],
                stages_to_include=["01_decompose"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(examples, [])
            self.assertEqual(stats.prompt_groups_considered, 0)

    def test_different_subtask_slot_does_not_pair(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=2)
            first_run = write_problem_generation_run(
                root,
                "first",
                "subtask_01",
                "Generate PDDL for opening a drawer",
                "(define (problem good))",
                return_code=0,
            )
            second_run = write_problem_generation_run(
                root,
                "second",
                "subtask_02",
                "Generate PDDL for opening a drawer",
                "(define (problem bad))",
                return_code=1,
            )
            first_summary = write_summary(root, "first_summary", first_run, tc=2, total=2)
            second_summary = write_summary(root, "second_summary", second_run, tc=1, total=2)

            examples, stats = generate_dpo_examples(
                [str(first_summary), str(second_summary)],
                stages_to_include=["05_problem_generation"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(examples, [])
            self.assertEqual(stats.prompt_groups_considered, 0)

    def test_problem_generation_uses_planner_manifest_score(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=1)
            better_run = write_problem_generation_run(
                root,
                "better",
                "subtask_01",
                "Generate PDDL for opening a drawer",
                "(define (problem good))",
                return_code=0,
            )
            worse_run = write_problem_generation_run(
                root,
                "worse",
                "subtask_01",
                "Generate PDDL for opening a drawer.",
                "(define (problem bad))",
                return_code=1,
            )
            better_summary = write_summary(root, "better_summary", better_run, tc=1, total=1)
            worse_summary = write_summary(root, "worse_summary", worse_run, tc=1, total=1)

            examples, stats = generate_dpo_examples(
                [str(better_summary), str(worse_summary)],
                stages_to_include=["05_problem_generation"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(stats.examples_written, 1)
            self.assertEqual(examples[0]["chosen"], "(define (problem good))")
            self.assertEqual(examples[0]["rejected"], "(define (problem bad))")

    def test_same_score_or_same_output_skips_pair(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=2)
            same_score_a = write_decompose_run(root, "same_score_a", "Task: open drawer", "answer a")
            same_score_b = write_decompose_run(root, "same_score_b", "Task: open drawer", "answer b")
            same_output_a = write_decompose_run(root, "same_output_a", "Task: break cup", "same answer")
            same_output_b = write_decompose_run(root, "same_output_b", "Task: break cup", "same answer")

            summaries = [
                write_summary(root, "same_score_a_summary", same_score_a, tc=1, total=2, task_index=0),
                write_summary(root, "same_score_b_summary", same_score_b, tc=1, total=2, task_index=0),
                write_summary(root, "same_output_a_summary", same_output_a, tc=2, total=2, task_index=1),
                write_summary(root, "same_output_b_summary", same_output_b, tc=1, total=2, task_index=1),
            ]

            examples, stats = generate_dpo_examples(
                [str(path) for path in summaries],
                stages_to_include=["01_decompose"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(examples, [])
            self.assertEqual(stats.skipped_same_score, 1)
            self.assertEqual(stats.skipped_same_output, 1)

    def test_conflicting_task_text_for_same_key_skips_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_dataset(root, subtask_count=2)
            first_run = write_decompose_run(root, "first", "Task: open drawer", "better")
            second_run = write_decompose_run(root, "second", "Task: open drawer", "worse")
            first_summary = write_summary(
                root,
                "first_summary",
                first_run,
                tc=2,
                total=2,
                task="open the drawer, then break the cup.",
            )
            second_summary = write_summary(
                root,
                "second_summary",
                second_run,
                tc=1,
                total=2,
                task="wash the plate instead.",
            )

            examples, stats = generate_dpo_examples(
                [str(first_summary), str(second_summary)],
                stages_to_include=["01_decompose"],
                prompt_similarity_threshold=0.90,
            )

            self.assertEqual(examples, [])
            self.assertEqual(stats.skipped_task_conflicts, 1)


if __name__ == "__main__":
    unittest.main()
