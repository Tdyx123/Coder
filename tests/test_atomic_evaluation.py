"""Regression contract for independently scored, source-authoritative goals."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from executor_system.evaluation import EvaluationContext, GoalSpec


class Runtime:
    def __init__(self, objects):
        self.objects = objects
        self.stats_lock = threading.Lock()

    def current_objects(self):
        return self.objects

    def resolve_object_alias(self, name):
        raise AssertionError('Scoring must not call the mutable execution resolver')


def scene():
    return [
        {'objectId': 'Shelf|1', 'objectType': 'Shelf', 'receptacleObjectIds': ['Pen|1', 'Mug|1']},
        {'objectId': 'Shelf|5', 'objectType': 'Shelf', 'receptacleObjectIds': ['CD|2']},
        *[{'objectId': value, 'objectType': value.split('|')[0]} for value in ['Pen|1', 'Mug|1', 'CD|1', 'CD|2']],
    ]


class AtomicEvaluationTest(unittest.TestCase):
    def test_type_relations_can_use_different_shelves(self):
        context = EvaluationContext.from_goals([{'name': 'Shelf', 'contains': ['Pen', 'CD', 'Mug']}])
        result = context.evaluate(Runtime(scene()))
        self.assertEqual(result['gcr'], 1)
        self.assertEqual(result['original_goal_count'], 1)
        self.assertEqual(result['atomic_goal_count'], 3)
        self.assertEqual(len(result['subgoal_results']), 3)

    def test_explicit_shelf_has_partial_credit_and_frozen_binding(self):
        bindings = [{'object': 'Shelf_1', 'object_id': 'Shelf|1'}]
        context = EvaluationContext.from_goals(
            [{'name': 'Shelf_1', 'contains': ['Pen', 'CD', 'Mug']}], object_id_bindings=bindings)
        bindings[0]['object_id'] = 'Shelf|5'
        result = context.evaluate(Runtime(scene()))
        self.assertEqual(result['gcr'], 2 / 3)
        self.assertEqual(result['tc'], 0)
        self.assertEqual(result['satisfied_goal_count'], 2)
        self.assertEqual(result['goal_results'][0]['status'], 'unsatisfied')

    def test_exact_contained_object_and_direct_ids(self):
        result = EvaluationContext.from_goals([
            {'name': 'Shelf|5', 'contains': ['CD|1', 'CD|2']}
        ]).evaluate(Runtime(scene()))
        self.assertEqual(result['gcr'], .5)

    def test_states_can_use_distinct_instances(self):
        objects = [
            {'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot', 'isDirty': True},
            {'objectId': 'Mug|2', 'objectType': 'Mug', 'temperature': 'Cold', 'isDirty': False},
        ]
        result = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT', 'CLEANED']}]).evaluate(Runtime(objects))
        self.assertEqual(result['gcr'], 1)
        exact = EvaluationContext.from_goals([{'name': 'Mug|1', 'states': ['HOT', 'CLEANED']}]).evaluate(Runtime(objects))
        self.assertEqual(exact['gcr'], .5)

    def test_missing_or_conflicting_explicit_binding_is_invalid(self):
        for bindings in ([], [{'object': 'Shelf_1', 'object_id': 'Shelf|1'},
                              {'object': 'Shelf_1', 'object_id': 'Shelf|5'}]):
            with self.subTest(bindings=bindings):
                result = EvaluationContext.from_goals([{'name': 'Shelf_1', 'contains': ['Pen']}],
                    object_id_bindings=bindings).evaluate(Runtime(scene()))
                self.assertEqual(result['evaluation_status'], 'invalid')
                self.assertIsNone(result['gcr'])
                self.assertTrue(result['subgoal_results'][0]['reason'])

    def test_missing_metadata_unknown_and_missing_instance_unsatisfied(self):
        result = EvaluationContext.from_goals([{'name': 'Shelf|1', 'contains': ['CD']}]).evaluate(
            Runtime([{'objectId': 'Shelf|1', 'objectType': 'Shelf'}]))
        self.assertIsNone(result['gcr'])
        result = EvaluationContext.from_goals([{'name': 'Shelf|1', 'contains': ['CD']}]).evaluate(Runtime([]))
        self.assertEqual(result['gcr'], 0)

    def test_history_respects_exact_identity(self):
        context = EvaluationContext.from_goals([{'name': 'Mug|1', 'states': ['HOT']}])
        context.record_observation('Mug|1', 'HOT', 'Mug|2')
        self.assertEqual(context.evaluate(Runtime([]))['gcr'], 0)
        context.record_observation('Mug|1', 'HOT', 'Mug|1')
        self.assertEqual(context.evaluate(Runtime([]))['gcr'], 1)

    def test_transformed_type_state_preserves_existing_evidence_rules(self):
        for name, state, object_type in [('Apple', 'SLICED', 'AppleSliced'), ('Egg', 'BROKEN', 'EggCracked')]:
            with self.subTest(name=name):
                result = EvaluationContext.from_goals([{'name': name, 'states': [state]}]).evaluate(
                    Runtime([{'objectId': object_type + '|1', 'objectType': object_type}]))
                self.assertEqual(result['gcr'], 1)

    def test_transformed_type_temperature_history_survives_cooling(self):
        from executor_system.goals import record_satisfied_temperature_goal_states
        runtime = Runtime([{'objectId': 'AppleSliced|1', 'objectType': 'AppleSliced', 'temperature': 'Hot'}])
        context = EvaluationContext.from_goals([{'name': 'Apple', 'states': ['HOT']}])
        self.assertEqual(record_satisfied_temperature_goal_states(runtime, context), 1)
        runtime.objects[0]['temperature'] = 'RoomTemp'
        self.assertEqual(context.evaluate(runtime)['gcr'], 1)

    def test_empty_conditions_mean_existence(self):
        result = EvaluationContext.from_goals([{'name': 'Shelf'}]).evaluate(Runtime(scene()))
        self.assertEqual(result['atomic_goal_count'], 1)
        self.assertEqual(result['gcr'], 1)

    def test_history_can_satisfy_type_after_instance_disappears(self):
        context = EvaluationContext.from_goals([{'name': 'Mug', 'states': ['HOT']}])
        context.record_observation('Mug_1', 'HOT', 'Mug|1')
        result = context.evaluate(Runtime([{'objectId': 'Mug|2', 'objectType': 'Mug', 'temperature': 'Cold'}]))
        self.assertEqual(result['gcr'], 1)
        self.assertIn('mug|1', [value.casefold() for value in result['subgoal_results'][0]['matched_object_ids']])

    def test_unavailable_scene_keeps_subgoal_diagnostics(self):
        runtime = Runtime([])
        def unavailable():
            raise RuntimeError('scene unavailable')
        runtime.current_objects = unavailable
        result = EvaluationContext.from_goals([{'name': 'Shelf', 'contains': ['Pen', 'CD']}]).evaluate(runtime)
        self.assertIsNone(result['gcr'])
        self.assertEqual(len(result['subgoal_results']), 2)
        self.assertTrue(all(item['status'] == 'unknown' and item['reason'] for item in result['subgoal_results']))

    def test_execution_condition_still_requires_one_instance(self):
        runtime = Runtime([
            {'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot', 'isDirty': True},
            {'objectId': 'Mug|2', 'objectType': 'Mug', 'temperature': 'Cold', 'isDirty': False},
        ])
        runtime.resolve_object_alias = lambda name: name
        goal = GoalSpec.from_value({'name': 'Mug', 'states': ['HOT', 'CLEANED']})
        self.assertEqual(EvaluationContext.from_goals([goal]).evaluate_goal(runtime, goal)['status'], 'unsatisfied')

class AtomicIntegrationTest(unittest.TestCase):
    def test_runtime_fallback_uses_registration_snapshot(self):
        from executor_system.runtime import ThorRuntime
        runtime = object.__new__(ThorRuntime)
        runtime.current_objects = lambda _agent_id=None: scene()
        runtime._current_object_by_id_optional = lambda *args: None
        runtime.register_object_id_bindings([{'object': 'Shelf_1', 'object_id': 'Shelf|1'}])
        runtime.object_alias_bindings['Shelf_1']['object_id'] = 'Shelf|5'
        result = runtime.evaluate([{'name': 'Shelf_1', 'contains': ['CD']}])
        self.assertEqual(result['gcr'], 0)

    def test_v3_result_requires_consistent_subgoal_evidence(self):
        from tests.test_run_result_contract import ResultValidationContractTest
        from executor_system.run_results import validate_result
        row = ResultValidationContractTest.scheduler_result()
        row.update(evaluation_version='atomic_goals_v3', atomic_goal_count=1)
        with self.assertRaises(ValueError):
            validate_result(row, returncode=0, expected_identity={})
        row['subgoal_results'] = [{'subgoal_index': 0, 'original_goal_index': 0, 'status': 'unsatisfied'}]
        with self.assertRaises(ValueError):
            validate_result(row, returncode=0, expected_identity={})

    def test_unmet_goal_report_uses_atomic_semantics(self):
        from executor_system.runtime import ThorRuntime
        runtime = object.__new__(ThorRuntime)
        goals = [{'name': 'Shelf', 'contains': ['Pen', 'CD', 'Mug']}]
        runtime.evaluation_context = EvaluationContext.from_goals(goals)
        runtime.current_objects = lambda: scene()
        runtime.resolve_object_alias = lambda name: name
        self.assertEqual(runtime.unmet_goals(goals), [])

    def test_converter_keeps_source_types(self):
        from baseline_converters.pddlrun import gcr_for_bundle
        from types import SimpleNamespace
        record = {'object_states': [{'name': 'Shelf', 'contains': ['CD']}]}
        bundle = SimpleNamespace(object_id_bindings=[
            {'object': 'Shelf_1', 'object_id': 'Shelf|1', 'object_type': 'Shelf', 'multiple': True}],
            task_plan=SimpleNamespace(stages=[]), object_mappings={})
        self.assertEqual(gcr_for_bundle(record, bundle), record['object_states'])

    def test_runtime_uses_source_goal_instead_of_old_bundle_binding(self):
        from executor_system.generated_plan_runtime import _runtime_inputs
        from unittest.mock import patch
        from types import SimpleNamespace
        bundle = SimpleNamespace(gcr=[{'name': 'Shelf_1', 'contains': ['CD']}])
        record = {'robot list': [4], 'object_states': [{'name': 'Shelf', 'contains': ['CD']}]}
        with patch('executor_system.generated_plan_runtime.load_task_record', return_value=record), \
             patch('executor_system.generated_plan_runtime.build_hardcoded_bundle', return_value=bundle):
            *_, ground_truth, returned = _runtime_inputs({}, 'FloorPlan303.jsonl', 9)
        self.assertEqual(ground_truth, record['object_states'])
        self.assertEqual(returned.gcr, record['object_states'])

    def test_result_validator_uses_atomic_denominator(self):
        from tests.test_run_result_contract import ResultValidationContractTest
        from executor_system.run_results import validate_result
        row = ResultValidationContractTest.scheduler_result()
        row.update(EvaluationContext.from_goals([{'name': 'Shelf|1', 'contains': ['Pen', 'CD', 'Mug']}]).evaluate(Runtime(scene())))
        self.assertEqual(validate_result(row, returncode=0, expected_identity={})['gcr'], 2/3)
        row['gcr'] = 1
        with self.assertRaises(ValueError):
            validate_result(row, returncode=0, expected_identity={})

class AtomicReportingTest(unittest.TestCase):
    def test_summary_rejects_mixed_versions(self):
        from summarize_run_metrics import coderun_metric_columns
        rows = [{'evaluation_version': version, 'metrics_schema_version': 2,
                 'evaluation_status': 'valid', 'gcr': 1} for version in ['fixed_goals_v2', 'atomic_goals_v3']]
        with self.assertRaisesRegex(ValueError, 'Mixed evaluation versions'):
            coderun_metric_columns({'results': rows}, 2)

    def test_movement_benchmark_keeps_version_groups_and_no_mixed_score(self):
        from benchmark_movement_modes import build_benchmark_report, acceptance_failures
        rows = [{'evaluation_version': version, 'metrics_schema_version': 2,
                 'evaluation_status': 'valid', 'gcr': 1, 'mode': 'step'}
                for version in ['fixed_goals_v2', 'atomic_goals_v3']]
        report = build_benchmark_report({'cases': []}, rows)
        self.assertEqual(len(report['result_groups']), 2)
        self.assertIsNone(report['aggregates']['step']['success_gcr'])
        self.assertIn('mixed_evaluation_versions', {item['code'] for item in acceptance_failures(report)})


if __name__ == '__main__':
    unittest.main()
