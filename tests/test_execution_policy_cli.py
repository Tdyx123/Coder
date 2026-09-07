"""Policy propagation and semantic failures at the generated child boundary."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_final_reliability_fixes as fixtures
from tests.snapshot_fakes import FakeRuntime
from executor_system import generated_plan_runtime as generated, context, task_plan
from executor_system.action_plan import Action, StagePlan, TaskPlan
from executor_system.execution_policy import PlanExecutionError
from executor_system.parallel_runner import run_generated_executable
from executor_system.run_results import validate_result
from scripts import benchmark_movement_modes as benchmark


class ExecutionPolicyCliTest(unittest.TestCase):
    def test_standalone_convenience_honors_strict(self):
        runtime = FakeRuntime()
        plan = TaskPlan('p', [StagePlan('s', {'robot1': [Action('Wait')]}, lambda w: False)])
        with context.runtime_scope(runtime), self.assertRaises(PlanExecutionError):
            task_plan.run_action_plan(plan, timeout_seconds=1, execution_policy='strict')
        self.assertEqual(runtime.execution_report['execution_policy'], 'strict')

    def test_shared_runtime_preserves_semantic_failed_report_and_policy(self):
        runtime = fixtures.FinalReliabilityFixesTest._runtime([
            {'objectId': 'Mug|1', 'objectType': 'Mug', 'temperature': 'Hot'}])
        goals = [{'name': 'Mug', 'states': ['HOT']}]
        bundle = SimpleNamespace(gcr=goals, noop_subtasks=[], task_plan=TaskPlan('metrics', [StagePlan('s', {'robot1': [Action('Wait')]})]),
            no_trans=2, object_mapping_warnings=[], object_id_bindings=[])
        report = dict(execution_status='failed', execution_quiescent=True, scheduler_version=2,
            execution_policy='strict', global_condition_satisfied=False, stages=[{'status':'failed'}],
            action_counts=dict(planned=0,started=0,succeeded=0,failed=0,skipped=0,cancelled=0,unexecuted=0,attempts=0),
            raw_action_sr=None, ignored_failure_count=0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'result.json'
            args = generated.parse_arguments(['--execution-policy','strict','--metrics-output',str(output)])
            with patch.object(generated, '_runtime_inputs', return_value=({'trans':1,'min_trans':1},'1',[],goals,bundle)), \
                 patch.object(generated, 'ThorRuntime', return_value=runtime), \
                 patch.object(generated, 'run_action_plan_tolerant', return_value=report) as execute, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(generated.run_runner_mode(args,{},'unused',0,str(output)),0)
            result = json.loads(output.read_text())
        self.assertEqual(execute.call_args.kwargs['execution_policy'], 'strict')
        self.assertEqual(result['execution_status'], 'failed')
        self.assertFalse(result['global_condition_satisfied'])
        self.assertEqual(result['stages'], report['stages'])
        self.assertTrue(validate_result(result, returncode=0, expected_identity={})['task_success'])

    def test_unquiescent_report_does_not_evaluate_or_commit_done(self):
        runtime = fixtures.FinalReliabilityFixesTest._runtime([])
        bundle = SimpleNamespace(gcr=[], noop_subtasks=[], task_plan=TaskPlan('metrics', [StagePlan('s', {'robot1': [Action('Wait')]})]),
            no_trans=0, object_mapping_warnings=[], object_id_bindings=[])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'result.json'
            args = generated.parse_arguments(['--metrics-output',str(output)])
            with patch.object(generated,'_runtime_inputs',return_value=({},'1',[],[],bundle)), \
                 patch.object(generated,'ThorRuntime',return_value=runtime), \
                 patch.object(generated,'run_action_plan_tolerant',return_value={
                     'execution_status':'failed','execution_quiescent':None,'scheduler_version':2}), \
                 patch.object(runtime,'step',wraps=runtime.step) as step, patch.object(runtime,'evaluate',wraps=runtime.evaluate) as evaluate, \
                 redirect_stdout(io.StringIO()):
                code=generated.run_runner_mode(args,{},'unused',0,str(output))
            step.assert_not_called()
            evaluate.assert_not_called()
            self.assertEqual(code,1)
            result=json.loads(output.read_text())
            self.assertEqual(result['evaluation_status'],'incomplete')
            self.assertIsNone(result['task_success'])

    def test_parent_rejects_wrong_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            script=root/'plan.py'
            script.write_text("import sys,json\np=sys.argv[sys.argv.index('--metrics-output')+1]\njson.dump({'execution_policy':'legacy'},open(p,'w'))\n")
            result=run_generated_executable(script, metrics_output=root/'metrics.json',timeout_seconds=1,execution_policy='strict')
            self.assertIn('does not match requested policy',result['error'])
            self.assertIsNone(result['task_success'])

    def test_benchmark_passes_policy_and_separates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'plan.py').write_text('')
            def execute(path, **kwargs):
                self.assertEqual(kwargs['execution_policy'], 'strict')
                self.assertIn('strict', kwargs['metrics_output'].parts)
                return dict(status='success',execution_policy='strict',scheduler_version=2,
                            execution_status='failed',evaluation_status='valid',task_success=False)
            with patch.object(benchmark,'run_generated_executable',side_effect=execute):
                report=benchmark.run_benchmark({'cases':[{'path':'plan.py'}]},repo_root=root,execution_policy='strict')
            self.assertEqual({g['scheduler_version'] for g in report['result_groups']},{2})
            self.assertTrue(all(r['failure_category']=='execution_failure' for r in report['results']))
