"""Grid-step implementation of the robot movement strategy."""

from __future__ import annotations

from contextlib import nullcontext

from .movement import (
    MovementConfig,
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
)
from .movement_coordinator import StepMovementCoordinator


class StepMovementStrategy:
    def __init__(
        self,
        runtime,
        config: MovementConfig,
        metrics: NavigationMetrics,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.metrics = metrics
        self.coordinator = StepMovementCoordinator(runtime, config, metrics)

    def navigate(self, request: NavigationRequest) -> NavigationResult:
        scope = getattr(self.runtime, "navigation_action_scope", None)
        with scope() if callable(scope) else nullcontext():
            return self._navigate(request)

    def _navigate(self, request: NavigationRequest) -> NavigationResult:
        if request.phase_coordinator is not None and request.action_wave is not None:
            return request.phase_coordinator.submit_step_navigation(
                request.action_wave,
                request,
                lambda requests, completed_agent_ids: self.coordinator.execute_batch(
                    requests,
                    completed_agent_ids=completed_agent_ids,
                ),
            )
        return self.coordinator.execute_batch((request,))[request.agent_id]
