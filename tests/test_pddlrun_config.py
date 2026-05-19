import json
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
from merge_conversation import (
    check_allocate_assignment_count,
    check_decompose_subtask_count,
    check_planner_plan_count,
)
from parsing_utils import ParsingUtils


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

            validated_problem_dir = Path(manager._get_validated_problem_file_path())
            validated_problem_dir.mkdir(parents=True, exist_ok=True)
            problem_file = validated_problem_dir / "task.pddl"
            problem_file.write_text("(define (problem task) (:domain robot1 ))", encoding="utf-8")
            output_file = Path(manager._get_plan_file_path()) / "task_plan.txt"
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
            planner_manifest = (
                Path(manager.current_task_run_dir)
                / manager.current_task_manifest["artifacts"]["planner"]["manifest"]
            )
            planner_records = json.loads(planner_manifest.read_text(encoding="utf-8"))
            self.assertEqual(planner_records[0]["compatibility_output"], str(output_file))
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

            plan_output_dir = Path(manager._get_plan_file_path())
            plan_output_dir.mkdir(parents=True, exist_ok=True)
            recorded_plan = plan_output_dir / "recorded_validated_plan.txt"
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
                result = manager._combine_all_plans("initial decomposed plan", ["Subtask 1: Robot 1;"])

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

    def test_extract_subtasks_accepts_markdown_bold_headers_without_summary_bullets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            decomposed_plan = (
                "Based on the provided domain and the task description, here is the decomposition.\n\n"
                "**GENERAL TASK DECOMPOSITION**\n"
                "Independent subtasks:\n"
                "- SubTask 1: Put the pot on the countertop.\n"
                "- SubTask 2: Put the houseplant on the countertop.\n"
                "- SubTask 3: Put the saltshaker on the sinkbasin.\n"
                "We can parallelize SubTask 1 and SubTask 2.\n\n"
                "**action description from domain for tasks required**\n\n"
                "**SubTask 1: Put the pot on the countertop**\n\n"
                "**Initial condition analyze due to previous subtask:**\n"
                "1. Robot not at pot location.\n"
                "2. Robot not holding pot.\n\n"
                "GoToObject: Robot goes to the pot.\n"
                "Parameters: ?robot, ?pot\n"
                "Preconditions: (not (inaction ?robot))\n"
                "Effects: (at ?robot ?pot), (not (inaction ?robot))\n\n"
                "PutObject: Robot puts the pot on the countertop.\n"
                "Parameters: ?robot, ?pot, ?countertop\n"
                "Preconditions: (holding ?robot ?pot)\n"
                "Effects: (at-location ?pot ?countertop)\n\n"
                "**SubTask 2: Put the houseplant on the countertop**\n\n"
                "**Initial condition analyze due to previous subtask:**\n"
                "1. Robot not at houseplant location.\n"
                "2. Robot not holding houseplant.\n\n"
                "GoToObject: Robot goes to the houseplant.\n"
                "Parameters: ?robot, ?houseplant\n"
                "Preconditions: (not (inaction ?robot))\n"
                "Effects: (at ?robot ?houseplant), (not (inaction ?robot))\n\n"
                "PutObject: Robot puts the houseplant on the countertop.\n"
                "Parameters: ?robot, ?houseplant, ?countertop\n"
                "Preconditions: (holding ?robot ?houseplant)\n"
                "Effects: (at-location ?houseplant ?countertop)\n\n"
                "**SubTask 3: Put the saltshaker on the sinkbasin**\n\n"
                "**Initial condition analyze due to previous subtask:**\n"
                "1. Robot not at saltshaker location.\n"
                "2. Robot not holding saltshaker.\n\n"
                "GoToObject: Robot goes to the saltshaker.\n"
                "Parameters: ?robot, ?saltshaker\n"
                "Preconditions: (not (inaction ?robot))\n"
                "Effects: (at ?robot ?saltshaker), (not (inaction ?robot))\n\n"
                "PutObject: Robot puts the saltshaker on the sinkbasin.\n"
                "Parameters: ?robot, ?saltshaker, ?sinkbasin\n"
                "Preconditions: (holding ?robot ?saltshaker)\n"
                "Effects: (at-location ?saltshaker ?sinkbasin)\n\n"
                "**Task put the pot on the countertop, put the houseplant on the countertop, "
                "and put the saltshaker on the sinkbasin is done.**"
            )

            subtasks = manager._extract_subtasks(decomposed_plan)

            self.assertEqual(len(subtasks), 3)
            self.assertTrue(subtasks[0].startswith("#SubTask 1: Put the pot on the countertop"))
            self.assertIn("Robot goes to the pot", subtasks[0])
            self.assertNotIn("houseplant", subtasks[0])
            self.assertTrue(subtasks[1].startswith("#SubTask 2: Put the houseplant on the countertop"))
            self.assertIn("Robot goes to the houseplant", subtasks[1])
            self.assertNotIn("saltshaker", subtasks[1])
            self.assertTrue(subtasks[2].startswith("#SubTask 3: Put the saltshaker on the sinkbasin"))
            self.assertIn("Robot goes to the saltshaker", subtasks[2])
            self.assertNotIn("is done", subtasks[2])

    def test_extract_subtasks_keeps_existing_hash_header_formats(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            body = "\n".join(f"Action detail line {idx}" for idx in range(1, 10))
            decomposed_plan = (
                "#Subtask 1 Put an Egg in the Fridge\n"
                f"{body}\n"
                "# SubTask 2: Slice the Tomato.\n"
                f"{body}\n"
            )

            subtasks = manager._extract_subtasks(decomposed_plan)

            self.assertEqual(len(subtasks), 2)
            self.assertTrue(subtasks[0].startswith("#Subtask 1 Put an Egg in the Fridge"))
            self.assertTrue(subtasks[1].startswith("# SubTask 2: Slice the Tomato."))

    def test_extract_subtasks_does_not_use_bullets_or_sentences_as_headers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            decomposed_plan = (
                "Independent subtasks:\n"
                "- SubTask 1: Put the pot on the countertop.\n"
                "- SubTask 2: Put the houseplant on the countertop.\n"
                "We can parallelize SubTask 1 and SubTask 2 because they do not depend on each other.\n"
            )

            self.assertEqual(manager._extract_subtasks(decomposed_plan), [decomposed_plan.strip()])

    def test_parsing_utils_matches_supported_header_variants(self):
        self.assertTrue(ParsingUtils.is_subtask_header_line("**SubTask 1: Put the pot on the table**"))
        self.assertTrue(ParsingUtils.is_subtask_header_line("#Subtask 1 Put an Egg in the Fridge"))
        self.assertTrue(
            ParsingUtils.is_subtask_header_line(
                "SubTask 1: Put the pot on the table",
                allow_bare=True,
            )
        )
        self.assertFalse(ParsingUtils.is_subtask_header_line("- SubTask 1: Put the pot on the table"))
        self.assertEqual(
            ParsingUtils.normalize_subtask_header_line("**SubTask 2: Open the Drawer**"),
            "#SubTask 2: Open the Drawer",
        )

    def test_split_and_store_tasks_accepts_markdown_bold_headers_without_bullet_headers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            processor = FileProcessor(tmp_dir)
            content = (
                "**SubTask 1: Put the pot on the countertop**\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: pot, countertop\n\n"
                "**SubTask 2: Put the houseplant on the countertop**\n"
                "**Assigned Robot**: robot1\n"
                "**Objects Involved**: houseplant, countertop\n"
            )
            bullet_only_content = (
                "Independent subtasks:\n"
                "- SubTask 1: Put the pot on the countertop.\n"
                "- SubTask 2: Put the houseplant on the countertop.\n"
            )

            subtasks, _ = processor.split_and_store_tasks(content)
            bullet_subtasks, _ = processor.split_and_store_tasks(bullet_only_content)

            self.assertEqual(len(subtasks), 2)
            self.assertIn("pot", subtasks[0])
            self.assertIn("houseplant", subtasks[1])
            self.assertEqual(bullet_subtasks, [bullet_only_content.strip()])

    def test_extract_sequence_operations_accepts_robot_ids_without_space_without_filling_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            allocated_plan = (
                "# Sequence of Operations:\n"
                "Subtask 1: Robot2;Subtask 2: Robot2;\n"
                "Subtask 3: Robot2;\n"
            )

            sequence_operations = manager._extract_sequence_operations(allocated_plan)
            assignments = manager._extract_robot_assignments(sequence_operations)

            self.assertEqual(assignments, {1: 2, 2: 2, 3: 2})
            self.assertNotIn("Subtask 4: Robot 1;", sequence_operations)

    def test_extract_sequence_operations_accepts_markdown_and_header_without_colon(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")

            markdown_sequence = manager._extract_sequence_operations(
                "**Sequence of Operations:**\nSubTask1:robot2;",
            )
            no_colon_sequence = manager._extract_sequence_operations(
                "# Sequence of Operations\nSubtask 1: robot4;",
            )

            self.assertEqual(manager._extract_robot_assignments(markdown_sequence), {1: 2})
            self.assertEqual(manager._extract_robot_assignments(no_colon_sequence), {1: 4})

    def test_extract_sequence_operations_selects_best_sequence_block(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            most_complete_plan = (
                "Sequence of Operations:\n"
                "Subtask 1: Robot 2;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 3;Subtask 2: Robot 4;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 5;\n"
            )
            tied_plan = (
                "Sequence of Operations:\n"
                "Subtask 1: Robot 2;Subtask 2: Robot 3;\n"
                "Sequence of Operations:\n"
                "Subtask 1: Robot 4;Subtask 2: Robot 5;\n"
            )

            most_complete_sequence = manager._extract_sequence_operations(most_complete_plan)
            tied_sequence = manager._extract_sequence_operations(tied_plan)

            self.assertEqual(
                manager._extract_robot_assignments(most_complete_sequence),
                {1: 3, 2: 4},
            )
            self.assertEqual(
                manager._extract_robot_assignments(tied_sequence),
                {1: 4, 2: 5},
            )

    def test_extract_sequence_operations_does_not_guess_non_numeric_assignments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            allocated_plan = (
                "# Sequence of Operations:\n"
                "Subtask A: Robot2;Subtask;Robot;\n"
                "Subtask 2: Robot ?;\n"
                "Subtask 3: Robot A;\n"
            )

            sequence_operations = manager._extract_sequence_operations(allocated_plan)
            assignments = manager._extract_robot_assignments(sequence_operations)

            self.assertEqual(assignments, {})

    def test_extract_sequence_operations_accepts_real_run_variants(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TaskManager(tmp_dir, "test-model")
            robot_lowercase_plan = (
                "# Sequence of Operations:\n"
                "Subtask 1: robot4;Subtask 2: robot1;\n"
                "Subtask 3: robot1;\n"
            )
            compact_and_described_plan = (
                "# Sequence of Operations:\n"
                "SubTask1:robot3;\n"
                "SubTask2\n"
                "- Robot2\n"
                "SubTask 3 (Open cabinet): Robot 4;\n"
            )

            robot_lowercase_sequence = manager._extract_sequence_operations(robot_lowercase_plan)
            compact_sequence = manager._extract_sequence_operations(compact_and_described_plan)

            self.assertEqual(
                manager._extract_robot_assignments(robot_lowercase_sequence),
                {1: 4, 2: 1, 3: 1},
            )
            self.assertEqual(
                manager._extract_robot_assignments(compact_sequence),
                {1: 3, 2: 2, 3: 4},
            )

    def test_check_decompose_subtask_count_checks_only_flat_index(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "subtasks": [{"skill": "Open"}, {"skill": "Break"}]}) + "\n"
                + json.dumps({"task": "second", "subtasks": [{"skill": "Open"}]}) + "\n",
                encoding="utf-8",
            )

            first_run = root / "runs" / "first"
            second_run = root / "runs" / "second_missing_output"
            (first_run / "01_decompose").mkdir(parents=True)
            second_run.mkdir(parents=True)
            body = "\n".join(f"Action detail line {idx}" for idx in range(1, 10))
            (first_run / "01_decompose" / "02_decompose_output.txt").write_text(
                "#Subtask 1: Open the Drawer\n" + body + "\n",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_run),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = check_decompose_subtask_count(str(summary_path), index=0)

            self.assertFalse(result["matched"])
            self.assertEqual(result["decompose_count"], 1)
            self.assertEqual(result["jsonl_count"], 2)
            self.assertEqual(result["flat_index"], 0)

            with self.assertRaises(IndexError):
                check_decompose_subtask_count(str(summary_path), index=2)

    def test_check_allocate_assignment_count_compares_against_assigned_robots(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "assigned_robots": [2, 2]}) + "\n"
                + json.dumps({"task": "second", "assigned_robots": [4, 4]}) + "\n"
                + json.dumps({"task": "third", "assigned_robots": [1]}) + "\n",
                encoding="utf-8",
            )

            first_run = root / "runs" / "first"
            second_run = root / "runs" / "second"
            invalid_run = root / "runs" / "invalid"
            for run_dir in (first_run, second_run, invalid_run):
                (run_dir / "02_allocate").mkdir(parents=True)

            (first_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 2;Subtask 2: Robot 2;\n",
                encoding="utf-8",
            )
            (second_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 4;\n",
                encoding="utf-8",
            )
            (invalid_run / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot;\n",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_run),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 2,
                                        "task": "third",
                                        "task_run_dir": str(invalid_run),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            matched = check_allocate_assignment_count(str(summary_path), index=0)
            mismatched = check_allocate_assignment_count(str(summary_path), index=1)
            invalid = check_allocate_assignment_count(str(summary_path), index=2)

            self.assertTrue(matched["matched"])
            self.assertEqual(matched["allocate_count"], 2)
            self.assertEqual(matched["jsonl_count"], 2)
            self.assertEqual(matched["assignments"], {1: 2, 2: 2})
            self.assertEqual(matched["flat_index"], 0)

            self.assertFalse(mismatched["matched"])
            self.assertEqual(mismatched["allocate_count"], 1)
            self.assertEqual(mismatched["jsonl_count"], 2)

            self.assertFalse(invalid["matched"])
            self.assertEqual(invalid["allocate_count"], 0)
            self.assertEqual(invalid["jsonl_count"], 1)
            self.assertEqual(invalid["assignments"], {})
            self.assertEqual(invalid["sequence_operations"], [])

    def test_check_planner_plan_count_compares_against_subtasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dataset_dir = root / "data" / "sample_set"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps({"task": "first", "subtasks": [{"skill": "Open"}, {"skill": "Break"}]}) + "\n"
                + json.dumps({"task": "second", "subtasks": [{"skill": "Put"}, {"skill": "Close"}]}) + "\n",
                encoding="utf-8",
            )

            first_outputs = root / "runs" / "first" / "08_planner" / "outputs"
            second_outputs = root / "runs" / "second" / "08_planner" / "outputs"
            first_outputs.mkdir(parents=True)
            second_outputs.mkdir(parents=True)

            first_plan_1 = first_outputs / "subtask_01_problem_validated_plan.txt"
            first_plan_2 = first_outputs / "subtask_02_problem_validated_plan.txt"
            first_plan_1.write_text("plan 1", encoding="utf-8")
            first_plan_2.write_text("plan 2", encoding="utf-8")
            (first_outputs / "subtask_01_problem_validated_stdout.txt").write_text(
                "ignored",
                encoding="utf-8",
            )
            (second_outputs / "subtask_01_problem_validated_plan.txt").write_text(
                "plan 1",
                encoding="utf-8",
            )

            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "repo_root": str(root),
                        "test_set": "sample_set",
                        "summaries": [
                            {
                                "floor_plan": "6",
                                "results": [
                                    {
                                        "floor_plan": "6",
                                        "task_index": 0,
                                        "task": "first",
                                        "task_run_dir": str(first_outputs.parents[1]),
                                    },
                                    {
                                        "floor_plan": "6",
                                        "task_index": 1,
                                        "task": "second",
                                        "task_run_dir": str(second_outputs.parents[1]),
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            matched = check_planner_plan_count(str(summary_path), index=0)
            mismatched = check_planner_plan_count(str(summary_path), index=1)

            self.assertTrue(matched["matched"])
            self.assertEqual(matched["planner_count"], 2)
            self.assertEqual(matched["jsonl_count"], 2)
            self.assertEqual(matched["flat_index"], 0)
            self.assertEqual(matched["plan_paths"], [str(first_plan_1), str(first_plan_2)])

            self.assertFalse(mismatched["matched"])
            self.assertEqual(mismatched["planner_count"], 1)
            self.assertEqual(mismatched["jsonl_count"], 2)
            self.assertEqual(mismatched["flat_index"], 1)

    def test_problemextracting_reads_real_domain_and_rewrites_to_local_robot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            resources_dir = root / "resources"
            prompt_dir = root / "prompts" / "v1"
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
            (prompt_dir / "pddl_train_task_allocationsep_problem.txt").write_text("# example\n", encoding="utf-8")
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
                robot_assignments={1: 1},
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
