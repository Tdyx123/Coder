import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.movement import (
    MovementConfig,
    MovementConfigurationError,
    MovementMode,
    NavigationMetrics,
)


class MovementConfigTest(unittest.TestCase):
    def test_default_mode_is_teleport(self):
        config = MovementConfig.resolve(environ={})

        self.assertIs(config.mode, MovementMode.TELEPORT)
        self.assertEqual(config.grid_size_m, 0.25)
        self.assertEqual(config.hard_clearance_m, 0.35)
        self.assertEqual(config.grid_snap_tolerance_m, 0.125001)
        self.assertEqual(config.max_replans, 8)
        self.assertEqual(config.max_failed_transitions, 8)
        self.assertEqual(config.max_assignment_trials, 256)

    def test_environment_selects_step_mode(self):
        config = MovementConfig.resolve(
            environ={"LAMMAP_MOVEMENT_MODE": "step"},
        )

        self.assertIs(config.mode, MovementMode.STEP)

    def test_explicit_mode_overrides_environment(self):
        config = MovementConfig.resolve(
            explicit_mode="teleport",
            environ={"LAMMAP_MOVEMENT_MODE": "step"},
        )

        self.assertIs(config.mode, MovementMode.TELEPORT)

    def test_invalid_mode_lists_allowed_values(self):
        with self.assertRaises(MovementConfigurationError) as raised:
            MovementConfig.resolve(explicit_mode="warp", environ={})

        message = str(raised.exception)
        self.assertIn("teleport", message)
        self.assertIn("step", message)


class NavigationMetricsTest(unittest.TestCase):
    def test_snapshot_is_detached_from_mutable_metrics(self):
        metrics = NavigationMetrics(MovementMode.STEP)
        metrics.record_request_started()
        metrics.record_action("MoveAhead")
        metrics.increment("micro_steps")
        metrics.add_planning_time(0.125)

        snapshot = metrics.to_dict()
        snapshot["action_counts"]["MoveAhead"] = 99
        snapshot["planning_durations_seconds"].append(99.0)

        current = metrics.to_dict()
        self.assertEqual(current["action_counts"]["MoveAhead"], 1)
        self.assertEqual(current["micro_steps"], 1)
        self.assertEqual(current["planning_time_seconds"], 0.125)
        self.assertEqual(current["planning_durations_seconds"], [0.125])

    def test_increment_rejects_non_integer_metric(self):
        metrics = NavigationMetrics(MovementMode.STEP)

        with self.assertRaises(AttributeError):
            metrics.increment("action_counts")


if __name__ == "__main__":
    unittest.main()
