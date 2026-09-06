import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_noop_audit import audit_problem_initial_state, verify_zero_action_plan


PROBLEM = """(define (problem noop)
  (:domain robot1)
  (:objects drawer lamp - object)
  (:init
    (object-open drawer)
  )
  (:goal (and
    (object-open drawer)
    (not (switch-on lamp))
  ))
)
"""


class PDDLNoopAuditTests(unittest.TestCase):
    def test_comments_cannot_supply_missing_sections(self):
        for section, contents in (("init", "(ready drawer)"), ("goal", "(ready drawer)")):
            other = "goal" if section == "init" else "init"
            problem = (
                f"; (:{section} {contents})\n"
                "(define (problem ready) (:domain robot1) "
                f"(:{other} (ready drawer)))"
            )
            with self.subTest(section=section):
                audit = audit_problem_initial_state(problem, [{"literal": "(ready drawer)"}])
                self.assertIn(f"missing_{section}_section", audit.failure_reasons)
                self.assertEqual(audit.problem, problem)

    def test_nested_sections_cannot_supply_missing_root_sections(self):
        for section in ("init", "goal"):
            other = "goal" if section == "init" else "init"
            problem = (
                "(define (problem ready) (:domain robot1) "
                f"(:metadata (:{section} (ready drawer))) "
                f"(:{other} (ready drawer)))"
            )
            with self.subTest(section=section):
                audit = audit_problem_initial_state(problem, [{"literal": "(ready drawer)"}])
                self.assertIn(f"missing_{section}_section", audit.failure_reasons)

    def test_duplicate_root_sections_are_rejected_without_repair(self):
        for section in ("init", "goal"):
            problem = (
                "(define (problem ready) (:domain robot1) "
                "(:init (ready drawer)) (:goal (ready drawer)) "
                f"(:{section} (ready other)))"
            )
            with self.subTest(section=section):
                audit = audit_problem_initial_state(problem, [])
                self.assertIn(f"duplicate_{section}_section", audit.failure_reasons)
                self.assertEqual(audit.problem, problem)
                self.assertEqual(audit.repairs, [])

    def test_only_a_complete_problem_root_can_be_audited(self):
        valid = "(define (problem ready) (:init (ready drawer)) (:goal (ready drawer)))"
        for problem in (
            valid[:-1], valid + ")", valid + " (extra)",
            "(wrapper " + valid + ")",
            valid.replace("(problem ready)", "(domain ready)"),
        ):
            with self.subTest(problem=problem):
                audit = audit_problem_initial_state(problem, [{"literal": "(ready drawer)"}])
                self.assertTrue(audit.failure_reasons)
                self.assertEqual(audit.problem, problem)
                self.assertEqual(audit.repairs, [])

    def test_comment_mask_preserves_offsets_when_removing_real_init_literal(self):
        problem = (
            "; (:init (ready fake)) (:goal (ready fake)) )\r\n"
            "(define (problem ready)\n"
            "  (:init\n"
            "    ; (ready comment) (((\n"
            "    (READY drawer) ; retain this comment )\n"
            "    (safe item)\n"
            "  )\n"
            "  (:goal ; (and (ready fake))\n"
            "    (ready DRAWER))\n"
            ")\n"
        )
        audit = audit_problem_initial_state(problem, [])

        self.assertEqual(audit.failure_reasons, [])
        self.assertEqual(audit.goal_literals, ["(ready drawer)"])
        self.assertEqual(audit.init_literals, ["(safe item)"])
        self.assertEqual(audit.problem, problem.replace("(READY drawer)", ""))

    def test_nested_conjunction_is_flattened_signed_normalized_and_deduplicated(self):
        problem = (
            "(define (problem ready) (:init (READY drawer)) "
            "(:goal (and (READY drawer) (and (not (SWITCH-ON Lamp)) "
            "(and (ready DRAWER) (NOT (switch-on lamp)))))))"
        )
        evidence = [{"literal": "(ready drawer)"}, {"literal": "(not (switch-on lamp))"}]
        proof = verify_zero_action_plan(
            subtask_id=1, problem=problem, plan_text="; cost = 0\n", evidence=evidence,
            planner_record={"status": "completed", "plan_generated": True, "return_code": 0},
        )

        self.assertTrue(proof["verified"], proof["failure_reasons"])
        self.assertEqual(proof["goal_literals"], ["(ready drawer)", "(not (switch-on lamp))"])
        self.assertEqual(len(proof["evidence"]), 2)

    def test_removing_init_literal_preserves_internal_comments_and_line_endings(self):
        for newline in ("\n", "\r\n", "\r"):
            prefix = "(define (problem ready) (:init "
            suffix = " (safe item)) (:goal (ready drawer)))"
            literal = (
                f"(ready ; preserve observation rationale ({newline}"
                f"\t; preserve another note ){newline}"
                " drawer)"
            )
            expected_remainder = (
                f"       ; preserve observation rationale ({newline}"
                f"\t; preserve another note ){newline}"
                "        "
            )
            with self.subTest(newline=repr(newline)):
                audit = audit_problem_initial_state(prefix + literal + suffix, [])

                self.assertEqual(audit.failure_reasons, [])
                self.assertEqual(audit.init_literals, ["(safe item)"])
                self.assertEqual(audit.problem, prefix + expected_remainder + suffix)
                self.assertEqual(audit.repairs[0]["literal"], "(ready drawer)")
                second_audit = audit_problem_initial_state(audit.problem, [])
                self.assertEqual(second_audit.failure_reasons, [])
                self.assertEqual(second_audit.problem, audit.problem)
                self.assertEqual(second_audit.repairs, [])

    def test_unsupported_and_empty_nested_goals_are_audit_errors(self):
        cases = (
            ("(and)", "empty_goal"),
            ("(and (ready drawer) (and))", "empty_goal"),
            ("(and (ready drawer) (or (ready drawer) (ready other)))", "unsupported_goal:or"),
            ("(forall (?x - object) (ready ?x))", "unsupported_goal:forall"),
            ("(exists (?x - object) (ready ?x))", "unsupported_goal:exists"),
            ("(ready ?x)", "unsupported_goal:ready"),
            ("(?predicate drawer)", "unsupported_goal:?predicate"),
            ("(not (ready ?x))", "unsupported_goal:not"),
        )
        for goal, failure in cases:
            problem = f"(define (problem ready) (:init (ready drawer)) (:goal {goal}))"
            with self.subTest(goal=goal):
                audit = audit_problem_initial_state(problem, [])
                self.assertIn(failure, audit.failure_reasons)
                self.assertEqual(audit.repairs, [])

    def test_untrusted_goal_literal_is_removed_from_initial_state(self):
        result = audit_problem_initial_state(PROBLEM, evidence=[])

        self.assertNotIn("(object-open drawer)", result.problem.split("(:goal", 1)[0])
        self.assertEqual(
            result.repairs,
            [
                {
                    "type": "untrusted_goal_in_initial_state",
                    "literal": "(object-open drawer)",
                    "before": "present_in_init",
                    "after": "removed_from_init",
                }
            ],
        )

    def test_trusted_positive_and_negative_goals_produce_verified_noop(self):
        evidence = [
            {
                "literal": "(object-open drawer)",
                "object_id": "Drawer|1",
                "source_field": "isOpen",
                "observed_value": True,
            },
            {
                "literal": "(not (switch-on lamp))",
                "object_id": "DeskLamp|1",
                "source_field": "isToggled",
                "observed_value": False,
            },
        ]
        audited = audit_problem_initial_state(PROBLEM, evidence=evidence)

        proof = verify_zero_action_plan(
            subtask_id=3,
            problem=audited.problem,
            plan_text="; cost = 0 (unit cost)\n",
            evidence=evidence,
            planner_record={
                "status": "completed",
                "plan_generated": True,
                "has_planner_error": False,
                "return_code": 0,
            },
            val_enabled=False,
            repairs=audited.repairs,
        )

        self.assertTrue(proof["verified"])
        self.assertEqual(proof["subtask_id"], 3)
        self.assertEqual(
            proof["goal_literals"],
            ["(object-open drawer)", "(not (switch-on lamp))"],
        )
        self.assertEqual(proof["val_status"], "not_run")
        self.assertEqual(len(proof["evidence"]), 2)
        self.assertEqual(proof["failure_reasons"], [])

    def test_negative_goal_is_not_proved_by_positive_fact_absence(self):
        problem = """(define (problem closed)
  (:domain robot1)
  (:objects drawer - object)
  (:init)
  (:goal (not (object-open drawer)))
)
"""

        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=[],
            planner_record={"status": "completed", "plan_generated": True, "return_code": 0},
        )

        self.assertFalse(proof["verified"])
        self.assertIn(
            "missing_goal_evidence:(not (object-open drawer))",
            proof["failure_reasons"],
        )

    def test_declared_evidence_polarity_must_match_literal(self):
        problem = """(define (problem open)
  (:domain robot1)
  (:objects drawer - object)
  (:init (object-open drawer))
  (:goal (object-open drawer))
)
"""

        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=[
                {
                    "literal": "(object-open drawer)",
                    "polarity": "negative",
                    "source_field": "isOpen",
                    "observed_value": False,
                }
            ],
            planner_record={"status": "completed", "plan_generated": True, "return_code": 0},
        )

        self.assertFalse(proof["verified"])
        self.assertIn(
            "missing_goal_evidence:(object-open drawer)",
            proof["failure_reasons"],
        )

    def test_val_failure_rejects_otherwise_valid_zero_action_plan(self):
        problem = """(define (problem open)
  (:domain robot1)
  (:objects drawer - object)
  (:init (object-open drawer))
  (:goal (object-open drawer))
)
"""
        evidence = [{"literal": "(object-open drawer)", "source_field": "isOpen"}]

        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=evidence,
            planner_record={"status": "completed", "plan_generated": True, "return_code": 0},
            val_enabled=True,
            val_record={"status": "invalid", "valid": False},
        )

        self.assertFalse(proof["verified"])
        self.assertEqual(proof["val_status"], "invalid")
        self.assertIn("val_not_valid", proof["failure_reasons"])

    def test_missing_planner_record_fails_closed(self):
        problem = """(define (problem open)
  (:domain robot1)
  (:objects drawer - object)
  (:init (object-open drawer))
  (:goal (object-open drawer))
)
"""

        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=[{"literal": "(object-open drawer)"}],
            planner_record={},
        )

        self.assertFalse(proof["verified"])
        self.assertIn("planner_not_successful", proof["failure_reasons"])

    def test_statusless_legacy_planner_record_requires_artifact_identity(self):
        problem = """(define (problem open)
  (:domain robot1)
  (:objects drawer - object)
  (:init (object-open drawer))
  (:goal (object-open drawer))
)
"""
        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=[{"literal": "(object-open drawer)"}],
            planner_record={
                "return_code": 0,
                "plan_generated": True,
                "has_planner_error": False,
            },
        )

        self.assertFalse(proof["verified"])
        self.assertIn("planner_not_successful", proof["failure_reasons"])

    def test_non_boolean_or_contradictory_val_signal_fails_closed(self):
        problem = """(define (problem open)
  (:domain robot1)
  (:objects drawer - object)
  (:init (object-open drawer))
  (:goal (object-open drawer))
)
"""
        common = {
            "subtask_id": 1,
            "problem": problem,
            "plan_text": "; cost = 0\n",
            "evidence": [{"literal": "(object-open drawer)"}],
            "planner_record": {
                "status": "completed",
                "return_code": 0,
                "plan_generated": True,
            },
            "val_enabled": True,
        }

        for val_record in (
            {"status": "completed", "valid": "false"},
            {"status": "valid", "valid": False},
        ):
            with self.subTest(val_record=val_record):
                proof = verify_zero_action_plan(
                    **common,
                    val_record=val_record,
                )
                self.assertFalse(proof["verified"])
                self.assertIn("val_not_valid", proof["failure_reasons"])

    def test_unsupported_goal_structure_cannot_be_a_noop(self):
        problem = """(define (problem choice)
  (:domain robot1)
  (:objects a b - object)
  (:init (ready a))
  (:goal (or (ready a) (ready b)))
)
"""

        proof = verify_zero_action_plan(
            subtask_id=1,
            problem=problem,
            plan_text="; cost = 0\n",
            evidence=[{"literal": "(ready a)"}],
            planner_record={"status": "completed", "plan_generated": True, "return_code": 0},
        )

        self.assertFalse(proof["verified"])
        self.assertIn("unsupported_goal:or", proof["failure_reasons"])


if __name__ == "__main__":
    unittest.main()
