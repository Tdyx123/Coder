"""Teleport-backed implementation of the robot movement strategy."""

from __future__ import annotations

from contextlib import nullcontext

from .movement import NavigationMetrics, NavigationRequest, NavigationResult
from .utils import log


class TeleportMovementStrategy:
    def __init__(self, runtime, metrics: NavigationMetrics) -> None:
        self.runtime = runtime
        self.metrics = metrics

    def navigate(self, request: NavigationRequest) -> NavigationResult:
        scope = getattr(self.runtime, "navigation_action_scope", None)
        with scope() if callable(scope) else nullcontext():
            return self._navigate(request)

    def _navigate(self, request: NavigationRequest) -> NavigationResult:
        active = request
        coordinator = active.phase_coordinator
        if coordinator is not None:
            waited = coordinator.wait_until_goto_candidates_clear(
                active.agent_id,
                active.candidate_positions[:1],
            )
            if waited:
                active = self.runtime.build_navigation_request(
                    active.robot,
                    active.dest_obj,
                    next_action=active.next_action,
                    phase_coordinator=coordinator,
                    action_wave=active.action_wave,
                )

        target_position = active.candidate_positions[0]
        log(
            f"Going to {active.dest_obj} "
            f"{active.destination.get('objectId')} "
            f"at reachable position {target_position}."
        )
        selected = self.runtime.teleport_and_face_candidate_positions(
            active.agent_id,
            active.candidate_positions,
            face_target=active.center,
            object_resource=active.object_resource,
            search_center=active.center,
            restrict_to_candidate_positions=True,
        )
        if coordinator is not None:
            notify_position_changed = getattr(
                coordinator,
                "notify_agent_position_changed",
                None,
            )
            if callable(notify_position_changed):
                notify_position_changed(active.agent_id)
        return NavigationResult(
            destination=dict(active.destination),
            position=dict(selected),
        )
