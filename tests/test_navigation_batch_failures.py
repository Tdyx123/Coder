"""Failures after serial selection belong to the selected navigation action."""

import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from executor_system.action_plan import Action
from executor_system.executor import PhaseCoordinator
from executor_system.movement import (
    MovementConfig,
    NavigationBatchAborted,
    NavigationBatchResult,
    NavigationDeferred,
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
    NoInteractionPoseError,
    StepNavigationError,
)
from executor_system.parallel_runner import TolerantExecutor, TolerantRunStats
from executor_system.runtime import ThorRuntime
from executor_system.step_movement import StepMovementStrategy
from executor_system.utils import position_to_grid_key
from tests.movement_fakes import GridThorRuntime


def position(x, z):
    return {"x": x * 0.25, "y": 0.0, "z": z * 0.25}


def make_case(*, selected_agent_id=0, open_grid=False, config_changes=None):
    walkable = (
        [position(x, z) for x in range(5) for z in range(5)]
        if open_grid
        else [position(x, z) for x in range(3) for z in (0, 4)]
    )
    target = position(2, selected_agent_id * 4)
    objects = [
        {"objectId": f"Target|{agent_id}", "visible": True, "position": target}
        for agent_id in range(2)
    ]
    runtime = GridThorRuntime(
        positions={0: position(0, 0), 1: position(0, 4)},
        walkable_by_agent={0: walkable, 1: walkable},
        objects=objects,
    )
    config = replace(MovementConfig.resolve("step", environ={}), **(config_changes or {}))
    runtime.movement_config = config
    runtime.navigation_metrics = NavigationMetrics(config.mode)
    strategy = StepMovementStrategy(runtime, config, runtime.navigation_metrics)
    runtime.movement_strategy = strategy
    requests = tuple(
        NavigationRequest(
            robot=f"robot{agent_id + 1}",
            agent_id=agent_id,
            dest_obj=obj["objectId"],
            destination=dict(obj),
            center=dict(target),
            candidate_positions=(dict(target),),
            object_resource=obj["objectId"],
            next_action=None,
            phase_coordinator=None,
        )
        for agent_id, obj in enumerate(objects)
    )
    return runtime, strategy, requests


