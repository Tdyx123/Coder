import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from merge_pddl_domains import PddlDomainMergeError, PddlDomainMergeHelper


ARTIFACT_DIR = ROOT / "tests" / "artifacts" / "merge_pddl_domains"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class PddlDomainMergeHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = PddlDomainMergeHelper(repo_root=ROOT)

    def test_helper_returns_string_without_writing_output_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base = tmp_root / "base.pddl"
            device = tmp_root / "device.pddl"
            write_text(
                base,
                """
(define (domain base)
  (:requirements :strips)
  (:types
    robot
    object
  )
  (:predicates (at ?robot - robot ?object - object))
  (:action GoToObject
    :parameters (?robot - robot ?object - object)
    :effect (at ?robot ?object)
  )
)
""".strip(),
            )
            write_text(
                device,
                """
(define (domain device)
  (:requirements :strips :typing)
  (:types
    robot
    object
    fridge - object
  )
  (:predicates (at ?robot - robot ?object - object) (cold ?object - object))
  (:action GoToObject
    :parameters (?robot - robot ?object - object)
    :effect (at ?robot ?object)
  )
  (:action ColdObject
    :parameters (?robot - robot ?fridge - fridge ?object - object)
    :effect (cold ?object)
  )
)
""".strip(),
            )

            merged = PddlDomainMergeHelper(repo_root=tmp_root, domain_dir=tmp_root).merge_domains(
                base,
                [device],
                include_inaction=False,
            )

            self.assertIsInstance(merged, str)
            self.assertIn("(:action ColdObject", merged)
            self.assertEqual(sorted(path.name for path in tmp_root.iterdir()), ["base.pddl", "device.pddl"])

    def test_fridge_merge_contains_expected_additions(self):
        merged = self.helper.merge_domains("allactionrobot_remaining.pddl", ["fridge.pddl"])

        self.assertIn("fridge - object", merged)
        self.assertIn("(cold ?object - object)", merged)
        self.assertIn("(cookable ?object - object)", merged)
        self.assertIn("(:action BreakEgg", merged)
        self.assertIn("(:action ColdObject", merged)
        self.assertIn("(inaction ?robot - robot)", merged)

    def test_multiple_device_domains_merge_and_dedupe_base_actions(self):
        merged = self.helper.merge_domains(
            "allactionrobot_remaining.pddl",
            [
                "fridge.pddl",
                "microwave.pddl",
                "coffee_machine.pddl",
                "toaster.pddl",
                "sink.pddl",
                "stove_burner.pddl",
            ],
        )

        for action_name in [
            "ColdObject",
            "RunMicrowave",
            "RunCoffeeMachine",
            "RunToaster",
            "FillWater",
            "CookByStoveBurner",
            "HeatByStoveBurner",
            "FireByStoveBurner",
        ]:
            self.assertIn(f"(:action {action_name}", merged)
        self.assertEqual(merged.count("(:action GoToObject"), 1)
        self.assertEqual(merged.count("(:action PickupObject"), 1)

    def test_conflicting_action_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base = tmp_root / "base.pddl"
            other = tmp_root / "other.pddl"
            write_text(
                base,
                """
(define (domain base)
  (:requirements :strips)
  (:types
    object
  )
  (:predicates (p) (q))
  (:action Same :parameters () :effect (p))
)
""".strip(),
            )
            write_text(
                other,
                """
(define (domain other)
  (:requirements :strips)
  (:types
    object
  )
  (:predicates (p) (q))
  (:action Same :parameters () :effect (q))
)
""".strip(),
            )

            with self.assertRaisesRegex(PddlDomainMergeError, "Conflicting action 'Same'"):
                PddlDomainMergeHelper(repo_root=tmp_root, domain_dir=tmp_root).merge_domains(base, [other])

    def test_conflicting_predicate_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base = tmp_root / "base.pddl"
            other = tmp_root / "other.pddl"
            write_text(
                base,
                """
(define (domain base)
  (:requirements :strips)
  (:types
    object
  )
  (:predicates (state ?object - object))
)
""".strip(),
            )
            write_text(
                other,
                """
(define (domain other)
  (:requirements :strips)
  (:types
    object
  )
  (:predicates (state ?left - object ?right - object))
)
""".strip(),
            )

            with self.assertRaisesRegex(PddlDomainMergeError, "Conflicting predicate 'state'"):
                PddlDomainMergeHelper(repo_root=tmp_root, domain_dir=tmp_root).merge_domains(base, [other])

    def test_conflicting_type_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base = tmp_root / "base.pddl"
            other = tmp_root / "other.pddl"
            write_text(
                base,
                """
(define (domain base)
  (:requirements :strips)
  (:types
    object
    item
  )
  (:predicates)
)
""".strip(),
            )
            write_text(
                other,
                """
(define (domain other)
  (:requirements :strips)
  (:types
    object
    item - object
  )
  (:predicates)
)
""".strip(),
            )

            with self.assertRaisesRegex(PddlDomainMergeError, "Conflicting type 'item'"):
                PddlDomainMergeHelper(repo_root=tmp_root, domain_dir=tmp_root).merge_domains(base, [other])

    def test_real_merge_output_is_written_to_test_artifacts(self):
        merged = self.helper.merge_domains(
            "allactionrobot_remaining.pddl",
            ["fridge.pddl", "microwave.pddl", "sink.pddl"],
        )
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = ARTIFACT_DIR / "merged_allactionrobot.pddl"
        output_path.write_text(merged, encoding="utf-8")

        self.assertEqual(output_path.read_text(encoding="utf-8"), merged)
        self.assertIn("(define (domain allactionrobot)", merged)
        self.assertIn("(:action RunMicrowave", merged)


if __name__ == "__main__":
    unittest.main()
