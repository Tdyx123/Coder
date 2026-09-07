"""Main-thread action admission, shared snapshots and bounded robot workers."""
import queue
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Optional

from .action_plan import (
    ACTION_SUCCESS, Action, ActionResult, ExecutionLogger, WorldState,
    ROBOT_EXECUTING, ROBOT_FINISHED_STAGE,
)
from .execution_control import (
    ExecutionCancelled, PlanExecutionTimeout, error_record,
    raise_if_execution_aborted, run_workers,
)
from .execution_policy import (
    ConditionEvaluationError, ExecutionPolicy, FailureDecision, StageFailureDecisionError, StageOutcome,
    evaluate_condition, evaluate_conditions, conditions_evidence, conditions_satisfied,
    resolve_stage_outcome, snapshot_report,
)
from .executor import Executor, PhaseCoordinator
from .movement import NavigationDeferred
from .world_snapshot import SnapshotStore
from .action_resources import (manager_for, resolve_action_resources,
                               action_resource_scope, ResourceBindingDeferred)


_IDLE_TICK = object()


@dataclass(frozen=True)
class PendingAction:
    robot_id: str
    cursor: int
    action: Action


@dataclass(frozen=True)
class RobotAdmission:
    status: str
    pending: Optional[PendingAction] = None
    reason: Optional[str] = None


