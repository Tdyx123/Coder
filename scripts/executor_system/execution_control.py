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
    def __init__(self, deadline: Optional[float] = None):
        self.deadline = deadline
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

    def _expire(self):
        with self._lock:
            if (not self._event.is_set() and self.deadline is not None
                    and time.monotonic() >= self.deadline):
                self._reason = 'task-plan execution exceeded timeout'
                self._timed_out = True
                self._event.set()

    @property
    def cancelled(self):
        self._expire()
        return self._event.is_set()

    @property
    def reason(self):
        self._expire()
        return self._reason

    def check(self) -> None:
        self._expire()
        if self._event.is_set():
            error = PlanExecutionTimeout if self._timed_out else ExecutionCancelled
            raise error(self._reason)

    def wait(self, timeout: float) -> bool:
        self._expire()
        if self.deadline is not None:
            timeout = min(timeout, max(0, self.deadline - time.monotonic()))
        self._event.wait(max(0, timeout))
        return self.cancelled


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
    control = coordinator.control
    errors = []
    error_lock = threading.Lock()
    accepting_errors = True
    runtime.worker_errors = list(getattr(runtime, 'worker_errors', []))
    runtime.execution_quiescent = False

    def wake():
        with coordinator.condition:
            coordinator.condition.notify_all()

    def run(executor):
        try:
            control.check()
            executor.execute()
        except BaseException as exc:
            with error_lock:
                if accepting_errors:
                    errors.append((executor.robot_id, exc, exc.__traceback__))
            control.cancel(str(exc))
            wake()

    threads = [threading.Thread(target=run, args=(executor,),
                                name=f'{stage_id}-{executor.robot_id}', daemon=True)
               for executor in executors]
    main_error = None
    try:
        for thread in threads:
            control.check()
            thread.start()
        while any(thread.is_alive() for thread in threads):
            control.check()
            for thread in threads:
                if thread.is_alive():
                    thread.join(0.01)
        control.check()
    except BaseException as exc:
        main_error = (exc, exc.__traceback__)
        control.cancel(str(exc))
        wake()
    finally:
        shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
        for thread in threads:
            if thread.ident is not None:
                while thread.is_alive() and time.monotonic() < shutdown_deadline:
                    try:
                        thread.join(min(0.05, max(0, shutdown_deadline - time.monotonic())))
                    except BaseException as exc:
                        if main_error is None or isinstance(main_error[0], Exception):
                            main_error = (exc, exc.__traceback__)
                        control.cancel(str(exc))
                        wake()
        with error_lock:
            accepting_errors = False
            runtime.worker_errors.extend(error_record(exc, phase=stage_id, robot_id=robot)
                                         for robot, exc, _tb in errors)
        alive = [thread.name for thread in threads if thread.is_alive()]
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
