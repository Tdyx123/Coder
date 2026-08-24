import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from multi_robot_avoidance import (  # noqa: E402
    CompositeConflictModel,
    FakeRuntime,
    GlobalWalkableMap,
    GeometryConflictModel,
    GridPoint,
    ScenarioValidationError,
    TableConflictModel,
    WalkableUpdateError,
    WorldState,
    load_scenario,
    plan_scenario,
)


def basic_scenario_data():
    return {
        "grid_size_m": 0.25,
        "hard_clearance_m": 0.35,
        "max_ticks": 32,
        "max_assignment_trials": 256,
        "walkable": [[x, z] for x in range(5) for z in range(5)],
        "conflicts": [],
        "robots": [
            {
                "id": "A",
                "start": [0, 0],
                "candidates": [
                    {"id": "A1", "position": [4, 0], "cost": 1.0}
                ],
            },
            {
                "id": "B",
                "start": [0, 4],
                "candidates": [
                    {"id": "B1", "position": [4, 4], "cost": 1.0}
                ],
            },
        ],
    }


class GlobalWalkableMapTest(unittest.TestCase):
    def test_single_robot_update_replaces_snapshot_and_recomputes_union(self):
        walkable_map = GlobalWalkableMap(("A", "B"), grid_size_m=0.25)
        initial = walkable_map.replace_all(
            {
                "A": [
                    {"x": 0.0, "y": 0.0, "z": 0.0},
                    {"x": 0.25, "y": 0.0, "z": 0.0},
                ],
                "B": [{"x": 1.0, "y": 0.0, "z": 0.0}],
            }
        )

        updated = walkable_map.update_robot(
            "A",
            [
                {"x": -0.25, "y": 0.0, "z": 0.0},
                {"x": -0.25, "y": 1.0, "z": 0.0},
            ],
        )

        self.assertEqual(initial.version, 1)
        self.assertEqual(updated.version, 2)
        self.assertTrue(updated.changed)
        self.assertEqual(updated.walkable, frozenset({GridPoint(-1, 0), GridPoint(4, 0)}))
        self.assertEqual(updated.added, frozenset({GridPoint(-1, 0)}))
        self.assertEqual(updated.removed, frozenset({GridPoint(0, 0), GridPoint(1, 0)}))

    def test_invalid_full_refresh_is_atomic(self):
        walkable_map = GlobalWalkableMap(("A", "B"), grid_size_m=0.25)
        walkable_map.replace_all(
            {
                "A": [{"x": 0.0, "z": 0.0}],
                "B": [{"x": 1.0, "z": 0.0}],
            }
        )

        with self.assertRaisesRegex(WalkableUpdateError, "non-empty"):
            walkable_map.replace_all(
                {
                    "A": [{"x": -0.25, "z": 0.0}],
                    "B": [],
                }
            )

        self.assertEqual(walkable_map.version, 1)
        self.assertEqual(
            walkable_map.walkable,
            frozenset({GridPoint(0, 0), GridPoint(4, 0)}),
        )

    def test_rejects_nonfinite_coordinates_and_unknown_robots(self):
        walkable_map = GlobalWalkableMap(("A", "B"), grid_size_m=0.25)
        walkable_map.replace_all(
            {
                "A": [{"x": 0.0, "z": 0.0}],
                "B": [{"x": 1.0, "z": 0.0}],
            }
        )

        with self.assertRaisesRegex(WalkableUpdateError, "finite numbers"):
            walkable_map.update_robot("A", [{"x": float("nan"), "z": 0.0}])
        with self.assertRaisesRegex(WalkableUpdateError, "Unknown robot"):
            walkable_map.update_robot("C", [{"x": 0.0, "z": 0.0}])

        self.assertEqual(walkable_map.version, 1)


