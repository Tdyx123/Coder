"""Shared runtime context for compatibility wrappers."""

from typing import Any, Optional
import threading
from contextlib import contextmanager

_local = threading.local()

runtime: Optional[Any] = None


def get_runtime() -> Any:
    active = getattr(_local, "runtime", runtime)
    if active is None:
        raise RuntimeError("AI2-THOR runtime has not been initialized.")
    return active


@contextmanager
def runtime_scope(value):
    sentinel = object()
    previous = getattr(_local, "runtime", sentinel)
    _local.runtime = value
    try:
        yield value
    finally:
        if previous is sentinel:
            del _local.runtime
        else:
            _local.runtime = previous
