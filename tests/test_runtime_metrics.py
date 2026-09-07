import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from executor_system.runtime_metrics import RuntimeMetrics, runtime_metadata


class FakeClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, amount):
        self.now += amount


class RuntimeMetricsTests(unittest.TestCase):
    def test_metrics_keep_counts_without_unbounded_samples(self):
        metrics = RuntimeMetrics()
        for _ in range(10000):
            metrics.observe('controller', .01)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['controller']['count'], 10000)
        self.assertAlmostEqual(snapshot['controller']['total_seconds'], 100)
        self.assertNotIn('samples', snapshot['controller'])
        for i in range(10000):
            metrics.observe(str(i), 0)
            metrics.increment(str(i))
        self.assertLessEqual(len(metrics.snapshot()), 68)
        self.assertLessEqual(len(metrics.snapshot()['counters']), 129)

    def test_thread_safety_exception_timing_and_snapshot_copy(self):
        clock = FakeClock()
        metrics = RuntimeMetrics(clock=clock)
        with self.assertRaises(ValueError):
            with metrics.measure('recovery'):
                clock.advance(4)
                raise ValueError('expected')
        threads = [threading.Thread(target=lambda: [metrics.increment('calls') for _ in range(1000)]) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['counters']['calls'], 4000)
        self.assertEqual(snapshot['recovery'], dict(count=1, total_seconds=4, max_seconds=4))
        snapshot['recovery']['count'] = 99
        self.assertEqual(metrics.snapshot()['recovery']['count'], 1)
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError): metrics.observe('invalid', value)

    def test_controller_lock_wait_call_and_reachable_overlap(self):
        from tests.test_world_snapshot import snapshot_runtime
        runtime = snapshot_runtime()
        clock = FakeClock()
        runtime.runtime_metrics = RuntimeMetrics(clock=clock)
        class Lock:
            def __enter__(self): clock.advance(2)
            def __exit__(self, *args): pass
        runtime.controller_lock = Lock()
        original = runtime.controller.step
        def submit(payload):
            clock.advance(3)
            return original(payload)
        runtime.controller.step = submit
        runtime._step_direct({'action': 'GetReachablePositions'}, save_frame=False)
        result = runtime.runtime_metrics.snapshot()
        self.assertEqual(result['lock_wait']['total_seconds'], 2)
        self.assertEqual(result['controller']['total_seconds'], 3)
        self.assertEqual(result['reachable_query']['total_seconds'], 3)
        self.assertEqual(result['counters']['controller_calls'], 1)
        self.assertEqual(result['counters']['reachable_queries'], 1)

    def test_planning_recovery_and_artifacts_instrument_real_boundaries(self):
        from executor_system.movement_coordinator import StepMovementCoordinator
        from executor_system.object_interactor import ObjectInteractor
        from executor_system.runtime_artifacts import RuntimeArtifacts
        clock = FakeClock()
        runtime = SimpleNamespace(runtime_metrics=RuntimeMetrics(clock=clock))
        coordinator = object.__new__(StepMovementCoordinator)
        coordinator.runtime = runtime
        def failed_scenario(*args):
            clock.advance(5)
            raise ValueError('scenario')
        coordinator._build_scenario = failed_scenario
        with self.assertRaises(ValueError): coordinator._plan({}, None, frozenset())
        interactor = ObjectInteractor(runtime)
        def bad_agent(*args):
            clock.advance(7)
            raise ValueError('agent')
        runtime.agent_id = bad_agent
        runtime._object_interaction_settings = lambda: {'PICKUP_OBJECT_CLIP_BACKOFF_DISTANCES': [1]}
        runtime.pickup_clip_backoff_position = bad_agent
        # Recovery boundary must time failures as well as successful retries.
        with patch.object(interactor, 'pickup_clip_backoff_position', side_effect=bad_agent):
            with self.assertRaises(ValueError):
                interactor.retry_pickup_after_clip_error(0, {}, RuntimeError('clipping'))
        artifacts = RuntimeArtifacts(runtime, cv2_provider=lambda: None, frame_converter=lambda e: None,
                                     metadata_enabled=lambda: True, logger=lambda m: None)
        class Lock:
            def __enter__(self):
                clock.advance(11)
                raise ValueError('artifact')
            def __exit__(self, *args): pass
        runtime.controller_lock = Lock()
        with self.assertRaises(ValueError): artifacts.write_final_metadata()
        result = runtime.runtime_metrics.snapshot()
        self.assertEqual(result['navigation_planning']['total_seconds'], 5)
        self.assertEqual(result['recovery']['total_seconds'], 7)
        self.assertEqual(result['artifacts']['total_seconds'], 11)

    def test_reproducibility_snapshot_is_allowlisted(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'secret-value'}):
            metadata = runtime_metadata(ROOT, config={'movement_mode': 'step'}, plan={'a': 1})
        self.assertEqual(len(metadata['plan_content_sha256']), 64)
        self.assertEqual(metadata['config']['movement_mode'], 'step')
        for key in ('code_sha', 'python_version', 'ai2thor_version', 'platform', 'gpu'):
            self.assertIn(key, metadata)
        self.assertNotIn('secret-value', json.dumps(metadata))

    def test_legacy_navigation_samples_are_bounded(self):
        from executor_system.movement import NavigationMetrics, MovementMode
        metrics = NavigationMetrics(MovementMode.STEP)
        for _ in range(10000): metrics.add_planning_time(.01)
        result = metrics.to_dict()
        self.assertLessEqual(len(result['planning_durations_seconds']), 128)
        self.assertAlmostEqual(result['planning_time_seconds'], 100)

    def test_legacy_benchmark_does_not_treat_preview_as_complete_percentile(self):
        from benchmark_movement_modes import _aggregate_mode
        result = _aggregate_mode('step', [{'mode': 'step', 'navigation_metrics': {
            'planning_durations_seconds': [.1], 'planning_durations_truncated': True}}])
        self.assertFalse(result['planner_fixture_p95_is_complete'])
        self.assertIsNone(result['planner_fixture_p95_seconds'])
