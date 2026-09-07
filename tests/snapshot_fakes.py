"""Complete multi-agent metadata for execution tests without Unity."""
import threading
from types import SimpleNamespace


class FakeEvent:
    def __init__(self, agent_id=0):
        self.metadata = {
            "lastActionSuccess": True,
            "agent": {"position": {"x": float(agent_id), "y": 0.0, "z": 0.0},
                      "rotation": {"y": 0.0}},
            "inventoryObjects": [],
            "objects": [],
        }


class FakeRuntime:
    physical_agent_count = 2

    def __init__(self):
        self.robot_agent_map = {f"robot{agent_id + 1}": agent_id
                                for agent_id in range(self.physical_agent_count)}
        self.robots = [dict(name=name, skills=[
            'GoToObject', 'PickupObject', 'TeleportObjectToHand', 'PutObject',
            'SwitchOn', 'SwitchOff', 'OpenObject', 'CloseObject', 'BreakObject',
            'BreakEgg', 'SliceObject', 'CleanObject', 'DirtyObject', 'EmptyLiquid',
            'RunMicrowave', 'RunCoffeeMachine', 'RunToaster', 'CookByStoveBurner',
            'HeatByStoveBurner', 'FireByStoveBurner', 'FillWater', 'ColdObject',
            'ThrowObject', 'MoveAhead', 'RotateLeft', 'RotateRight', 'LookUp',
            'LookDown', 'Teleport', 'ToggleObjectOn', 'ToggleObjectOff'], mass_capacity=100)
            for name in self.robot_agent_map]
        self.controller_lock = threading.RLock()
        self.state_version = 0
        events = [FakeEvent(agent_id) for agent_id in range(self.physical_agent_count)]
        self.controller = SimpleNamespace(last_event=SimpleNamespace(events=events, metadata=events[0].metadata))
        self.objects = []

    @property
    def objects(self):
        return self.controller.last_event.metadata["objects"]

    @objects.setter
    def objects(self, value):
        with self.controller_lock:
            for event in self.controller.last_event.events:
                event.metadata["objects"] = value

    def physical_agent_id(self, robot_id):
        return self.robot_agent_map[str(robot_id)]

    def current_agent_position(self, agent_id):
        return dict(self.agent_event(agent_id).metadata["agent"]["position"])

    def agent_event(self, agent_id):
        return self.controller.last_event.events[agent_id]

    def agent_held_objects_for(self, agent_id):
        return {obj["objectId"] for obj in self.agent_event(agent_id).metadata["inventoryObjects"]}

    def current_objects(self, _agent_id=None):
        return [dict(obj) for obj in self.objects]

    def step(self, _payload, **_kwargs):
        with self.controller_lock:
            self.state_version += 1
            return self.controller.last_event
