import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pddlrun_llmseparate as runner


class AllocationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prompts = self.root / "prompts" / "v1"
        prompts.mkdir(parents=True)
        (prompts / "pddl_train_task_allocationsep_solution.txt").write_text(
            "# allocation examples\n", encoding="utf-8"
        )
        self.manager = runner.TaskManager(
            str(self.root), "global-model", config=runner.RunConfig(self.root),
            allocate_model="allocation-model",
        )
        self.manager.current_task_run_dir = str(self.root / "run")
        self.manager.current_task_manifest = {"artifacts": {}}
        self.robots = [
            {"name": "robot1", "skills": ["OpenObject", "GoToObject"], "mass_capacity": 100},
            {"name": "robot2", "skills": ["RunMicrowave", "PutObject"], "mass_capacity": 0.01},
        ]
        self.subtasks = [
            "#SubTask 1: Microwave the apple in the microwave\n"
            "Skills Required: GoToObject, PickupObject, OpenObject, RunMicrowave",
            "#SubTask 2: Put the apple in the fridge\n"
            "Skills Required: GoToObject, OpenObject, PutObject, CloseObject",
        ]
        self.valid = "# Sequence of Operations:\nSubtask 1: Robot 2;\nSubtask 2: Robot 1;"

    def run_attempt(self, responses, subtasks=None, robots=None, attempt_index=1):
        subtasks = self.subtasks if subtasks is None else subtasks
        robots = self.robots if robots is None else robots
        replies = [reply if isinstance(reply, Exception) else ({}, reply) for reply in responses]
        with patch.object(self.manager.llm, "query_model", side_effect=replies) as query, \
                patch.object(self.manager, "_generate_problem_files", return_value=[]) as problems, \
                patch.object(self.manager, "_plan_generated_problems", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            result = self.manager._run_feedback_attempt(
                "\n\n".join(subtasks), subtasks, robots, "objects = []", [], {},
                attempt_index=attempt_index,
            )
        self.assertEqual(problems.call_args.args[1], result["robot_assignments"])
        output_path = self.root / "run/02_allocate/02_allocate_output.txt"
        self.assertEqual(output_path.read_text(encoding="utf-8"), result["allocated_plan"])
        self.assertEqual(
            self.manager._extract_robot_assignments(
                self.manager._extract_sequence_operations(result["allocated_plan"])
            ), result["robot_assignments"],
        )
        return result, query

    def test_valid_first_response_preserves_parallel_plan_without_retry(self):
        parallel = "# Sequence of Operations:\nSubtask 1: Robot 2;Subtask 2: Robot 1;"
        result, query = self.run_attempt([parallel])
        self.assertEqual(result["allocated_plan"], parallel)
        self.assertEqual(result["robot_assignments"], {1: 2, 2: 1})
        self.assertEqual(query.call_count, 1)

    def test_invalid_allocations_retry_once_with_context_and_validation_errors(self):
        cases = [
            ("No allocation is possible.", "No assignments"),
            ("Subtask 1: Robot 2;", "Missing subtask IDs: [2]"),
            (self.valid + "\nSubtask 3: Robot 1;", "Unexpected subtask IDs: [3]"),
            ("Subtask 1: Robot 9;Subtask 2: Robot 1;", "Invalid robot IDs"),
            ("Subtask 1: Robot 0;Subtask 2: Robot 1;", "Invalid robot IDs"),
            ("Subtask 1: Robot 1.5;Subtask 2: Robot 1;", "Invalid numeric IDs"),
            ("Subtask 1: Robot -1;Subtask 2: Robot 1;", "Invalid numeric IDs"),
        ]
        for invalid, error in cases:
            with self.subTest(invalid=invalid):
                result, query = self.run_attempt([invalid, self.valid])
                self.assertEqual(result["robot_assignments"], {1: 2, 2: 1})
                self.assertEqual(query.call_count, 2)
                original = query.call_args_list[0]
                retry = query.call_args_list[1]
                self.assertEqual(retry.args[1], "allocation-model")
                messages = retry.args[0]
                self.assertEqual(messages[0], original.args[0][0])
                self.assertEqual(messages[1], {"role": "assistant", "content": invalid})
                self.assertEqual(messages[2]["role"], "user")
                self.assertIn(error, messages[2]["content"])
                self.assertIn("Required subtask IDs: [1, 2]", messages[2]["content"])
                self.assertIn("Allowed task-local robot IDs: [1, 2]", messages[2]["content"])
                self.assertIn("# Sequence of Operations:", messages[2]["content"])

    def test_two_failures_rebuild_all_assignments_using_only_core_skill(self):
        result, query = self.run_attempt(["Subtask 1: Robot 1;", "Subtask 2: Robot 1;"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["robot_assignments"], {1: 2, 2: 2})
        self.assertEqual(result["allocated_plan"],
                         "# Sequence of Operations:\nSubtask 1: Robot 2;\nSubtask 2: Robot 2;\n")

    def test_retry_exception_uses_fallback_and_records_error(self):
        result, query = self.run_attempt(["", runner.LLMError("repair unavailable")])
        self.assertEqual(result["robot_assignments"], {1: 2, 2: 2})
        self.assertEqual(query.call_count, 2)
        self.assertIn("repair unavailable", result["allocation_result"]["recovery"]["retry_error"])

    def test_initial_api_failure_is_not_converted_to_fallback(self):
        with self.assertRaisesRegex(runner.PDDLError, "initial unavailable"):
            self.run_attempt([runner.LLMError("initial unavailable")])

    def test_empty_robot_team_fails_before_model_call(self):
        with patch.object(self.manager.llm, "query_model") as query:
            with self.assertRaisesRegex(runner.PDDLError, "No robots available"):
                self.manager._run_feedback_attempt("task", ["task"], [], "", [], {})
        query.assert_not_called()

    def test_no_matching_core_skill_does_not_try_secondary_skill(self):
        tasks = ["#SubTask 1: Microwave the apple\nSkills Required: OpenObject, RunMicrowave"]
        robots = [{"name": "robot1", "skills": []}, {"name": "robot2", "skills": ["OpenObject"]}]
        result, _ = self.run_attempt(["", ""], subtasks=tasks, robots=robots)
        self.assertEqual(result["robot_assignments"], {1: 1})

    def test_first_matching_robot_uses_task_local_order(self):
        team = runner.build_robot_team([1, 9, 5])
        result, _ = self.run_attempt(["", ""], subtasks=self.subtasks[:1], robots=team)
        self.assertEqual(result["robot_assignments"], {1: 2})

    def test_title_action_aliases_and_object_names(self):
        cases = [
            ("Open the microwave", "OpenObject"),
            ("打开微波炉", "OpenObject"),
            ("打开微波炉电源", "SwitchOn"),
            ("微波加热苹果", "RunMicrowave"),
            ("Heat the apple in the microwave", "RunMicrowave"),
            ("Put the sliced apple in the fridge", "PutObject"),
            ("将切片苹果放入冰箱", "PutObject"),
            ("把洗净的盘子放到桌上", "PutObject"),
            ("Place the washed plate on the table", "PutObject"),
            ("Wash the plate", "CleanObject"),
            ("Prepare the egg in the bowl", "BreakEgg"),
            ("Cook the egg in the pan", "BreakEgg"),
            ("Cook the potato on the stove burner", "CookByStoveBurner"),
            ("Heat the pot on the stove burner", "HeatByStoveBurner"),
            ("Cool the bread in the fridge", "ColdObject"),
            ("Make coffee in the mug using the coffee machine", "RunCoffeeMachine"),
            ("Toast the bread in the toaster", "RunToaster"),
            ("Fill the mug with water", "FillWater"),
            ("Switch off the light", "SwitchOff"),
            ("Switch on the lamp", "SwitchOn"),
            ("Close the fridge", "CloseObject"),
            ("Slice the tomato", "SliceObject"),
            ("Break the cup", "BreakObject"),
            ("Break the egg", "BreakObject"),
            ("Pick up the cup", "PickupObject"),
            ("Go to the fridge", "GoToObject"),
        ]
        for title, skill in cases:
            with self.subTest(title=title):
                result, _ = self.run_attempt(
                    ["", ""], subtasks=[f"#SubTask 1: {title}"],
                    robots=[{"name": "robot1", "skills": []}, {"name": "robot2", "skills": [skill]}],
                )
                self.assertEqual(result["robot_assignments"], {1: 2})

    def test_unknown_title_uses_structured_skills_then_body_action_phrases(self):
        cases = [
            ("Skills Required: GoToObject, PutObject, RunMicrowave", "RunMicrowave"),
            ("OpenObject: Open the fridge.\nPutObject: Put apple in fridge.", "OpenObject"),
            ("Skills Required: SliceObject, CleanObject", "SliceObject"),
            ("CleanObject: Wash the plate.\nSkills Required: SliceObject", "CleanObject"),
            ("Skills Required: PrepareEgg, PickupObject", "BreakEgg"),
            ("Skills Required: CookEgg, PickupObject", "BreakEgg"),
            ("Skills Required: put_in, pickup_object", "PutObject"),
            ("1. Robot must wash the plate.\n2. Robot puts it on the table.", "CleanObject"),
            ("Steps:\nWash the plate.", "CleanObject"),
            ("Actions:\nGo to the plate\nWash the plate", "CleanObject"),
            ("- Go to the plate\n- Wash the plate", "CleanObject"),
            ("Preconditions:\nOpen: true\nActions:\nPutObject: Put apple in fridge", "PutObject"),
            ("# Initial conditions:\n1. Cup is dirty.\nCleanObject: Robot cleans the cup.", "CleanObject"),
            ("GoToObject: Go to the plate\nEffects: (at robot plate)\nWash: Wash the plate", "CleanObject"),
        ]
        for body, skill in cases:
            with self.subTest(body=body):
                result, _ = self.run_attempt(
                    ["", ""], subtasks=["#SubTask 1: Complete task\n" + body],
                    robots=[{"name": "robot1", "skills": []}, {"name": "robot2", "skills": [skill]}],
                )
                self.assertEqual(result["robot_assignments"], {1: 2})

    def test_states_and_preconditions_do_not_create_action_matches(self):
        tasks = [
            "#SubTask 1: Inspect Microwave and CoffeeMachine\n"
            "# Initial condition analyze:\n1. Robot must open the fridge.\n"
            "Preconditions: wash the plate\nEffects: (sliced Apple)\n",
            "#SubTask 2: The cup is open and the apple is sliced",
        ]
        result, _ = self.run_attempt(["", ""], subtasks=tasks, robots=[
            {"name": "robot1", "skills": []},
            {"name": "robot2", "skills": ["OpenObject", "CleanObject", "RunMicrowave", "RunCoffeeMachine", "SliceObject"]},
        ])
        self.assertEqual(result["robot_assignments"], {1: 1, 2: 1})

    def test_missing_and_broken_config_use_first_robot_with_diagnostic(self):
        path = self.root / "fallback.json"
        for content in [None, "{invalid", "{}", '[]']:
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                with patch.object(runner, "ALLOCATION_FALLBACK_RULES_PATH", path):
                    result, _ = self.run_attempt(["", ""])
                self.assertEqual(result["robot_assignments"], {1: 1, 2: 1})
                self.assertTrue(result["allocation_result"]["recovery"]["config_error"])

    def test_feedback_attempts_keep_separate_recovery_artifacts(self):
        self.manager.feedback_enabled = True
        first, first_query = self.run_attempt(["first invalid", "first retry invalid"], attempt_index=1)
        second, second_query = self.run_attempt(["second invalid", self.valid], attempt_index=2)
        self.assertEqual((first_query.call_count, second_query.call_count), (2, 2))
        for index, initial in [(1, "first invalid"), (2, "second invalid")]:
            directory = self.root / f"run/02_allocate/attempt_{index:02d}"
            self.assertEqual((directory / "03_initial_output.txt").read_text(), initial)
            messages = json.loads((directory / "04_repair_messages.json").read_text())
            self.assertEqual(messages[1]["content"], initial)
            self.assertEqual((directory / "04_repair_prompt.txt").read_text(), messages[2]["content"])
            recovery = json.loads((directory / "06_recovery.json").read_text())
            self.assertTrue(recovery["initial_errors"])
            if index == 1:
                self.assertEqual(recovery["source"], "skill_fallback")
                self.assertEqual([row["skill"] for row in recovery["fallback_assignments"]],
                                 ["RunMicrowave", "PutObject"])
                self.assertEqual((directory / "02_allocate_output.txt").read_text(), first["allocated_plan"])
            else:
                self.assertEqual(recovery["source"], "repair_llm")
                self.assertEqual((directory / "05_repair_output.txt").read_text(), self.valid)
                self.assertEqual((directory / "02_allocate_output.txt").read_text(), second["allocated_plan"])
        manifest = json.loads((self.root / "run/run_manifest.json").read_text())
        self.assertIn("attempt_01_recovery", manifest["artifacts"]["allocate"])
        self.assertIn("attempt_02_recovery", manifest["artifacts"]["allocate"])


if __name__ == "__main__":
    unittest.main()
