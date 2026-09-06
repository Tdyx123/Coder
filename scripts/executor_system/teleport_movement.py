"""Teleport-backed implementation of the robot movement strategy."""

from __future__ import annotations

from contextlib import nullcontext

from .movement import (
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
    NoInteractionPoseError,
)
from .utils import log, position_to_grid_key


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
        interaction_center = active.interaction_center or active.center
        interaction_resource = (
            active.interaction_object_resource
            or active.interaction_target
            or active.object_resource
        )
        excluded_grid_keys = set()
        candidate_grid_keys = {
            position_to_grid_key(position)
            for position in active.candidate_positions
        }
        while True:
            movement_kwargs = {
                "face_target": interaction_center,
                "object_resource": interaction_resource,
                "search_center": interaction_center,
                "restrict_to_candidate_positions": True,
            }
            if excluded_grid_keys:
                movement_kwargs["excluded_grid_keys"] = set(excluded_grid_keys)
            selected = self.runtime.teleport_and_face_candidate_positions(
                active.agent_id,
                active.candidate_positions,
                **movement_kwargs,
            )
            if active.interaction_destination is None:
                break
            interaction_destination = self.runtime.find_object(
                interaction_resource,
                agent_id=active.agent_id,
                require_center=True,
            )
            visible = interaction_destination.get("visible")
            if visible is None or bool(visible):
                break
            self.metrics.increment("invisible_candidates")
            self.metrics.increment("invisible_interaction_candidates")
            excluded_grid_keys.add(position_to_grid_key(selected))
            if candidate_grid_keys.issubset(excluded_grid_keys):
                raise NoInteractionPoseError(
                    "NO_INTERACTION_POSE: interaction target is invisible "
                    f"from every candidate for agent {active.agent_id} target "
                    f"{active.interaction_target or active.dest_obj!r}"
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