class ScenarioModelTest(unittest.TestCase):
    def test_load_scenario_applies_thor_aligned_values(self):
        scenario = load_scenario(basic_scenario_data())

        self.assertEqual(scenario.grid_size_m, 0.25)
        self.assertEqual(scenario.hard_clearance_m, 0.35)
        self.assertEqual([robot.robot_id for robot in scenario.robots], ["A", "B"])
        self.assertEqual(scenario.robots[0].start, GridPoint(0, 0))

    def test_geometry_conflict_treats_clearance_boundary_as_occupied(self):
        model = GeometryConflictModel(grid_size_m=0.25, hard_clearance_m=0.35)

        self.assertTrue(model.conflicts(GridPoint(0, 0), GridPoint(1, 0)))
        self.assertFalse(model.conflicts(GridPoint(0, 0), GridPoint(1, 1)))

    def test_load_scenario_accepts_one_robot(self):
        data = basic_scenario_data()
        data["robots"] = data["robots"][:1]

        scenario = load_scenario(data)
        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual([robot.robot_id for robot in scenario.robots], ["A"])
        self.assertEqual(result.status, "PLANNED")
        self.assertEqual(
            result.plan.paths["A"],
            (
                GridPoint(0, 0),
                GridPoint(1, 0),
                GridPoint(2, 0),
                GridPoint(3, 0),
                GridPoint(4, 0),
            ),
        )

    def test_load_scenario_rejects_robot_count_outside_one_to_four(self):
        for robots in ([], basic_scenario_data()["robots"] * 3):
            with self.subTest(robot_count=len(robots)):
                data = basic_scenario_data()
                data["robots"] = robots

                with self.assertRaisesRegex(
                    ScenarioValidationError,
                    "1 to 4 robots",
                ):
                    load_scenario(data)

    def test_load_scenario_rejects_invalid_blocked_transitions(self):
        for blocked_transitions in (
            [[[0, 0], [0, 0]]],
            [[[0, 0], [99, 99]]],
        ):
            with self.subTest(blocked_transitions=blocked_transitions):
                data = basic_scenario_data()
                data["blocked_transitions"] = blocked_transitions

                with self.assertRaises(ScenarioValidationError):
                    load_scenario(data)

    def test_composite_conflict_model_includes_explicit_point_pairs(self):
        geometry = GeometryConflictModel(grid_size_m=0.25, hard_clearance_m=0.1)
        table = TableConflictModel([(GridPoint(0, 0), GridPoint(3, 3))])
        model = CompositeConflictModel((geometry, table))

        self.assertTrue(model.conflicts(GridPoint(0, 0), GridPoint(3, 3)))
        self.assertFalse(model.conflicts(GridPoint(0, 0), GridPoint(0, 1)))

    def test_load_scenario_rejects_explicit_initial_position_conflict(self):
        data = basic_scenario_data()
        data["conflicts"] = [[[0, 0], [0, 4]]]

        with self.assertRaisesRegex(ScenarioValidationError, "initial conflict"):
            load_scenario(data)

    def test_load_scenario_normalizes_dynamic_walkable_events(self):
        data = basic_scenario_data()
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
                {
                    "at_committed_step": 1,
                    "type": "robot_step",
                    "robot_id": "A",
                    "reachable_positions": all_positions,
                },
                {
                    "at_committed_step": 1,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
            ]
        }

        scenario = load_scenario(data)

        events = scenario.execution.walkable_events
        self.assertEqual(
            [(event.at_committed_step, event.event_type) for event in events],
            [(0, "active_refresh"), (1, "robot_step"), (1, "active_refresh")],
        )
        self.assertEqual(events[1].robot_id, "A")
        self.assertEqual(len(events[1].reachable_positions), 25)
        self.assertEqual(set(events[2].reachable_positions_by_robot), {"A", "B"})

    def test_dynamic_walkable_requires_initial_active_refresh(self):
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 1,
                    "type": "robot_step",
                    "robot_id": "A",
                    "reachable_positions": [{"x": 0.0, "z": 0.0}],
                }
            ]
        }

        with self.assertRaisesRegex(ScenarioValidationError, "active_refresh"):
            load_scenario(data)


