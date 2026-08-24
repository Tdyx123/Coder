import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.executor import PhaseCoordinator
from executor_system.movement import (
    MovementConfig,
    NavigationBatchAborted,
    NavigationMetrics,
    NavigationRequest,
    NavigationResult,
)
from executor_system.parallel_runner import PlanExecutionTimeout
from executor_system.step_movement import StepMovementStrategy
from executor_system.utils import position_to_grid_key
from multi_robot_avoidance import GeometryConflictModel, GridPoint
from tests.movement_fakes import GridThorRuntime


class CoordinatorRuntime:
    def __init__(self, mode, physical_agent_count):
        self.movement_config = MovementConfig.resolve(mode, environ={})
        self.physical_agent_count = physical_agent_count


def navigation_request(agent_id):
    position = {"x": agent_id * 0.5, "y": 0.0, "z": 0.0}
    destination = {
        "objectId": f"Target|{agent_id}",
        "objectType": "Target",
        "visible": True,
        "position": dict(position),
    }
    return NavigationRequest(
        robot=f"robot{agent_id + 1}",
        agent_id=agent_id,
        dest_obj=destination["objectId"],
        destination=destination,
        center=dict(position),
        candidate_positions=(dict(position),),
        object_resource=destination["objectId"],
        next_action=None,
        phase_coordinator=None,
    )


def navigation_result(request):
    return NavigationResult(
        destination=dict(request.destination),
        position=dict(request.candidate_positions[0]),
    )


def thor_position(grid_x, grid_z):
    return {
        "x": float(grid_x) * 0.25,
        "y": 0.0,
        "z": float(grid_z) * 0.25,
    }


