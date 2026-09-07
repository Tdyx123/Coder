"""Task cancellation and bounded cooperative worker shutdown.

Already submitted simulator calls may finish. An unquiescent runtime must be
reaped by its process owner, and must never be evaluated or reused.
"""
import threading
import time
import traceback
from typing import Optional

SHUTDOWN_TIMEOUT_SECONDS = 5.0
CLEANUP_TIMEOUT_SECONDS = 10.0


class ExecutionCancelled(RuntimeError):
    pass


class ExecutionShutdownTimeout(RuntimeError):
    pass


class PlanExecutionTimeout(TimeoutError):
    pass


class ExecutionControl:
    def __init__(
        self,
        deadline: Optional[float] = None,
        *,
        parent: Optional["ExecutionControl"] = None,
    ):
        if parent is not None and parent.deadline is not None:
            deadline = (
                parent.deadline
                if deadline is None
                else min(deadline, parent.deadline)
            )
        self.deadline = deadline
        self._parent = parent
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = None
        self._timed_out = False

    def cancel(self, reason: str) -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = str(reason)
                self._event.set()

    def tighten_deadline(self, deadline) -> None:
        if deadline is not None:
            with self._lock:
                if self.deadline is None or deadline < self.deadline:
                    self.deadline = deadline

    def child(self, *, deadline: Optional[float] = None) -> "ExecutionControl":
        """Create a stage-scoped control that inherits only parent aborts."""

        return ExecutionControl(deadline, parent=self)

    def _expire(self):
        with self._lock:
            if (not self._event.is_set() and self.deadline is not None
                    and time.monotonic() >= self.deadline):
                self._reason = 'task-plan execution exceeded timeout'
                self._timed_out = True
                self._event.set()

    @property
    def cancelled(self):
        if self._parent is not None and self._parent.cancelled:
            return True
        self._expire()
        return self._event.is_set()

    @property
    def reason(self):
        if self._parent is not None and self._parent.cancelled:
            return self._parent.reason
        self._expire()
        return self._reason

    def check(self) -> None:
        if self._parent is not None:
            self._parent.check()
        self._expire()
        if self._event.is_set():
            error = PlanExecutionTimeout if self._timed_out else ExecutionCancelled
            raise error(self._reason)

    def wait(self, timeout: float) -> bool:
        end = time.monotonic() + max(0, timeout)
        while True:
            if self.cancelled:
                return True
            remaining = max(0, end - time.monotonic())
            if self.deadline is not None:
                remaining = min(remaining, max(0, self.deadline - time.monotonic()))
            if remaining <= 0:
                return self.cancelled
            # A parent event cannot directly wake the child's event, so use a
            # small bounded slice while retaining prompt inherited cancellation.
            self._event.wait(min(remaining, 0.05))


def ensure_control(runtime, deadline=None):
    if not getattr(runtime, 'reusable', True):
        raise ExecutionShutdownTimeout('runtime has unquiescent workers and cannot be reused')
    control = getattr(runtime, 'execution_control', None)
    if control is None:
        control = runtime.execution_control = ExecutionControl(deadline)
    else:
        control.tighten_deadline(deadline)
    return control


def install_control(runtime, timeout_seconds):
    if not getattr(runtime, 'reusable', True) or not getattr(runtime, 'execution_quiescent', True):
        raise ExecutionShutdownTimeout('runtime has unquiescent workers and cannot be reused')
    deadline = None if timeout_seconds is None else time.monotonic() + max(0, float(timeout_seconds))
    runtime.execution_control = ExecutionControl(deadline)
    runtime.execution_quiescent = True
    runtime.worker_errors = []
    runtime.execution_report = {}
    return runtime.execution_control