class JointAssignmentTest(unittest.TestCase):
    def test_planner_uses_second_candidate_when_first_choices_conflict(self):
        data = basic_scenario_data()
        data["robots"][0]["candidates"] = [
            {"id": "A1", "position": [4, 2], "cost": 1.0},
            {"id": "A2", "position": [4, 0], "cost": 2.0},
        ]
        data["robots"][1]["candidates"] = [
            {"id": "B1", "position": [4, 2], "cost": 1.0},
            {"id": "B2", "position": [4, 4], "cost": 2.0},
        ]
        scenario = load_scenario(data)

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "PLANNED")
        self.assertNotEqual(
            [result.plan.assignment[robot_id].candidate_id for robot_id in ("A", "B")],
            ["A1", "B1"],
        )
        self.assertEqual(result.plan.metrics["candidate_cost"], 3.0)
        self.assertFalse(result.plan.metrics["search_truncated"])
        self.assertIn(
            "reservations_created",
            [entry["event"] for entry in result.decision_trace.entries],
        )

    def test_joint_assignment_avoids_a_greedy_dead_end(self):
        data = {
            "grid_size_m": 0.25,
            "hard_clearance_m": 0.35,
            "walkable": [[x, z] for x in range(9) for z in range(9)],
            "conflicts": [
                [[8, 0], [8, 8]],
                [[8, 0], [8, 6]],
            ],
            "robots": [
                {
                    "id": "A",
                    "start": [0, 0],
                    "candidates": [
                        {"id": "A1", "position": [8, 0], "cost": 1.0},
                        {"id": "A2", "position": [8, 2], "cost": 2.0},
                    ],
                },
                {
                    "id": "B",
                    "start": [0, 8],
                    "candidates": [
                        {"id": "B1", "position": [8, 8], "cost": 1.0},
                        {"id": "B2", "position": [8, 6], "cost": 100.0},
                    ],
                },
            ],
        }
        scenario = load_scenario(data)

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "PLANNED")
        self.assertEqual(
            {
                robot_id: candidate.candidate_id
                for robot_id, candidate in result.plan.assignment.items()
            },
            {"A": "A2", "B": "B1"},
        )

    def test_planner_skips_candidate_removed_from_dynamic_walkable(self):
        data = basic_scenario_data()
        data["robots"][0]["candidates"] = [
            {"id": "A1", "position": [4, 0], "cost": 1.0},
            {"id": "A2", "position": [4, 1], "cost": 2.0},
        ]
        scenario = load_scenario(data)
        dynamic_scenario = replace(
            scenario,
            walkable=frozenset(
                point for point in scenario.walkable if point != GridPoint(4, 0)
            ),
        )

        result = plan_scenario(
            dynamic_scenario,
            WorldState.from_scenario(dynamic_scenario),
        )

        self.assertEqual(result.status, "PLANNED")
        self.assertEqual(result.plan.assignment["A"].candidate_id, "A2")
        rejected = [
            entry
            for entry in result.decision_trace.entries
            if entry.get("reason") == "candidate_not_walkable"
        ]
        self.assertEqual(rejected[0]["candidates"], ["A1", "B1"])
        self.assertEqual(rejected[0]["invalid_candidates"], ["A1"])


