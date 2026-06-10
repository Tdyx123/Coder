"""Conflict resolver that combines position and object resource decisions."""

from typing import Any, Optional, Set

from .executor_requests import (
    ConflictDecision,
    MoveToPositionRequest,
    PositionClearRequest,
)
from .resource_manager import ObjectResourceMgr, PositionResourceMgr


class ConflictResolver:
    def __init__(
        self,
        position_resources: PositionResourceMgr,
        object_resources: ObjectResourceMgr,
    ) -> None:
        self.position_resources = position_resources
        self.object_resources = object_resources

    def resolve_position_conflict(self, request: Any) -> ConflictDecision:
        return self.position_resources.resolve(request)

    def position_clear_request(
        self,
        request: Any,
        decision: ConflictDecision,
        completed_agent_ids: Set[int],
    ) -> Optional[PositionClearRequest]:
        if decision.can_run:
            return None
        done_blockers = tuple(
            blocker_id
            for blocker_id in decision.blockers
            if blocker_id in completed_agent_ids
        )
        if not done_blockers:
            return None
        return PositionClearRequest(
            priority_agent_id=request.agent_id,
            target_position=dict(request.target_position),
            blocker_ids=done_blockers,
            protect_navigation_path=isinstance(request, MoveToPositionRequest),
        )

    def resolve_object_conflict(
        self,
        request: Any,
        completed_agent_ids: Set[int],
    ) -> ConflictDecision:
        return self.object_resources.resolve(request, completed_agent_ids)