def error_record(exc, *, phase, robot_id=None):
    return {'phase': str(phase), 'robot_id': robot_id,
            'exception_type': type(exc).__name__, 'message': str(exc),
            'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}


def raise_if_execution_aborted(runtime, exc):
    """Preserve interrupts and original infrastructure errors through recovery."""
    if not isinstance(exc, Exception) or isinstance(exc, (ExecutionCancelled, PlanExecutionTimeout, ExecutionShutdownTimeout)):
        raise exc.with_traceback(exc.__traceback__)
    control = getattr(runtime, 'execution_control', None)
    if control is not None and control.cancelled:
        raise exc.with_traceback(exc.__traceback__)


def run_workers(runtime, executors, coordinator, stage_id):
    from .execution_policy import StageFailureDecisionError

    control = coordinator.control
    errors = []
    error_lock = threading.Lock()
    accepting_errors = True
    runtime.worker_errors = list(getattr(runtime, 'worker_errors', []))
    runtime.execution_quiescent = False

    def wake():
        with coordinator.condition:
            coordinator.condition.notify_all()

    def cancel_for_error(exc):
        control.cancel(str(exc))
        root_control = getattr(runtime, 'execution_control', None)
        if root_control is None or root_control is control or root_control.cancelled:
            return
        if isinstance(exc, (StageFailureDecisionError, ExecutionCancelled)):
            return
        root_control.cancel(str(exc))

    def run(executor, exited):
        try:
            control.check()
            executor.execute()
        except BaseException as exc:
            with error_lock:
                if accepting_errors:
                    errors.append((executor.robot_id, exc, exc.__traceback__))
            cancel_for_error(exc)
            wake()
        finally:
            # A real SIGINT inside Thread.join can mark CPython's Thread as
            # stopped before its target returns. Only the target can certify
            # it will no longer access the controller or execution state.
            exited.set()

    workers = []
    for executor in executors:
        exited = threading.Event()
        thread = threading.Thread(target=run, args=(executor, exited),
                                  name=f'{stage_id}-{executor.robot_id}', daemon=True)
        workers.append((thread, exited))
    launched_workers = []
    main_error = None
    try:
        for thread, exited in workers:
            control.check()
            # If start itself is interrupted, conservatively require target
            # exit evidence for every launch that may have reached the OS.
            launched_workers.append((thread, exited))
            thread.start()
        while any(not exited.is_set() for _thread, exited in launched_workers):
            control.check()
            for thread, exited in launched_workers:
                if not exited.is_set():
                    thread.join(0.01)
        control.check()
    except BaseException as exc:
        main_error = (exc, exc.__traceback__)
        cancel_for_error(exc)
        wake()
    finally:
        shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
        for _thread, exited in launched_workers:
            while not exited.is_set() and time.monotonic() < shutdown_deadline:
                try:
                    exited.wait(min(0.05, max(0, shutdown_deadline - time.monotonic())))
                except BaseException as exc:
                    if main_error is None or isinstance(main_error[0], Exception):
                        main_error = (exc, exc.__traceback__)
                    cancel_for_error(exc)
                    wake()
        with error_lock:
            accepting_errors = False
            runtime.worker_errors.extend(error_record(exc, phase=stage_id, robot_id=robot)
                                         for robot, exc, _tb in errors)
        alive = [thread.name for thread, exited in launched_workers if not exited.is_set()]
        runtime.execution_quiescent = not alive
        if alive:
            runtime.reusable = False
            error = ExecutionShutdownTimeout('workers did not stop: ' + ', '.join(alive))
            if main_error:
                raise error from main_error[0]
            raise error
    if main_error and not isinstance(main_error[0], Exception):
        raise main_error[0].with_traceback(main_error[1])
    # Preserve the initiating exception, including BaseException interrupt types;
    # cooperative cancellations in sibling threads must not replace it.
    primary = next((item for item in errors if not isinstance(item[1], Exception)), None)
    primary = primary or next((item for item in errors if not isinstance(item[1], ExecutionCancelled)), None)
    if primary:
        raise primary[1].with_traceback(primary[2])
    if main_error:
        raise main_error[0].with_traceback(main_error[1])


def close_runtime(runtime, cleanup_errors):
    """Close only a quiescent runtime, within a separate cleanup budget."""
    if not getattr(runtime, 'execution_quiescent', True):
        return
    errors = []
    def close():
        try:
            runtime.stop()
        except BaseException as exc:
            errors.append(error_record(exc, phase='cleanup'))
    worker = threading.Thread(target=close, name='runtime-cleanup', daemon=True)
    worker.start()
    worker.join(CLEANUP_TIMEOUT_SECONDS)
    if worker.is_alive():
        runtime.reusable = False
        cleanup_errors.append(error_record(ExecutionShutdownTimeout('controller cleanup exceeded timeout'), phase='cleanup'))
    else:
        cleanup_errors.extend(errors)
