import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ai2thor_object_cache import get_ai2_thor_objects_cache_path
from pddlrun_llmseparate import (
    FileProcessor,
    PDDLPlanner,
    RunConfig,
    TaskManager,
    build_robot_domain_name_map,
    build_robot_team,
    load_run_config,
)


class PDDLRunConfigTests(unittest.TestCase):
    def test_load_run_config_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "run.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "storage": {"base_dir": "custom/intermediate"},
                        "data": {
                            "dataset_dir": "fixtures/data",
                            "ai2thor_objects_cache_dir": "cache/objects",
                        },
                        "planner": {"executable": "planner/fd.py"},
                    }
                ),
                encoding="utf-8",
            )

            config = load_run_config(root, config_path)

            self.assertEqual(config.storage_base_dir, root / "custom" / "intermediate")
            self.assertEqual(config.dataset_file("final_test", "FloorPlan15"), root / "fixtures" / "data" / "final_test" / "FloorPlan15.jsonl")
            self.assertEqual(config.ai2thor_objects_cache_dir, root / "cache" / "objects")
            self.assertEqual(config.planner_executable, root / "planner" / "fd.py")

    def test_missing_config_uses_current_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = load_run_config(root, root / "missing.yaml")

            self.assertEqual(config.storage_base_dir, root / "logs" / "intermediate_runs")
            self.assertEqual(config.task_manager_runs_dir, root / "logs" / "task_manager_runs")
            self.assertEqual(config.dataset_file("final_test", 6), root / "data" / "final_test" / "FloorPlan6.jsonl")
            self.assertEqual(config.ai2thor_objects_cache_dir, root / "data" / "ai2thor_objects_cache")

    def test_ai2thor_cache_path_accepts_configured_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir) / "objects"

            path = get_ai2_thor_objects_cache_path(21, cache_dir=cache_dir)

            self.assertEqual(path, cache_dir / "FloorPlan21.json")

    def test_planner_uses_configured_command_alias_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(
                root,
                values={
                    "planner": {
                        "executable": "custom/fast-downward.py",
                        "alias": "custom-alias",
                        "timeout_seconds": 17,
                    }
                },
            )
            file_processor = FileProcessor(str(root), config=config)
            planner = PDDLPlanner(str(root), file_processor, config=config)
            problem_file = root / "problem.pddl"
            problem_file.write_text("(define (problem test))", encoding="utf-8")

            def fake_run(command, stdout, stderr, text, timeout):
                self.assertEqual(command[0], str(root / "custom" / "fast-downward.py"))
                self.assertEqual(command[1:3], ["--alias", "custom-alias"])
                self.assertEqual(command[-1], str(problem_file))
                self.assertEqual(timeout, 17)

                class Result:
                    stdout = "Solution found!"
                    stderr = ""

                return Result()

            with patch("pddlrun_llmseparate.subprocess.run", side_effect=fake_run):
                planner.run_plan(str(root / "domain.pddl"), str(problem_file))

            self.assertEqual(problem_file.with_name("problem_plan.txt").read_text(encoding="utf-8"), "Solution found!")

    def test_run_planners_passes_plan_file_before_alias_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = RunConfig(
                root,
                values={
                    "planner": {
                        "executable": "custom/fast-downward.py",
                        "alias": "custom-alias",
                        "timeout_seconds": 17,
                    }
                },
            )
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_task_run_dir = str(root / "task_run")
            manager.current_task_manifest = {"artifacts": {}}
            Path(manager.current_task_run_dir).mkdir(parents=True)

            resources_dir = root / "resources"
            resources_dir.mkdir(parents=True, exist_ok=True)
            domain_file = resources_dir / "robot1.pddl"
            domain_file.write_text("(define (domain robot1))", encoding="utf-8")

            problem_file = Path(manager.file_processor.validated_subtask_path) / "task.pddl"
            problem_file.write_text("(define (problem task) (:domain robot1 ))", encoding="utf-8")
            output_file = problem_file.with_name("task_plan.txt")
            captured = {}

            def fake_run(command, stdout, stderr, text, timeout):
                captured["command"] = command
                self.assertEqual(timeout, 17)

                class Result:
                    stdout = "planner stdout"
                    stderr = ""
                    returncode = 0

                return Result()

            with patch("pddlrun_llmseparate.subprocess.run", side_effect=fake_run), \
                    patch.object(manager.file_processor, "write_file", wraps=manager.file_processor.write_file) as write_file:
                manager.run_planners()

            command = captured["command"]
            self.assertEqual(command[0], str(root / "custom" / "fast-downward.py"))
            self.assertEqual(command[1:3], ["--plan-file", str(output_file)])
            self.assertLess(command.index("--plan-file"), command.index("--alias"))
            self.assertEqual(command[3:5], ["--alias", "custom-alias"])
            self.assertEqual(command[-2:], [str(domain_file), str(problem_file)])
            self.assertFalse(output_file.exists())
            self.assertEqual(manager.current_task_manifest["planner"]["plan_output_files"], [str(output_file)])
            self.assertNotIn((str(output_file), "planner stdout"), [call.args for call in write_file.call_args_list])
            self.assertTrue(
                any(str(call.args[0]).endswith("08_planner/stdout/task_stdout.txt") and call.args[1] == "planner stdout"
                    for call in write_file.call_args_list)
            )

    def test_combine_all_plans_reads_recorded_plan_output_files_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = TaskManager(str(root), "test-model", config=RunConfig(root))
            manager.current_task_run_dir = str(root / "task_run")
            Path(manager.current_task_run_dir).mkdir(parents=True)

            recorded_plan = root / "recorded_plan.txt"
            recorded_plan.write_text("move robot apple (1)", encoding="utf-8")
            unrelated_plan = Path(manager.file_processor.validated_subtask_path) / "unrelated_plan.txt"
            unrelated_plan.write_text("drop robot banana (1)", encoding="utf-8")

            manager.current_task_manifest = {
                "artifacts": {},
                "planner": {
                    "plan_output_files": [str(recorded_plan)]
                },
            }
            captured = {}

            def fake_query_model(messages, model, max_tokens=None, frequency_penalty=0.0):
                captured["prompt"] = messages[-1]["content"]
                return {}, "combined plan"

            with patch.object(manager.llm, "query_model", side_effect=fake_query_model):
                result = manager._combine_all_plans("initial decomposed plan")

            self.assertEqual(result, "combined plan")
            self.assertIn("move robot apple (1)", captured["prompt"])
            self.assertNotIn("drop robot banana (1)", captured["prompt"])

    def test_build_robot_team_keeps_source_metadata_out_of_robot_dict(self):
        team = build_robot_team([15, 6])

        self.assertEqual(team[0]["name"], "robot1")
        self.assertNotIn("source_robot_id", team[0])
        self.assertNotIn("source_robot_name", team[0])
        self.assertEqual(team[1]["name"], "robot2")
        self.assertNotIn("source_robot_id", team[1])
        self.assertNotIn("source_robot_name", team[1])

    def test_build_robot_domain_name_map_preserves_source_robot_identity(self):
        self.assertEqual(
            build_robot_domain_name_map([15, 6]),
            {"robot1": "robot15", "robot2": "robot6"},
        )

    def test_problemextracting_reads_real_domain_and_rewrites_to_local_robot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources_dir = root / "resources"
            prompt_dir = root / "data" / "pythonic_plans"
            resources_dir.mkdir(parents=True)
            prompt_dir.mkdir(parents=True)
            (resources_dir / "robot15.pddl").write_text(
                "(define (domain robot15)\n"
                "  (:action UseRobot15\n"
                "    :parameters (?robot - robot)\n"
                "    :precondition (and (ready robot15) (near robot150))\n"
                "  )\n"
                ")",
                encoding="utf-8",
            )
            (prompt_dir / "pddl_train_task_allocationsep_problem.py").write_text("# example\n", encoding="utf-8")
            config = RunConfig(root)
            manager = TaskManager(str(root), "test-model", config=config)
            manager.current_robot_domain_names = {"robot1": "robot15"}

            captured = {}

            class FakeLLM:
                def query_model(self, messages, model, max_tokens=None, frequency_penalty=0):
                    captured["prompt"] = messages[-1]["content"]
                    return {}, "(define (problem generated))"

            subtask = (
                "#SubTask 1: Test\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: Apple\n"
            )

            result = manager.problemextracting(
                subtasks=[subtask],
                llm=FakeLLM(),
                model="test-model",
                file_processor=manager.file_processor,
                objects_ai="\n\nobjects = []",
                prompt_allocation_set="pddl_train_task_allocationsep",
            )

            self.assertEqual(result, ["(define (problem generated))"])
            self.assertIn("(define (domain robot1)", captured["prompt"])
            self.assertIn("(ready robot1)", captured["prompt"])
            self.assertIn("(near robot150)", captured["prompt"])
            self.assertNotIn("(domain robot15)", captured["prompt"])

    def test_domain_robot_replacement_does_not_replace_partial_tokens(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            replaced = manager._replace_domain_robot_name(
                "(domain robot10) robot10 robot100 robot10_extra",
                "robot10",
                "robot1",
            )

            self.assertEqual(replaced, "(domain robot1) robot1 robot100 robot10_extra")


if __name__ == "__main__":
    unittest.main()
