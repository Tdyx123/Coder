import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pddlrun_llmseparate as runner
import llm_handler
from llm_logger import get_llm_logger
from parsing_utils import ParsingUtils


ACTION = (
    "OpenObject: Open the cabinet.\n"
    "Parameters: ?robot, ?cabinet\n"
    "Preconditions: None.\n"
    "Effects: (object-open ?cabinet)"
)
VALID = "#SubTask 1: Open the cabinet\n" + ACTION
SECOND = "#SubTask 2: Open the drawer\n" + ACTION.replace("cabinet", "drawer")
TWO = "- SubTask 1: Open the cabinet\n- SubTask 2: Open the drawer\n\n" + VALID + "\n\n" + SECOND
DOMAIN = "(define (domain test) (:action OpenObject) (:action GoToObject))"


def response(text, finish_reason="stop", tokens=100):
    return ({"choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
             "usage": {"prompt_tokens": 200, "completion_tokens": tokens, "total_tokens": 200 + tokens}}, text)


class DecompositionRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        prompts = self.root / "prompts/v1"
        prompts.mkdir(parents=True)
        (prompts / "pddl_train_task_decomposesep.txt").write_text("# original examples\n")
        self.manager = runner.TaskManager(str(self.root), "test-model", config=runner.RunConfig(self.root))
        self.manager.current_task_run_dir = str(self.root / "run")
        self.manager.current_task_manifest = {"artifacts": {}, "task": "Open the cabinet"}
        self.calls = []

    def generate(self, replies, write_artifacts=True):
        replies = iter(replies)

        def query(messages, model, **kwargs):
            self.calls.append((copy.deepcopy(messages), kwargs))
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(self.manager.llm, "query_model", side_effect=query):
            return self.manager._generate_decomposed_plan(
                "Open the cabinet", DOMAIN, [], "objects = ['Cabinet', 'Drawer']",
                write_artifacts=write_artifacts,
            )

    def manifest(self):
        return json.loads((self.root / "run/run_manifest.json").read_text())["decompose_validation"]

    def test_short_complete_subtask_survives_parser_and_needs_no_retry(self):
        self.assertEqual([VALID], ParsingUtils.extract_subtasks(VALID))
        self.assertEqual(VALID, self.generate([response(VALID)]))
        self.assertEqual(1, len(self.calls))

    def test_multiline_none_is_a_field_value_not_an_action(self):
        text = VALID.replace("Preconditions: None.", "Preconditions:\nNone")
        self.assertEqual(text, self.generate([response(text)]))
        self.assertEqual(1, len(self.calls))

    def test_empty_output_retries_once_without_empty_assistant_message(self):
        self.assertEqual(VALID, self.generate([response(""), response(VALID)]))
        self.assertEqual(2, len(self.calls))
        messages, kwargs = self.calls[1]
        self.assertEqual(["user", "user"], [m["role"] for m in messages])
        self.assertEqual(self.calls[0][0][0], messages[0])
        self.assertIn("EMPTY_OUTPUT", messages[-1]["content"])
        self.assertEqual(1300, kwargs["max_completion_tokens"])
        self.assertEqual("passed", self.manifest()["status"])

    def test_truncation_combines_all_errors_and_doubles_budget(self):
        broken = TWO[:TWO.index("#SubTask 2:")].replace("Effects: (object-open ?cabinet)", "Effects: (object-open ?cab")
        self.generate([response(broken, "length", 1300), response(TWO)])
        messages, kwargs = self.calls[1]
        self.assertEqual(["user", "assistant", "user"], [m["role"] for m in messages])
        self.assertEqual(broken, messages[1]["content"])
        feedback = messages[-1]["content"]
        for code in ("TRUNCATED_OUTPUT", "INCOMPLETE_EXPRESSION", "MISSING_SUBTASK_BODY"):
            self.assertIn(code, feedback)
        self.assertIn("SubTask 2", feedback)
        self.assertEqual(2600, kwargs["max_completion_tokens"])
        self.assertEqual(2, len(self.manifest()["attempts"]))

    def test_token_cap_alone_does_not_retry_complete_output(self):
        self.generate([response(VALID, None, 1300)])
        self.assertEqual(1, len(self.calls))

    def test_missing_finish_reason_with_capped_incomplete_tail_doubles_budget(self):
        self.generate([response(VALID[:-1], None, 1300), response(VALID)])
        self.assertEqual(2600, self.calls[1][1]["max_completion_tokens"])

    def test_length_reason_alone_triggers_one_retry(self):
        self.generate([response(VALID, "length", 1300), response(VALID)])
        self.assertEqual(2600, self.calls[1][1]["max_completion_tokens"])

    def test_interior_missing_field_identifies_action_occurrence(self):
        broken = VALID + "\n\n" + ACTION.replace("Effects: (object-open ?cabinet)", "Effects:") + "\n\n" + ACTION
        self.generate([response(broken), response(VALID)])
        feedback = self.calls[1][0][-1]["content"]
        self.assertIn("ACTION_FIELDS", feedback)
        self.assertIn("action occurrence 2", feedback)
        self.assertIn("Effects", feedback)
        self.assertEqual(1300, self.calls[1][1]["max_completion_tokens"])

    def test_multiline_markdown_fields_and_none_preconditions_are_valid(self):
        valid = VALID.replace("Parameters:", "- **Parameters:**").replace(
            "Preconditions: None.", "**Preconditions:**\nNone."
        ).replace("Effects: (object-open ?cabinet)", "effects:\n(and\n  (object-open ?cabinet)\n)")
        self.generate([response(valid)])
        self.assertEqual(1, len(self.calls))

    def test_summary_only_and_empty_body_both_get_action_feedback(self):
        for broken in ("- SubTask 1: Open the cabinet", "#SubTask 1: Open the cabinet"):
            with self.subTest(broken=broken):
                self.calls.clear()
                self.generate([response(broken), response(VALID)])
                self.assertEqual(2, len(self.calls))
                self.assertRegex(self.calls[1][0][-1]["content"], "NO_ACTIONS|MISSING_SUBTASK_BODY")

    def test_duplicate_body_id_retries_with_goal_titles(self):
        broken = VALID + "\n\n" + SECOND.replace("SubTask 2", "SubTask 1")
        self.generate([response(broken), response(TWO)])
        feedback = self.calls[1][0][-1]["content"]
        self.assertIn("DUPLICATE_BODY_ID", feedback)
        self.assertIn("Open the drawer", feedback)

    def test_retry_cannot_hide_missing_subtask_by_deleting_overview(self):
        broken = "- SubTask 1: Open the cabinet\n- SubTask 2: Open the drawer\n" + VALID
        with self.assertRaises(runner.PDDLError):
            self.generate([response(broken), response(VALID)])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("retry_exhausted", self.manifest()["status"])

    def test_failed_second_response_is_preserved_and_stops(self):
        with self.assertRaises(runner.PDDLError):
            self.generate([response(""), response("Still no decomposition")])
        manifest = self.manifest()
        self.assertEqual("retry_exhausted", manifest["status"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(2, len(manifest["attempts"]))
        for attempt, expected in zip(manifest["attempts"], ["", "Still no decomposition"]):
            self.assertEqual(expected, (self.root / "run" / attempt["output"]).read_text())

    def test_local_header_normalization_does_not_call_model_again(self):
        variant = TWO.replace("#SubTask 1:", "## SubTask 1:").replace("#SubTask 2:", "SubTask 2:")
        result = self.generate([response(variant)])
        self.assertEqual(2, len(ParsingUtils.extract_subtasks(result)))
        self.assertIn("#SubTask 2:", result)
        self.assertEqual(1, len(self.calls))

    def test_unrecoverable_parser_loss_fails_without_model_retry(self):
        with patch.object(ParsingUtils, "extract_subtasks", return_value=[]):
            with self.assertRaises(runner.PDDLError):
                self.generate([response(VALID)])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("parse_failed", self.manifest()["status"])

    def test_skip_artifact_writes_still_validates_and_retries(self):
        self.assertEqual(VALID, self.generate([response(""), response(VALID)], write_artifacts=False))
        self.assertEqual(2, len(self.calls))
        self.assertFalse((self.root / "run").exists())

    def test_retry_api_error_keeps_first_attempt_and_stops(self):
        with self.assertRaises(runner.PDDLError):
            self.generate([response(""), runner.LLMError("request failed")])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("api_failed", self.manifest()["status"])
        first = self.manifest()["attempts"][0]
        self.assertTrue((self.root / "run" / first["output"]).exists())

    def test_process_tasks_never_allocates_after_failed_decomposition(self):
        domain = self.root / "resources/allactionrobot.pddl"
        domain.parent.mkdir(exist_ok=True)
        domain.write_text(DOMAIN)
        with patch.object(self.manager.llm, "query_model", side_effect=[response(""), response("")]) as query, \
                patch.object(self.manager, "_run_feedback_attempt") as allocate, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(runner.PDDLError):
                self.manager.process_tasks(["Open the cabinet"], [[]], "objects = ['Cabinet']")
        self.assertEqual(2, query.call_count)
        allocate.assert_not_called()

    def test_object_response_finish_reason_is_supported(self):
        metadata = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")],
                                   usage=SimpleNamespace(prompt_tokens=200, completion_tokens=1300, total_tokens=1500))
        self.generate([(metadata, VALID), response(VALID)])
        self.assertEqual(2600, self.calls[1][1]["max_completion_tokens"])

    def test_numbered_action_without_colon_cannot_hide_empty_effects(self):
        broken = VALID.replace("Effects: (object-open ?cabinet)", "Effects:")
        broken += "\n2. CloseObject\nParameters: ?robot, ?cabinet\nPreconditions: None.\nEffects: (not (object-open ?cabinet))"
        self.generate([response(broken), response(VALID)])
        self.assertEqual(2, len(self.calls))
        self.assertIn("ACTION_FIELDS", self.calls[1][0][-1]["content"])
        self.assertIn("action occurrence 1", self.calls[1][0][-1]["content"])

    def test_repeated_fields_without_action_heading_are_rejected(self):
        broken = VALID + "\nParameters: ?robot, ?drawer\nPreconditions: None.\nEffects: (object-open ?drawer)"
        self.generate([response(broken), response(VALID)])
        self.assertEqual(2, len(self.calls))
        self.assertIn("ACTION_FIELDS", self.calls[1][0][-1]["content"])

    def test_reordered_body_ids_are_locally_aligned_for_allocation(self):
        reordered = "- SubTask 1: Open the cabinet\n- SubTask 2: Open the drawer\n" + SECOND + "\n\n" + VALID
        result = self.generate([response(reordered)])
        parsed = ParsingUtils.extract_subtasks(result)
        self.assertIn("#SubTask 1:", parsed[0])
        self.assertIn("#SubTask 2:", parsed[1])
        self.assertEqual(1, len(self.calls))

    def test_sparse_body_ids_stop_before_position_based_allocation(self):
        with self.assertRaises(runner.PDDLError):
            self.generate([response(VALID.replace("SubTask 1", "SubTask 3"))])
        self.assertEqual("parse_failed", self.manifest()["status"])
        self.assertEqual(1, len(self.calls))

    def test_body_missing_from_overview_gets_coverage_feedback(self):
        broken = "- SubTask 1: Open the cabinet\n" + VALID + "\n\n" + SECOND
        self.generate([response(broken), response(TWO)])
        self.assertEqual(2, len(self.calls))
        self.assertIn("overview", self.calls[1][0][-1]["content"])
        self.assertIn("SubTask 2", self.calls[1][0][-1]["content"])

    def test_retry_cannot_drop_goal_from_duplicate_body_ids(self):
        broken = VALID + "\n\n" + SECOND.replace("SubTask 2", "SubTask 1")
        with self.assertRaises(runner.PDDLError):
            self.generate([response(broken), response(VALID)])
        self.assertEqual("retry_exhausted", self.manifest()["status"])
        self.assertEqual(2, len(self.calls))

    def test_first_api_failure_is_audited_without_content_retry(self):
        with self.assertRaises(runner.PDDLError):
            self.generate([runner.LLMError("request failed")])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("api_failed", self.manifest()["status"])

    def test_provider_finish_reason_is_written_to_llm_log(self):
        logger = get_llm_logger()
        log_file = self.root / "calls.jsonl"
        logger.set_context(task_log_file=str(log_file))
        self.addCleanup(logger.clear_context)
        with patch.object(self.manager.llm, "_get_provider_for_model", return_value=({}, "test-provider")), \
                patch.object(llm_handler, "complete_with_provider", return_value=response(VALID, "length", 1300)[0]):
            self.manager.llm.query_model("original request", "test-model", max_completion_tokens=1300)
        entry = json.loads(log_file.read_text())
        self.assertEqual("length", entry["finish_reason"])
        self.assertEqual(1300, entry["usage"]["completion_tokens"])


if __name__ == "__main__":
    unittest.main()
