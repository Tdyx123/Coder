"""Grid-step implementation of the robot movement strategy."""

from __future__ import annotations

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
        return self.coordinator.execute_batch((request,))[request.agent_id]
