"""Structural and scene validation for registered actions."""
from .action_registry import ActionRegistry


class PlanValidator:
    def __init__(self, runtime_obj=None):
        self.runtime = runtime_obj
        self.registry = ActionRegistry()

    def validate(self, plan):
        self.validate_structure(plan)
        if self.runtime is not None:
            self.validate_scene(plan)

    def validate_structure(self, plan):
        if not plan.stages:
            raise RuntimeError('Action-level plan must contain at least one stage.')
        if plan.global_success_condition is not None and not callable(plan.global_success_condition):
            raise ValueError('global_success_condition must be callable')
        for stage in plan.stages:
            if stage.stage_failure_policy not in {'FAIL_STAGE', 'SKIP'}:
                raise ValueError('invalid stage_failure_policy')
            if stage.synchronization_policy not in {'BARRIER_AT_STAGE_END', 'BARRIER_EACH_STEP', 'EVENT_CONDITION'}:
                raise ValueError('invalid synchronization_policy')
            if stage.stage_success_condition is not None and not callable(stage.stage_success_condition):
                raise ValueError('stage_success_condition must be callable')
            if not stage.robot_action_queues:
                raise RuntimeError(f'Stage {stage.stage_id!r} has no robot queues.')
            for robot_id, actions in stage.robot_action_queues.items():
                if not actions:
                    raise RuntimeError(f'Stage {stage.stage_id!r} queue for {robot_id!r} is empty.')
                for cursor, action in enumerate(actions):
                    try:
                        self.validate_action(stage.stage_id, robot_id, action)
                    except (ValueError, RuntimeError, TypeError) as exc:
                        raise type(exc)(f'stage {stage.stage_id!r} robot {robot_id!r} cursor {cursor}: {exc}') from exc

    def validate_scene(self, plan):
        from .world_snapshot import SnapshotStore
        from .execution_control import ensure_control
        snapshot = SnapshotStore().capture(self.runtime, ensure_control(self.runtime))
        for stage in plan.stages:
            for robot_id, actions in stage.robot_action_queues.items():
                for cursor, action in enumerate(actions):
                    try:
                        self.registry.validate_scene_action(self.runtime, snapshot, robot_id, action)
                    except (ValueError, RuntimeError, KeyError, IndexError) as exc:
                        raise RuntimeError(f'stage {stage.stage_id!r} robot {robot_id!r} cursor {cursor}: {exc}') from exc

    def validate_action(self, stage_id: str, robot_id: str, action) -> None:
        if action.on_failure not in {'FAIL_STAGE', 'FAIL_ROBOT', 'SKIP', 'RETRY', 'WAIT_AND_RETRY', 'SKIP_IF_EFFECT_ALREADY_TRUE'}:
            raise ValueError("invalid on_failure")
        if action.on_conflict not in {'WAIT', 'RETRY_NEXT_TICK', 'SKIP', 'FAIL_STAGE'}:
            raise ValueError("invalid on_conflict")
        for name, value in (("max_retries", action.max_retries), ("timeout_ticks", action.timeout_ticks)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if action.wait_until is not None and not callable(action.wait_until):
            raise ValueError("wait_until must be callable")
        for condition in (*action.expected_preconditions, *action.expected_effects):
            if callable(condition):
                continue
            if not isinstance(condition, dict) or not condition.get('name') or set(condition) - {'name', 'states', 'state', 'contains'}:
                raise ValueError("conditions must be callable or a named goal dictionary")
        self.registry.validate_shape(action)
