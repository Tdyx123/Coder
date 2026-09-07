"""Serialized controller submission and atomic event publication.

The runtime supplies the controller, locks and event/identity hooks explicitly.
There is no worker thread or controller copy here: replacements on the runtime
remain visible, and snapshots share the same controller lock and version.
"""
from typing import Any, Dict
from contextlib import nullcontext
from .runtime_metrics import metrics_for

from .execution_control import ensure_control, ExecutionShutdownTimeout


class ControllerClient:
    def __init__(self, runtime):
        self.runtime = runtime

    def step(
        self,
        payload: Dict[str, Any],
        *,
        check_success: bool = True,
        save_frame: bool = True,
    ):
        runtime = self.runtime
        scope_state = getattr(runtime, "_navigation_action_scope_state", None)
        if scope_state is not None and getattr(scope_state, "depth", 0) > 0:
            action = payload.get("action")
            if action:
                runtime.navigation_metrics.record_action(str(action))
        control = ensure_control(runtime)
        control.check()
        metrics = metrics_for(runtime)
        committed = False
        try:
            lock_started = metrics.clock()
            with runtime.controller_lock:
                metrics.observe("lock_wait", max(0.0, metrics.clock() - lock_started))
                control.check()
                from .action_resources import active_resources
                admitted = active_resources(runtime)
                if admitted is not None:
                    admitted.before_step(payload)
                transformation_objects = None
                held_before = ()
                if payload.get('action') in ('SliceObject', 'BreakObject', 'PickupObject',
                                             'PutObject', 'ThrowObject', 'DropHandObject'):
                    transformation_objects = {
                        str(obj.get('objectId')): dict(obj)
                        for obj in runtime.current_objects() if obj.get('objectId')
                    }
                    if payload.get('action') in ('PutObject', 'ThrowObject', 'DropHandObject'):
                        held_before = tuple(runtime.agent_held_objects_for(int(payload.get('agentId', 0))))
                try:
                    metrics.increment('controller_calls')
                    reachable = payload.get('action') == 'GetReachablePositions'
                    if reachable:
                        metrics.increment('reachable_queries')
                    with metrics.measure('controller'):
                        with metrics.measure('reachable_query') if reachable else nullcontext():
                            event = runtime.controller.step(dict(payload))
                except BaseException as exc:
                    metrics.increment("controller_exceptions")
                    control.cancel(str(exc))
                    root_control = getattr(runtime, "execution_control", None)
                    if root_control is not None and root_control is not control:
                        root_control.cancel(str(exc))
                    raise
                # The controller has published a new last_event even if its
                # metadata cannot be read. Publish that version and notify it;
                # a parsing failure below makes the task unusable, not retryable.
                runtime.state_version = getattr(runtime, "state_version", 0) + 1
                committed = True
                try:
                    metadata = getattr(event, 'metadata', {}) or {}
                    if metadata.get('lastActionSuccess') is False:
                        metrics.increment('controller_action_failures')
                    runtime._commit_transformation_identities(event, payload, transformation_objects, held_before)
                    runtime._commit_world_event(event, payload)
                except BaseException as exc:
                    control.cancel(f"world event commit failed: {exc}")
                    root_control = getattr(runtime, "execution_control", None)
                    if root_control is not None and root_control is not control:
                        root_control.cancel(f"world event commit failed: {exc}")
                    if not isinstance(exc, Exception):
                        raise
                    from .world_snapshot import SnapshotReadError
                    raise SnapshotReadError(f"could not commit world event: {exc}") from exc
                if save_frame:
                    runtime.save_frames(event)
        finally:
            # Never acquire the scheduler condition while holding controller_lock.
            # Metadata/frame failures cannot undo an already published event.
            scheduler = getattr(runtime, "stage_scheduler", None)
            if committed and scheduler is not None:
                scheduler.notify_world_changed()
        if check_success:
            runtime.assert_success(event, payload)
        return event

    def commit_world_event(self, event, payload: Dict[str, Any]) -> None:
        """Commit every returned event, including an unsuccessful action event.

        Called only under controller_lock, after state_version advances.
        Hand overrides are evidence of a
        successful low-level action, never asynchronous high-level bookkeeping.
        """
        runtime = self.runtime
        committed = getattr(runtime, "_committed_held_object_overrides", None)
        if committed is None:
            committed = runtime._committed_held_object_overrides = {}
        # Once inventory metadata confirms an object, it takes over as the
        # source of truth. A later empty inventory must not revive old evidence.
        events = getattr(event, "events", None) or [event]
        confirmed = {
            str(obj["objectId"])
            for agent_event in events
            for obj in (getattr(agent_event, "metadata", {}) or {}).get("inventoryObjects", ())
            if isinstance(obj, dict) and obj.get("objectId")
        }
        overrides, override_lock = runtime._held_object_override_state()
        with override_lock:
            for owner, evidence in committed.items():
                for object_id in confirmed:
                    evidence.pop(object_id, None)
                    overrides.get(owner, set()).discard(object_id)
        metadata = getattr(event, "metadata", {}) or {}
        if not metadata.get("lastActionSuccess", not bool(metadata.get("errorMessage"))):
            return
        action = payload.get("action")
        agent_id = int(payload.get("agentId", 0))
        if action == "PickupObject" and payload.get("objectId"):
            object_id = str(payload["objectId"])
            committed[agent_id] = {} if object_id in confirmed else {object_id: {
                "source": "action_commit", "action": action, "version": runtime.state_version,
            }}
            runtime.release_agent_held_objects(agent_id)
            if object_id not in confirmed:
                runtime.record_agent_held_object(agent_id, object_id)
        elif action in {"PutObject", "ThrowObject", "DropHandObject"}:
            committed.pop(agent_id, None)
            runtime.release_agent_held_objects(agent_id)

    def stop(self) -> bool:
        """Claim and stop once; return whether preview cleanup is needed."""
        runtime = self.runtime
        if not getattr(runtime, "execution_quiescent", True):
            raise ExecutionShutdownTimeout("cannot stop a controller with active workers")
        with runtime.controller_lock:
            if getattr(runtime, "_stopped", False):
                return False
            runtime._stopped = True
            controller = runtime.controller
            # Claim the controller before stopping it.  A cleanup failure is
            # recorded by the caller, while concurrent/repeated close calls
            # remain bounded and never retry a partially stopped controller.
            runtime.controller = None
        if controller is not None:
            controller.stop()
        return True

