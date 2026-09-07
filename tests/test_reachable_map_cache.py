"""Deterministic event/full navigation contracts at the real coordinator boundary."""
import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.execution_control import ExecutionCancelled, ExecutionControl
from executor_system.movement import MovementConfig, NavigationMetrics, StepNavigationError
from executor_system.movement_coordinator import StepMovementCoordinator
from multi_robot_avoidance import GridPoint
from tests.test_step_movement import navigation_case, thor_position


def coordinator(runtime, mode='event'):
    config = MovementConfig.resolve('step', environ={})
    config = replace(config, reachable_refresh_mode=mode)
    return StepMovementCoordinator(runtime, config, NavigationMetrics(config.mode))


class ReachableMapCacheTest(unittest.TestCase):
    def run_corridor(self, refresh_mode, micro_steps=12):
        runtime, request = navigation_case(
            walkable=[(x, 0) for x in range(micro_steps + 1)],
            candidates=[(micro_steps, 0)])
        nav = coordinator(runtime, refresh_mode)
        result = nav.execute_batch((request,))
        self.assertEqual(runtime.successful_move_count, micro_steps)
        self.assertNotIn('Teleport', [action[0] for action in runtime.actions])
        collisions = sum(
            math.hypot(a['x'] - b['x'], a['z'] - b['z']) < 0.35
            for frame in runtime.position_history
            for aid, a in frame.items() for bid, b in frame.items() if aid < bid)
        return SimpleNamespace(final_positions={i: r.position for i, r in result.items()},
                               collisions=collisions,
                               reachable_queries=runtime.reachable_queries,
                               runtime=runtime, nav=nav)

    def test_event_refresh_reuses_unchanged_map(self):
        full = self.run_corridor('full')
        event = self.run_corridor('event')
        self.assertEqual(event.final_positions, full.final_positions)
        self.assertEqual(event.runtime.actions, full.runtime.actions)
        self.assertEqual(event.collisions, 0)
        self.assertEqual(full.reachable_queries, 13)
        self.assertLessEqual(event.reachable_queries, full.reachable_queries // 2)
        self.assertEqual(event.runtime.query_move_counts, [0, 4, 8, 12])

    def test_two_robots_match_full_actions_and_preserve_spacing(self):
        runs = []
        for mode in ('full', 'event'):
            runtime, request = navigation_case(
                walkable=[(x, z) for x in range(13) for z in (0, 2)], candidates=[(12, 0)])
            runtime.physical_agent_count = 2
            runtime.positions[1] = thor_position(0, .5)
            runtime.walkable_by_agent[1] = list(runtime.walkable_by_agent[0])
            runtime.position_history[0][1] = dict(runtime.positions[1])
            second = replace(request, robot='robot2', agent_id=1,
                             candidate_positions=(thor_position(3, .5),))
            nav = coordinator(runtime, mode)
            results = nav.execute_batch((request, second))
            self.assertEqual(results[0].position, thor_position(3))
            self.assertEqual(results[1].position, thor_position(3, .5))
            self.assertEqual(runtime.successful_move_count, 24)
            for frame in runtime.position_history:
                a, b = frame[0], frame[1]
                self.assertGreaterEqual(math.hypot(a['x'] - b['x'], a['z'] - b['z']), .35)
            self.assertNotIn('Teleport', [action[0] for action in runtime.actions])
            runs.append(runtime)
        self.assertEqual(runs[0].actions, runs[1].actions)
        self.assertLessEqual(runs[1].reachable_queries, runs[0].reachable_queries // 2)

    def test_query_confirmed_additions_restore_removed_cells(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(7)], candidates=[(6, 0)])
        nav = coordinator(runtime)
        initial = nav.refresh_world()
        runtime.walkable_by_agent[0].remove(thor_position(1.0))
        runtime.objects['Apple|1']['isOpen'] = False
        removed = nav.refresh_world()
        self.assertNotIn(GridPoint(4, 0), removed.walkable_map.walkable)
        self.assertGreater(removed.version, initial.version)
        runtime.walkable_by_agent[0].extend([thor_position(1.0), thor_position(1.75)])
        runtime.objects['Apple|1']['isOpen'] = True
        restored = nav.refresh_world()
        self.assertIn(GridPoint(4, 0), restored.walkable_map.walkable)
        self.assertIn(GridPoint(7, 0), restored.walkable_map.walkable)
        self.assertGreater(restored.version, removed.version)

    def test_refresh_cadence_cannot_exceed_four_successful_steps(self):
        config = MovementConfig.resolve('step', environ={})
        self.assertEqual(config.reachable_refresh_mode, 'full')
        self.assertEqual(config.full_refresh_interval_steps, 4)
        from executor_system.movement import MovementConfigurationError
        for invalid in (0, 5, -1, True, 1.5):
            with self.subTest(interval=invalid), self.assertRaises(MovementConfigurationError):
                replace(config, full_refresh_interval_steps=invalid)
        with self.assertRaises(MovementConfigurationError):
            replace(config, reachable_refresh_mode='unsafe')

    def test_topology_metadata_refreshes_but_temperature_and_frames_do_not(self):
        for field, value in [('isOpen', True), ('position', thor_position(1.0)),
                             ('axisAlignedBoundingBox', {'center': thor_position(1.0)}),
                             ('isPickedUp', True), ('isSliced', True)]:
            with self.subTest(field=field):
                runtime, _ = navigation_case(walkable=[(x, 0) for x in range(5)], candidates=[(4, 0)])
                nav = coordinator(runtime)
                first = nav.refresh_world()
                runtime.objects['Apple|1']['temperature'] = 'Hot'
                runtime.objects['Apple|1']['visible'] = False
                nav.refresh_world()
                self.assertEqual(runtime.reachable_queries, 1)
                runtime.objects['Apple|1'][field] = value
                changed = nav.refresh_world()
                self.assertEqual(runtime.reachable_queries, 2)
                self.assertGreater(changed.version, first.version)
                if field == 'isOpen':
                    runtime.objects['Apple|1'][field] = False
                    nav.refresh_world()
                    self.assertEqual(runtime.reachable_queries, 3)

    def test_instance_and_inventory_changes_force_full_refresh(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(5)], candidates=[(4, 0)])
        nav = coordinator(runtime)
        nav.refresh_world()
        runtime.objects['Slice|1'] = {'objectId': 'Slice|1', 'position': thor_position(1)}
        nav.refresh_world()
        del runtime.objects['Apple|1']
        nav.refresh_world()
        runtime.held_by_agent[0] = {'Slice|1'}
        nav.refresh_world()
        self.assertEqual(runtime.reachable_queries, 4)

    def test_two_missing_queries_remove_point_and_replan_before_using_it(self):
        cells = [(x, z) for x in range(7) for z in (0, 1)]
        runtime, request = navigation_case(walkable=cells, candidates=[(6, 0)])
        runtime.walkable_after_successful_moves[1] = {
            0: [thor_position(x * .25, z * .25) for x, z in cells if (x, z) != (4, 0)]}
        runtime.objects_after_successful_moves[1] = {'Apple|1': {
            **runtime.objects['Apple|1'], 'isOpen': True}}
        nav = coordinator(runtime)
        result = nav.execute_batch((request,))
        self.assertEqual(result[0].position, thor_position(1.5))
        self.assertFalse(any(target == (4, 0) for _, target in runtime.successful_edges))
        self.assertNotIn(GridPoint(4, 0), nav.walkable_map.walkable)
        self.assertEqual(runtime.query_move_counts[:3], [0, 1, 1])
        self.assertGreater(nav.metrics.replans, 0)
        counters = runtime.runtime_metrics.snapshot()['counters']
        self.assertEqual(counters['reachable_suspected_removals'], 1)
        self.assertEqual(counters['reachable_confirmed_removals'], 1)

    def test_one_missing_query_is_not_permanent_removal(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(6)], candidates=[(5, 0)])
        nav = coordinator(runtime)
        first = nav.refresh_world()
        runtime.query_responses[2] = [thor_position(x * .25) for x in (0, 1, 2, 3, 5)]
        runtime.objects['Apple|1']['isOpen'] = True
        second = nav.refresh_world()
        self.assertIn(GridPoint(4, 0), second.walkable_map.walkable)
        self.assertEqual(runtime.reachable_queries, 3)
        self.assertGreater(second.version, first.version)
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters'].get('reachable_confirmed_removals', 0), 0)

    def test_occupancy_suppression_is_temporary_and_recovers_when_robot_moves(self):
        runtime, _ = navigation_case(walkable=[(x, z) for x in range(7) for z in (0, 2)], candidates=[(6, 0)])
        runtime.physical_agent_count = 2
        runtime.positions[1] = thor_position(1.0)
        runtime.walkable_by_agent[1] = list(runtime.walkable_by_agent[0])
        nav = coordinator(runtime)
        nav.refresh_world()
        for aid in (0, 1):
            runtime.walkable_by_agent[aid] = [p for p in runtime.walkable_by_agent[aid] if p != thor_position(.75)]
        runtime.objects['Apple|1']['isOpen'] = True
        suppressed = nav.refresh_world()
        self.assertNotIn(GridPoint(3, 0), suppressed.walkable_map.walkable)
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters'].get('reachable_confirmed_removals', 0), 0)
        for aid in (0, 1):
            runtime.walkable_by_agent[aid].append(thor_position(.75))
        runtime.positions[1] = thor_position(1.5, .5)
        restored = nav.refresh_world()
        self.assertIn(GridPoint(3, 0), restored.walkable_map.walkable)

    def test_subgrid_occupancy_change_rechecks_temporary_segment(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(7)], candidates=[(6, 0)])
        runtime.physical_agent_count = 2
        runtime.positions[1] = thor_position(1.0)
        runtime.walkable_by_agent[1] = list(runtime.walkable_by_agent[0])
        nav = coordinator(runtime)
        nav.refresh_world()
        for aid in (0, 1):
            runtime.walkable_by_agent[aid].remove(thor_position(.75))
        runtime.objects['Apple|1']['isOpen'] = True
        nav.refresh_world()
        before = runtime.reachable_queries
        # Same snapped grid cell, but the missing cell is now outside 0.35m.
        runtime.positions[1] = thor_position(1.125)
        nav.refresh_world()
        self.assertGreater(runtime.reachable_queries, before)
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters']['reachable_confirmed_removals'], 1)

    def test_different_agent_sets_are_queried_and_unioned(self):
        runtime, _ = navigation_case(walkable=[(0, 0), (1, 0), (2, 0)], candidates=[(2, 0)])
        runtime.physical_agent_count = 2
        runtime.positions[1] = thor_position(1.0)
        runtime.walkable_by_agent[1] = [thor_position(.75), thor_position(1.0)]
        nav = coordinator(runtime)
        world = nav.refresh_world()
        self.assertEqual(world.walkable_map.walkable, frozenset(GridPoint(x, 0) for x in range(5)))
        self.assertEqual(runtime.query_agents, [0, 1])
        runtime.walkable_by_agent[0].remove(thor_position(.5))
        runtime.walkable_by_agent[1].append(thor_position(.5))
        runtime.objects['Apple|1']['isOpen'] = True
        self.assertIn(GridPoint(2, 0), nav.refresh_world().walkable_map.walkable)
        self.assertEqual(runtime.query_agents, [0, 1, 0, 1])

    def test_failed_edge_stays_blocked_in_batch_and_fresh_batch_rechecks(self):
        runtime, request = navigation_case(walkable=[(x, z) for x in range(3) for z in (0, 1)], candidates=[(2, 0)])
        runtime.failed_edges.add(((0, 0), (1, 0)))
        nav = coordinator(runtime)
        nav.execute_batch((request,))
        self.assertEqual(runtime.edge_attempts[((0, 0), (1, 0))], 1)
        self.assertGreaterEqual(runtime.query_move_counts.count(0), 2)
        runtime.positions[0] = thor_position(0)
        runtime.failed_edges.clear()
        queries = runtime.reachable_queries
        nav.execute_batch((request,))
        self.assertGreater(runtime.reachable_queries, queries)
        self.assertEqual(runtime.edge_attempts[((0, 0), (1, 0))], 2)

    def test_position_deviation_refreshes_before_another_move(self):
        runtime, request = navigation_case(walkable=[(x, z) for x in range(4) for z in (0, 1)], candidates=[(3, 0)])
        runtime.position_overrides[((0, 0), (1, 0))] = thor_position(0, .25)
        nav = coordinator(runtime)
        nav.execute_batch((request,))
        self.assertIn(1, runtime.query_move_counts)
        self.assertEqual(nav.metrics.position_deviations, 1)

    def test_unmappable_position_forces_query_and_stops(self):
        runtime, request = navigation_case(walkable=[(x, 0) for x in range(4)], candidates=[(3, 0)])
        runtime.position_overrides[((0, 0), (1, 0))] = thor_position(.125, .125)
        nav = coordinator(runtime)
        with self.assertRaises(StepNavigationError):
            nav.execute_batch((request,))
        self.assertEqual(len(runtime.actions), 1)
        self.assertIn(1, runtime.query_move_counts)

    def test_invisible_candidate_forces_query_even_when_no_candidates_remain(self):
        runtime, request = navigation_case(walkable=[(x, 0) for x in range(4)], candidates=[(3, 0)])
        runtime.objects['Apple|1']['visible'] = False
        nav = coordinator(runtime)
        with self.assertRaises(StepNavigationError):
            nav.execute_batch((request,))
        self.assertIn(3, runtime.query_move_counts)

    def test_cached_refresh_still_checks_cancellation(self):
        runtime, _ = navigation_case(walkable=[(0, 0), (1, 0)], candidates=[(1, 0)])
        runtime.execution_control = ExecutionControl()
        nav = coordinator(runtime)
        nav.refresh_world()
        runtime.execution_control.cancel('cancel cached navigation')
        with self.assertRaises(ExecutionCancelled):
            nav.refresh_world()
        self.assertEqual(runtime.reachable_queries, 1)

    def test_confirming_previous_temporary_omission_advances_version(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(7)], candidates=[(6, 0)])
        nav = coordinator(runtime)
        nav.refresh_world()
        runtime.walkable_by_agent[0].remove(thor_position(.25))
        runtime.objects['Apple|1']['isOpen'] = True
        temporary = nav.refresh_world()
        self.assertNotIn(GridPoint(1, 0), temporary.walkable_map.walkable)
        runtime.positions[0] = thor_position(1.5)
        confirmed = nav.refresh_world()
        self.assertGreater(confirmed.version, temporary.version)
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters']['reachable_confirmed_removals'], 1)

    def test_another_robot_position_deviation_invalidates_plan(self):
        runtime, request = navigation_case(
            walkable=[(x, z) for x in range(7) for z in range(5)], candidates=[(6, 0)])
        runtime.physical_agent_count = 2
        runtime.positions[1] = thor_position(0, 1)
        runtime.walkable_by_agent[1] = list(runtime.walkable_by_agent[0])
        nav = coordinator(runtime)
        move = runtime.move_to_adjacent_position_direct
        def displaced(agent_id, target):
            success = move(agent_id, target)
            if runtime.successful_move_count == 1:
                runtime.positions[1] = thor_position(1.0, .5)
            return success
        runtime.move_to_adjacent_position_direct = displaced
        nav.execute_batch((request,))
        self.assertEqual(nav.metrics.position_deviations, 1)
        self.assertIn(1, runtime.query_move_counts)

    def test_full_refresh_counter_measures_compatibility_path(self):
        full = self.run_corridor('full')
        self.assertEqual(full.runtime.runtime_metrics.snapshot()['counters'].get('reachable_full_refreshes'), 13)

    def test_queries_use_latest_positions_and_canonical_snapshot_type(self):
        from executor_system.reachable_map import RuntimeWorldSnapshot
        from executor_system.movement_coordinator import RuntimeWorldSnapshot as LegacySnapshot
        self.assertIs(RuntimeWorldSnapshot, LegacySnapshot)
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(5)], candidates=[(4, 0)])
        nav = coordinator(runtime)
        nav.refresh_world()
        runtime.positions[0] = thor_position(.25)
        self.assertEqual(nav.refresh_world().positions[0], GridPoint(1, 0))
        self.assertEqual(runtime.reachable_queries, 1)

    def test_runtime_configuration_and_controller_commits_invalidate_event_cache(self):
        from tests.test_world_snapshot import snapshot_runtime
        runtime = snapshot_runtime()
        runtime.reachable_refresh_mode = 'event'
        runtime.configure_movement('step', environ={})
        self.assertEqual(runtime.movement_config.reachable_refresh_mode, 'event')
        nav = runtime.movement_strategy.coordinator
        # Real runtime/controller commits, fake only the expensive simulator query.
        runtime.refresh_reachable_positions = lambda aid: [thor_position(x * .25) for x in range(5)]
        nav.refresh_world()
        for action in ('OpenObject', 'CloseObject'):
            runtime._step_direct({'action': action, 'agentId': 0, 'objectId': 'Apple|1'}, save_frame=False)
        before = runtime.runtime_metrics.snapshot()['counters']['reachable_full_refreshes']
        nav.refresh_world()
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters']['reachable_full_refreshes'], before + 1)
        runtime._step_direct({'action': 'Pass', 'agentId': 0}, save_frame=False)
        nav.refresh_world()
        self.assertEqual(runtime.runtime_metrics.snapshot()['counters']['reachable_full_refreshes'], before + 1)

    def test_query_error_does_not_return_stale_cache_or_allow_retry(self):
        runtime, _ = navigation_case(walkable=[(x, 0) for x in range(5)], candidates=[(4, 0)])
        nav = coordinator(runtime)
        nav.refresh_world()
        runtime.objects['Apple|1']['isOpen'] = True
        runtime.query_responses[2] = RuntimeError('reachability query failed')
        with self.assertRaisesRegex(RuntimeError, 'reachability query failed'):
            nav.refresh_world()
        with self.assertRaises(ExecutionCancelled):
            nav.refresh_world()


if __name__ == '__main__':
    unittest.main()
