"""COT catalog identities must survive conversion and physical-agent binding."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_cot_baseline_converter import (
    load_bundle_data, write_dataset, write_json, write_parallel_summary, write_success_run,
)
from baseline_converters import cot
from executor_system.generated_plan_runtime import _runtime_inputs, build_robot_team
from executor_system.runtime import ThorRuntime
from executor_system.utils import robot_agent_id


class CotRealRobotIdsTests(unittest.TestCase):
    def convert_fixture(self, root, ids, actions, context_robots=None):
        dataset = write_dataset(root)
        record = json.loads(dataset.read_text())
        record['robot list'] = ids
        dataset.write_text(json.dumps(record) + '\n', encoding='utf-8')
        parallel = root / 'baselines/COT/parallel_runs/fixture'
        run, row = write_success_run(parallel)
        context_path = run / '00_inputs/task_context.json'
        context = json.loads(context_path.read_text())
        context['robots'] = context_robots or [
            {'symbol': 'robot1'}, {'symbol': 'robot2'},
        ]
        write_json(context_path, context)
        write_json(run / '02_plan/01_final_plan.json', {'plan': actions})
        write_parallel_summary(parallel, [row])
        indexed = cot.collect_summary_runs([parallel / 'summary.json'])[0]
        result = cot.process_task_run(indexed, repo_root=root)
        return result, dataset, run / 'plan_to_code/executable_plan.py'

    @staticmethod
    def action(robot, kind='GoToObject'):
        return {'action': kind, 'arguments': [robot, 'mug'], 'reasoning_step': 1}

    def test_real_ids_survive_conversion_and_runtime_without_source_ids(self):
        for ids, names in [([16, 6], ['robot16', 'robot6']), ([2, 1], ['robot2', 'robot1'])]:
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as tmp:
                result, dataset, executable = self.convert_fixture(
                    Path(tmp), ids, [self.action(name) for name in names],
                )
                self.assertTrue(result['success'], result)
                data = load_bundle_data(executable)
                self.assertEqual(data['robot_id_mode'], 'real')
                queues = [stage['robot_action_queues'] for stage in data['task_plan']['stages']]
                self.assertEqual([next(iter(q)) for q in queues], names)
                self.assertEqual([a['robot_id'] for q in queues for actions in q.values() for a in actions], names)
                _, _, robots, _, _ = _runtime_inputs(data, str(dataset), 0)
                self.assertEqual([r['name'] for r in robots], names)
                self.assertEqual([robot_agent_id(r) for r in robots], [0, 1])
                self.assertTrue(all('source_id' not in r for r in robots))
                runtime = object.__new__(ThorRuntime)
                runtime.robots = robots
                runtime.physical_agent_count = 2
                self.assertEqual(list(runtime.build_robot_agent_map().values()), [0, 1])
                runtime.physical_agent_count = 1
                self.assertEqual(list(runtime.build_robot_agent_map().values()), [0, 0])

    def test_catalog_overrides_stale_context_identity_and_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.convert_fixture(Path(tmp), [6], [self.action('robot6', 'PickupObject')],
                [{'symbol': 'robot1', 'source_id': 999, 'skills': [], 'mass_capacity': 0}])
            self.assertTrue(result['success'], result)

    def test_invalid_dataset_ids_fail_without_executable(self):
        for ids in (None, [], [6, 6], [0], [-1], [999], [True], [6.5], ['robot6'], '6'):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as tmp:
                result, _, executable = self.convert_fixture(Path(tmp), ids, [self.action('robot6')])
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['failure_reason'], 'validation_data_missing')
                self.assertIn('robot', result['error'].lower())
                self.assertFalse(executable.exists())

    def test_outside_team_or_local_alias_is_rejected(self):
        for name in ('robot1', 'robot99'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                result, _, executable = self.convert_fixture(Path(tmp), [16, 6], [self.action(name)])
                self.assertFalse(result['success'])
                self.assertIn('unknown robot', result['error'])
                self.assertFalse(executable.exists())

    def test_catalog_skill_and_capacity_are_enforced(self):
        for ids, name, reason in [([16], 'robot16', 'missing_skill'), ([8], 'robot8', 'mass_exceeded')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                result, _, executable = self.convert_fixture(Path(tmp), ids, [self.action(name, 'PickupObject')])
                self.assertEqual(result['failure_reason'], reason, result)
                self.assertEqual(result['validation_error']['robot_id'], name)
                self.assertFalse(executable.exists())

    def test_rejected_plan_removes_stale_code_except_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _, executable = self.convert_fixture(root, [16, 6], [self.action('robot16')])
            self.assertTrue(result['success'], result)
            previous = executable.read_text()
            run = executable.parent.parent
            write_json(run / '02_plan/01_final_plan.json', {'plan': [self.action('robot1')]})
            indexed = cot.collect_summary_runs(cot.discover_top_level_summaries(root / 'baselines/COT/parallel_runs'))[0]
            result = cot.process_task_run(indexed, repo_root=root, dry_run=True)
            self.assertFalse(result['success'])
            self.assertEqual(executable.read_text(), previous)
            result = cot.process_task_run(indexed, repo_root=root)
            self.assertFalse(result['success'])
            self.assertIn('unknown robot', result['error'])
            self.assertFalse(executable.exists())

    def test_old_bundle_keeps_local_names_and_agent_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, dataset, executable = self.convert_fixture(Path(tmp), [16, 6], [self.action('robot16')])
            self.assertTrue(result['success'], result)
            data = load_bundle_data(executable)
            del data['robot_id_mode']
            _, _, robots, _, _ = _runtime_inputs(data, str(dataset), 0)
            self.assertEqual([r['name'] for r in robots], ['robot1', 'robot2'])
            self.assertEqual([robot_agent_id(r) for r in robots], [0, 1])
            self.assertEqual(build_robot_team([16, 6]), robots)

    def test_explicit_agent_id_wins_over_catalog_name(self):
        self.assertEqual(robot_agent_id({'name': 'robot16', 'agent_id': 0}), 0)
        self.assertEqual(robot_agent_id('robot2'), 1)
        for value in (-1, True, '0', 0.5, None):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                robot_agent_id({'name': 'robot16', 'agent_id': value})


if __name__ == '__main__':
    unittest.main()
