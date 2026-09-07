"""Concurrent recovery tests against the real step planner and runtime scopes."""

import sys
import math
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from executor_system.action_plan import Action
from executor_system.executor import Executor, PhaseCoordinator
from executor_system.parallel_runner import PlanExecutionTimeout
from executor_system.execution_control import install_control
from executor_system.runtime import ThorRuntime
from tests.movement_fakes import GridThorRuntime


def position(x, z=0.0):
    return {"x": x, "y": 0.0, "z": z}


def recovery_runtime(agent_count=2, *, starts=None, targets=None, cells=None):
    cells = cells or [position(x * 0.25, z) for z in range(agent_count) for x in range(3)]
    starts = starts or {i: position(0.0, i) for i in range(agent_count)}
    targets = targets or {i: position(0.5, i) for i in range(agent_count)}
    objects = [
        {"objectId": f"Tomato|{i}", "position": targets[i], "visible": True}
        for i in range(agent_count)
    ]
    grid = GridThorRuntime(
        starts,
        {i: cells for i in range(agent_count)},
        objects,
    )
    runtime = object.__new__(ThorRuntime)
    runtime.physical_agent_count = agent_count
    runtime.robot_agent_map = {f"robot{i + 1}": i for i in range(agent_count)}
    runtime.global_reachable_positions = cells
    for name in (
        "physical_agent_id", "current_agent_position", "agent_position_items",
        "refresh_reachable_positions", "move_to_adjacent_position_direct",
        "face_position_direct", "find_object", "scene_object_bounds",
    ):
        setattr(runtime, name, getattr(grid, name))
    runtime.teleport_candidate_positions = lambda center, **_kwargs: [dict(center)]
    runtime.record_operated_object_name = lambda _obj: None
    runtime.configure_movement("step", environ={})
    runtime.step = lambda _payload, **_kwargs: SimpleNamespace(
        metadata={"lastActionSuccess": True}
    )
    return runtime, grid


