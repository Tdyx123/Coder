"""Compatibility aliases for the per-robot Executor.

There is no central worker thread. ControllerClient serializes the synchronous
controller submission boundary using the runtime's existing lock.
"""

from .executor import CentralStepExecutor, Executor, SynchronousExecutor

__all__ = ["Executor", "CentralStepExecutor", "SynchronousExecutor"]