class SpaceTimePlanningTest(unittest.TestCase):
    def test_directed_blocked_transition_forces_detour(self):
        scenario = load_scenario(
            {
                "grid_size_m": 0.25,
                "hard_clearance_m": 0.1,
                "walkable": [
                    [0, 0],
                    [1, 0],
                    [2, 0],
                    [0, 1],
                    [1, 1],
                    [2, 1],
                ],
                "blocked_transitions": [[[0, 0], [1, 0]]],
                "robots": [
                    {
                        "id": "A",
                        "start": [0, 0],
                        "candidates": [
                            {"id": "A1", "position": [2, 0], "cost": 0}
                        ],
                    }
                ],
            }
        )

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "PLANNED")
        path = result.plan.paths["A"]
        self.assertEqual(path[0], GridPoint(0, 0))
        self.assertEqual(path[-1], GridPoint(2, 0))
        self.assertEqual(len(path), 5)
        self.assertNotIn(
            (GridPoint(0, 0), GridPoint(1, 0)),
            tuple(zip(path, path[1:])),
        )

    def test_directed_blocked_transition_keeps_reverse_direction_open(self):
        scenario = load_scenario(
            {
                "grid_size_m": 0.25,
                "hard_clearance_m": 0.1,
                "walkable": [[0, 0], [1, 0]],
                "blocked_transitions": [[[0, 0], [1, 0]]],
                "robots": [
                    {
                        "id": "A",
                        "start": [1, 0],
                        "candidates": [
                            {"id": "A1", "position": [0, 0], "cost": 0}
                        ],
                    }
                ],
            }
        )

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "PLANNED")
        self.assertEqual(
            result.plan.paths["A"],
            (GridPoint(1, 0), GridPoint(0, 0)),
        )

    def test_vertex_conflict_is_resolved_with_a_wait(self):
        data = {
            "grid_size_m": 0.25,
            "hard_clearance_m": 0.1,
            "walkable": [[0, 1], [1, 1], [2, 1], [1, 0], [1, 2]],
            "robots": [
                {
                    "id": "A",
                    "start": [0, 1],
                    "candidates": [
                        {"id": "A1", "position": [2, 1], "cost": 1.0}
                    ],
                },
                {
                    "id": "B",
                    "start": [1, 0],
                    "candidates": [
                        {"id": "B1", "position": [1, 2], "cost": 1.0}
                    ],
                },
            ],
        }
        scenario = load_scenario(data)

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "PLANNED")
        self.assertTrue(
            any(
                source == target
                for path in result.plan.paths.values()
                for source, target in zip(path, path[1:])
            )
        )
        for positions in result.plan.reservations.positions_by_tick.values():
            self.assertEqual(len(set(positions.values())), 2)

    def test_edge_swap_in_a_two_cell_corridor_has_no_plan(self):
        data = {
            "grid_size_m": 0.25,
            "hard_clearance_m": 0.1,
            "walkable": [[0, 0], [1, 0]],
            "robots": [
                {
                    "id": "A",
                    "start": [0, 0],
                    "candidates": [
                        {"id": "A1", "position": [1, 0], "cost": 1.0}
                    ],
                },
                {
                    "id": "B",
                    "start": [1, 0],
                    "candidates": [
                        {"id": "B1", "position": [0, 0], "cost": 1.0}
                    ],
                },
            ],
        }
        scenario = load_scenario(data)

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "NO_PLAN_FOUND")
        self.assertIsNone(result.plan)

    def test_goal_position_remains_reserved_through_horizon(self):
        scenario = load_scenario(basic_scenario_data())

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(
            result.plan.reservations.positions_by_tick[scenario.max_ticks]["A"],
            GridPoint(4, 0),
        )

    def test_synchronous_move_is_rejected_when_no_serial_order_is_safe(self):
        data = {
            "grid_size_m": 1.0,
            "hard_clearance_m": 1.1,
            "walkable": [[0, 0], [1, 0], [0, 1], [1, 1]],
            "robots": [
                {
                    "id": "A",
                    "start": [0, 0],
                    "candidates": [
                        {"id": "A1", "position": [1, 0], "cost": 1.0}
                    ],
                },
                {
                    "id": "B",
                    "start": [1, 1],
                    "candidates": [
                        {"id": "B1", "position": [0, 1], "cost": 1.0}
                    ],
                },
            ],
        }
        scenario = load_scenario(data)

        result = plan_scenario(scenario, WorldState.from_scenario(scenario))

        self.assertEqual(result.status, "NO_PLAN_FOUND")


