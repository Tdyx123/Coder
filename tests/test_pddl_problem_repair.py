import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddl_problem_repair import repair_problem_pddl


DOMAIN = """(define (domain robot1)
  (:types robot coffee_machine - object)
  (:predicates
    (ready ?object - object)
    (at ?object - object)
  )
)
"""

CLEAN_PROBLEM = """(define (problem sample)
  (:domain robot1)
  (:objects
    robot1 - robot
    drawer - object
  )
  (:init
    (ready drawer)
  )
  (:goal (ready drawer))
)
"""


class PDDLProblemRepairTests(unittest.TestCase):
    def test_clean_problem_is_unchanged(self):
        result = repair_problem_pddl(CLEAN_PROBLEM, DOMAIN)

        self.assertEqual(result.status, "unchanged")
        self.assertFalse(result.changed)
        self.assertEqual(result.problem, CLEAN_PROBLEM)
        self.assertEqual(result.detected_categories, [])
        self.assertEqual(result.changes, [])
        self.assertEqual(result.unresolved, [])

    def test_extracts_define_block_from_markdown_and_natural_language(self):
        markdown = f"Here is the problem:\n```pddl\n{CLEAN_PROBLEM}```\nDone."
        result = repair_problem_pddl(markdown, DOMAIN)

        self.assertEqual(result.status, "repaired")
        self.assertTrue(result.changed)
        self.assertEqual(result.problem, CLEAN_PROBLEM)
        self.assertIn("markdown_fence_in_pddl", result.detected_categories)
        self.assertIn("removed_markdown_or_surrounding_text", result.changes)

        prose = f"Generated problem follows.\n{CLEAN_PROBLEM}End of problem."
        prose_result = repair_problem_pddl(prose, DOMAIN)
        self.assertIn("natural_language_leak", prose_result.detected_categories)
        self.assertEqual(prose_result.problem, CLEAN_PROBLEM)

    def test_merges_duplicate_objects_and_rewrites_references(self):
        problem = """(define (problem duplicate)
  (:domain robot1)
  (:objects Drawer drawer - object robot1 - robot)
  (:init (at drawer))
  (:goal (at drawer))
)
"""

        result = repair_problem_pddl(problem, DOMAIN)

        self.assertIn("duplicate_object_definition", result.detected_categories)
        self.assertIn("merged_object:drawer->Drawer", result.changes)
        self.assertIn("Drawer - object", result.problem)
        self.assertNotIn("Drawer Drawer", result.problem)
        self.assertIn("(at Drawer)", result.problem)

    def test_repairs_normalized_and_unknown_object_types(self):
        problem = """(define (problem types)
  (:domain robot1)
  (:objects machine - coffee-machine mystery - imaginary robot1 - robot)
  (:init)
  (:goal (and))
)
"""

        result = repair_problem_pddl(problem, DOMAIN)

        self.assertIn("pddl_unknown_type", result.detected_categories)
        self.assertIn("type:coffee-machine->coffee_machine", result.changes)
        self.assertIn("type:imaginary->object", result.changes)
        self.assertIn("machine - coffee_machine", result.problem)
        self.assertIn("mystery - object", result.problem)

    def test_positive_initial_fact_wins_over_conflicting_negative(self):
        problem = """(define (problem contradiction)
  (:domain robot1)
  (:objects drawer - object)
  (:init
    (ready drawer)
    (not (ready drawer))
  )
  (:goal (ready drawer))
)
"""

        result = repair_problem_pddl(problem, DOMAIN)

        self.assertIn("initial_state_contradiction", result.detected_categories)
        self.assertIn("removed_conflicting_negative:( ready drawer )", result.changes)
        self.assertIn("(ready drawer)", result.problem)
        self.assertNotIn("(not (ready drawer))", result.problem)

    def test_repairs_multiple_categories_in_one_pass(self):
        problem = """(define (problem multiple)
  (:domain robot1)
  (:objects Cup cup - coffee-machine)
  (:init (ready cup) (not (ready Cup)))
  (:goal (ready cup))
)
"""

        result = repair_problem_pddl(problem, DOMAIN)

        self.assertEqual(
            result.detected_categories,
            [
                "duplicate_object_definition",
                "pddl_unknown_type",
                "initial_state_contradiction",
            ],
        )
        self.assertIn("Cup - coffee_machine", result.problem)
        self.assertNotIn("(not (ready Cup))", result.problem)

    def test_unbalanced_or_leaking_define_is_unresolved_without_discarding_problem(self):
        unbalanced = "(define (problem broken) (:domain robot1)"
        result = repair_problem_pddl(unbalanced, DOMAIN)

        self.assertEqual(result.status, "unresolved")
        self.assertFalse(result.changed)
        self.assertEqual(result.problem, unbalanced)
        self.assertIn("unbalanced_problem_define_block", result.unresolved)

        leaking = CLEAN_PROBLEM.replace(
            "(:goal (ready drawer))",
            "; here is an invalid explanation\n  (:goal (ready drawer))",
        )
        leak_result = repair_problem_pddl(leaking, DOMAIN)
        self.assertEqual(leak_result.status, "unresolved")
        self.assertEqual(leak_result.problem, leaking)
        self.assertIn("define_block_contains_natural_language", leak_result.unresolved)


if __name__ == "__main__":
    unittest.main()
