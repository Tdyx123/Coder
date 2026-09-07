import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.action_plan import Action, StagePlan, TaskPlan, TaskRunner
from executor_system.parallel_runner import run_action_plan_tolerant
from tests.snapshot_fakes import FakeRuntime


class PlanContractTest(unittest.TestCase):
    def reject(self, action=None, **stage_kwargs):
        runtime = FakeRuntime()
        with self.assertRaises((ValueError, RuntimeError, KeyError, TypeError)):
            run_action_plan_tolerant(runtime, TaskPlan('t', [StagePlan('s',
                {'robot1': [action or Action('Wait')]}, **stage_kwargs)]), timeout_seconds=1)
        self.assertEqual(runtime.state_version, 0)

    def test_missing_pickup_argument(self): self.reject(Action('PickupObject'))
    def test_conflicting_object_argument(self): self.reject(Action('PickupObject', {'args': ('Mug',), 'objectId': 'Apple|1'}))
    def test_failure_policy(self): self.reject(Action('Wait', on_failure='BAD'))
    def test_conflict_policy(self): self.reject(Action('Wait', on_conflict='BAD'))
    def test_stage_policy(self): self.reject(stage_failure_policy='RETRY')
    def test_sync_policy(self): self.reject(synchronization_policy='BAD')
    def test_negative_retries(self): self.reject(Action('Wait', max_retries=-1))
    def test_invalid_condition(self): self.reject(Action('Wait', expected_effects=('HOT',)))
    def test_unknown_robot(self):
        with self.assertRaises((ValueError, RuntimeError, KeyError)):
            TaskRunner(FakeRuntime()).execute(TaskPlan('t', [StagePlan('s', {'robot9': [Action('Wait')]})]))
    def test_nonfinite_timeout(self):
        for timeout in (float('nan'), float('inf'), -1):
            with self.subTest(timeout=timeout), self.assertRaises((ValueError, RuntimeError)):
                TaskRunner(FakeRuntime()).execute(TaskPlan('t', [StagePlan('s', {'robot1': [Action('Wait')]})]), timeout_seconds=timeout)