class ActionWaveCoordinatorTest(unittest.TestCase):
    def run_threads(self, workers):
        threads = [threading.Thread(target=worker) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertFalse(
            [thread.name for thread in threads if thread.is_alive()],
            "action-wave worker did not terminate",
        )

    def test_teleport_mode_does_not_wait_for_action_wave(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("teleport", 2),
            active_agent_ids=[0, 1],
        )

        wave = coordinator.before_action(0, "GoToObject", 0)

        self.assertIsNone(wave)

    def test_navigation_batch_is_sorted_and_runs_once_regardless_of_arrival(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 2),
            active_agent_ids=[0, 1],
        )
        requests = {agent_id: navigation_request(agent_id) for agent_id in (0, 1)}
        waves = {}
        results = {}
        errors = []
        executed_batches = []
        agent_one_started = threading.Event()

        def execute_batch(batch, completed_agent_ids):
            executed_batches.append(
                (
                    tuple(request.agent_id for request in batch),
                    frozenset(completed_agent_ids),
                )
            )
            return {request.agent_id: navigation_result(request) for request in batch}

        def worker(agent_id):
            try:
                if agent_id == 1:
                    agent_one_started.set()
                wave = coordinator.before_action(agent_id, "GoToObject", 0)
                waves[agent_id] = wave
                results[agent_id] = coordinator.submit_step_navigation(
                    wave,
                    requests[agent_id],
                    execute_batch,
                )
            except BaseException as exc:
                errors.append(exc)

        thread_one = threading.Thread(target=worker, args=(1,))
        thread_one.start()
        self.assertTrue(agent_one_started.wait(timeout=0.5))
        thread_zero = threading.Thread(target=worker, args=(0,))
        thread_zero.start()
        for thread in (thread_one, thread_zero):
            thread.join(timeout=2.0)

        self.assertFalse(errors)
        self.assertFalse(thread_one.is_alive())
        self.assertFalse(thread_zero.is_alive())
        self.assertEqual(waves[0], waves[1])
        self.assertEqual(waves[0].navigation_agent_ids, (0, 1))
        self.assertEqual(executed_batches, [((0, 1), frozenset())])
        self.assertEqual(results[0].position, requests[0].candidate_positions[0])
        self.assertEqual(results[1].position, requests[1].candidate_positions[0])

    def test_non_navigation_action_waits_until_navigation_batch_finishes(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 2),
            active_agent_ids=[0, 1],
        )
        request = navigation_request(0)
        batch_started = threading.Event()
        release_batch = threading.Event()
        non_navigation_returned = threading.Event()
        errors = []

        def execute_batch(batch, _completed_agent_ids):
            batch_started.set()
            if not release_batch.wait(timeout=1.0):
                raise RuntimeError("test did not release batch")
            return {item.agent_id: navigation_result(item) for item in batch}

        def navigation_worker():
            try:
                wave = coordinator.before_action(0, "GoToObject", 0)
                coordinator.submit_step_navigation(wave, request, execute_batch)
            except BaseException as exc:
                errors.append(exc)

        def non_navigation_worker():
            try:
                coordinator.before_action(1, "OpenObject", 0)
                non_navigation_returned.set()
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=navigation_worker),
            threading.Thread(target=non_navigation_worker),
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(batch_started.wait(timeout=0.5))
        self.assertFalse(non_navigation_returned.is_set())
        release_batch.set()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(errors)
        self.assertTrue(non_navigation_returned.is_set())
        self.assertFalse([thread for thread in threads if thread.is_alive()])

    def test_mark_done_shrinks_unannounced_participant_set(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 2),
            active_agent_ids=[0, 1],
        )
        request = navigation_request(0)
        result = []
        errors = []
        waiting = threading.Event()

        def worker():
            try:
                waiting.set()
                wave = coordinator.before_action(0, "GoToObject", 0)
                result.append(
                    coordinator.submit_step_navigation(
                        wave,
                        request,
                        lambda batch, _completed: {
                            item.agent_id: navigation_result(item)
                            for item in batch
                        },
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(waiting.wait(timeout=0.5))
        coordinator.mark_agent_done(1)
        thread.join(timeout=2.0)

        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].position, request.candidate_positions[0])

    def test_completed_agents_are_snapshotted_for_batch_executor(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 3),
            active_agent_ids=[0, 1, 2],
        )
        coordinator.mark_agent_done(2)
        completed_snapshots = []
        errors = []

        def execute_batch(batch, completed_agent_ids):
            completed_snapshots.append(frozenset(completed_agent_ids))
            return {item.agent_id: navigation_result(item) for item in batch}

        def worker(agent_id):
            try:
                request = navigation_request(agent_id)
                wave = coordinator.before_action(agent_id, "GoToObject", 0)
                coordinator.submit_step_navigation(wave, request, execute_batch)
            except BaseException as exc:
                errors.append(exc)

        self.run_threads([lambda: worker(1), lambda: worker(0)])

        self.assertFalse(errors)
        self.assertEqual(completed_snapshots, [frozenset({2})])

    def test_batch_failure_propagates_root_and_aborts_other_participants(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 3),
            active_agent_ids=[0, 1, 2],
        )
        errors = {}

        def execute_batch(_batch, _completed_agent_ids):
            raise RuntimeError("joint planner exploded")

        def navigation_worker(agent_id):
            try:
                request = navigation_request(agent_id)
                wave = coordinator.before_action(agent_id, "GoToObject", 0)
                coordinator.submit_step_navigation(wave, request, execute_batch)
            except BaseException as exc:
                errors[agent_id] = exc

        def non_navigation_worker():
            try:
                coordinator.before_action(2, "OpenObject", 0)
            except BaseException as exc:
                errors[2] = exc

        self.run_threads(
            [
                lambda: navigation_worker(1),
                non_navigation_worker,
                lambda: navigation_worker(0),
            ]
        )

        self.assertIs(type(errors[0]), RuntimeError)
        self.assertEqual(str(errors[0]), "joint planner exploded")
        self.assertIsInstance(errors[1], NavigationBatchAborted)
        self.assertIsInstance(errors[2], NavigationBatchAborted)

    def test_navigation_failure_before_submission_aborts_waiting_wave(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 2),
            active_agent_ids=[0, 1],
        )
        errors = {}
        wave_ready = threading.Event()

        def failing_worker():
            try:
                wave = coordinator.before_action(0, "GoToObject", 0)
                failure = RuntimeError("target lookup failed")
                coordinator.abort_action_wave(wave, 0, failure)
                errors[0] = failure
                wave_ready.set()
            except BaseException as exc:
                errors[0] = exc
                wave_ready.set()

        def waiting_worker():
            try:
                request = navigation_request(1)
                wave = coordinator.before_action(1, "GoToObject", 0)
                self.assertTrue(wave_ready.wait(timeout=1.0))
                coordinator.submit_step_navigation(
                    wave,
                    request,
                    lambda _batch, _completed: self.fail(
                        "aborted wave must not execute a batch"
                    ),
                )
            except BaseException as exc:
                errors[1] = exc

        self.run_threads([failing_worker, waiting_worker])

        self.assertEqual(str(errors[0]), "target lookup failed")
        self.assertIsInstance(errors[1], NavigationBatchAborted)

    def test_expired_deadline_uses_existing_timeout_error(self):
        coordinator = PhaseCoordinator(
            CoordinatorRuntime("step", 1),
            active_agent_ids=[0],
            deadline=time.monotonic() - 1.0,
            timeout_error_factory=PlanExecutionTimeout,
        )

        with self.assertRaises(PlanExecutionTimeout):
            coordinator.before_action(0, "GoToObject", 0)


