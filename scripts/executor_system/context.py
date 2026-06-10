"""Shared runtime context for compatibility wrappers."""

from typing import Any, Optional

runtime: Optional[Any] = None


def get_runtime() -> Any:
    if runtime is None:
        raise RuntimeError("AI2-THOR runtime has not been initialized.")
    return runtime
