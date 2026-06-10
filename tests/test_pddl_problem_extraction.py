import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pddlrun_llmseparate import TaskManager


class PDDLProblemExtractionTests(unittest.TestCase):
    def test_extract_pddl_problem_block_uses_balanced_parentheses(self):
        manager = TaskManager.__new__(TaskManager)
        llm_text = (
            "After validating the problem file against the domain, I found several issues:\n\n"
            "Here's the corrected problem file:\n\n"
            "```pddl\n"
            "(define (problem put_pan_in_cabinet)\n"
            "    (:domain robot9)\n"
            "    \n"
            "    (:objects\n"
            "        robot1 - robot\n"
            "        pan - object\n"
            "        cabinet - object\n"
            "        counterTop - object\n"
            "        robot_init_location - object\n"
            "    )\n"
            "    \n"
            "    (:init\n"
            "        (at robot1 robot_init_location)\n"
            "        (at-location pan counterTop)\n"
            "        (is-openable cabinet)\n"
            "        (not (object-open cabinet))\n"
            "    )\n"
            "    \n"
            "    (:goal\n"
            "        (at-location pan cabinet)\n"
            "    )\n"
            ")\n"
            "```\n\n"
            "The corrected problem file is now syntactically valid.\n"
        )

        extracted = manager._extract_pddl_problem_block(llm_text)

        self.assertTrue(extracted.startswith("(define (problem put_pan_in_cabinet)"))
        self.assertTrue(extracted.endswith(")"))
        self.assertIn("    \n    (:objects", extracted)
        self.assertNotIn("```", extracted)
        self.assertNotIn("After validating", extracted)
        self.assertNotIn("The corrected problem file", extracted)


if __name__ == "__main__":
    unittest.main()
