import copy
import sys
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from executor_system.action_plan import Action, RobotExecutionState, StagePlan, TaskPlan, TaskRunner, WorldState
from executor_system.execution_control import ExecutionControl, ExecutionCancelled
from executor_system.runtime import ThorRuntime
from executor_system.executor import Executor
from executor_system.world_snapshot import SnapshotReadError, SnapshotStore


def multi_event(*, x=0, held=(), success=True):
    objects = [{"objectId": "Apple|1", "objectType": "Apple", "isPickedUp": bool(held),
                "position": {"x": x, "y": 0, "z": 0}, "parentReceptacles": []}]
    events = [SimpleNamespace(metadata={
        "lastActionSuccess": success, "errorMessage": "" if success else "rejected",
        "agent": {"position": {"x": x + i, "y": 0, "z": 0}, "rotation": {"y": 90 * i}},
        "inventoryObjects": [{"objectId": obj} for obj in held] if i == 1 else [],
        "objects": copy.deepcopy(objects),
    }) for i in range(2)]
    return SimpleNamespace(events=events, metadata=events[1].metadata)


class ProgrammableController:
    def __init__(self):
        self.last_event = multi_event()
        self.next_event = multi_event()

    def step(self, _payload):
        self.last_event = self.next_event
        return self.last_event


def snapshot_runtime():
    runtime = object.__new__(ThorRuntime)
    runtime.controller = ProgrammableController()
    runtime.controller_lock = threading.RLock()
    runtime.robot_agent_map = {"robot1": 0, "robot2": 1}
    runtime.physical_agent_count = 2
    runtime.state_version = 0
    runtime.execution_control = ExecutionControl()
    return runtime


class WorldSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.runtime = snapshot_runtime()
        self.control = self.runtime.execution_control
        self.store = SnapshotStore()

    def step(self, action="Pass", **payload):
        return self.runtime._step_direct({"action": action, "agentId": 1, **payload}, save_frame=False)

    def test_snapshot_is_immutable_and_detached(self):
        snapshot = self.store.capture(self.runtime, self.control)
        with self.assertRaises(TypeError):
            snapshot.robot_positions["robot1"]["x"] = 99
        with self.assertRaises(TypeError):
            snapshot.objects_by_id["Apple|1"]["position"]["x"] = 99
        with self.assertRaises(TypeError):
            snapshot.held_objects["robot1"] = set()
        with self.assertRaises(FrozenInstanceError):
            snapshot.version = 99
        self.runtime.controller.last_event.events[0].metadata["agent"]["position"]["x"] = 99
        self.assertEqual(snapshot.robot_positions["robot1"]["x"], 0)
        self.assertEqual(snapshot.objects_by_id["Apple|1"]["parentReceptacles"], ())
        self.assertEqual(snapshot.version, self.runtime.state_version)

    def test_robot_a_callback_sees_robot_b_and_empty_refresh_is_complete(self):
        world = WorldState(self.runtime)
        a = RobotExecutionState("robot1", "stage", [])
        world.refresh([a])
        old_snapshot = world.snapshot
        self.runtime.controller.next_event = multi_event(x=4, held=("Apple|1",))
        self.step("PickupObject", objectId="Apple|1")
        world.refresh([a])
        self.assertEqual(world.held_objects["robot2"], frozenset({"Apple|1"}))
        self.assertEqual(world.robot_positions["robot1"]["x"], 4)
        self.assertEqual(world.robot_positions["robot2"]["x"], 5)
        self.assertEqual(world.version, 1)
        self.assertEqual(old_snapshot.version, 0)
        self.assertFalse(old_snapshot.held_objects["robot2"])
        world.refresh([])
        self.assertEqual(set(world.robot_positions), {"robot1", "robot2"})
        self.assertEqual(world.robot_rotations["robot2"], 90)

    def test_capture_includes_unmapped_physical_agents(self):
        self.runtime.robot_agent_map = {"robot1": 0}
        snapshot = self.store.capture(self.runtime, self.control)
        self.assertEqual(set(snapshot.robot_positions), {"robot1", "robot2"})

    def test_missing_object_property_remains_unknown_data(self):
        snapshot = self.store.capture(self.runtime, self.control)
        self.assertNotIn("isCooked", snapshot.objects_by_id["Apple|1"])

    def test_unreadable_snapshot_raises_and_cancels_task_with_cause(self):
        world = WorldState(self.runtime)
        world.refresh([])
        self.runtime.controller.last_event.events[1].metadata.pop("agent")
        with self.assertRaises(SnapshotReadError) as caught:
            world.refresh([])
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertTrue(self.control.cancelled)

    def test_partial_multi_agent_event_is_not_a_valid_snapshot(self):
        self.runtime.controller.last_event.events.pop()
        with self.assertRaises(SnapshotReadError):
            self.store.capture(self.runtime, self.control)

    def test_cancelled_capture_never_reads_controller(self):
        self.control.cancel("stop")
        self.runtime.controller = None
        with self.assertRaisesRegex(ExecutionCancelled, "stop"):
            self.store.capture(self.runtime, self.control)

    def test_failed_action_still_commits_version_and_notifies_after_unlock(self):
        observed = []
        self.runtime.controller_lock = threading.Lock()
        def notify():
            acquired = self.runtime.controller_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            self.runtime.controller_lock.release()
            observed.append(self.store.capture(self.runtime, self.control).version)
        self.runtime.stage_scheduler = SimpleNamespace(notify_world_changed=notify)
        self.runtime.controller.next_event = multi_event(x=9, success=False)
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            self.step()
        self.assertEqual(observed, [1])
        self.assertEqual(self.store.capture(self.runtime, self.control).robot_positions["robot1"]["x"], 9)

    def test_controller_exception_does_not_commit_and_cancels_root(self):
        def broken(_payload):
            raise OSError("lost connection")
        self.runtime.controller.step = broken
        child = self.control.child()
        with self.runtime.action_deadline_scope(control=child):
            with self.assertRaisesRegex(OSError, "lost connection"):
                self.step()
        self.assertEqual(self.runtime.state_version, 0)
        self.assertTrue(self.control.cancelled)

    def test_committed_event_notifies_even_if_frame_saving_fails(self):
        versions = []
        self.runtime.stage_scheduler = SimpleNamespace(
            notify_world_changed=lambda: versions.append(self.runtime.state_version))
        def broken_frame(_event):
            raise OSError("frame write failed")
        self.runtime.save_frames = broken_frame
        with self.assertRaisesRegex(OSError, "frame write failed"):
            self.runtime._step_direct({"action": "Pass"})
        self.assertEqual(versions, [1])

    def test_malformed_teleport_commit_cancels_root_without_retry(self):
        malformed = multi_event(x=17)
        malformed.events[1].metadata["inventoryObjects"] = None
        following = multi_event(x=99)
        pending = iter((malformed, following))
        calls, notifications = [], []
        child = self.control.child()
        self.runtime.controller_lock = threading.Lock()

        def step(payload):
            calls.append(payload["action"])
            self.runtime.controller.last_event = next(pending)
            return self.runtime.controller.last_event

        def notify():
            acquired = self.runtime.controller_lock.acquire(blocking=False)
            self.assertTrue(acquired, "notification must run after controller unlock")
            self.runtime.controller_lock.release()
            notifications.append((self.runtime.state_version, self.control.cancelled, child.cancelled))

        self.runtime.controller.step = step
        self.runtime.stage_scheduler = SimpleNamespace(notify_world_changed=notify)
        with self.runtime.action_deadline_scope(control=child):
            with self.assertRaises(SnapshotReadError) as caught:
                self.runtime._step_with_retries(
                    {"action": "Teleport", "agentId": 1}, check_success=True,
                    save_frame=False, retry_on_failure=True, max_retries=1)

        self.assertIsInstance(caught.exception.__cause__, TypeError)
        self.assertEqual(calls, ["Teleport"])
        self.assertIs(self.runtime.controller.last_event, malformed)
        self.assertEqual(self.runtime.state_version, 1)
        self.assertTrue(self.control.cancelled)
        self.assertTrue(child.cancelled)
        self.assertEqual(notifications, [(1, True, True)])
        with self.assertRaises(ExecutionCancelled):
            self.store.capture(self.runtime, self.control)

    def test_pickup_override_has_commit_provenance_and_release_clears_it(self):
        self.step("PickupObject", objectId="Apple|1")
        snapshot = self.store.capture(self.runtime, self.control)
        self.assertEqual(snapshot.held_objects["robot2"], frozenset({"Apple|1"}))
        source = snapshot.held_object_sources["robot2"]["Apple|1"]
        self.assertEqual(dict(source), {"source": "action_commit", "action": "PickupObject", "version": 1})
        with self.assertRaises(TypeError):
            source["version"] = 100
        self.runtime.controller.next_event = multi_event()
        self.step("DropHandObject")
        self.assertFalse(self.store.capture(self.runtime, self.control).held_objects["robot2"])

    def test_metadata_is_recorded_as_inventory_source(self):
        self.runtime.controller.next_event = multi_event(held=("Apple|1",))
        self.step()
        snapshot = self.store.capture(self.runtime, self.control)
        self.assertEqual(dict(snapshot.held_object_sources["robot2"]["Apple|1"]),
                         {"source": "inventory", "version": 1})

    def test_failed_pickup_cannot_add_override(self):
        self.runtime.controller.next_event = multi_event(success=False)
        with self.assertRaises(RuntimeError):
            self.step("PickupObject", objectId="Apple|1")
        self.assertFalse(self.store.capture(self.runtime, self.control).held_objects["robot2"])

    def test_inventory_confirmation_retires_override_in_later_events(self):
        self.step("PickupObject", objectId="Apple|1")
        self.runtime.controller.next_event = multi_event(held=("Apple|1",))
        self.step()
        self.runtime.controller.next_event = multi_event()
        self.step()
        self.assertFalse(self.store.capture(self.runtime, self.control).held_objects["robot2"])

    def test_goal_effects_use_same_snapshot_as_callback(self):
        executor = Executor(self.runtime, robot_id="robot1", actions=[])
        def mutate_live_world(world):
            self.assertFalse(world.objects_by_id["Apple|1"]["isPickedUp"])
            for event in self.runtime.controller.last_event.events:
                event.metadata["objects"][0]["isPickedUp"] = True
            return True
        action = Action("PickupObject", expected_effects=(mutate_live_world, {"name": "Apple", "state": "PICKED"}),
                        on_failure="SKIP_IF_EFFECT_ALREADY_TRUE")
        self.assertIs(executor.effects_satisfied_after_failure(action), False)

    def test_global_condition_receives_complete_world_after_single_robot_stage(self):
        observed = []
        def global_condition(world):
            observed.append(set(world.robot_positions))
            return True
        plan = TaskPlan("task", [StagePlan("stage", {"robot1": [Action("Pass")]})],
                        global_success_condition=global_condition)
        with patch("executor_system.action_plan.AI2ThorAdapter.execute", return_value=multi_event()):
            TaskRunner(self.runtime).execute(plan)
        self.assertEqual(observed, [{"robot1", "robot2"}])

    def test_snapshot_error_aborts_task_before_any_action(self):
        self.runtime.controller.last_event.events[1].metadata.pop("objects")
        with self.assertRaises(SnapshotReadError):
            TaskRunner(self.runtime).execute(TaskPlan("task", [StagePlan("stage", {"robot1": [Action("Pass")]})]))
        self.assertTrue(self.runtime.execution_control.cancelled)
        self.assertEqual(self.runtime.state_version, 0)

    def test_uncommitted_legacy_override_does_not_contaminate_snapshot(self):
        self.runtime.record_agent_held_object(1, "Apple|old")
        self.assertFalse(self.store.capture(self.runtime, self.control).held_objects["robot2"])

    def test_capture_waits_for_atomic_event_and_version_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.controller.step
        def delayed(payload):
            result = original(payload)
            entered.set()
            self.assertTrue(release.wait(2))
            return result
        self.runtime.controller.next_event = multi_event(x=10)
        self.runtime.controller.step = delayed
        worker = threading.Thread(target=self.step)
        captures = []
        reader = threading.Thread(target=lambda: captures.append(self.store.capture(self.runtime, self.control)))
        worker.start()
        self.assertTrue(entered.wait(2))
        reader.start()
        release.set()
        worker.join(2)
        reader.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(captures[0].version, 1)
        self.assertEqual(captures[0].robot_positions["robot1"]["x"], 10)


if __name__ == "__main__":
    unittest.main()
