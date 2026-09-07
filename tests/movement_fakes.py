from collections import Counter

from executor_system.utils import position_to_grid_key


class GridThorRuntime:
    """A deterministic stateful substitute for the Unity action boundary."""

    def __init__(self, positions, walkable_by_agent, objects):
        self.physical_agent_count = len(positions)
        self.positions = {
            int(agent_id): dict(position)
            for agent_id, position in positions.items()
        }
        self.walkable_by_agent = {
            int(agent_id): [dict(position) for position in positions]
            for agent_id, positions in walkable_by_agent.items()
        }
        self.objects = {
            str(obj["objectId"]): dict(obj)
            for obj in objects
        }
        self.reachable_queries = 0
        self.query_move_counts = []
        self.query_agents = []
        self.query_responses = {}
        self.held_by_agent = {}
        self.objects_after_successful_moves = {}
        self.actions = []
        self.position_history = [
            {
                agent_id: dict(position)
                for agent_id, position in sorted(self.positions.items())
            }
        ]
        self.failed_edges = set()
        self.edge_errors = {}
        self.edge_attempts = Counter()
        self.successful_edges = []
        self.position_overrides = {}
        self.walkable_after_successful_moves = {}
        self.object_visibility_by_position = {}
        self.scene_bounds = []
        self.successful_move_count = 0

    def physical_agent_id(self, robot):
        if isinstance(robot, dict):
            robot = robot.get("name")
        return int(str(robot).removeprefix("robot")) - 1

    def current_agent_position(self, agent_id):
        return dict(self.positions[int(agent_id)])

    def agent_position_items(self, exclude_agent_id=None):
        return [
            (agent_id, dict(position))
            for agent_id, position in sorted(self.positions.items())
            if agent_id != exclude_agent_id
        ]

    def current_objects(self, agent_id=None):
        return list(self.objects.values())

    def agent_held_objects_for(self, agent_id):
        return set(self.held_by_agent.get(agent_id, ()))

    def refresh_reachable_positions(self, agent_id):
        self.reachable_queries += 1
        self.query_move_counts.append(self.successful_move_count)
        self.query_agents.append(int(agent_id))
        response = self.query_responses.get(self.reachable_queries)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return [dict(position) for position in response]
        return [
            dict(position)
            for position in self.walkable_by_agent[int(agent_id)]
        ]

    def move_to_adjacent_position_direct(self, agent_id, target):
        agent_id = int(agent_id)
        source_key = position_to_grid_key(self.positions[agent_id])
        target_key = position_to_grid_key(target)
        edge = (source_key, target_key)
        self.actions.append(("MoveAhead", agent_id, source_key, target_key))
        self.edge_attempts[edge] += 1
        if edge in self.edge_errors:
            raise self.edge_errors[edge]
        reachable_keys = {
            position_to_grid_key(position)
            for position in self.walkable_by_agent[agent_id]
        }
        if edge in self.failed_edges or target_key not in reachable_keys:
            return False
        actual_target = self.position_overrides.pop(edge, target)
        self.positions[agent_id] = dict(actual_target)
        self.successful_edges.append(edge)
        self.position_history.append(
            {
                current_agent_id: dict(position)
                for current_agent_id, position in sorted(self.positions.items())
            }
        )
        self.successful_move_count += 1
        objects = self.objects_after_successful_moves.get(self.successful_move_count)
        if objects is not None:
            self.objects = {key: dict(value) for key, value in objects.items()}
        replacement = self.walkable_after_successful_moves.get(
            self.successful_move_count
        )
        if replacement is not None:
            self.walkable_by_agent = {
                int(current_agent_id): [dict(position) for position in positions]
                for current_agent_id, positions in replacement.items()
            }
        return True

    def face_position_direct(self, agent_id, target):
        self.actions.append(("Face", int(agent_id), dict(target)))

    def find_object(self, object_id, *, agent_id=None, require_center=False):
        obj = dict(self.objects[str(object_id)])
        if agent_id is not None:
            visibility = self.object_visibility_by_position.get(str(object_id), {})
            current_key = position_to_grid_key(self.positions[int(agent_id)])
            if current_key in visibility:
                obj["visible"] = bool(visibility[current_key])
        if require_center and not obj.get("position"):
            raise RuntimeError(f"Object {object_id!r} has no usable center.")
        return obj

    def scene_object_bounds(self, agent_id=None):
        return list(self.scene_bounds)
