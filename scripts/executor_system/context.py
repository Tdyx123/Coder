"""Scoped runtime context for legacy helper entry points."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

_active_runtime = ContextVar('executor_runtime', default=None)
# Legacy single-runtime callers may still assign this value. Production paths
# pass their runtime explicitly and bind at each worker entrance.
runtime: Optional[Any] = None


def get_bound_runtime() -> Optional[Any]:
    return _active_runtime.get()


def get_runtime() -> Any:
    active = get_bound_runtime()
    if active is None:
        active = runtime
    if active is None:
        raise RuntimeError('AI2-THOR runtime has not been initialized.')
    return active


@contextmanager
def bind_runtime(value):
    token = _active_runtime.set(value)
    try:
        yield value
    finally:
        _active_runtime.reset(token)


runtime_scope = bind_runtime