class NavigationExecutionScopeTests(unittest.TestCase):
    def start_worker(self, function, errors):
        def run():
            try:
                function()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def finish_workers(self, threads):
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "navigation worker deadlocked")

    def recover(self, runtime, agent_id):
        return runtime.retry_slice_after_interaction_reposition(
            agent_id,
            {"action": "SliceObject", "agentId": agent_id, "objectId": f"Tomato|{agent_id}"},
            None,
        )

    def test_recoveries_are_serialized_through_final_slice(self):
        # Both trajectories cross the origin. Treating the peer as static is
        # safe only if the other recovery cannot move during this whole batch.
        runtime, grid = recovery_runtime(
            starts={0: position(-0.5), 1: position(0.0, -0.5)},
            targets={0: position(0.5), 1: position(0.0, 0.5)},
            cells=[position(x * 0.25) for x in range(-2, 3)]
            + [position(0.0, z * 0.25) for z in range(-2, 3)],
        )
        first_slice = threading.Event()
        release_slice = threading.Event()
        second_started = threading.Event()
        second_built = threading.Event()
        errors = []
        threads = []
        original_build = runtime.build_navigation_request
        original_step = runtime.step

        def build(robot, *args, **kwargs):
            if robot == "robot2":
                second_built.set()
            return original_build(robot, *args, **kwargs)

        def step(payload, **kwargs):
            if payload["agentId"] == 0:
                first_slice.set()
                if not release_slice.wait(3):
                    raise RuntimeError("test did not release first Slice")
            return original_step(payload, **kwargs)

        runtime.build_navigation_request = build
        runtime.step = step
        try:
            threads.append(self.start_worker(lambda: self.recover(runtime, 0), errors))
            self.assertTrue(first_slice.wait(2))

            def second_recovery():
                second_started.set()
                self.recover(runtime, 1)

            threads.append(self.start_worker(second_recovery, errors))
            self.assertTrue(second_started.wait(2))
            self.assertFalse(second_built.wait(0.1), "second recovery raced the first Slice")
        finally:
            release_slice.set()
            self.finish_workers(threads)
        self.assertEqual(errors, [])
        self.assertTrue(second_built.is_set())
        self.assertEqual(grid.positions, {0: position(0.5), 1: position(0.0, 0.5)})
        for snapshot in grid.position_history:
            self.assertGreaterEqual(
                math.hypot(snapshot[0]["x"] - snapshot[1]["x"], snapshot[0]["z"] - snapshot[1]["z"]),
                runtime.movement_config.hard_clearance_m,
            )
        self.assertEqual(runtime.navigation_metrics.to_dict()["interaction_repositions"], 2)

    def test_joint_requests_collect_before_waiting_for_recovery_lock(self):
        runtime, grid = recovery_runtime(3)
        phase = PhaseCoordinator(runtime, (1, 2))
        first_slice = threading.Event()
        release_slice = threading.Event()
        batch_collected = threading.Event()
        batch_finished = threading.Event()
        errors = []
        threads = []
        original_step = runtime.step

        def step(payload, **kwargs):
            first_slice.set()
            if not release_slice.wait(3):
                raise RuntimeError("test did not release first Slice")
            return original_step(payload, **kwargs)

        runtime.step = step

        def navigate(agent_id):
            wave = phase.before_action(agent_id, "GoToObject", 0)
            request = runtime.build_navigation_request(f"robot{agent_id + 1}", f"Tomato|{agent_id}")

            def execute(requests, completed):
                batch_collected.set()
                result = runtime.movement_strategy.coordinator.execute_batch(requests, completed)
                batch_finished.set()
                return result

            phase.submit_step_navigation(wave, request, execute)

        try:
            threads.append(self.start_worker(lambda: self.recover(runtime, 0), errors))
            self.assertTrue(first_slice.wait(2))
            threads.extend(self.start_worker(lambda i=i: navigate(i), errors) for i in (1, 2))
            self.assertTrue(batch_collected.wait(2), "wave collection blocked on navigation lock")
            self.assertFalse(batch_finished.wait(0.1), "joint batch overlapped Slice recovery")
            self.assertEqual(grid.positions[1], position(0.0, 1))
            self.assertEqual(grid.positions[2], position(0.0, 2))
        finally:
            release_slice.set()
            self.finish_workers(threads)
        self.assertEqual(errors, [])
        self.assertTrue(batch_finished.is_set())
        self.assertEqual(grid.positions[1], position(0.5, 1))
        self.assertEqual(grid.positions[2], position(0.5, 2))

    def test_executor_deadline_expires_waiting_for_recovery_without_moving(self):
        runtime, grid = recovery_runtime()
        first_slice = threading.Event()
        release_slice = threading.Event()
        errors = []
        original_step = runtime.step

        def step(payload, **kwargs):
            if payload["agentId"] == 0:
                first_slice.set()
                if not release_slice.wait(3):
                    raise RuntimeError("test did not release first Slice")
            return original_step(payload, **kwargs)

        runtime.step = step
        thread = self.start_worker(lambda: self.recover(runtime, 0), errors)
        try:
            self.assertTrue(first_slice.wait(2))
            phase = PhaseCoordinator(
                runtime, (1,), deadline=time.monotonic() + 0.05,
                timeout_error_factory=PlanExecutionTimeout,
            )
            executor = Executor(runtime, "robot2", phase_coordinator=phase)
            executor.adapter = SimpleNamespace(execute=lambda *_args, **_kwargs: self.recover(runtime, 1))
            with self.assertRaises(PlanExecutionTimeout):
                executor.execute_action(Action("SliceObject", {"args": ("Tomato|1",)}))
            self.assertEqual(grid.positions[1], position(0.0, 1))
        finally:
            release_slice.set()
            self.finish_workers([thread])
        self.assertEqual(errors, [])
        # Scope exit cannot revive a cancelled task. A new task may reuse the
        # controller only after all workers have stopped.
        with self.assertRaises(PlanExecutionTimeout):
            self.recover(runtime, 1)
        install_control(runtime, None)
        self.assertIsNotNone(self.recover(runtime, 1))
        self.assertEqual(grid.positions[1], position(0.5, 1))

    def test_recovery_waits_for_normal_navigation_batch(self):
        runtime, grid = recovery_runtime()
        moving = threading.Event()
        release_move = threading.Event()
        recovery_started = threading.Event()
        recovery_built = threading.Event()
        errors = []
        threads = []
        original_move = runtime.move_to_adjacent_position_direct
        original_build = runtime.build_navigation_request

        def move(agent_id, target):
            if agent_id == 0:
                moving.set()
                if not release_move.wait(3):
                    raise RuntimeError("test did not release normal batch")
            return original_move(agent_id, target)

        def build(robot, *args, **kwargs):
            if robot == "robot2":
                recovery_built.set()
            return original_build(robot, *args, **kwargs)

        def recover():
            recovery_started.set()
            self.recover(runtime, 1)

        runtime.move_to_adjacent_position_direct = move
        runtime.build_navigation_request = build
        try:
            threads.append(self.start_worker(
                lambda: runtime.navigate_to_object("robot1", "Tomato|0", allow_hand_preparation=False), errors,
            ))
            self.assertTrue(moving.wait(2))
            threads.append(self.start_worker(recover, errors))
            self.assertTrue(recovery_started.wait(2))
            self.assertFalse(recovery_built.wait(0.1), "recovery overlapped normal batch")
            self.assertEqual(grid.positions[1], position(0.0, 1))
        finally:
            release_move.set()
            self.finish_workers(threads)
        self.assertEqual(errors, [])
        self.assertTrue(recovery_built.is_set())

    def test_expired_deadline_stops_before_next_microstep_and_releases_lock(self):
        runtime, grid = recovery_runtime()
        now = [0.0]
        original_move = runtime.move_to_adjacent_position_direct

        def move(agent_id, target):
            result = original_move(agent_id, target)
            now[0] = 11.0
            return result

        runtime.move_to_adjacent_position_direct = move
        phase = PhaseCoordinator(runtime, (0,), deadline=10.0, timeout_error_factory=PlanExecutionTimeout)
        executor = Executor(runtime, "robot1", phase_coordinator=phase)
        executor.adapter = SimpleNamespace(execute=lambda *_args, **_kwargs: self.recover(runtime, 0))
        with patch("executor_system.runtime.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(PlanExecutionTimeout):
                executor.execute_action(Action("SliceObject", {"args": ("Tomato|0",)}))
        self.assertEqual(grid.successful_move_count, 1)
        self.assertEqual(grid.positions[0], position(0.25))
        runtime.move_to_adjacent_position_direct = original_move
        with self.assertRaises(PlanExecutionTimeout):
            self.recover(runtime, 1)
        install_control(runtime, None)
        errors = []
        thread = self.start_worker(lambda: self.recover(runtime, 1), errors)
        self.finish_workers([thread])
        self.assertEqual(errors, [])

    def test_recovery_releases_lock_and_marker_after_navigation_or_slice_error(self):
        for failure_stage in ("navigation", "slice"):
            with self.subTest(failure_stage=failure_stage):
                runtime, _grid = recovery_runtime()
                original_build = runtime.build_navigation_request
                original_step = runtime.step

                def fail(*_args, **_kwargs):
                    raise RuntimeError("injected failure")

                if failure_stage == "navigation":
                    runtime.build_navigation_request = fail
                    self.assertIsNone(self.recover(runtime, 0))
                else:
                    runtime.step = fail
                    with self.assertRaisesRegex(RuntimeError, "injected failure"):
                        self.recover(runtime, 0)
                runtime.build_navigation_request = original_build
                runtime.step = original_step
                # A different thread must be able to acquire the lock afterwards.
                errors = []
                thread = self.start_worker(lambda: self.recover(runtime, 1), errors)
                self.finish_workers([thread])
                self.assertEqual(errors, [])
                self.assertEqual(getattr(runtime._interaction_reposition_state, "active_keys", set()), set())


if __name__ == "__main__":
    unittest.main()
