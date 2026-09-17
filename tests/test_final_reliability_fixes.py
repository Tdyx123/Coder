"""Regressions for the five final batch-1 review findings."""
import importlib.util
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_evaluation_contract
from executor_system.action_plan import Action, StagePlan, TaskPlan
from tests.test_evaluation_contract import FakeRuntime
from tests.test_run_result_storage import complete_result
from executor_system import generated_plan_runtime as generated, parallel_runner
from executor_system.evaluation import EvaluationContext
from executor_system.goals import record_satisfied_temperature_goal_states
from executor_system.movement import MovementConfig
from executor_system.run_results import RunResultStore, task_key_for_executable
import generate_single_subtask_code as generator


class FinalReliabilityFixesTest(unittest.TestCase):
    def test_disappeared_history_scores_each_state_independently(self):
        for hot_id, cold_id, success in [('Mug|1', 'Mug|2', True),
                                          ('Mug|1', 'mug|1', True),
                                          (None, 'Mug|2', False),
                                          ('Mug|1', None, False),
                                          (None, None, False)]:
            with self.subTest(hot_id=hot_id, cold_id=cold_id):
                context = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT', 'COLD']}])
                context.record_observation('Mug', 'HOT', hot_id)
                context.record_observation('Mug', 'COLD', cold_id)
                result = context.evaluate(FakeRuntime([]))
                self.assertEqual(result['task_success'], success)

    def test_disappeared_bound_alias_rejects_another_or_unknown_instance(self):
        for observed_id, success in [('Mug|1', True), ('Mug|2', False), (None, False)]:
            with self.subTest(observed_id=observed_id):
                context = EvaluationContext.from_goals([{'name': 'Mug_1', 'states': ['HOT']}],
                    object_id_bindings=[{'object': 'Mug_1', 'object_id': 'Mug|1'}])
                context.record_observation('Mug_1', 'HOT', observed_id)
                result = context.evaluate(FakeRuntime([], {'Mug_1': 'Mug|1'}))
                self.assertEqual(result['task_success'], success)

    def test_unknown_history_cannot_join_current_identified_instance(self):
        context = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT', 'COLD']}])
        context.record_observation('Mug', 'HOT', None)
        result = context.evaluate(FakeRuntime([
            {'objectId': 'Mug|2', 'objectType': 'Mug', 'temperature': 'Cold'}]))
        self.assertFalse(result['task_success'])

    def test_temperature_collector_retains_each_matching_instance(self):
        context = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT', 'COLD']}])
        runtime = FakeRuntime([
            {'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot'},
            {'objectId': 'Mug|2', 'objectType': 'Mug', 'temperature': 'Hot'}])
        record_satisfied_temperature_goal_states(runtime, context)
        runtime._objects[1]['temperature'] = 'Cold'
        record_satisfied_temperature_goal_states(runtime, context)
        result = context.evaluate(runtime)
        self.assertTrue(result['task_success'])
        self.assertEqual(result['subgoal_results'][0]['candidates'][1]['status'], 'satisfied')

    def test_parent_timeout_preserves_v2_diagnostics_in_durable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunResultStore(root, 'timeout-test')
            script = root / 'timeout.py'
            key = task_key_for_executable(script)
            metrics_path = store.attempt_dir(key, 1) / 'child_metrics.json'
            metrics_path.parent.mkdir(parents=True)
            evidence = dict(worker_errors=[{'message': 'worker evidence'}],
                            cleanup_errors=[{'message': 'cleanup evidence'}],
                            navigation_metrics={'controller_steps': 7},
                            ru_inputs={'no_trans': 2, 'no_trans_gt': 1, 'max_trans': 4})
            child = complete_result(store.run_id, key, **evidence)
            metrics_path.write_text(json.dumps(child))
            outcome = SimpleNamespace(returncode=-9, timed_out=True, pid=123, pgid=123,
                                      termination_events=[{'signal': 'SIGKILL'}], wall_time_seconds=3)
            with patch.object(parallel_runner, 'run_owned_process', return_value=outcome):
                result = parallel_runner.run_generated_executable(script,
                    metrics_output=metrics_path, timeout_seconds=1, run_id=store.run_id)
            self.assertEqual(result.get('metrics_schema_version'), 2)
            self.assertEqual(result['evaluation_version'], 'fixed_goals_v2')
            self.assertEqual(result['returncode'], 124)
            self.assertEqual(result['evaluation_status'], 'incomplete')
            for field in ('gcr', 'tc', 'sr', 'ru', 'task_success', 'satisfied_goal_count'):
                self.assertIsNone(result[field])
            success_key = task_key_for_executable(root / 'success.py')
            success = complete_result(store.run_id, success_key)
            store.record_attempt(key, 1, result)
            store.record_attempt(success_key, 1, success)
            saved = json.loads((metrics_path.parent / 'result.json').read_text())
            summary = store.rebuild_summary()
            rebuilt = next(item for item in summary['results'] if item['task_key'] == key)
            for field, expected in evidence.items():
                self.assertEqual(saved[field], expected)
                self.assertEqual(rebuilt[field], expected)
            self.assertEqual(rebuilt['action_counts'], child['action_counts'])
            self.assertTrue(rebuilt['process_cleanup_events'][0]['timed_out'])
            for summary in [summary, parallel_runner.build_summary([result, success], time.monotonic())]:
                group = next(group for group in summary['result_groups']
                             if group['evaluation_version'] == 'fixed_goals_v2')
                self.assertEqual(group['total_task_count'], 2)
                self.assertEqual(group['valid_evaluation_count'], 1)

    def test_parent_timeout_keeps_legacy_and_rejects_wrong_v2_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'timeout.py'
            key = task_key_for_executable(script)
            path = root / 'child_metrics.json'
            for child in [{'status': 'success', 'sr': 1, 'worker_errors': [{'message': 'legacy'}]},
                          complete_result('wrong-run', key),
                          complete_result('run', key, metrics_schema_version=3)]:
                with self.subTest(child=child):
                    path.write_text(json.dumps(child))
                    outcome = SimpleNamespace(returncode=-9, timed_out=True, pid=123, pgid=123,
                                              termination_events=[], wall_time_seconds=3)
                    with patch.object(parallel_runner, 'run_owned_process', return_value=outcome):
                        result = parallel_runner.run_generated_executable(script,
                            metrics_output=path, timeout_seconds=1, run_id='run')
                    self.assertEqual(result['evaluation_version'], 'legacy_v1')
                    self.assertEqual(result['process_status'], 'timeout')
                    self.assertIsNone(result['task_success'])
                    if 'worker_errors' in child:
                        self.assertEqual(result.get('worker_errors'), child['worker_errors'])
                    else:
                        self.assertIn('invalid runner metrics', result.get('error', ''))

    @staticmethod
    def _runtime(objects):
        runtime = test_evaluation_contract.EvaluationContractTest._entry_runtime(objects)
        runtime.movement_config = MovementConfig.resolve('step')
        runtime.navigation_metrics = SimpleNamespace(to_dict=lambda: {})
        return runtime

    def _shared_result(self, root, goals, objects, task_changes=None):
        runtime = self._runtime(objects)
        bundle = SimpleNamespace(gcr=goals, noop_subtasks=[],
                                 task_plan=TaskPlan('metrics', [StagePlan('s', {'robot1': [Action('Wait')]})]), no_trans=2,
                                 object_mapping_warnings=[], object_id_bindings=[])
        task = {'trans': 1, 'min_trans': 4, 'max_trans': 9, **(task_changes or {})}
        path = root / 'shared.json'
        args = SimpleNamespace(metrics_output=str(path), movement_mode='step', timeout_seconds=1)
        with patch.object(generated, '_runtime_inputs', return_value=(task, '1', [], goals, bundle)), \
             patch.object(generated, 'ThorRuntime', return_value=runtime), \
             patch.object(generated, 'run_action_plan_tolerant', return_value=parallel_runner.TolerantRunStats().to_dict()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(generated.run_runner_mode(args, {}, 'unused', 0, str(root / 'shared.py')), 0)
        return json.loads(path.read_text())

    def test_shared_entry_preserves_completed_invalid_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            for goals in [[{'name': 'Mug', 'states': ['CLEANED']}], []]:
                with self.subTest(goals=goals):
                    result = self._shared_result(Path(directory), goals,
                        [{'objectId': 'Mug|1', 'objectType': 'Mug'}])
                    self.assertEqual(result['process_status'], 'completed')
                    self.assertEqual(result['evaluation_status'], 'invalid')
                    for field in ('gcr', 'tc', 'sr', 'ru', 'task_success', 'satisfied_goal_count'):
                        self.assertIsNone(result[field])

    def test_shared_entry_ru_inputs_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._shared_result(root, [{'name': 'Mug', 'states': ['HOT']}],
                [{'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot'}])
            self._assert_ru_round_trip(root, result)

    def test_template_entry_ru_inputs_round_trip(self):
        goals = [{'name': 'Mug', 'states': ['HOT']}]
        source = generator.render_executable(None, 0, {
            'task': 'heat mug', 'task_plan': {'task_id': 'task', 'stages': []},
            'no_trans': 2, 'phases': [], 'plan_files': [], 'object_mappings': [],
            'object_mapping_warnings': [], 'object_id_bindings': []},
            task_record={'task': 'heat mug', 'object_states': goals,
                         'trans': 1, 'min_trans': 4, 'max_trans': 9}, floor_plan=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'template.py'
            path.write_text(source)
            spec = importlib.util.spec_from_file_location('final_fix_template', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runtime = self._runtime([{'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot'}])
            output = root / 'template.json'
            with patch.object(module, 'ThorRuntime', return_value=runtime), \
                 patch.object(module, 'run_action_plan_tolerant', return_value=parallel_runner.TolerantRunStats().to_dict()), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(module.run_runner_mode(SimpleNamespace(
                    metrics_output=str(output), timeout_seconds=1)), 0)
            self._assert_ru_round_trip(root, json.loads(output.read_text()))

    def _assert_ru_round_trip(self, root, result):
        self.assertEqual(result.get('ru_inputs'), {'no_trans': 2, 'no_trans_gt': 1, 'max_trans': 4})
        self.assertEqual(result['ru'], 1.0)
        self.assertTrue(result['task_success'])
        store = RunResultStore(root, result['run_id'])
        result['returncode'] = 0
        path = store.record_attempt(result['task_key'], result['attempt'], result)
        self.assertEqual(json.loads(path.read_text())['ru_inputs'], result['ru_inputs'])
        self.assertEqual(store.rebuild_summary()['results'][0]['ru_inputs'], result['ru_inputs'])


if __name__ == '__main__':
    unittest.main()
