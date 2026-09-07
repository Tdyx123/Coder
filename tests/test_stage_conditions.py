import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.action_plan import Action, StagePlan, TaskPlan, TaskRunner
from executor_system.parallel_runner import run_action_plan_tolerant
from executor_system.executor import Executor
from tests.snapshot_fakes import FakeRuntime


class StageConditionsTest(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()

    def run_plan(self, stages, **kwargs):
        return run_action_plan_tolerant(self.runtime, TaskPlan('t', stages, **kwargs),
                                       timeout_seconds=1, execution_policy='strict')

    def test_stage_condition_checked_after_execution(self):
        calls = []
        def condition(world):
            calls.append(world.version)
            return False
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait')]}, condition)])
        self.assertEqual(report['execution_status'], 'failed')
        self.assertGreaterEqual(len(calls), 2)

    def test_stage_condition_becomes_true(self):
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait')]},
                                        lambda w: w.version > 0)])
        self.assertEqual(report['execution_status'], 'completed')

    def test_initial_true_skips_actions(self):
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait')]}, lambda w: True)])
        self.assertEqual(report['action_counts']['skipped'], 1)
        self.assertEqual(report['action_counts']['attempts'], 0)
        self.assertEqual(report['actions'][0]['reason'], 'stage_condition_already_satisfied')

    def test_global_sees_all_robots(self):
        seen = []
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait')]})],
            global_success_condition=lambda w: seen.append(set(w.robot_positions)) or len(w.robot_positions) == 2)
        self.assertEqual(report['execution_status'], 'completed')
        self.assertEqual(seen, [{'robot1', 'robot2'}])

    def test_effect_failure_and_stage_skip(self):
        report = self.run_plan([
            StagePlan('s', {'robot1': [Action('Wait', expected_effects=(lambda w: False,)), Action('Wait')]}, stage_failure_policy='SKIP'),
            StagePlan('next', {'robot2': [Action('Wait')]})])
        self.assertEqual(report['execution_status'], 'failed')
        self.assertEqual(report['action_counts']['succeeded'], 1)
        self.assertEqual(report['actions'][1]['reason'], 'stage_failed')
        json.dumps(report, allow_nan=False)

    def test_stage_fail_stops_next(self):
        report = self.run_plan([
            StagePlan('s', {'robot1': [Action('Wait')]}, lambda w: False),
            StagePlan('next', {'robot2': [Action('Wait')]})])
        self.assertEqual(report['action_counts']['succeeded'], 1)
        self.assertEqual(report['actions'][-1]['reason'], 'previous_stage_failed')

    def test_precondition_waits_then_executes(self):
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait',
            expected_preconditions=(lambda w: w.version > 0,), timeout_ticks=3)]})])
        self.assertEqual(report['action_counts']['succeeded'], 1)
        self.assertTrue(report['stages'][0]['waits'])

    def test_unknown_effect_fails_and_history_cannot_satisfy_current(self):
        self.runtime.objects = [{'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'RoomTemp'}]
        from executor_system.evaluation import EvaluationContext
        self.runtime.evaluation_context = EvaluationContext.from_goals([{'name': 'Mug', 'state': 'HOT'}])
        self.runtime.evaluation_context.record_observation('Mug', 'HOT', 'Mug|1')
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait',
            expected_effects=({'name': 'Mug', 'state': 'HOT'},))]})])
        self.assertEqual(report['action_counts']['failed'], 1)

    def test_callback_exception_is_structured(self):
        def broken(world):
            raise ValueError('callback boom')
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait', wait_until=broken)]})])
        self.assertEqual(report['execution_status'], 'failed')
        self.assertIn('ConditionEvaluationError', json.dumps(report))
        self.assertIn('ValueError', json.dumps(report))

    def test_ordinary_runner_raises_report(self):
        from executor_system.execution_policy import PlanExecutionError
        with self.assertRaises(PlanExecutionError) as raised:
            TaskRunner(self.runtime, execution_policy='strict').execute(TaskPlan('t', [
                StagePlan('s', {'robot1': [Action('Wait')]}, lambda w: False)]))
        self.assertEqual(raised.exception.report['execution_status'], 'failed')

    def test_fail_robot_preserves_peer(self):
        report = self.run_plan([StagePlan('s', {
            'robot1': [Action('Wait', expected_effects=(lambda w: False,), on_failure='FAIL_ROBOT'), Action('Wait')],
            'robot2': [Action('Wait'), Action('Wait')]})])
        self.assertEqual(report['action_counts']['succeeded'], 2)
        self.assertEqual(report['actions'][1]['reason'], 'robot_failed')

    def test_standalone_uses_conditions(self):
        executor = Executor(self.runtime, 'robot1', [Action('Wait', expected_effects=(lambda w: False,))])
        executor.execute()
        self.assertEqual(executor.state.last_action_result.status, 'FAILED')

    def test_legacy_false_stage_condition_continues_but_reports_failed(self):
        report = run_action_plan_tolerant(self.runtime, TaskPlan('t', [
            StagePlan('s', {'robot1': [Action('Wait')]}, lambda w: False),
            StagePlan('next', {'robot2': [Action('Wait')]})]), timeout_seconds=1)
        self.assertEqual(report['execution_status'], 'failed')
        self.assertEqual(report['action_counts']['succeeded'], 2)

    def test_false_global_condition_reports_failed_in_both_policies(self):
        for policy in ('strict', 'legacy'):
            report = run_action_plan_tolerant(FakeRuntime(), TaskPlan('t', [
                StagePlan('s', {'robot1': [Action('Wait')]})], lambda w: False),
                execution_policy=policy, timeout_seconds=1)
            self.assertEqual(report['execution_status'], 'failed')

    def test_dictionary_precondition_unknown_waits_and_times_out_once(self):
        self.runtime.objects = [{'objectId': 'Mug|1', 'objectType': 'Mug'}]
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait', timeout_ticks=1,
            expected_preconditions=({'name': 'Mug', 'state': 'HOT'},))]})])
        self.assertEqual(report['action_counts']['failed'], 1)
        self.assertEqual(report['action_counts']['attempts'], 1)
        self.assertIsNone(report['actions'][0]['preconditions'][0]['satisfied'])

    def test_callback_effect_error_preserves_original_and_fails_task(self):
        def broken(world):
            raise LookupError('effects boom')
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait', expected_effects=(broken,))]},
                                        stage_failure_policy='SKIP'),
                                StagePlan('next', {'robot2': [Action('Wait')]})])
        self.assertEqual(report['execution_status'], 'failed')
        self.assertIn('LookupError', json.dumps(report))
        self.assertEqual(report['action_counts']['attempts'], 1)

    def test_final_snapshot_and_goal_evidence_are_serializable(self):
        self.runtime.objects = [{'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot'}]
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait',
            expected_effects=({'name': 'Mug', 'state': 'HOT'},))]})])
        self.assertEqual(set(report['final_snapshot']['robot_positions']), {'robot1', 'robot2'})
        self.assertTrue(report['actions'][0]['effects'][0]['satisfied'])
        json.dumps(report, allow_nan=False)

    def test_failed_action_still_checks_final_stage_condition(self):
        calls = []
        report = self.run_plan([StagePlan('s', {'robot1': [Action('Wait',
            expected_effects=(lambda w: False,))]},
            lambda w: calls.append(w.version) or False)])
        self.assertEqual(report['execution_status'], 'failed')
        self.assertEqual(len(calls), 2)
        self.assertIs(report['stages'][0]['condition']['final'], False)
