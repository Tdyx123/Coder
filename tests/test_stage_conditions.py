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
    def test_legacy_tick_callbacks_observe_completed_queue_progress(self):
        from executor_system.action_plan import AI2ThorAdapter
        from executor_system.execution_control import PlanExecutionTimeout
        for entry in ('ordinary', 'tolerant'):
            for commits in (0, 3):
                with self.subTest(entry=entry, commits=commits):
                    runtime = FakeRuntime()
                    observed = []
                    def execute(adapter, robot, action, **kwargs):
                        world = kwargs['world_state']
                        observed.append((world.tick, world.version))
                        for _ in range(commits):
                            runtime.step({'action': 'Pass'})
                    plan = TaskPlan('ticks', [StagePlan('s', {'robot1': [
                        Action('Wait'),
                        Action('Wait', wait_until=lambda w: w.tick >= 1,
                               expected_preconditions=(lambda w: w.tick == 1,),
                               expected_effects=(lambda w: w.tick == 1,)),
                    ]}, lambda w: w.tick >= 2)],
                        global_success_condition=lambda w: w.tick == 2)
                    with patch.object(AI2ThorAdapter, 'execute', execute):
                        if entry == 'ordinary':
                            try:
                                TaskRunner(runtime).execute(plan, timeout_seconds=.2)
                            except PlanExecutionTimeout:
                                pass  # Assert the saved execution result below.
                        else:
                            run_action_plan_tolerant(runtime, plan, timeout_seconds=.2)
                    report = runtime.execution_report
                    self.assertEqual(report['execution_status'], 'completed')
                    self.assertEqual(observed, [(0, 0), (1, commits)])
                    self.assertTrue(report['global_condition_satisfied'])

    def test_tick_is_shared_by_robots_and_resets_at_stage_boundary(self):
        from executor_system.action_plan import AI2ThorAdapter
        observed = []
        def execute(adapter, robot, action, **kwargs):
            observed.append((action.action_id, kwargs['world_state'].tick))
        plan = TaskPlan('shared-tick', [
            StagePlan('first', {
                'robot1': [Action('Wait', action_id='peer', wait_until=lambda w: w.tick >= 2)],
                'robot2': [Action('Wait', action_id='a'), Action('Wait', action_id='b')],
            }, lambda w: w.tick >= 3),
            StagePlan('second', {'robot1': [Action('Wait', action_id='reset',
                wait_until=lambda w: w.tick == 0)]}, lambda w: w.tick >= 1),
        ], global_success_condition=lambda w: w.tick == 1)
        with patch.object(AI2ThorAdapter, 'execute', execute):
            report = run_action_plan_tolerant(self.runtime, plan, timeout_seconds=.3)
        self.assertEqual(report['execution_status'], 'completed')
        self.assertEqual(observed, [('a', 0), ('b', 1), ('peer', 2), ('reset', 0)])

    def test_standalone_world_returns_confirmed_tick(self):
        from executor_system.action_plan import StageRunner
        for entry in ('stage', 'executor'):
            with self.subTest(entry=entry):
                runtime = FakeRuntime()
                actions = [Action('WaitUntil'), Action('WaitUntil')]
                if entry == 'stage':
                    world = StageRunner(runtime).execute_stage(StagePlan('s', {'robot1': actions}))
                else:
                    world = Executor(runtime, 'robot1', actions).execute()
                self.assertEqual(world.tick, 2)
                self.assertEqual(world.version, 0)

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

    def test_shutdown_drain_keeps_peer_failure_and_attempt(self):
        import time
        from executor_system.stage_scheduler import StageScheduler
        original_receive = StageScheduler._receive_results
        def receive(scheduler):
            if scheduler.inflight and not scheduler.action_records:
                deadline = time.monotonic() + .5
                while scheduler.results.qsize() < 2 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(scheduler.results.qsize(), 2)
                pending_results = [scheduler.results.get_nowait(), scheduler.results.get_nowait()]
                for result in sorted(pending_results, key=lambda item: item[0].robot_id):
                    scheduler.results.put(result)
            return original_receive(scheduler)
        def fail(adapter, robot, action, **kwargs):
            raise RuntimeError(robot + ' original failure')
        with patch.object(StageScheduler, '_receive_results', receive), patch(
                'executor_system.action_plan.AI2ThorAdapter.execute', fail):
            report = self.run_plan([StagePlan('s', {
                'robot1': [Action('Wait')],
                'robot2': [Action('Wait', on_failure='SKIP')]})])
        self.assertEqual(report['action_counts']['failed'], 2)
        self.assertEqual(report['action_counts']['cancelled'], 0)
        self.assertEqual(len(report['stages'][0]['attempts']), 2)
        self.assertIn('robot2 original failure', json.dumps(report['errors']))
        self.assertEqual(report['actions'][1]['failure_decision'], 'skip')

    def test_tolerant_stage_deadline_tightens_explicit_control(self):
        import time
        from executor_system.execution_control import install_control, PlanExecutionTimeout
        from executor_system.parallel_runner import TolerantStageRunner, TolerantRunStats
        root = install_control(self.runtime, 1)
        runner = TolerantStageRunner(self.runtime, stats=TolerantRunStats(),
            deadline=time.monotonic() - 1, stage_index=0, control=root)
        with self.assertRaises(PlanExecutionTimeout):
            runner.execute_stage(StagePlan('s', {'robot1': [Action('Wait')]}))
        self.assertEqual(self.runtime.state_version, 0)

    def test_shutdown_drain_records_deferred_and_never_retries(self):
        import time
        from executor_system.stage_scheduler import StageScheduler
        from executor_system.movement import NavigationDeferred
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                self.runtime = FakeRuntime()
                calls = []
                original_receive = StageScheduler._receive_results
                def receive(scheduler):
                    if scheduler.inflight and not scheduler.action_records:
                        deadline = time.monotonic() + .5
                        while scheduler.results.qsize() < 2 and time.monotonic() < deadline:
                            time.sleep(.001)
                        self.assertEqual(scheduler.results.qsize(), 2)
                        results = [scheduler.results.get_nowait(), scheduler.results.get_nowait()]
                        for result in sorted(results, key=lambda item: item[0].robot_id):
                            scheduler.results.put(result)
                    return original_receive(scheduler)
                def fail(adapter, robot, action, **kwargs):
                    calls.append(robot)
                    if deferred and robot == 'robot2':
                        raise NavigationDeferred(1, fallback_status='NO_PLAN_FOUND')
                    raise RuntimeError(robot + ' failure')
                with patch.object(StageScheduler, '_receive_results', receive), patch(
                        'executor_system.action_plan.AI2ThorAdapter.execute', fail):
                    report = self.run_plan([StagePlan('s', {
                        'robot1': [Action('Wait')],
                        'robot2': [Action('Teleport', on_failure='RETRY', max_retries=3)]})])
                self.assertEqual(sorted(calls), ['robot1', 'robot2'])
                self.assertEqual(report['action_counts']['attempts'], 2)
                self.assertEqual(len(report['stages'][0]['attempts']), 2)
                if deferred:
                    self.assertEqual(report['stages'][0]['attempts'][1]['status'], 'deferred')
                    self.assertEqual(report['action_counts']['cancelled'], 1)
                else:
                    self.assertEqual(report['action_counts']['failed'], 2)
                    self.assertEqual(report['actions'][1]['reason'], 'stage_stopped_before_retry')

    def _run_drained_effect_case(self, effect, *, ordinary=False,
                                 initial_temperature='RoomTemp', final_temperature='Hot'):
        import time
        from executor_system.stage_scheduler import StageScheduler
        self.runtime.objects = [{'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': initial_temperature}]
        original_receive = StageScheduler._receive_results
        def receive(scheduler):
            if scheduler.inflight and not scheduler.action_records:
                deadline = time.monotonic() + .5
                while scheduler.results.qsize() < 2 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(scheduler.results.qsize(), 2)
                results = [scheduler.results.get_nowait(), scheduler.results.get_nowait()]
                for result in sorted(results, key=lambda item: item[0].robot_id):
                    scheduler.results.put(result)
            return original_receive(scheduler)
        calls = []
        def fail(adapter, robot, action, **kwargs):
            calls.append(robot)
            if robot == 'robot2':
                with self.runtime.controller_lock:
                    self.runtime.objects[0]['temperature'] = final_temperature
                    self.runtime.state_version += 1
            raise RuntimeError(robot + ' completed failure')
        plan = TaskPlan('t', [StagePlan('s', {
            'robot1': [Action('Wait')],
            'robot2': [Action('Wait', on_failure='SKIP_IF_EFFECT_ALREADY_TRUE',
                              expected_effects=(effect,),
                              resource_policy={'object_resources': ('Mug|1',)}), Action('Wait')]})])
        with patch.object(StageScheduler, '_receive_results', receive), patch(
                'executor_system.action_plan.AI2ThorAdapter.execute', fail), patch.object(
                self.runtime, 'step', side_effect=AssertionError('finalization must not step')):
            try:
                if ordinary:
                    return TaskRunner(self.runtime, execution_policy='strict').execute(plan, timeout_seconds=1)
                return run_action_plan_tolerant(self.runtime, plan, execution_policy='strict', timeout_seconds=1)
            finally:
                self.assertEqual(sorted(calls), ['robot1', 'robot2'])

    def test_drained_effect_proof_uses_final_snapshot(self):
        for dictionary in (False, True):
            with self.subTest(dictionary=dictionary):
                self.runtime = FakeRuntime()
                seen = []
                effect = ({'name': 'Mug', 'state': 'HOT'} if dictionary else
                          lambda world: seen.append(world.version) or world.objects_by_id['Mug|1']['temperature'] == 'Hot')
                report = self._run_drained_effect_case(effect)
                self.assertEqual(report['execution_status'], 'failed')
                self.assertEqual(report['action_counts']['succeeded'], 1)
                self.assertEqual(report['action_counts']['failed'], 1)
                self.assertEqual(report['action_counts']['attempts'], 2)
                self.assertEqual(report['actions'][1]['reason'], 'effects_already_satisfied')
                self.assertTrue(report['actions'][1]['effects'][0]['satisfied'])
                self.assertTrue(report['stages'][0]['snapshot_is_current'])
                self.assertEqual(report['actions'][2]['reason'], 'stage_failed')
                self.assertEqual(self.runtime.action_resource_manager.holders(), {})
                if not dictionary:
                    self.assertEqual(seen, [1])

    def test_drained_effect_callback_error_preserves_cause_and_cleanup(self):
        from executor_system.execution_policy import PlanExecutionError, ConditionEvaluationError
        original = ValueError('drained effect callback failed')
        def effect(world):
            raise original
        with self.assertRaises(PlanExecutionError) as caught:
            self._run_drained_effect_case(effect, ordinary=True)
        self.assertIsInstance(caught.exception.__cause__, ConditionEvaluationError)
        self.assertIs(caught.exception.__cause__.__cause__, original)
        report = caught.exception.report
        self.assertIn('drained effect callback failed', json.dumps(report['errors']))
        self.assertEqual(len(report['actions']), 3)
        self.assertEqual(report['action_counts']['attempts'], 2)
        self.assertEqual(report['actions'][2]['status'], 'unexecuted')
        self.assertEqual(self.runtime.action_resource_manager.holders(), {})
        self.assertIsNone(self.runtime.stage_scheduler)

    def test_drained_callback_cannot_replace_primary_task_timeout(self):
        from executor_system.execution_control import PlanExecutionTimeout
        from executor_system.stage_scheduler import StageScheduler
        original = PlanExecutionTimeout('primary task timeout')
        receive = StageScheduler._receive_results
        def timeout_after_results(scheduler):
            if scheduler.inflight:
                raise original
            return receive(scheduler)
        calls = []
        def effect(world):
            calls.append(world.version)
            raise ValueError('secondary cleanup callback error')
        with patch.object(StageScheduler, '_receive_results', timeout_after_results):
            with self.assertRaises(PlanExecutionTimeout) as caught:
                self._run_drained_effect_case(effect, ordinary=True)
        self.assertIs(caught.exception, original)
        self.assertEqual(self.runtime.execution_report['execution_status'], 'timeout')
        self.assertEqual(calls, [])
        self.assertEqual(self.runtime.execution_report['actions'][1]['reason'], 'effects_unknown')
        self.assertEqual(self.runtime.action_resource_manager.holders(), {})
        self.assertIsNone(self.runtime.stage_scheduler)

    def test_abort_drain_cannot_prove_effects_with_admission_snapshot(self):
        from executor_system.execution_control import PlanExecutionTimeout, ExecutionCancelled
        from executor_system.stage_scheduler import StageScheduler
        for error_type in (PlanExecutionTimeout, ExecutionCancelled):
            for dictionary in (False, True):
                with self.subTest(error_type=error_type.__name__, dictionary=dictionary):
                    self.runtime = FakeRuntime()
                    original = error_type('abort after returned results')
                    receive = StageScheduler._receive_results
                    def abort(scheduler):
                        if scheduler.inflight:
                            raise original
                        return receive(scheduler)
                    seen = []
                    effect = ({'name': 'Mug', 'state': 'HOT'} if dictionary else
                              lambda world: seen.append(world.version) or world.objects_by_id['Mug|1']['temperature'] == 'Hot')
                    with patch.object(StageScheduler, '_receive_results', abort):
                        with self.assertRaises(error_type) as caught:
                            self._run_drained_effect_case(effect, ordinary=True,
                                initial_temperature='Hot', final_temperature='RoomTemp')
                    self.assertIs(caught.exception, original)
                    report = self.runtime.execution_report
                    self.assertEqual(report['action_counts']['succeeded'], 0)
                    self.assertEqual(report['action_counts']['failed'], 2)
                    self.assertEqual(report['actions'][1]['reason'], 'effects_unknown')
                    self.assertEqual(report['actions'][1]['effects'], [])
                    self.assertFalse(report['stages'][0]['snapshot_is_current'])
                    self.assertEqual(self.runtime.state_version, 1)
                    self.assertEqual(self.runtime.objects[0]['temperature'], 'RoomTemp')
                    self.assertEqual(seen, [])
                    self.assertEqual(self.runtime.action_resource_manager.holders(), {})
