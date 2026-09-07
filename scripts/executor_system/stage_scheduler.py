"""Main-thread action admission, shared snapshots and bounded robot workers."""
import queue
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
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
    ConditionEvaluationError, ExecutionPolicy, StageFailureDecisionError, StageOutcome,
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
    def __init__(self, runtime, stage, *, control, policy, stats=None,
                 logger=None, stage_index=0):
        self.runtime, self.stage, self.control = runtime, stage, control
        self.policy = ExecutionPolicy(policy)
        self.stats, self.stage_index = stats, stage_index
        self.logger = logger or ExecutionLogger()
        self.coordinator = PhaseCoordinator(
            runtime, [runtime.physical_agent_id(robot) for robot in stage.robot_action_queues],
            control=control,
        )
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
        self._tick = 0
        self._last_pass = time.monotonic()
        self._tick_inflight = False
        for robot, actions in stage.robot_action_queues.items():
            kwargs = dict(stage_id=stage.stage_id, stage_index=stage_index,
                          logger=self.logger, phase_coordinator=self.coordinator,
                          execution_policy=self.policy)
            if stats is None:
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
            state.status = ROBOT_FINISHED_STAGE
            self.coordinator.mark_agent_done(self.runtime.physical_agent_id(robot))
        else:
            self.admissions[robot] = RobotAdmission(
                'READY', PendingAction(robot, state.action_cursor, state.next_action()))

    def execute_worker(self, executor):
        """One persistent thread per robot; state transitions belong to drive()."""
        mailbox = self.mailboxes[executor.robot_id]
        while True:
            self.control.check()
            try:
                job = mailbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if job is None:
                return executor.world_state
            if job is _IDLE_TICK:
                self.control.check()
                scope = getattr(self.runtime, 'action_deadline_scope', None)
                with (scope(control=self.control) if callable(scope) else nullcontext()):
                    event = self.runtime.step(
                        {'action': 'Pass', 'agentId': self.runtime.physical_agent_id(executor.robot_id)},
                        check_success=False, save_frame=False,
                    )
                self.control.check()
                self.results.put((None, event, None))
                with self.condition:
                    self.condition.notify_all()
                continue
            pending, wave, snapshot = job
            self.control.check()
            executor.world_state.snapshot = snapshot
            agent_id = self.runtime.physical_agent_id(executor.robot_id)
            event, error = None, None
            try:
                action_wave = wave if agent_id in wave.navigation_agent_ids else None
                if action_wave is None:
                    self.coordinator.wait_admitted_navigation(wave, agent_id)
                with action_resource_scope(self.runtime, self._ledger_key(pending),
                                           self.resource_requests[self._ledger_key(pending)]):
                    event = executor.execute_action(pending.action, action_wave=action_wave)
                self.control.check()
                executor.record_temperature_goal_progress()
            except Exception as exc:
                raise_if_execution_aborted(self.runtime, exc)
                self.control.check()
                self.coordinator.abort_action_wave(wave, agent_id, exc)
                error = exc
            except BaseException as exc:
                self.coordinator.abort_action_wave(wave, agent_id, exc)
                raise
            self.results.put((pending, event, error))
            with self.condition:
                self.condition.notify_all()

    def _condition_ready(self, pending):
        condition = pending.action.wait_until
        if condition is None:
            return True
        try:
            return bool(condition(self.world))
        except Exception as exc:
            raise ConditionEvaluationError(
                f'{pending.robot_id} condition for {pending.action.action_type}: {exc}'
            ) from exc

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
                return True
            blockers = self.resource_manager.blockers(resolved.keys)
            reason = f'resources held by {blockers}'
            if pending.action.on_conflict == 'FAIL_STAGE':
                result = ActionResult(pending.robot_id, pending.action, 'FAILED',
                                      error_message=reason, failure_decision='fail_stage',
                                      failure_error_code='resource_conflict')
                self.executors[pending.robot_id].state.last_action_result = result
                self.logger.result(self._tick, result)
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(key, 'failed')
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
                    ledger.record_terminal(key, 'skipped')
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
            self._record_failure(self.executors[pending.robot_id], pending, exc)
            self._set_pending(self.executors[pending.robot_id])
        return False

    def _release_resources(self, pending):
        key = self._ledger_key(pending)
        lease = self.resource_leases.pop(key, None)
        self.resource_requests.pop(key, None)
        if lease is not None:
            lease.release()
            self.notify_world_changed()

    def _record_failure(self, executor, pending, exc):
        executor.world_state.snapshot = self.world.snapshot
        if self.stats is None:
            executor.handle_failure(pending.action, exc, self._tick)
        else:
            executor.handle_failure(pending.action, pending.cursor, exc, self._tick)
        result = executor.state.last_action_result
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
            self.inflight.remove(robot)
            self._release_resources(pending)
            if isinstance(exc, (NavigationDeferred, ResourceBindingDeferred)):
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
                ledger = getattr(self.runtime, 'action_ledger', None)
                if ledger is not None:
                    ledger.record_terminal(self._ledger_key(pending), 'succeeded')
            self._tick += 1
            self._set_pending(executor)

    def _ledger_key(self, pending):
        return f'{self.stage_index}:{pending.robot_id}:{pending.cursor}'

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
            if not self._condition_ready(pending):
                state = self.executors[robot].state
                self.admissions[robot] = RobotAdmission('WAITING_CONDITION', pending,
                                                       'wait_until is false')
                if pending.action.timeout_ticks is not None and state.wait_ticks >= pending.action.timeout_ticks:
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
        for pending in admitted:
            self.control.check()
            robot = pending.robot_id
            self.wait_rounds[robot] = 0
            self.executors[robot].state.status = ROBOT_EXECUTING
            self.admissions[robot] = RobotAdmission('EXECUTING', pending)
            self.inflight.add(robot)
            if self.stats is not None:
                self.stats.record_started()
            ledger = getattr(self.runtime, 'action_ledger', None)
            if ledger is not None:
                ledger.record_started(self._ledger_key(pending))
                ledger.record_attempt()
            self.mailboxes[robot].put((pending, wave, self.world.snapshot))
        return True

    def _diagnose(self):
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

    def run(self):
        previous = getattr(self.runtime, 'stage_scheduler', None)
        self.runtime.stage_scheduler = self
        try:
            run_workers(self.runtime, list(self.executors.values()), self.coordinator,
                        self.stage.stage_id, drive=self._drive)
            self.world.snapshot = self.snapshot_store.capture(self.runtime, self.control)
            return StageOutcome('partial' if self._errors else 'completed', True,
                                tuple(self._errors), self.world.snapshot)
        except StageFailureDecisionError as exc:
            # run_workers has quiesced the stage, preserving root cancellation
            # and infrastructure exceptions. Read the final world with the root.
            root = getattr(self.runtime, 'execution_control', None)
            if root is not None:
                self.world.snapshot = self.snapshot_store.capture(self.runtime, root)
            errors = tuple(self._errors) + (error_record(exc, phase=self.stage.stage_id),)
            return StageOutcome('failed', self.stage.stage_failure_policy == 'SKIP',
                                errors, self.world.snapshot)
        finally:
            self._diagnose()
            self.runtime.stage_scheduler = previous
            # run_workers has established target exit (or made runtime unusable).
            # Task 4 also releases leases for cancelled admissions here.
            if getattr(self.runtime, 'execution_quiescent', True):
                for admission in self.admissions.values():
                    if admission.pending is not None:
                        self._release_resources(admission.pending)