class NavigationBatchFailureTest(unittest.TestCase):
    def run_workers(self, workers):
        threads = [threading.Thread(target=worker, daemon=True) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)
        self.assertFalse([thread for thread in threads if thread.is_alive()])

    def run_wave(self, runtime, strategy, requests):
        phase = PhaseCoordinator(
            runtime,
            active_agent_ids=[request.agent_id for request in requests],
            deadline=time.monotonic() + 2.0,
        )
        results, errors, waves = {}, {}, {}

        def worker(request):
            try:
                wave = phase.before_action(request.agent_id, "GoToObject", 0)
                waves[request.agent_id] = wave
                results[request.agent_id] = strategy.navigate(
                    replace(request, phase_coordinator=phase, action_wave=wave)
                )
            except BaseException as exc:
                errors[request.agent_id] = exc
                if request.agent_id in waves:
                    phase.abort_action_wave(waves[request.agent_id], request.agent_id, exc)

        self.run_workers([lambda request=request: worker(request) for request in reversed(requests)])
        return phase, results, errors

    def test_selected_movement_failure_defers_others_even_when_leader_is_not_selected(self):
        for selected in (0, 1):
            with self.subTest(selected=selected):
                runtime, strategy, requests = make_case(selected_agent_id=selected)
                runtime.failed_edges.add(((0, selected * 4), (1, selected * 4)))

                phase, results, errors = self.run_wave(runtime, strategy, requests)

                self.assertEqual(results, {})
                self.assertIsInstance(errors[selected], StepNavigationError)
                self.assertNotIsInstance(errors[selected], NavigationBatchAborted)
                self.assertIsInstance(errors[1 - selected], NavigationDeferred)
                self.assertEqual(phase._action_wave.wave_id, 1)
                self.assertEqual(runtime.navigation_metrics.to_dict()["serial_fallback_batches"], 1)

    def test_selected_transition_and_replan_budgets_keep_other_requests_deferred(self):
        for changes, message in (
            ({"max_failed_transitions": 0}, "failed transition budget"),
            ({"max_replans": 0}, "replan budget"),
        ):
            with self.subTest(changes=changes):
                runtime, strategy, requests = make_case(config_changes=changes)
                runtime.failed_edges.add(((0, 0), (1, 0)))

                outcome = strategy.coordinator.execute_batch(requests)

                self.assertEqual(set(outcome.failed_agent_errors), {0})
                self.assertIn(message, str(outcome.failed_agent_errors[0]))
                self.assertEqual(outcome.deferred_agent_ids, frozenset({1}))
                self.assertEqual(dict(outcome), {})
                self.assertEqual(runtime.navigation_metrics.to_dict()["budget_exhaustions"], 1)

    def test_selected_invisible_candidate_exhaustion_keeps_other_requests_deferred(self):
        runtime, strategy, requests = make_case()
        runtime.objects["Target|0"]["visible"] = False

        outcome = strategy.coordinator.execute_batch(requests)

        self.assertIsInstance(outcome.failed_agent_errors[0], NoInteractionPoseError)
        self.assertEqual(outcome.deferred_agent_ids, frozenset({1}))
        self.assertEqual(runtime.navigation_metrics.to_dict()["invisible_candidates"], 1)

    def test_unwalkable_candidate_does_not_escape_selected_visibility_failure(self):
        runtime, strategy, requests = make_case()
        runtime.objects["Target|0"]["visible"] = False
        requests = (
            replace(requests[0], candidate_positions=(*requests[0].candidate_positions, position(8, 8))),
            requests[1],
        )

        outcome = strategy.coordinator.execute_batch(requests)

        self.assertIsInstance(outcome.failed_agent_errors[0], NoInteractionPoseError)
        self.assertEqual(outcome.deferred_agent_ids, frozenset({1}))

    def test_selected_final_facing_failure_preserves_original_exception(self):
        for held_item in (False, True):
            with self.subTest(held_item=held_item):
                runtime, strategy, requests = make_case()
                original = RuntimeError("RotateRight failed while holding an object")
                runtime.held_item_rotation_failure = lambda _exc: held_item

                def face(_agent_id, _target):
                    raise original

                runtime.face_position_direct = face
                outcome = strategy.coordinator.execute_batch(requests)

                error = outcome.failed_agent_errors[0]
                if held_item:
                    self.assertIsInstance(error, NoInteractionPoseError)
                    self.assertIs(error.__cause__, original)
                else:
                    self.assertIs(error, original)
                self.assertEqual(outcome.deferred_agent_ids, frozenset({1}))

    def test_success_before_serial_failure_is_preserved_with_deferred_peer(self):
        runtime, strategy, requests = make_case(open_grid=True)
        runtime.physical_agent_count = 3
        runtime.positions[1] = position(0, 2)
        runtime.positions[2] = position(0, 4)
        runtime.walkable_by_agent[2] = list(runtime.walkable_by_agent[0])
        shared = position(4, 2)
        requests = tuple(
            replace(
                requests[0],
                robot=f"robot{agent_id + 1}",
                agent_id=agent_id,
                dest_obj=f"Target|{agent_id}",
                object_resource=f"Target|{agent_id}",
                candidate_positions=(position(2, agent_id * 2), shared)
                if agent_id else (position(2, 0),),
            )
            for agent_id in range(3)
        )
        for agent_id in range(3):
            runtime.objects[f"Target|{agent_id}"] = {
                "objectId": f"Target|{agent_id}",
                "visible": True,
                "position": position(2, agent_id * 2),
            }
            if agent_id:
                runtime.object_visibility_by_position[f"Target|{agent_id}"] = {
                    (2, agent_id * 2): False,
                }
        strategy = StepMovementStrategy(runtime, runtime.movement_config, runtime.navigation_metrics)
        original = RuntimeError("RotateRight failed at the shared fallback")
        original_face = runtime.face_position_direct

        def face(agent_id, target):
            if agent_id == 1 and position_to_grid_key(runtime.positions[agent_id]) == (4, 2):
                raise original
            original_face(agent_id, target)

        runtime.face_position_direct = face
        _phase, results, errors = self.run_wave(runtime, strategy, requests)

        self.assertEqual(set(results), {0})
        self.assertEqual(position_to_grid_key(results[0].position), (2, 0))
        self.assertIs(errors[1], original)
        self.assertIsInstance(errors[2], NavigationDeferred)

    def test_refresh_and_planner_faults_after_selection_still_abort_the_batch(self):
        for boundary in ("refresh", "planner"):
            with self.subTest(boundary=boundary):
                runtime, strategy, requests = make_case()
                original = StepNavigationError(f"global {boundary} fault")
                if boundary == "refresh":
                    original_refresh = runtime.refresh_reachable_positions

                    def refresh(agent_id):
                        if runtime.successful_move_count:
                            raise original
                        return original_refresh(agent_id)

                    runtime.refresh_reachable_positions = refresh
                else:
                    runtime.failed_edges.add(((0, 0), (1, 0)))
                    original_plan = strategy.coordinator._plan

                    def plan(*args, **kwargs):
                        if runtime.navigation_metrics.to_dict()["serial_fallback_batches"]:
                            raise original
                        return original_plan(*args, **kwargs)

                    strategy.coordinator._plan = plan

                _phase, results, errors = self.run_wave(runtime, strategy, requests)

                self.assertEqual(results, {})
                self.assertIs(errors[0], original)
                self.assertIsInstance(errors[1], NavigationBatchAborted)
                self.assertIs(errors[1].__cause__, original)

    def test_timeout_and_cancellation_during_movement_or_facing_are_global(self):
        for boundary in ("move", "face"):
            for error_class in (TimeoutError, KeyboardInterrupt):
                with self.subTest(boundary=boundary, error_class=error_class):
                    runtime, strategy, requests = make_case()
                    original = error_class("stop navigation")

                    def fail(*_args):
                        raise original

                    if boundary == "move":
                        runtime.move_to_adjacent_position_direct = fail
                    else:
                        runtime.face_position_direct = fail

                    with self.assertRaises(error_class) as caught:
                        strategy.coordinator.execute_batch(requests)
                    self.assertIs(caught.exception, original)

    def test_invalid_planner_status_after_serial_selection_aborts_every_request(self):
        runtime, strategy, requests = make_case()
        runtime.failed_edges.add(((0, 0), (1, 0)))
        original_plan = strategy.coordinator._plan

        def plan(*args, **kwargs):
            if runtime.navigation_metrics.to_dict()["serial_fallback_batches"]:
                return SimpleNamespace(plan=None, status="INVALID_SCENARIO")
            return original_plan(*args, **kwargs)

        with patch.object(strategy.coordinator, "_plan", side_effect=plan):
            _phase, results, errors = self.run_wave(runtime, strategy, requests)

        self.assertEqual(results, {})
        self.assertIsInstance(errors[0], StepNavigationError)
        self.assertIn("INVALID_SCENARIO", str(errors[0]))
        self.assertIsInstance(errors[1], NavigationBatchAborted)
        self.assertIs(errors[1].__cause__, errors[0])

    def test_tolerant_execution_retries_deferred_navigation_at_same_cursor(self):
        runtime, strategy, requests = make_case(
            open_grid=True,
            config_changes={"max_failed_transitions": 0},
        )
        runtime.failed_edges.update({((0, 0), (1, 0)), ((0, 0), (0, 1))})
        runtime.navigate_to_object = ThorRuntime.navigate_to_object.__get__(runtime)
        runtime.prepare_hand_for_goto_if_needed = lambda *_args: None
        runtime.record_operated_object_name = lambda _destination: None
        from tests.snapshot_fakes import FakeRuntime as SnapshotRuntime
        snapshot_runtime = SnapshotRuntime()
        runtime.controller_lock = snapshot_runtime.controller_lock
        runtime.robot_agent_map = snapshot_runtime.robot_agent_map
        runtime.state_version = 0
        runtime.controller = snapshot_runtime.controller
        for agent_id, event in enumerate(runtime.controller.last_event.events):
            event.metadata["agent"]["position"] = runtime.positions[agent_id]
            event.metadata["objects"] = list(runtime.objects.values())
        runtime.agent_event = snapshot_runtime.agent_event
        runtime.agent_held_objects_for = lambda _agent_id: set()
        seen = []

        def build_request(robot, target, *, next_action, phase_coordinator, action_wave):
            agent_id = runtime.physical_agent_id(robot)
            state = phase_coordinator._action_wave
            seen.append((agent_id, action_wave.wave_id, state.announced[agent_id][1], target))
            return replace(
                requests[agent_id],
                phase_coordinator=phase_coordinator,
                action_wave=action_wave,
            )

        runtime.build_navigation_request = build_request
        phase = PhaseCoordinator(runtime, [0, 1], deadline=time.monotonic() + 2.0)
        stats = TolerantRunStats()
        executors = [
            TolerantExecutor(
                runtime,
                f"robot{agent_id + 1}",
                [Action("GoToObject", {"args": (f"Target|{agent_id}",)})],
                stage_id="isolation",
                stats=stats,
                deadline=time.monotonic() + 2.0,
                phase_coordinator=phase,
            )
            for agent_id in range(2)
        ]
        errors = []

        def execute(executor):
            try:
                executor.execute()
            except BaseException as exc:
                errors.append(exc)

        self.run_workers([lambda executor=executor: execute(executor) for executor in executors])

        self.assertEqual(errors, [])
        self.assertEqual(sorted(seen), [(0, 0, 0, "Target|0"), (1, 0, 0, "Target|1"), (1, 1, 0, "Target|1")])
        self.assertEqual(position_to_grid_key(runtime.positions[1]), (2, 0))
        self.assertEqual([executor.state.action_cursor for executor in executors], [1, 1])
        self.assertEqual([executor.state.retries_by_action for executor in executors], [{}, {}])
        summary = stats.to_dict()
        self.assertEqual(summary["executed_actions"], 2)
        self.assertEqual(summary["failed_actions"], 1)
        self.assertEqual([failure["robot_id"] for failure in summary["robot_failures"]], ["robot1"])
        metrics = runtime.navigation_metrics.to_dict()
        self.assertEqual((metrics["requests"], metrics["successes"], metrics["failures"]), (3, 1, 1))


