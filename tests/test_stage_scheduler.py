"""Main-thread admission regressions; all fake waits have explicit bounds."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.action_plan import Action, AI2ThorAdapter, StagePlan, TaskPlan
from executor_system.movement import MovementConfig
from executor_system.parallel_runner import run_action_plan_tolerant
from tests.snapshot_fakes import FakeRuntime


class StageSchedulerTest(unittest.TestCase):
    def run_bounded(self, runtime, plan, execute, timeout=0.35):
        result, errors = [], []
        def run():
            try:
                result.append(run_action_plan_tolerant(runtime, plan, timeout_seconds=timeout))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(AI2ThorAdapter, 'execute', execute):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout + 2)
        self.assertFalse(thread.is_alive(), 'scheduler failed bounded shutdown')
        if errors:
            raise errors[0]
        return result[0]

    def test_waiting_robot_does_not_block_dependency(self):
        for mode in ('step', 'teleport'):
            with self.subTest(mode=mode):
                runtime = FakeRuntime()
                runtime.movement_config = MovementConfig.resolve(mode)
                ready = threading.Event()
                plan = TaskPlan('dependency', [StagePlan('s', {
                    'robot1': [Action('Wait', wait_until=lambda world: ready.is_set())],
                    'robot2': [Action('Wait')],
                })])
                def execute(adapter, robot_id, action, **kwargs):
                    if robot_id == 'robot2':
                        ready.set()
                report = self.run_bounded(runtime, plan, execute)
                self.assertFalse(report['timed_out'])
                self.assertTrue(ready.is_set())

    def test_synchronization_policies_have_observable_order(self):
        for policy in ('BARRIER_AT_STAGE_END', 'BARRIER_EACH_STEP', 'EVENT_CONDITION'):
            with self.subTest(policy=policy):
                runtime = FakeRuntime()
                runtime.movement_config = MovementConfig.resolve('step')
                first_started, second_started = threading.Event(), threading.Event()
                order = []
                def execute(adapter, robot_id, action, **kwargs):
                    tag = action.action_id
                    order.append(tag + ':start')
                    if tag == 'slow':
                        first_started.set()
                        second_started.wait(0.12)
                    elif tag == 'first':
                        self.assertTrue(first_started.wait(0.2))
                    elif tag == 'second':
                        second_started.set()
                    order.append(tag + ':end')
                stage = StagePlan('s', {
                    'robot1': [Action('Wait', action_id='first'), Action('Wait', action_id='second')],
                    'robot2': [Action('Wait', action_id='slow')],
                }, synchronization_policy=policy)
                report = self.run_bounded(runtime, TaskPlan('order', [stage]), execute, timeout=1)
                self.assertFalse(report['timed_out'])
                if policy == 'BARRIER_EACH_STEP':
                    self.assertLess(order.index('slow:end'), order.index('second:start'))
                else:
                    self.assertLess(order.index('second:start'), order.index('slow:end'))

    def test_idle_dependency_timeout_saves_diagnostics_and_rate_limits_pass(self):
        runtime = FakeRuntime()
        passes = []
        original = runtime.step
        def step(payload, **kwargs):
            passes.append((time.monotonic(), threading.current_thread().name))
            return original(payload, **kwargs)
        runtime.step = step
        stage = StagePlan('cycle', {
            'robot1': [Action('Wait', wait_until=lambda world: False)],
            'robot2': [Action('Wait', wait_until=lambda world: False)],
        })
        report = self.run_bounded(runtime, TaskPlan('cycle', [stage]), lambda *a, **kw: None, timeout=0.18)
        self.assertTrue(report['timed_out'])
        diagnostic = runtime.scheduler_diagnostics
        self.assertEqual(set(diagnostic['robots']), {'robot1', 'robot2'})
        self.assertTrue(all(item['status'] == 'WAITING_CONDITION' for item in diagnostic['robots'].values()))
        self.assertLessEqual(len(passes), 4)
        self.assertTrue(all(b[0] - a[0] >= .045 for a, b in zip(passes, passes[1:])))
        self.assertTrue(runtime.execution_quiescent)

    def test_admitted_navigation_excludes_waiter_but_keeps_physical_obstacle(self):
        from types import SimpleNamespace
        class ThreeRobots(FakeRuntime):
            physical_agent_count = 3
        runtime = ThreeRobots()
        runtime.movement_config = MovementConfig.resolve('step')
        done = threading.Event()
        batches = []
        def execute(adapter, robot, action, **kwargs):
            if action.action_type != 'GoToObject':
                return
            coordinator, wave = kwargs['phase_coordinator'], kwargs['action_wave']
            request = SimpleNamespace(agent_id=runtime.physical_agent_id(robot))
            def batch(requests, completed):
                batches.append(([r.agent_id for r in requests], completed))
                done.set()
                return {r.agent_id: object() for r in requests}
            coordinator.submit_step_navigation(wave, request, batch)
        plan = TaskPlan('navigation', [StagePlan('s', {
            'robot1': [Action('Wait', wait_until=lambda world: done.is_set())],
            'robot2': [Action('GoToObject', {'args': ('Target',)})],
            'robot3': [Action('GoToObject', {'args': ('Target',)})],
        })])
        report = self.run_bounded(runtime, plan, execute)
        self.assertFalse(report['timed_out'])
        self.assertEqual(batches, [([1, 2], frozenset())])

    def test_late_navigation_uses_next_wave_without_waiting_for_prior_high_level_action(self):
        from types import SimpleNamespace
        runtime = FakeRuntime()
        runtime.movement_config = MovementConfig.resolve('step')
        first_started, late_started = threading.Event(), threading.Event()
        waves = {}
        def execute(adapter, robot, action, **kwargs):
            if action.action_type == 'Wait':
                self.assertTrue(first_started.wait(.2))
                return
            coordinator, wave = kwargs['phase_coordinator'], kwargs['action_wave']
            waves[robot] = wave
            agent_id = runtime.physical_agent_id(robot)
            def batch(requests, completed):
                if robot == 'robot1':
                    first_started.set()
                    self.assertTrue(late_started.wait(.2))
                else:
                    late_started.set()
                return {agent_id: object()}
            coordinator.submit_step_navigation(wave, SimpleNamespace(agent_id=agent_id), batch)
        plan = TaskPlan('late', [StagePlan('s', {
            'robot1': [Action('GoToObject', {'args': ('Target',)})],
            'robot2': [Action('Wait'), Action('GoToObject', {'args': ('Target',)})],
        })])
        # The first non-navigation action waits for its own wave's navigation,
        # so make robot2's first action condition-ready only after wave 1 starts.
        plan.stages[0].robot_action_queues['robot2'][0] = Action('Wait', wait_until=lambda world: first_started.is_set())
        report = self.run_bounded(runtime, plan, execute, timeout=1)
        self.assertFalse(report['timed_out'])
        self.assertTrue(late_started.is_set())
        self.assertNotEqual(waves['robot1'].wave_id, waves['robot2'].wave_id)
        self.assertEqual(waves['robot1'].navigation_agent_ids, (0,))
        self.assertEqual(waves['robot2'].navigation_agent_ids, (1,))

    def test_member_failure_wakes_batch_and_deferred_request_keeps_cursor(self):
        from types import SimpleNamespace
        from executor_system.movement import NavigationBatchResult
        runtime = FakeRuntime()
        runtime.movement_config = MovementConfig.resolve('step')
        batches, observed = [], []
        def execute(adapter, robot, action, **kwargs):
            coordinator, wave = kwargs['phase_coordinator'], kwargs['action_wave']
            agent_id = runtime.physical_agent_id(robot)
            observed.append((robot, action.action_id))
            def batch(requests, completed):
                batches.append(tuple(r.agent_id for r in requests))
                if len(batches) == 1:
                    return NavigationBatchResult({}, failed_agent_errors={0: RuntimeError('selected failed')}, deferred_agent_ids=frozenset({1}))
                return {1: object()}
            coordinator.submit_step_navigation(wave, SimpleNamespace(agent_id=agent_id), batch)
        stage = StagePlan('s', {
            'robot1': [Action('GoToObject', {'args': ('Target',)}, action_id='a')],
            'robot2': [Action('GoToObject', {'args': ('Target',)}, action_id='b')],
        })
        report = self.run_bounded(runtime, TaskPlan('defer', [stage]), execute)
        self.assertFalse(report['timed_out'])
        self.assertEqual(batches, [(0, 1), (1,)])
        self.assertEqual([action for robot, action in observed if robot == 'robot2'], ['b', 'b'])

    def test_pre_submission_navigation_failure_wakes_other_admitted_member(self):
        from types import SimpleNamespace
        runtime = FakeRuntime()
        runtime.movement_config = MovementConfig.resolve('step')
        submitted = threading.Event()
        def execute(adapter, robot, action, **kwargs):
            if robot == 'robot1':
                self.assertTrue(submitted.wait(.2))
                raise RuntimeError('request construction failed')
            submitted.set()
            return kwargs['phase_coordinator'].submit_step_navigation(
                kwargs['action_wave'], SimpleNamespace(agent_id=1), lambda *a: self.fail('incomplete batch ran'))
        stage = StagePlan('s', {robot: [Action('GoToObject', {'args': ('Target',)})]
                                for robot in ('robot1', 'robot2')})
        report = self.run_bounded(runtime, TaskPlan('failure', [stage]), execute)
        self.assertFalse(report['timed_out'])
        self.assertEqual(report['action_counts']['failed'], 2)

    def test_event_condition_only_rechecks_on_events_while_work_runs(self):
        samples = {}
        for policy in ('BARRIER_AT_STAGE_END', 'EVENT_CONDITION'):
            runtime = FakeRuntime()
            calls, ready = [], threading.Event()
            def condition(world):
                calls.append(threading.current_thread().name)
                return ready.is_set()
            def execute(adapter, robot, action, **kwargs):
                if robot == 'robot2':
                    ready.wait(.16)
                    samples[policy] = len(calls)
                    ready.set()
            stage = StagePlan('s', {'robot1': [Action('Wait', wait_until=condition)],
                                    'robot2': [Action('Wait')]}, synchronization_policy=policy)
            self.run_bounded(runtime, TaskPlan('events', [stage]), execute, timeout=1)
        self.assertGreater(samples['BARRIER_AT_STAGE_END'], samples['EVENT_CONDITION'])
        self.assertEqual(samples['EVENT_CONDITION'], 1)

    def test_snapshot_lock_wait_respects_deadline(self):
        from executor_system.execution_control import ExecutionControl, PlanExecutionTimeout
        from executor_system.world_snapshot import SnapshotStore
        runtime = FakeRuntime()
        runtime.controller_lock = threading.Lock()
        entered, release = threading.Event(), threading.Event()
        def holder():
            with runtime.controller_lock:
                entered.set()
                release.wait(.25)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(.2))
        started = time.monotonic()
        try:
            with self.assertRaises(PlanExecutionTimeout):
                SnapshotStore().capture(runtime, ExecutionControl(started + .04))
            self.assertLess(time.monotonic() - started, .15)
        finally:
            release.set()
            thread.join(.5)
        self.assertFalse(thread.is_alive())

    def test_conditions_and_admission_order_belong_to_scheduler_thread(self):
        from executor_system.execution_control import install_control
        from executor_system.execution_policy import ExecutionPolicy
        from executor_system.stage_scheduler import StageScheduler
        class ThreeRobots(FakeRuntime):
            physical_agent_count = 3
        runtime = ThreeRobots()
        thread_names, selected = [], []
        def condition(world):
            thread_names.append(threading.current_thread().ident)
            return True
        stage = StagePlan('order', {
            'robot2': [Action('WaitUntil', wait_until=condition, base_priority=5)],
            'robot1': [Action('WaitUntil', wait_until=condition, base_priority=5)],
            'robot3': [Action('WaitUntil', wait_until=condition, critical=True)],
        })
        scheduler = StageScheduler(runtime, stage, control=install_control(runtime, 1).child(),
                                   policy=ExecutionPolicy.LEGACY)
        def admit(pending):
            selected.append(pending.robot_id)
            return True
        scheduler._admit_resources = admit
        outcome = scheduler.run()
        self.assertEqual(outcome.status, 'completed')
        self.assertEqual(selected, ['robot3', 'robot1', 'robot2'])
        self.assertEqual(set(thread_names), {threading.current_thread().ident})

    def test_cancelled_admission_cannot_create_new_wave(self):
        from executor_system.execution_control import ExecutionControl, ExecutionCancelled
        from executor_system.executor import PhaseCoordinator
        runtime = FakeRuntime()
        control = ExecutionControl()
        phase = PhaseCoordinator(runtime, (0, 1), control=control)
        control.cancel('stage stopped')
        with self.assertRaises(ExecutionCancelled):
            phase.admit_wave({0: ('GoToObject', 0)})
        self.assertEqual(phase._admitted_waves, {})

    def test_policy_stage_failure_returns_outcome_and_leaves_parent_live(self):
        from executor_system.execution_control import install_control
        from executor_system.execution_policy import ExecutionPolicy
        from executor_system.stage_scheduler import StageScheduler
        runtime = FakeRuntime()
        root = install_control(runtime, 1)
        stage = StagePlan('fail', {'robot1': [Action('Wait')]})
        scheduler = StageScheduler(runtime, stage, control=root.child(), policy=ExecutionPolicy.STRICT)
        with patch.object(AI2ThorAdapter, 'execute', side_effect=RuntimeError('action rejected')):
            outcome = scheduler.run()
        self.assertEqual(outcome.status, 'failed')
        self.assertFalse(outcome.continue_task)
        self.assertIn('action rejected', outcome.errors[0]['message'])
        self.assertIsNotNone(outcome.snapshot)
        self.assertTrue(scheduler.control.cancelled)
        self.assertFalse(root.cancelled)

    def test_world_notification_wakes_condition_before_current_action_ends(self):
        runtime = FakeRuntime()
        changed, waiter_started = threading.Event(), threading.Event()
        order = []
        def execute(adapter, robot, action, **kwargs):
            if robot == 'robot2':
                changed.set()
                runtime.stage_scheduler.notify_world_changed()
                self.assertTrue(waiter_started.wait(.2))
                order.append('producer:end')
            else:
                order.append('waiter:start')
                waiter_started.set()
        stage = StagePlan('s', {
            'robot1': [Action('Wait', wait_until=lambda world: changed.is_set())],
            'robot2': [Action('Wait')],
        }, synchronization_policy='EVENT_CONDITION')
        report = self.run_bounded(runtime, TaskPlan('notify', [stage]), execute)
        self.assertFalse(report['timed_out'])
        self.assertLess(order.index('waiter:start'), order.index('producer:end'))
        self.assertIsNone(runtime.stage_scheduler)

    def test_wait_timeout_ticks_advance_only_on_coordinator_pass(self):
        runtime = FakeRuntime()
        actions = []
        stage = StagePlan('s', {'robot1': [
            Action('Wait', wait_until=lambda world: False, timeout_ticks=2),
            Action('Wait', action_id='after'),
        ]})
        report = self.run_bounded(runtime, TaskPlan('ticks', [stage]),
                                  lambda adapter, robot, action, **kwargs: actions.append(action.action_id))
        self.assertFalse(report['timed_out'])
        self.assertEqual(runtime.state_version, 2)
        self.assertEqual(actions, ['after'])
        self.assertEqual(report['action_counts']['failed'], 1)

    def test_callback_exception_preserves_cause_and_cancels_parent(self):
        from executor_system.execution_control import install_control
        from executor_system.execution_policy import ConditionEvaluationError, ExecutionPolicy
        from executor_system.stage_scheduler import StageScheduler
        runtime = FakeRuntime()
        root = install_control(runtime, 1)
        original = ValueError('condition broken')
        def broken(world):
            raise original
        stage = StagePlan('s', {'robot1': [Action('Wait', wait_until=broken)]})
        scheduler = StageScheduler(runtime, stage, control=root.child(), policy=ExecutionPolicy.LEGACY)
        with self.assertRaises(ConditionEvaluationError) as caught:
            scheduler.run()
        self.assertIs(caught.exception.__cause__, original)
        self.assertTrue(root.cancelled)
        self.assertTrue(runtime.execution_quiescent)