class FakeRuntimeTest(unittest.TestCase):
    def test_execute_commits_all_micro_steps_and_finishes_robots(self):
        scenario = load_scenario(basic_scenario_data())
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan
        runtime = FakeRuntime(world)

        result = runtime.execute(plan)

        self.assertEqual(result.status, "EXECUTED")
        self.assertEqual(result.committed_micro_steps, len(plan.micro_steps))
        self.assertEqual(
            result.world_state.positions,
            {
                robot_id: candidate.position
                for robot_id, candidate in plan.assignment.items()
            },
        )
        self.assertEqual(set(result.world_state.statuses.values()), {"DONE"})
        self.assertEqual(result.world_state.version, len(plan.micro_steps))

    def test_dynamic_walkable_updates_after_every_step_without_replanning_when_unchanged(self):
        static_scenario = load_scenario(basic_scenario_data())
        static_plan = plan_scenario(
            static_scenario,
            WorldState.from_scenario(static_scenario),
        ).plan
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
                *[
                    {
                        "at_committed_step": index,
                        "type": "robot_step",
                        "robot_id": step.robot_id,
                        "reachable_positions": all_positions,
                    }
                    for index, step in enumerate(static_plan.micro_steps, start=1)
                ],
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan
        runtime = FakeRuntime(world, scenario=scenario)

        result = runtime.execute(plan)

        self.assertEqual(result.status, "EXECUTED")
        self.assertEqual(result.walkable_version, len(plan.micro_steps) + 1)
        self.assertEqual(result.walkable, scenario.walkable)
        self.assertEqual(result.replan_count, 0)

    def test_removed_future_path_point_triggers_replan_and_detour(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        detour_positions = [
            position
            for position in all_positions
            if not (position["x"] == 0.5 and position["z"] == 0.0)
        ]
        expected_actors = ["A", "A", "B", "A", "B", "A", "B", "A", "B", "A"]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": detour_positions,
                    },
                },
                *[
                    {
                        "at_committed_step": index,
                        "type": "robot_step",
                        "robot_id": robot_id,
                        "reachable_positions": detour_positions,
                    }
                    for index, robot_id in enumerate(expected_actors, start=1)
                ],
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan
        runtime = FakeRuntime(world, scenario=scenario)

        result = runtime.execute(plan)

        self.assertEqual(result.status, "EXECUTED")
        self.assertEqual(result.replan_count, 1)
        self.assertEqual(result.committed_micro_steps, 10)
        self.assertEqual(result.world_state.positions["A"], GridPoint(4, 0))
        committed_positions = [
            entry["position"]
            for entry in result.decision_trace.entries
            if entry["event"] == "micro_step_committed" and entry["robot_id"] == "A"
        ]
        self.assertIn([1, 1], committed_positions)
        self.assertIn(
            "plan_invalidated",
            [entry["event"] for entry in result.decision_trace.entries],
        )

    def test_missing_step_update_aborts_dynamic_execution(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                }
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "INVALID_WALKABLE_UPDATE")
        self.assertEqual(result.committed_micro_steps, 1)
        self.assertIn("requires exactly one robot_step", result.message)
        self.assertEqual(result.walkable_version, 1)

    def test_update_omitting_current_robot_position_rolls_back_boundary(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        without_first_target = [
            position
            for position in all_positions
            if not (position["x"] == 0.25 and position["z"] == 0.0)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": without_first_target,
                    },
                },
                {
                    "at_committed_step": 1,
                    "type": "robot_step",
                    "robot_id": "A",
                    "reachable_positions": without_first_target,
                },
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "INVALID_WALKABLE_UPDATE")
        self.assertIn("omits robot positions", result.message)
        self.assertEqual(result.walkable_version, 1)
        self.assertIn(GridPoint(1, 0), result.walkable)

    def test_removed_only_endpoint_returns_replan_failed(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        without_endpoint = [
            position
            for position in all_positions
            if not (position["x"] == 1.0 and position["z"] == 0.0)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": without_endpoint,
                    },
                },
                {
                    "at_committed_step": 1,
                    "type": "robot_step",
                    "robot_id": "A",
                    "reachable_positions": without_endpoint,
                },
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "REPLAN_FAILED")
        self.assertEqual(result.replan_count, 1)
        self.assertEqual(result.committed_micro_steps, 1)
        self.assertEqual(set(result.world_state.statuses.values()), {"ABORTED"})
        self.assertEqual(plan.reservations.released_from_tick, 0)

    def test_active_refresh_after_robot_update_uses_final_union_without_replan(self):
        static_scenario = load_scenario(basic_scenario_data())
        static_plan = plan_scenario(
            static_scenario,
            WorldState.from_scenario(static_scenario),
        ).plan
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        without_off_path_point = [
            position
            for position in all_positions
            if not (position["x"] == 0.5 and position["z"] == 0.5)
        ]
        step_events = [
            {
                "at_committed_step": index,
                "type": "robot_step",
                "robot_id": step.robot_id,
                "reachable_positions": without_off_path_point,
            }
            for index, step in enumerate(static_plan.micro_steps, start=1)
        ]
        step_events.insert(
            1,
            {
                "at_committed_step": 1,
                "type": "active_refresh",
                "reachable_positions_by_robot": {
                    "A": without_off_path_point,
                    "B": without_off_path_point,
                },
            },
        )
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
                *step_events,
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "EXECUTED")
        self.assertEqual(result.replan_count, 0)
        self.assertEqual(result.walkable_version, len(plan.micro_steps) + 2)
        updates_at_one = [
            entry["update_type"]
            for entry in result.decision_trace.entries
            if entry["event"] == "walkable_updated"
            and entry["at_committed_step"] == 1
        ]
        self.assertEqual(updates_at_one, ["robot_step", "active_refresh"])

    def test_invalid_active_refresh_rolls_back_same_boundary_robot_update(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        without_first_target = [
            position
            for position in all_positions
            if not (position["x"] == 0.25 and position["z"] == 0.0)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
                {
                    "at_committed_step": 1,
                    "type": "robot_step",
                    "robot_id": "A",
                    "reachable_positions": all_positions,
                },
                {
                    "at_committed_step": 1,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": without_first_target,
                        "B": without_first_target,
                    },
                },
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "INVALID_WALKABLE_UPDATE")
        self.assertEqual(result.walkable_version, 1)
        self.assertIn(GridPoint(1, 0), result.walkable)
        self.assertEqual(
            [
                entry
                for entry in result.decision_trace.entries
                if entry["event"] == "walkable_updated"
                and entry["at_committed_step"] == 1
            ],
            [],
        )

    def test_removed_endpoint_replans_to_alternate_candidate(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        without_primary_endpoint = [
            position
            for position in all_positions
            if not (position["x"] == 1.0 and position["z"] == 0.0)
        ]
        expected_actors = ["A", "A", "B", "A", "B", "A", "B", "A", "B"]
        data = basic_scenario_data()
        data["robots"][0]["candidates"] = [
            {"id": "A1", "position": [4, 0], "cost": 1.0},
            {"id": "A2", "position": [4, 1], "cost": 2.0},
        ]
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": without_primary_endpoint,
                    },
                },
                *[
                    {
                        "at_committed_step": index,
                        "type": "robot_step",
                        "robot_id": robot_id,
                        "reachable_positions": without_primary_endpoint,
                    }
                    for index, robot_id in enumerate(expected_actors, start=1)
                ],
            ]
        }
        scenario = load_scenario(data)
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan

        result = FakeRuntime(world, scenario=scenario).execute(plan)

        self.assertEqual(result.status, "EXECUTED")
        self.assertEqual(result.replan_count, 1)
        self.assertEqual(result.world_state.positions["A"], GridPoint(4, 1))

    def test_external_version_change_rejects_stale_plan_before_first_step(self):
        scenario = load_scenario(basic_scenario_data())
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan
        runtime = FakeRuntime(world)

        result = runtime.execute(plan, external_version_bump_before_micro_step=0)

        self.assertEqual(result.status, "COMMIT_REJECTED_STALE_WORLD")
        self.assertEqual(result.committed_micro_steps, 0)
        self.assertEqual(result.world_state.positions, plan.initial_positions)

    def test_execution_failure_keeps_prior_moves_and_releases_future_reservations(self):
        scenario = load_scenario(basic_scenario_data())
        world = WorldState.from_scenario(scenario)
        plan = plan_scenario(scenario, world).plan
        runtime = FakeRuntime(world)
        first_step = plan.micro_steps[0]
        failed_step = plan.micro_steps[1]

        result = runtime.execute(plan, failure_at_micro_step=1)

        self.assertEqual(result.status, "EXECUTION_FAILED")
        self.assertEqual(result.committed_micro_steps, 1)
        self.assertEqual(result.failed_robot_id, failed_step.robot_id)
        self.assertEqual(result.world_state.positions[first_step.robot_id], first_step.target)
        self.assertEqual(result.world_state.positions[failed_step.robot_id], failed_step.source)
        self.assertEqual(result.world_state.statuses[failed_step.robot_id], "FAILED")
        self.assertTrue(
            all(
                status in {"FAILED", "ABORTED"}
                for status in result.world_state.statuses.values()
            )
        )
        self.assertEqual(plan.reservations.released_from_tick, failed_step.tick + 1)
        release_events = [
            entry
            for entry in result.decision_trace.entries
            if entry["event"] == "reservations_released"
        ]
        self.assertEqual(
            release_events,
            [{"event": "reservations_released", "from_tick": failed_step.tick + 1}],
        )
        self.assertTrue(
            all(
                tick < plan.reservations.released_from_tick
                for tick in plan.reservations.positions_by_tick
            )
        )