class NavigationBatchPartitionTest(unittest.TestCase):
    run_workers = NavigationBatchFailureTest.run_workers
    run_wave = NavigationBatchFailureTest.run_wave
    def test_batch_mapping_contains_only_successful_navigation_results(self):
        request = make_case()[2][0]
        result = NavigationResult(destination=request.destination, position=request.center)
        original = ValueError("selected request failed")
        batch = NavigationBatchResult(
            results={0: result},
            failed_agent_errors={1: original},
            deferred_agent_ids=frozenset({2}),
        )
        self.assertEqual(dict(batch), {0: result})
        self.assertEqual(len(batch), 1)
        self.assertNotIn(1, batch)

    def test_invalid_partition_is_published_as_one_global_error(self):
        request = make_case()[2][0]
        success = NavigationResult(destination=request.destination, position=request.center)
        for results, failed, deferred in (
            ({0: success}, {0: ValueError("overlap")}, {1}),
            ({0: success}, {1: ValueError("overlap")}, {1}),
            ({0: success}, {}, {0, 1}),
            ({}, {0: ValueError("missing peer")}, set()),
            ({0: success}, {2: ValueError("unknown peer")}, {1}),
            ({0: success}, {1: "invalid exception"}, set()),
        ):
            with self.subTest(results=set(results), failed=set(failed), deferred=deferred):
                runtime, strategy, requests = make_case()
                outcome = NavigationBatchResult(
                    results=results,
                    failed_agent_errors=failed,
                    deferred_agent_ids=frozenset(deferred),
                )
                with patch.object(strategy.coordinator, "execute_batch", return_value=outcome):
                    _phase, published, errors = self.run_wave(runtime, strategy, requests)
                self.assertEqual(published, {})
                self.assertIsInstance(errors[0], RuntimeError)
                self.assertNotIsInstance(errors[0], NavigationBatchAborted)
                self.assertIn("invalid", str(errors[0]))
                self.assertIsInstance(errors[1], NavigationBatchAborted)


if __name__ == "__main__":
    unittest.main()