class StageScheduler:
    IDLE_TICK = _IDLE_TICK
    def __init__(self, runtime, stage, *, control, policy, stats=None,
                 logger=None, stage_index=0, executors=None, coordinator=None):
        self.runtime, self.stage, self.control = runtime, stage, control
        self.policy = ExecutionPolicy(policy)
        self.stats, self.stage_index = stats, stage_index
        self.logger = logger or ExecutionLogger()
        self.coordinator = coordinator or PhaseCoordinator(
            runtime, [runtime.physical_agent_id(robot) for robot in stage.robot_action_queues],
            control=control,
        )
        self.coordinator.control = control
        self.coordinator.deadline = control.deadline
        self.coordinator.scheduler_admissions = True
        self.condition = self.coordinator.condition
        self.admissions = {}
        self.executors = {}
        self.mailboxes = {}
        self.wait_rounds = {}
        self.resource_manager = manager_for(runtime)
        self.resource_leases = {}
        self.resource_requests = {}
        self.results = queue.Queue()
        self.inflight = set()
        self.world = WorldState(runtime)
        self.snapshot_store = SnapshotStore()
        self._world_changed = True
        self._errors = []
        self._final_snapshot_version = None
        self.retry_after = {}
        self.action_records = {}
        self.attempt_counts = {}
        self.precondition_evidence = {}
        self.report = {'stage_id': stage.stage_id, 'stage_index': stage_index,
                       'actions': [], 'attempts': [], 'waits': [], 'waves': [],
                       'resources': [], 'condition': {'initial': None, 'final': None}}
        self.outcome = None
        self._tick = 0
        self._last_pass = time.monotonic()
        self._tick_inflight = False
        for robot, actions in stage.robot_action_queues.items():
            kwargs = dict(stage_id=stage.stage_id, stage_index=stage_index,
                          logger=self.logger, phase_coordinator=self.coordinator,
                          execution_policy=self.policy)
            if executors is not None:
                executor = executors[robot]
                executor.phase_coordinator = self.coordinator
                executor.control = control
            elif stats is None:
                executor = Executor(runtime, robot, actions, **kwargs)
            else:
                from .parallel_runner import TolerantExecutor
                executor = TolerantExecutor(runtime, robot, actions, stats=stats,
                                            deadline=control.deadline, **kwargs)
            executor.scheduler = self
            self.executors[robot] = executor
            self.mailboxes[robot] = queue.Queue()
            self.wait_rounds[robot] = 0
            self._set_pending(executor)

    def notify_world_changed(self):
        # Runtime calls this after releasing its controller lock.
        with self.condition:
            self._world_changed = True
            self.condition.notify_all()

    def _set_pending(self, executor):
        robot, state = executor.robot_id, executor.state
        if state.finished():
            last = state.last_action_result
            status = 'FAILED' if last is not None and last.failure_decision == 'fail_robot' else 'FINISHED'
            self.admissions[robot] = RobotAdmission(status)
            if status != 'FAILED':
                state.status = ROBOT_FINISHED_STAGE
            self.coordinator.mark_agent_done(self.runtime.physical_agent_id(robot))
        else:
            self.admissions[robot] = RobotAdmission(
                'READY', PendingAction(robot, state.action_cursor, state.next_action()))

    def execute_worker(self, executor):
        """Compatibility hook; Executor owns the single robot worker loop."""
        return executor._execute_queue()

    def _condition_ready(self, pending):
        conditions = list(pending.action.expected_preconditions)
        if pending.action.wait_until is not None:
            conditions.insert(0, pending.action.wait_until)
        evidence = conditions_evidence(conditions, self.world)
        self.precondition_evidence[self._ledger_key(pending)] = evidence
        return conditions_satisfied(evidence) is True

    def _admit_resources(self, pending):
        key = self._ledger_key(pending)
        try:
            resolved = self.resource_requests.get(key)
            if resolved is None or pending.action.on_conflict == 'RETRY_NEXT_TICK':
                resolved = resolve_action_resources(self.runtime, self.world.snapshot,
                                                    pending.robot_id, pending.action)
                self.resource_requests[key] = resolved
            lease = self.resource_manager.try_acquire(key, resolved.keys)
            if lease is not None:
                self.resource_leases[key] = lease
                self.report['resources'].append({'event': 'acquired', 'action_key': key,
                    'keys': list(resolved.keys), 'world_version': self.world.version})
                return True
            blockers = self.resource_manager.blockers(resolved.keys)
            reason = f'resources held by {blockers}'
            if pending.action.on_conflict == 'FAIL_STAGE':
                result = ActionResult(pending.robot_id, pending.action, 'FAILED',
                                      error_message=reason, failure_decision='fail_stage',
                                      failure_error_code='resource_conflict')
                self.executors[pending.robot_id].state.last_action_result = result
                self.logger.result(self._tick, result)
                self._record_attempt(pending)
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(key, 'failed')
                if self.stats is not None:
                    self.stats.record_failure(self.stage.stage_id, pending.robot_id, pending.action,
                        pending.cursor, RuntimeError(reason),
                        decision=FailureDecision('fail_stage', 'resource_conflict', 0))
                self._observe_result(pending, result)
                raise StageFailureDecisionError(f'RESOURCE_CONFLICT: {reason}')
            if pending.action.on_conflict == 'SKIP':
                executor = self.executors[pending.robot_id]
                result = ActionResult(pending.robot_id, pending.action, 'SKIPPED',
                                      error_message=reason, failure_decision='skip',
                                      failure_error_code='resource_conflict')
                executor.state.last_action_result = result
                executor.state.action_cursor += 1
                executor.state.wait_ticks = 0
                self.wait_rounds[pending.robot_id] = 0
                self.logger.result(self._tick, result)
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(key, 'skipped', started=False)
                self._observe_result(pending, result)
                self._release_resources(pending)
                self._set_pending(executor)
                return False
            self.admissions[pending.robot_id] = RobotAdmission('WAITING_RESOURCE', pending, reason)
            state = self.executors[pending.robot_id].state
            if pending.action.timeout_ticks is not None and state.wait_ticks >= pending.action.timeout_ticks:
                raise RuntimeError(f'RESOURCE_TIMEOUT: {reason}')
        except StageFailureDecisionError:
            raise
        except Exception as exc:
            raise_if_execution_aborted(self.runtime, exc)
            self._release_resources(pending)
            self._record_attempt(pending)
            self._record_failure(self.executors[pending.robot_id], pending, exc)
            self._set_pending(self.executors[pending.robot_id])
        return False

    def _release_resources(self, pending):
        key = self._ledger_key(pending)
        lease = self.resource_leases.pop(key, None)
        self.resource_requests.pop(key, None)
        if lease is not None:
            self.report['resources'].append({'event': 'released', 'action_key': key,
                                             'world_version': self.world.version})
            lease.release()
            self.notify_world_changed()

    def _observe_result(self, pending, result):
        if result is None:
            return
        result.attempts = self.attempt_counts.get(self._ledger_key(pending), result.attempts)
        record = {'action_key': self._ledger_key(pending), 'stage_id': self.stage.stage_id,
                  'robot_id': pending.robot_id, 'cursor': pending.cursor,
                  'action_type': pending.action.action_type, 'status': result.status.lower(),
                  'reason': result.failure_error_code or result.error_message or 'action_completed',
                  'error': result.error_message, 'attempts': result.attempts,
                  'requested_failure_policy': pending.action.on_failure,
                  'failure_decision': result.failure_decision,
                  'world_version': self.world.version,
                  'preconditions': self.precondition_evidence.get(self._ledger_key(pending), []),
                  'effects': getattr(self.executors[pending.robot_id], 'last_effects_evidence', [])}
        if record['status'] == 'success':
            record['status'] = 'succeeded'
        self.report['attempts'].append(dict(record))
        if result.failure_decision not in ('retry', 'wait_retry'):
            if self._ledger_key(pending) not in self.action_records:
                self.world.tick += 1
            self.action_records[self._ledger_key(pending)] = record

    def _record_failure(self, executor, pending, exc, *, finalizing=False):
        executor.world_state.snapshot = self.world.snapshot
        executor.world_state.tick = self.world.tick
        executor.state.last_action_result = None
        try:
            if finalizing:
                executor.handle_failure(pending.action, exc, self._tick, finalizing=True,
                                        final_snapshot_current=self._has_current_final_snapshot())
            else:
                executor.handle_failure(pending.action, exc, self._tick)
        finally:
            result = executor.state.last_action_result
            self._observe_result(pending, result)
            if result is not None and result.status != ACTION_SUCCESS and result.failure_decision not in ('retry', 'wait_retry'):
                self._errors.append(error_record(exc, phase=self.stage.stage_id,
                                                 robot_id=pending.robot_id))

    def _receive_results(self):
        changed = False
        while True:
            try:
                pending, event, exc = self.results.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            if pending is None:
                self._tick_inflight = False
                self._last_pass = time.monotonic()
                for robot, admission in self.admissions.items():
                    if admission.status in ('WAITING_CONDITION', 'WAITING_RESOURCE'):
                        self.executors[robot].state.wait_ticks += 1
                self.notify_world_changed()
                continue
            robot = pending.robot_id
            executor = self.executors[robot]
            if executor.world_state.version > self.world.version:
                self.world.snapshot = executor.world_state.snapshot
            self.inflight.remove(robot)
            if isinstance(exc, (NavigationDeferred, ResourceBindingDeferred)):
                self.report['attempts'].append({'action_key': self._ledger_key(pending),
                    'status': 'deferred', 'reason': type(exc).__name__, 'error': str(exc),
                    'world_version': self.world.version})
                if self.stats is not None:
                    self.stats.record_deferred()
            elif exc is not None:
                self._record_failure(executor, pending, exc)
            else:
                result = ActionResult(robot, pending.action, ACTION_SUCCESS, event=event)
                executor.state.last_action_result = result
                executor.state.action_cursor += 1
                executor.state.wait_ticks = 0
                self.logger.result(self._tick, result)
                self._observe_result(pending, result)
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(self._ledger_key(pending), 'succeeded')
            self._release_resources(pending)
            self._tick += 1
            self._set_pending(executor)

    def _ledger_key(self, pending):
        return f'{self.stage_index}:{pending.robot_id}:{pending.cursor}'

    def _record_attempt(self, pending):
        """Count worker admissions and failed admissions in every report alike."""
        key = self._ledger_key(pending)
        self.attempt_counts[key] = self.attempt_counts.get(key, 0) + 1
        if self.stats is not None:
            self.stats.record_started()
        ledger = getattr(self.runtime, 'action_ledger', None)
        if ledger is not None:
            ledger.record_started(key)
            ledger.record_attempt()

    def _select_ready(self):
        # Admission rounds continue while peers make progress. Waiting age is
        # independent of timeout_ticks, which count completed idle Passes only.
        for robot, admission in self.admissions.items():
            if admission.status in ('WAITING_CONDITION', 'WAITING_RESOURCE'):
                self.wait_rounds[robot] += 1
        ready = []
        for robot, admission in tuple(self.admissions.items()):
            if admission.status in ('EXECUTING', 'FINISHED', 'FAILED'):
                continue
            pending = admission.pending
            if self.retry_after.get(robot, 0) > time.monotonic():
                self.admissions[robot] = RobotAdmission('WAITING_CONDITION', pending, 'wait_retry')
                continue
            if not self._condition_ready(pending):
                state = self.executors[robot].state
                self.admissions[robot] = RobotAdmission('WAITING_CONDITION', pending,
                                                       'precondition_or_wait_until_not_satisfied')
                if pending.action.timeout_ticks is not None and state.wait_ticks >= pending.action.timeout_ticks:
                    self._record_attempt(pending)
                    self._record_failure(self.executors[robot], pending, RuntimeError(
                        f'{robot} timed out waiting for {pending.action.action_type}.'))
                    self._set_pending(self.executors[robot])
                continue
            self.admissions[robot] = RobotAdmission('READY', pending)
            ready.append(pending)
        ready.sort(key=lambda p: (-p.action.base_priority - self.wait_rounds[p.robot_id] * 10
                                 - (50 if p.action.critical else 0), p.robot_id, p.cursor))
        return ready

    def _dispatch(self, ready):
        admitted = []
        for pending in ready:
            if self._admit_resources(pending):
                admitted.append(pending)

        if not admitted:
            return False
        wave = self.coordinator.admit_wave({
            self.runtime.physical_agent_id(p.robot_id): (p.action.action_type, p.cursor)
            for p in admitted
        })
        self.report['waves'].append({'wave_id': wave.wave_id,
            'robots': [p.robot_id for p in admitted],
            'navigation_agent_ids': list(wave.navigation_agent_ids),
            'action_keys': [self._ledger_key(p) for p in admitted]})
        for pending in admitted:
            self.control.check()
            robot = pending.robot_id
            self.wait_rounds[robot] = 0
            self.executors[robot].state.status = ROBOT_EXECUTING
            self.admissions[robot] = RobotAdmission('EXECUTING', pending)
            self.inflight.add(robot)
            self._record_attempt(pending)
            self.mailboxes[robot].put((pending, wave, self.world.snapshot, self.world.tick))
        return True

    def _diagnose(self):
        for robot, admission in self.admissions.items():
            if admission.status in ('WAITING_CONDITION', 'WAITING_RESOURCE'):
                record = {'robot_id': robot, 'status': admission.status,
                          'reason': admission.reason, 'world_version': self.world.version,
                          'cursor': admission.pending.cursor,
                          'wait_ticks': self.executors[robot].state.wait_ticks,
                          'resource_holders': self.resource_manager.holders()}
                if not self.report['waits'] or self.report['waits'][-1] != record:
                    self.report['waits'].append(record)
        self.runtime.scheduler_diagnostics = {
            'stage_id': self.stage.stage_id, 'world_version': self.world.version,
            'robots': {robot: {
                'status': admission.status, 'reason': admission.reason,
                'cursor': admission.pending.cursor if admission.pending else None,
                'action': admission.pending.action.action_type if admission.pending else None,
                'wait_rounds': self.wait_rounds[robot],
            } for robot, admission in self.admissions.items()},
            'resource_holders': self.resource_manager.holders(),
        }

    def _drive(self):
        while True:
            self._diagnose()
            self.control.check()
            completed = self._receive_results()
            if all(a.status in ('FINISHED', 'FAILED') for a in self.admissions.values()):
                for mailbox in self.mailboxes.values():
                    mailbox.put(None)
                return
            with self.condition:
                dirty = self._world_changed
                self._world_changed = False
            each_step_blocked = self.stage.synchronization_policy == 'BARRIER_EACH_STEP' and self.inflight
            evaluate = (self.stage.synchronization_policy != 'EVENT_CONDITION' or dirty or completed)
            has_pending = any(a.status not in ('EXECUTING', 'FINISHED', 'FAILED')
                              for a in self.admissions.values())
            if not self._tick_inflight and not each_step_blocked and evaluate and has_pending:
                self.world.snapshot = self.snapshot_store.capture(self.runtime, self.control)
                ready = self._select_ready()
                if self._dispatch(ready):
                    continue
            if not self.inflight and not self._tick_inflight:
                remaining = .05 - (time.monotonic() - self._last_pass)
                if remaining <= 0:
                    self.control.check()
                    # Reuse a supervised robot target: a hung controller call
                    # must remain covered by run_workers' actual-exit proof.
                    self._tick_inflight = True
                    self.mailboxes[min(self.mailboxes)].put(_IDLE_TICK)
                    continue
            else:
                remaining = .05
            with self.condition:
                # Do not lose a result or notification between evaluation and wait.
                if not self.results.empty() or self._world_changed:
                    continue
                self.condition.wait(timeout=max(0, min(.05, remaining)))

    def _has_current_final_snapshot(self):
        return (self._final_snapshot_version is not None
                and self._final_snapshot_version == self.world.version
                and self.world.version == getattr(self.runtime, 'state_version', None))

    def _harvest_completed_results(self):
        """Preserve returned outcomes after quiescence without resuming retries."""
        while True:
            try:
                pending, event, exc = self.results.get_nowait()
            except queue.Empty:
                return
            if pending is None or self._ledger_key(pending) in self.action_records:
                continue
            executor = self.executors[pending.robot_id]
            if isinstance(exc, (NavigationDeferred, ResourceBindingDeferred)):
                self.report['attempts'].append({'action_key': self._ledger_key(pending),
                    'status': 'deferred', 'reason': type(exc).__name__, 'error': str(exc),
                    'world_version': executor.world_state.version})
                if self.stats is not None:
                    self.stats.record_deferred()
                # This attempt yielded instead of completing its logical action.
                continue
            if exc is not None:
                self._record_failure(executor, pending, exc, finalizing=True)
            else:
                result = ActionResult(pending.robot_id, pending.action, ACTION_SUCCESS, event=event)
                executor.state.last_action_result = result
                executor.state.action_cursor = pending.cursor + 1
                self.logger.result(self._tick, result)
                self._observe_result(pending, result)
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(self._ledger_key(pending), 'succeeded')
            self.inflight.discard(pending.robot_id)
            self._release_resources(pending)
            self._set_pending(executor)

    def _tail_records(self, reason, *, skipped=False):
        ledger = getattr(self.runtime, 'action_ledger', None)
        for robot, executor in self.executors.items():
            last = executor.state.last_action_result
            robot_failed = last is not None and last.failure_decision == 'fail_robot'
            for cursor, action in enumerate(executor.state.action_queue):
                key = f'{self.stage_index}:{robot}:{cursor}'
                if key in self.action_records:
                    continue
                admitted = robot in self.inflight and self.admissions[robot].pending.cursor == cursor
                status = 'skipped' if skipped else 'cancelled' if admitted else 'unexecuted'
                tail_reason = 'robot_failed' if robot_failed else reason
                self.action_records[key] = {'action_key': key, 'stage_id': self.stage.stage_id,
                    'robot_id': robot, 'cursor': cursor, 'action_type': action.action_type,
                    'status': status, 'reason': tail_reason, 'attempts': self.attempt_counts.get(key, 0),
                    'requested_failure_policy': action.on_failure}
                if ledger is not None and status in ('skipped', 'cancelled'):
                    ledger.record_terminal(key, status, started=admitted)
            if skipped:
                executor.state.action_cursor = len(executor.state.action_queue)
                self._set_pending(executor)

    def run(self):
        previous = getattr(self.runtime, 'stage_scheduler', None)
        self.runtime.stage_scheduler = self
        tail_reason = 'queue_not_executed'
        try:
            self.world.snapshot = self.snapshot_store.capture(self.runtime, self.control)
            condition = self.stage.stage_success_condition
            initial = None if condition is None else evaluate_condition(condition, self.world)
            self.report['condition']['initial'] = initial
            if initial is True:
                self._tail_records('stage_condition_already_satisfied', skipped=True)
            else:
                run_workers(self.runtime, list(self.executors.values()), self.coordinator,
                            self.stage.stage_id, drive=self._drive)
            self.world.snapshot = self.snapshot_store.capture(self.runtime, self.control)
            self._final_snapshot_version = self.world.version
            final = None if condition is None else evaluate_condition(condition, self.world)
            self.report['condition']['final'] = final
            if final is False:
                self._errors.append({'phase': self.stage.stage_id, 'robot_id': None,
                    'exception_type': 'StageConditionUnsatisfied',
                    'message': 'stage_success_condition is false'})
                tail_reason = 'stage_condition_unsatisfied'
            failed_robot = any(a.status == 'FAILED' for a in self.admissions.values())
            base = StageOutcome('failed' if failed_robot else 'partial' if self._errors else 'completed',
                                True, tuple(self._errors), self.world.snapshot)
            self.outcome = resolve_stage_outcome(self.policy, self.stage, [base], final)
        except (StageFailureDecisionError, ConditionEvaluationError) as exc:
            root = getattr(self.runtime, 'execution_control', None)
            if root is not None and not root.cancelled:
                self.world.snapshot = self.snapshot_store.capture(self.runtime, root)
                self._final_snapshot_version = self.world.version
            errors = tuple(self._errors) + (error_record(exc, phase=self.stage.stage_id),)
            tail_reason = 'condition_evaluation_error' if isinstance(exc, ConditionEvaluationError) else 'stage_failed'
            self.outcome = resolve_stage_outcome(self.policy, self.stage,
                [StageOutcome('failed', False, errors, self.world.snapshot)], None)
            if isinstance(exc, ConditionEvaluationError):
                if root is not None:
                    root.cancel(str(exc))
                raise
            if self.stage.stage_success_condition is not None:
                try:
                    self.report['condition']['final'] = evaluate_condition(self.stage.stage_success_condition, self.world)
                except ConditionEvaluationError as condition_error:
                    if root is not None:
                        root.cancel(str(condition_error))
                    self.outcome = StageOutcome('failed', False, errors + (error_record(condition_error, phase=self.stage.stage_id),), self.world.snapshot)
                    raise
        except BaseException as exc:
            status = 'timeout' if isinstance(exc, PlanExecutionTimeout) else 'cancelled' if isinstance(exc, ExecutionCancelled) else 'failed'
            tail_reason = status
            self.outcome = StageOutcome(status, False, (error_record(exc, phase=self.stage.stage_id),), self.world.snapshot)
            raise
        finally:
            primary_error = sys.exc_info()[1]
            finalization_error = None
            if getattr(self.runtime, 'execution_quiescent', True):
                while True:
                    try:
                        self._harvest_completed_results()
                        break
                    except BaseException as exc:
                        # The failing result was already dequeued. Preserve its
                        # cause and continue draining before tail/lease cleanup.
                        self._errors.append(error_record(exc, phase=self.stage.stage_id))
                        finalization_error = finalization_error or exc
                if finalization_error is not None and primary_error is None:
                    tail_reason = 'condition_evaluation_error'
                    self.outcome = StageOutcome('failed', False, tuple(self._errors), self.world.snapshot)
                if self.outcome is not None:
                    errors = tuple(self._errors) + tuple(error for error in self.outcome.errors if error not in self._errors)
                    self.outcome = replace(self.outcome, errors=errors)
            self._diagnose()
            self._tail_records(tail_reason)
            self.report['actions'] = [self.action_records[key] for key in sorted(self.action_records,
                key=lambda key: (key.split(':')[1], int(key.split(':')[-1])))]
            if self.outcome is not None:
                self.report.update(status=self.outcome.status, continue_task=self.outcome.continue_task,
                    errors=list(self.outcome.errors), world_version=self.world.version)
            self.report['snapshot'] = snapshot_report(self.world.snapshot)
            self.report['snapshot_is_current'] = self._has_current_final_snapshot()
            self.report['diagnostics'] = self.runtime.scheduler_diagnostics
            self.runtime.stage_scheduler = previous
            if getattr(self.runtime, 'execution_quiescent', True):
                for key, lease in list(self.resource_leases.items()):
                    lease.release()
                    self.report['resources'].append({'event': 'released', 'action_key': key,
                                                     'world_version': self.world.version})
                    self.resource_leases.pop(key, None)
                    self.resource_requests.pop(key, None)
            if finalization_error is not None:
                root = getattr(self.runtime, 'execution_control', None)
                if root is not None:
                    root.cancel(str(finalization_error))
                if primary_error is None:
                    raise finalization_error
        return self.outcome