class ResultAndCliTest(unittest.TestCase):
    def test_planning_result_serialization_is_deterministic(self):
        scenario = load_scenario(basic_scenario_data())

        first = plan_scenario(scenario, WorldState.from_scenario(scenario)).to_dict()
        second = plan_scenario(scenario, WorldState.from_scenario(scenario)).to_dict()

        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual(set(first), {
            "status",
            "message",
            "base_world_version",
            "assignment",
            "paths",
            "priority_order",
            "reservations",
            "micro_steps",
            "metrics",
            "execution",
            "decision_trace",
        })

    def test_search_limit_is_distinct_from_exhausted_search(self):
        data = {
            "grid_size_m": 0.25,
            "hard_clearance_m": 0.1,
            "max_assignment_trials": 1,
            "walkable": [[0, 0], [1, 0]],
            "robots": [
                {
                    "id": "A",
                    "start": [0, 0],
                    "candidates": [
                        {"id": "A1", "position": [1, 0], "cost": 1.0},
                        {"id": "A2", "position": [1, 0], "cost": 2.0},
                    ],
                },
                {
                    "id": "B",
                    "start": [1, 0],
                    "candidates": [
                        {"id": "B1", "position": [0, 0], "cost": 1.0}
                    ],
                },
            ],
        }
        limited = load_scenario(data)

        limited_result = plan_scenario(limited, WorldState.from_scenario(limited))
        data["max_assignment_trials"] = 2
        exhaustive = load_scenario(data)
        exhaustive_result = plan_scenario(
            exhaustive,
            WorldState.from_scenario(exhaustive),
        )

        self.assertEqual(limited_result.status, "SEARCH_LIMIT_REACHED")
        self.assertEqual(exhaustive_result.status, "NO_PLAN_FOUND")

    def test_cli_reads_json_and_emits_executed_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario_path = Path(temp_dir) / "scenario.json"
            scenario_path.write_text(
                json.dumps(basic_scenario_data()),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "multi_robot_avoidance.py"),
                    "--scenario-file",
                    str(scenario_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["status"], "EXECUTED")
        self.assertEqual(output["execution"]["committed_micro_steps"], 8)
        self.assertEqual(len(output["execution"]["walkable"]), 25)
        self.assertEqual(output["execution"]["walkable_version"], 0)

    def test_cli_consumes_dynamic_walkable_events(self):
        static_scenario = load_scenario(basic_scenario_data())
        static_plan = plan_scenario(
            static_scenario,
            WorldState.from_scenario(static_scenario),
        ).plan
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                },
                *[
                    {
                        "at_committed_step": index,
                        "type": "robot_step",
                        "robot_id": step.robot_id,
                        "reachable_positions": all_positions,
                    }
                    for index, step in enumerate(static_plan.micro_steps, start=1)
                ],
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario_path = Path(temp_dir) / "dynamic.json"
            scenario_path.write_text(json.dumps(data), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "multi_robot_avoidance.py"),
                    "--scenario-file",
                    str(scenario_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["execution"]["walkable_version"], 9)
        self.assertEqual(output["execution"]["replan_count"], 0)

    def test_cli_returns_exit_four_for_missing_dynamic_step_update(self):
        all_positions = [
            {"x": x * 0.25, "y": 0.0, "z": z * 0.25}
            for x in range(5)
            for z in range(5)
        ]
        data = basic_scenario_data()
        data["execution"] = {
            "walkable_events": [
                {
                    "at_committed_step": 0,
                    "type": "active_refresh",
                    "reachable_positions_by_robot": {
                        "A": all_positions,
                        "B": all_positions,
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario_path = Path(temp_dir) / "missing-step-update.json"
            scenario_path.write_text(json.dumps(data), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "multi_robot_avoidance.py"),
                    "--scenario-file",
                    str(scenario_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 4, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout)["status"],
            "INVALID_WALKABLE_UPDATE",
        )

    def test_cli_returns_invalid_scenario_exit_code(self):
        data = basic_scenario_data()
        data["robots"] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario_path = Path(temp_dir) / "invalid.json"
            scenario_path.write_text(json.dumps(data), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "multi_robot_avoidance.py"),
                    "--scenario-file",
                    str(scenario_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["status"], "INVALID_SCENARIO")


if __name__ == "__main__":
    unittest.main()
