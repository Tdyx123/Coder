"""Reorganized execution-system modules.

Pipeline:
TaskPlan -> PlanValidator -> TaskRunner -> StageRunner -> per-robot Executor ->
AI2ThorAdapter -> ThorRuntime -> WorldState.
"""

from importlib import import_module


__all__ = [
    "Action",
    "ActionRegistry",
    "ActionResult",
    "ActionSpec",
    "ControllerClient",
    "MultiStageActionPlan",
    "NormalizedAction",
    "ObjectInteractor",
    "ObjectResolver",
    "PlannedAction",
    "PreparedAction",
    "ReachableMapCache",
    "ResourceRequest",
    "RobotExecutionState",
    "RuntimeArtifacts",
    "RuntimeMetrics",
    "RuntimeWorldSnapshot",
    "StagePlan",
    "TaskPlan",
    "bind_runtime",
    "get_bound_runtime",
    "get_runtime",
    "runtime_scope",
]


_PUBLIC_MODULES = {
    "Action": "plan_types",
    "ActionResult": "plan_types",
    "MultiStageActionPlan": "plan_types",
    "PlannedAction": "plan_types",
    "ResourceRequest": "plan_types",
    "RobotExecutionState": "plan_types",
    "StagePlan": "plan_types",
    "TaskPlan": "plan_types",
    "ActionRegistry": "action_registry",
    "ActionSpec": "action_registry",
    "NormalizedAction": "action_registry",
    "PreparedAction": "action_registry",
    "ControllerClient": "controller_client",
    "ObjectInteractor": "object_interactor",
    "ObjectResolver": "object_resolver",
    "ReachableMapCache": "reachable_map",
    "RuntimeWorldSnapshot": "reachable_map",
    "RuntimeArtifacts": "runtime_artifacts",
    "RuntimeMetrics": "runtime_metrics",
    "bind_runtime": "context",
    "get_bound_runtime": "context",
    "get_runtime": "context",
    "runtime_scope": "context",
}


def __getattr__(name):
    """Resolve public symbols without loading execution code on package import."""
    try:
        module_name = _PUBLIC_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
