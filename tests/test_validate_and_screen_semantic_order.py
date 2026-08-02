import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from validate_and_screen_semantic_order import (
    DEFAULT_GOLD,
    DEFAULT_INPUT,
    MIN_ALIGNMENT_MARGIN,
    MIN_ALIGNMENT_SCORE,
    run_validation_and_screen,
    stages_to_edges,
    strict_parser_decision,
    wilson_lower,
)


class ValidateAndScreenSemanticOrderTest(unittest.TestCase):
    def test_stage_edges_form_transitive_closure(self):
        self.assertEqual(
            stages_to_edges([[1, 2], [3], [4, 5]]),
            {
                (1, 3),
                (2, 3),
                (1, 4),
                (1, 5),
                (2, 4),
                (2, 5),
                (3, 4),
                (3, 5),
            },
        )

    def test_wilson_lower_is_conservative(self):
        self.assertLess(wilson_lower(100, 100), 1.0)
        self.assertGreater(wilson_lower(100, 100), 0.90)

    def test_strict_parser_abstains_on_low_margin(self):
        record = {
            "semantic": {
                "clauses": ["open drawer", "open cabinet"],
                "stages": [[1], [2]],
                "issues": [],
                "stage_scores": {"1": [12, 10], "2": [2, 20]},
                "closure_edges": [{"before": 1, "after": 2}],
            }
        }

        decision = strict_parser_decision(record)

        self.assertEqual(decision["status"], "abstained")
        self.assertIn(
            f"minimum alignment margin 2 < {MIN_ALIGNMENT_MARGIN}",
            decision["reasons"],
        )

    def test_repository_gold_exceeds_confidence_and_coverage_gates(self):
        validation, screened, summary = run_validation_and_screen(
            ROOT / DEFAULT_INPUT,
            ROOT / DEFAULT_GOLD,
        )

        self.assertTrue(validation["gate"]["passed"])
        self.assertGreaterEqual(validation["parser"]["precision"], 0.90)
        self.assertGreaterEqual(
            validation["parser"]["precision_lower_95"],
            0.90,
        )
        self.assertGreaterEqual(
            validation["parser"]["forced_edge_recall"],
            0.40,
        )
        self.assertGreaterEqual(
            validation["parser"]["ordered_task_coverage"],
            0.40,
        )
        self.assertEqual(len(screened), 148)
        self.assertEqual(summary["records"], 148)
        self.assertEqual(
            summary["reference_allocation_backtest"],
            {
                "conflict_records": 49,
                "parallelized_forced_edges": 89,
                "reversed_forced_edges": 13,
            },
        )
        self.assertEqual(validation["parser"]["records_abstained"], 0)
        self.assertGreaterEqual(MIN_ALIGNMENT_SCORE, 10)


if __name__ == "__main__":
    unittest.main()
