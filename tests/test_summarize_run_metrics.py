import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import summarize_run_metrics
from summarize_run_metrics import main


def write_json(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_plan(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_main(argv):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        result_code = main(argv)
    return result_code, stdout.getvalue()


def assert_row(test_case, output, expected_row):
    lines = output.rstrip("\n").splitlines()
    test_case.assertEqual(len(lines), 1)
    test_case.assertEqual(lines[0].split(","), expected_row)


class SummarizeRunMetricsTest(unittest.TestCase):
    def test_main_summarizes_directory_inputs_as_comma_separated_row(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_dir = root / "logs" / "task_a"
            missing_outputs_dir = root / "logs" / "task_b"
            write_plan(
                task_dir / "08_planner" / "outputs" / "subtask_01_plan.txt",
                [
                    "(gotoobject robot1 apple)",
                    "; cost = 2 (unit cost)",
                    "",
                    "(pickupobject robot1 apple countertop)",
                ],
            )
            write_plan(
                task_dir / "08_planner" / "outputs" / "subtask_02_plan.txt",
                ["  (openobject robot1 fridge)  "],
            )
            missing_outputs_dir.mkdir(parents=True)

            parallel_run = root / "parallel_runs" / "sample"
            write_json(
                parallel_run / "summary.json",
                {
                    "success_count": 2,
                    "failure_count": 1,
                    "all_pass_count": 1,
                    "pass_one_count": 2,
                    "summaries": [
                        {
                            "results": [
                                {
                                    "duration_seconds": 10,
                                    "task_run_dir": str(task_dir),
                                    "llm_token_usage": {"total_tokens": 11},
                                },
                                {
                                    "duration_seconds": 20,
                                    "llm_token_usage": {"total_tokens": "29"},
                                },
                                {
                                    "duration_seconds": "bad",
                                    "task_run_dir": str(missing_outputs_dir),
                                    "llm_token_usage": {"total_tokens": None},
                                },
                            ]
                        }
                    ],
                },
            )

            coderun_dir = root / "coderun_results"
            write_json(
                coderun_dir / "old.json",
                {
                    "total_results": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "results": [{"gcr": 0.0, "action_sr": 0.0}],
                },
            )
            write_json(
                coderun_dir / "new.json",
                {
                    "total_results": 4,
                    "success_count": 2,
                    "failure_count": 2,
                    "results": [
                        {"gcr": 1.0, "action_sr": 1.0},
                        {"gcr": 0.5, "action_sr": 0.5},
                        {"gcr": "bad", "action_sr": "bad"},
                        {"gcr": None, "action_sr": None},
                    ],
                },
            )
            os.utime(coderun_dir / "old.json", (100, 100))
            os.utime(coderun_dir / "new.json", (200, 200))

            result_code, output = run_main(
                [
                    "--method",
                    "METHOD",
                    "--parallel-run",
                    str(parallel_run),
                    "--coderun-result",
                    str(coderun_dir),
                ]
            )

            self.assertEqual(result_code, 0)
            assert_row(
                self,
                output,
                [
                    "METHOD",
                    "",
                    "15 +- 5",
                    "0.3333",
                    "0.6667",
                    "1 +- 1.414",
                    "1.333",
                    "0.25",
                    "0.75 +- 0.25",
                    "0.75 +- 0.25",
                    "40",
                    "30",
                ],
            )

    def test_main_generate_code_sr_uses_parallel_summary_denominator_with_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "sample"
            write_json(
                parallel_run / "summary.json",
                {
                    "success_count": 10,
                    "failure_count": 0,
                    "all_pass_count": 0,
                    "pass_one_count": 0,
                    "summaries": [{"results": []}],
                },
            )
            coderun_result = root / "coderun_results" / "sample.json"
            write_json(
                coderun_result,
                {
                    "total_results": 9,
                    "success_count": 8,
                    "failure_count": 0,
                    "timeout_count": 1,
                    "results": [{"gcr": 1.0, "action_sr": 1.0}],
                },
            )

            result_code, output = run_main(
                [
                    "--method",
                    "METHOD",
                    "--parallel-run",
                    str(parallel_run),
                    "--coderun-result",
                    str(coderun_result),
                ]
            )

            self.assertEqual(result_code, 0)
            row = output.rstrip("\n").split(",")
            self.assertEqual(row[6], "0.9")

    def test_timeout_results_count_as_zero_for_coderun_metrics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "sample"
            write_json(
                parallel_run / "summary.json",
                {
                    "success_count": 3,
                    "failure_count": 0,
                    "all_pass_count": 0,
                    "pass_one_count": 0,
                    "summaries": [{"results": []}],
                },
            )
            coderun_result = root / "coderun_results" / "sample.json"
            write_json(
                coderun_result,
                {
                    "total_results": 3,
                    "success_count": 2,
                    "failure_count": 0,
                    "timeout_count": 1,
                    "results": [
                        {"status": "success", "gcr": 1.0, "action_sr": 1.0},
                        {"status": "success", "gcr": 0.5, "action_sr": 0.5},
                        {
                            "status": "timeout",
                            "timed_out": True,
                            "gcr": None,
                            "action_sr": 1.0,
                        },
                    ],
                },
            )

            result_code, output = run_main(
                [
                    "--method",
                    "METHOD",
                    "--parallel-run",
                    str(parallel_run),
                    "--coderun-result",
                    str(coderun_result),
                ]
            )

            self.assertEqual(result_code, 0)
            row = output.rstrip("\n").split(",")
            self.assertEqual(row[6], "1")
            self.assertEqual(row[7], "0.3333")
            self.assertEqual(row[8], "0.5 +- 0.4082")
            self.assertEqual(row[9], "0.5 +- 0.4082")

    def test_main_accepts_files_and_writes_output_with_empty_zero_denominators(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_summary = root / "parallel_runs" / "sample" / "summary.json"
            coderun_result = root / "coderun_results" / "sample.json"
            output_path = root / "metrics.csv"
            write_json(
                parallel_summary,
                {
                    "success_count": 0,
                    "failure_count": 0,
                    "all_pass_count": 0,
                    "pass_one_count": 0,
                    "summaries": [{"results": [{"duration_seconds": "bad"}]}],
                },
            )
            write_json(
                coderun_result,
                {
                    "total_results": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "results": [{"gcr": "bad", "action_sr": "bad"}],
                },
            )

            result_code, stdout = run_main(
                [
                    "--method",
                    "zero",
                    "--parallel-run",
                    str(parallel_summary),
                    "--coderun-result",
                    str(coderun_result),
                    "--output",
                    str(output_path),
                ]
            )

            self.assertEqual(result_code, 0)
            self.assertEqual(stdout, "")
            assert_row(
                self,
                output_path.read_text(encoding="utf-8"),
                ["zero", "", "", "", "", "0 +- 0", "", "", "", "", "", ""],
            )

    def test_lammap_baseline_uses_planner_summary_and_baseline_action_counts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            write_json(
                baseline_root
                / "parallel_runs"
                / "pddlrun_llmseparate_sample"
                / "summary.json",
                {
                    "success_count": 3,
                    "failure_count": 1,
                    "summaries": [
                        {
                            "results": [
                                {
                                    "duration_seconds": 10,
                                    "llm_token_usage": {"total_tokens": 25},
                                },
                                {
                                    "duration_seconds": 20,
                                    "llm_token_usage": {"total_tokens": 17},
                                },
                                {
                                    "duration_seconds": "bad",
                                    "llm_token_usage": {"total_tokens": "bad"},
                                },
                            ]
                        }
                    ],
                },
            )
            write_json(
                baseline_root
                / "plan_to_code_results"
                / "plan_to_code_results.json",
                [
                    {"action_count": 2},
                    {"action_count": 4},
                    {"action_count": "bad"},
                ],
            )
            coderun_result = root / "coderun_results" / "lammap.json"
            write_json(
                coderun_result,
                {
                    "total_results": 2,
                    "success_count": 99,
                    "failure_count": 1,
                    "results": [
                        {"gcr": 1.0, "action_sr": 0.5},
                        {"gcr": 0.0, "action_sr": 1.0},
                    ],
                },
            )

            result_code, output = run_main(
                [
                    "--baseline",
                    "LaMMA-P",
                    "--parallel-run",
                    str(baseline_root),
                    "--coderun-result",
                    str(coderun_result),
                ]
            )

            self.assertEqual(result_code, 0)
            assert_row(
                self,
                output,
                [
                    "LaMMA-P",
                    "",
                    "15 +- 5",
                    "",
                    "",
                    "3 +- 1",
                    "0.5",
                    "0.5",
                    "0.5 +- 0.5",
                    "0.75 +- 0.25",
                    "42",
                    "30",
                ],
            )

    def test_lammap_baseline_uses_default_root_when_parallel_run_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            write_json(
                baseline_root
                / "parallel_runs"
                / "pddlrun_llmseparate_sample"
                / "summary.json",
                {
                    "success_count": 2,
                    "failure_count": 0,
                    "summaries": [{"results": [{"duration_seconds": 8}]}],
                },
            )
            write_json(
                baseline_root
                / "plan_to_code_results"
                / "plan_to_code_results.json",
                [{"action_count": 5}],
            )
            coderun_result = root / "coderun_results" / "lammap.json"
            write_json(
                coderun_result,
                {
                    "total_results": 1,
                    "results": [{"gcr": 1.0, "action_sr": 1.0}],
                },
            )

            with patch.object(summarize_run_metrics, "REPO_ROOT", root):
                result_code, output = run_main(
                    [
                        "--baseline",
                        "LaMMA-P",
                        "--coderun-result",
                        str(coderun_result),
                    ]
                )

            self.assertEqual(result_code, 0)
            assert_row(
                self,
                output,
                [
                    "LaMMA-P",
                    "",
                    "8 +- 0",
                    "",
                    "",
                    "5 +- 0",
                    "0.5",
                    "1",
                    "1 +- 0",
                    "1 +- 0",
                    "",
                    "8",
                ],
            )

    def test_smart_llm_baseline_uses_decomposed_plan_count_as_generate_denominator(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "SMART-LLM"
            write_json(
                baseline_root / "plan_to_code_results.json",
                [
                    {"action_count": 2},
                    {"action_count": 6},
                    {"action_count": None},
                ],
            )
            (baseline_root / "logs" / "1" / "task_a").mkdir(parents=True)
            (baseline_root / "logs" / "1" / "task_a" / "decomposed_plan.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            (baseline_root / "logs" / "2" / "task_b").mkdir(parents=True)
            (baseline_root / "logs" / "2" / "task_b" / "decomposed_plan.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            coderun_result = root / "coderun_results" / "smart.json"
            write_json(
                coderun_result,
                {
                    "total_results": 1,
                    "success_count": 0,
                    "failure_count": 0,
                    "results": [{"gcr": 1.0, "action_sr": 0.25}],
                },
            )

            result_code, output = run_main(
                [
                    "--baseline",
                    "SMART-LLM",
                    "--parallel-run",
                    str(baseline_root),
                    "--coderun-result",
                    str(coderun_result),
                ]
            )

            self.assertEqual(result_code, 0)
            assert_row(
                self,
                output,
                [
                    "SMART-LLM",
                    "",
                    "",
                    "",
                    "",
                    "4 +- 2",
                    "0.5",
                    "1",
                    "1 +- 0",
                    "0.25 +- 0",
                    "",
                    "",
                ],
            )

    def test_smart_llm_baseline_uses_default_root_when_parallel_run_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "SMART-LLM"
            write_json(
                baseline_root / "plan_to_code_results.json",
                [{"action_count": 3}],
            )
            (baseline_root / "logs" / "1" / "task").mkdir(parents=True)
            (baseline_root / "logs" / "1" / "task" / "decomposed_plan.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            coderun_result = root / "coderun_results" / "smart.json"
            write_json(
                coderun_result,
                {
                    "total_results": 1,
                    "results": [{"gcr": 0.0, "action_sr": 0.5}],
                },
            )

            with patch.object(summarize_run_metrics, "REPO_ROOT", root):
                result_code, output = run_main(
                    [
                        "--baseline",
                        "SMART-LLM",
                        "--coderun-result",
                        str(coderun_result),
                    ]
                )

            self.assertEqual(result_code, 0)
            assert_row(
                self,
                output,
                [
                    "SMART-LLM",
                    "",
                    "",
                    "",
                    "",
                    "3 +- 0",
                    "1",
                    "0",
                    "0 +- 0",
                    "0.5 +- 0",
                    "",
                    "",
                ],
            )

    def test_method_and_baseline_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(
                    [
                        "--method",
                        "METHOD",
                        "--baseline",
                        "LaMMA-P",
                        "--parallel-run",
                        "unused",
                        "--coderun-result",
                        "unused",
                    ]
                )
        self.assertEqual(error.exception.code, 2)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["--parallel-run", "unused", "--coderun-result", "unused"])
        self.assertEqual(error.exception.code, 2)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["--method", "METHOD", "--coderun-result", "unused"])
        self.assertEqual(error.exception.code, 2)

    def test_lammap_baseline_requires_unique_parallel_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            write_json(
                baseline_root / "plan_to_code_results" / "plan_to_code_results.json",
                [],
            )
            coderun_result = root / "coderun_results" / "lammap.json"
            write_json(coderun_result, {"total_results": 0, "results": []})

            with self.assertRaisesRegex(RuntimeError, "Expected exactly one"):
                main(
                    [
                        "--baseline",
                        "LaMMA-P",
                        "--parallel-run",
                        str(baseline_root),
                        "--coderun-result",
                        str(coderun_result),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
