"""Deprecated compatibility facade for the robot-level Executor."""

from .executor import CentralStepExecutor, Executor, SynchronousExecutor

__all__ = ["Executor", "CentralStepExecutor", "SynchronousExecutor"]
