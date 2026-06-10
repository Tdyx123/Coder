"""Request and decision objects shared by the synchronous execution pipeline."""

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .utils import position_to_grid_key


@dataclass
class StepRequest:
    payload: Dict[str, Any]
    check_success: bool
    save_frame: bool
    retry_on_failure: bool
    agent_id: int
    object_resource: Optional[str] = None
    object_resources: Tuple[str, ...] = ()
    max_retries: int = 3
    attempts: int = 0
    event: Any = None
    exception: Optional[BaseException] = None
    done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.object_resources:
            self.object_resources = tuple(
                dict.fromkeys(str(resource) for resource in self.object_resources if resource)
            )
            if self.object_resource is None and self.object_resources:
                self.object_resource = self.object_resources[0]
        elif self.object_resource:
            self.object_resources = (str(self.object_resource),)


@dataclass
class MoveToPositionRequest:
    agent_id: int
    target_position: Dict[str, float]
    chunk_steps: int
    requeues_remaining: int
    initial_requeues: int
    object_resource: Optional[str] = None
    object_resources: Tuple[str, ...] = ()
    exception: Optional[BaseException] = None
    done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.object_resources:
            self.object_resources = tuple(
                dict.fromkeys(str(resource) for resource in self.object_resources if resource)
            )
            if self.object_resource is None and self.object_resources:
                self.object_resource = self.object_resources[0]
        elif self.object_resource:
            self.object_resources = (str(self.object_resource),)


@dataclass
class TeleportToPositionRequest:
    agent_id: int
    candidate_positions: List[Dict[str, float]]
    max_retries: int = 3
    object_resource: Optional[str] = None
    object_resources: Tuple[str, ...] = ()
    search_center: Optional[Dict[str, float]] = None
    excluded_grid_keys: Set[Tuple[int, int]] = field(default_factory=set)
    restrict_to_candidate_positions: bool = False
    result_position: Optional[Dict[str, float]] = None
    exception: Optional[BaseException] = None
    done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.object_resources:
            self.object_resources = tuple(
                dict.fromkeys(str(resource) for resource in self.object_resources if resource)
            )
            if self.object_resource is None and self.object_resources:
                self.object_resource = self.object_resources[0]
        elif self.object_resource:
            self.object_resources = (str(self.object_resource),)
        if self.search_center is None and self.candidate_positions:
            self.search_center = dict(self.candidate_positions[0])

    @property
    def target_position(self) -> Dict[str, float]:
        if not self.candidate_positions:
            raise RuntimeError("TeleportToPosition has no candidate positions.")
        for position in self.candidate_positions:
            if position_to_grid_key(position) not in self.excluded_grid_keys:
                return position
        return self.candidate_positions[0]

    def search_center_position(self) -> Dict[str, float]:
        if self.search_center is not None:
            return dict(self.search_center)
        return dict(self.target_position)


@dataclass
class TeleportAndFacePositionRequest:
    agent_id: int
    candidate_positions: List[Dict[str, float]]
    face_target: Dict[str, float]
    max_retries: int = 3
    object_resource: Optional[str] = None
    object_resources: Tuple[str, ...] = ()
    search_center: Optional[Dict[str, float]] = None
    excluded_grid_keys: Set[Tuple[int, int]] = field(default_factory=set)
    restrict_to_candidate_positions: bool = False
    result_position: Optional[Dict[str, float]] = None
    exception: Optional[BaseException] = None
    done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.object_resources:
            self.object_resources = tuple(
                dict.fromkeys(str(resource) for resource in self.object_resources if resource)
            )
            if self.object_resource is None and self.object_resources:
                self.object_resource = self.object_resources[0]
        elif self.object_resource:
            self.object_resources = (str(self.object_resource),)
        if self.search_center is None and self.candidate_positions:
            self.search_center = dict(self.candidate_positions[0])

    @property
    def target_position(self) -> Dict[str, float]:
        if not self.candidate_positions:
            raise RuntimeError("TeleportAndFacePosition has no candidate positions.")
        for position in self.candidate_positions:
            if position_to_grid_key(position) not in self.excluded_grid_keys:
                return position
        return self.candidate_positions[0]

    def search_center_position(self) -> Dict[str, float]:
        if self.search_center is not None:
            return dict(self.search_center)
        return dict(self.target_position)


@dataclass(frozen=True)
class ConflictDecision:
    can_run: bool
    reason: str = ""
    blockers: Tuple[int, ...] = ()
    handoff_owner: Optional[int] = None
    resource: Optional[str] = None


@dataclass(frozen=True)
class PositionClearRequest:
    priority_agent_id: int
    target_position: Dict[str, float]
    blocker_ids: Tuple[int, ...]
    protect_navigation_path: bool = True


@dataclass
class ObjectHandoffRequest:
    request: Any
    object_resource: str
    from_agent_id: int
    to_agent_id: int
