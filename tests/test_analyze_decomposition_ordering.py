import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_decomposition_ordering import (
    SubtaskView,
    allocation_violations,
    extract_parallel_claims,
    infer_semantic_stages,
    narrative_claim_violations,
    repair_schedule,
    resource_collisions,
)


def subtask(index, title, canonical_index, skill, objects, robot=1):
    return SubtaskView(
        decomposed_index=index,
        title=title,
        task_subtask_index=canonical_index,
        skill=skill,
        objects=tuple(objects),
        reference_robot=robot,
    )


class AnalyzeDecompositionOrderingTest(unittest.TestCase):
    def test_then_builds_barrier_while_and_stays_parallel(self):
        subtasks = [
            subtask(1, "Break the statue", 2, "Break", ["Statue"]),
            subtask(2, "Break the vase", 3, "Break", ["Vase"]),
            subtask(3, "Put the keychain on the chair", 1, "PutOn", ["KeyChain", "Chair"]),
        ]

        semantic = infer_semantic_stages(
            "break the statue and the vase, then put the keychain on the chair.",
            subtasks,
        )

        self.assertEqual(semantic.stages, [[1, 2], [3]])
        self.assertEqual(semantic.direct_edges, {(1, 3), (2, 3)})
        self.assertEqual(semantic.closure_edges, {(1, 3), (2, 3)})
        self.assertEqual(semantic.issues, [])

    def test_pronouns_are_aligned_by_action_and_explicit_target(self):
        subtasks = [
            subtask(1, "Fill the kettle", 2, "FillWater", ["Kettle", "Faucet"]),
            subtask(2, "Put the kettle on the stoveburner", 3, "PutOn", ["Kettle", "StoveBurner"]),
            subtask(3, "Heat the kettle", 1, "HeatByStoveBurner", ["Kettle", "StoveBurner"]),
        ]

        semantic = infer_semantic_stages(
            "fill the kettle with water, then put it on the stoveburner, then heat it.",
            subtasks,
        )

        self.assertEqual(semantic.stages, [[1], [2], [3]])
        self.assertEqual(
            semantic.closure_edges,
            {(1, 2), (1, 3), (2, 3)},
        )

    def test_switch_it_on_pronoun_is_an_action_match(self):
        subtasks = [
            subtask(1, "Put the box on the floor", 1, "PutOn", ["Box", "Floor"]),
            subtask(2, "Open the laptop", 2, "Open", ["Laptop"]),
            subtask(3, "Switch on the laptop", 3, "SwitchOn", ["Laptop"]),
        ]

        semantic = infer_semantic_stages(
            "put the box on the floor, then open the laptop and switch it on.",
            subtasks,
        )

        self.assertEqual(semantic.stages, [[1], [2, 3]])
        self.assertGreaterEqual(semantic.scores[3][1], 10)
        self.assertEqual(semantic.issues, [])

    def test_allocation_violation_distinguishes_parallel_and_reverse(self):
        parallel, reversed_edges = allocation_violations(
            {(1, 2), (1, 3)},
            {1: 1, 2: 1, 3: 0},
        )

        self.assertEqual(parallel, [(1, 2)])
        self.assertEqual(reversed_edges, [(1, 3)])

    def test_parallel_claim_conflicting_with_then_is_flagged(self):
        output = """
# GENERAL TASK DECOMPOSITION
# SubTask 1: Break the statue
# SubTask 2: Break the vase
# SubTask 3: Put the keychain on the chair
# We can parallelize SubTask 1 and SubTask 2.
# SubTask 3 can be done after or in parallel with the breaking tasks.
# action description from domain for tasks required
"""
        claims = extract_parallel_claims(output, 3)
        violations = narrative_claim_violations(claims, {(1, 3), (2, 3)})

        self.assertEqual(len(claims), 2)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["kind"], "ambiguous_after_or_parallel")

    def test_parallel_claim_parser_does_not_mix_following_sentence_numbers(self):
        output = """
# GENERAL TASK DECOMPOSITION
# We can parallelize SubTask 1 and SubTask 2 because they are independent. SubTask 3 should be done after both.
# action description from domain for tasks required
"""
        claims = extract_parallel_claims(output, 3)
        violations = narrative_claim_violations(claims, {(1, 3), (2, 3)})

        self.assertEqual(claims[0]["pairs"], [[1, 2]])
        self.assertEqual(violations, [])

    def test_repair_schedule_respects_barrier_and_robot_capacity(self):
        waves = repair_schedule(
            [[1, 2], [3, 4]],
            {1: 1, 2: 2, 3: 1, 4: 1},
        )

        self.assertEqual(waves, [[1, 2], [3], [4]])

    def test_resource_collisions_are_reported_separately(self):
        collisions = resource_collisions(
            {1: 0, 2: 0, 3: 1},
            {1: 2, 2: 2, 3: 2},
        )

        self.assertEqual(
            collisions,
            [{
                "stage": 1,
                "local_robot": 2,
                "subtasks": [1, 2],
                "colliding_pairs": [[1, 2]],
            }],
        )


if __name__ == "__main__":
    unittest.main()