class JointMovementWaveTest(unittest.TestCase):
    def test_crossing_requests_use_one_safe_joint_planning_batch(self):
        walkable = [
            thor_position(x, z)
            for x in range(5)
            for z in range(5)
        ]
        destinations = [
            {
                "objectId": "Target|0",
                "objectType": "Target",
                "visible": True,
                "position": thor_position(4, 1),
            },
            {
                "objectId": "Target|1",
                "objectType": "Target",
                "visible": True,
                "position": thor_position(3, 4),
            },
        ]
        runtime = GridThorRuntime(
            positions={0: thor_position(0, 1), 1: thor_position(3, 0)},
            walkable_by_agent={0: walkable, 1: walkable},
            objects=destinations,
        )
        config = MovementConfig.resolve("step", environ={})
        runtime.movement_config = config
        metrics = NavigationMetrics(config.mode)
        strategy = StepMovementStrategy(runtime, config, metrics)
        coordinator = PhaseCoordinator(runtime, active_agent_ids=[0, 1])
        requests = {
            agent_id: NavigationRequest(
                robot=f"robot{agent_id + 1}",
                agent_id=agent_id,
                dest_obj=destination["objectId"],
                destination=dict(destination),
                center=dict(destination["position"]),
                candidate_positions=(dict(destination["position"]),),
                object_resource=destination["objectId"],
                next_action=None,
                phase_coordinator=coordinator,
            )
            for agent_id, destination in enumerate(destinations)
        }
        results = {}
        errors = []

        def worker(agent_id):
            try:
                wave = coordinator.before_action(agent_id, "GoToObject", 0)
                results[agent_id] = strategy.navigate(
                    replace(requests[agent_id], action_wave=wave)
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(1,)),
            threading.Thread(target=worker, args=(0,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)

        self.assertFalse(errors)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(position_to_grid_key(results[0].position), (4, 1))
        self.assertEqual(position_to_grid_key(results[1].position), (3, 4))
        self.assertEqual(metrics.to_dict()["planning_batches"], 1)
        self.assertNotIn("Teleport", [action[0] for action in runtime.actions])

        conflict_model = GeometryConflictModel(0.25, 0.35)
        for positions in runtime.position_history:
            left = GridPoint(*position_to_grid_key(positions[0]))
            right = GridPoint(*position_to_grid_key(positions[1]))
            self.assertFalse(conflict_model.conflicts(left, right))
        for previous, current in zip(
            runtime.successful_edges,
            runtime.successful_edges[1:],
        ):
            self.assertNotEqual(previous, (current[1], current[0]))


if __name__ == "__main__":
    unittest.main()
