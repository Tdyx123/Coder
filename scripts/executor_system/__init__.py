"""Reorganized execution-system modules.

Pipeline:
TaskPlan -> PlanValidator -> TaskRunner -> StageRunner -> per-robot Executor ->
AI2ThorAdapter -> ThorRuntime -> WorldState.
"""
