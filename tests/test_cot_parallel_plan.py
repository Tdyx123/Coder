import json
import tempfile
import unittest
from pathlib import Path

from tests.test_cot_baseline_converter import (
    load_cot_module, load_bundle_data, write_dataset,
    write_success_run, write_parallel_summary, failed_task_row,
)


PARALLEL_PLAN = """Stage 1 (parallel robot chains; wait for all before the next stage)
  robot1 (sequential):
    (GoToObject robot1 drawer)
    (OpenObject robot1 drawer)
  robot9 (sequential):
    (GoToObject robot9 mug)

Stage 2 (parallel robot chains; wait for all before the next stage)
  robot1 (sequential):
    (GoToObject robot1 sinkbasin)
"""


class CotParallelPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = self.root / 'baselines' / 'COT'
        write_dataset(self.root)
        self.cot = load_cot_module()

    def add_run(self, name, text=None):
        run = self.baseline / 'parallel_runs' / name
        task, row = write_success_run(run)
        write_parallel_summary(run, [row])
        if text is not None:
            (task / '02_plan/04_parallel_plan.txt').write_text(text, encoding='utf-8')
        return task

    def convert(self, *args):
        return self.cot.main(['--root', str(self.baseline), *args])

    def results(self):
        return json.loads((self.baseline / 'plan_to_code_results/plan_to_code_results.json').read_text())

    def test_parallel_text_overrides_json_and_preserves_stage_queues(self):
        tasks = [self.add_run('a', PARALLEL_PLAN), self.add_run('b', PARALLEL_PLAN)]
        (tasks[1] / '02_plan/01_final_plan.json').unlink()
        self.assertEqual(self.convert(), 0)
        for task in tasks:
            bundle = load_bundle_data(task / 'plan_to_code/executable_plan.py')
            stages = bundle['task_plan']['stages']
            self.assertEqual([s['stage_id'] for s in stages], ['Stage 1', 'Stage 2'])
            self.assertEqual(list(stages[0]['robot_action_queues']), ['robot1', 'robot9'])
            self.assertEqual([a['action_type'] for a in stages[0]['robot_action_queues']['robot1']],
                             ['GoToObject', 'OpenObject'])
            self.assertEqual(bundle['no_trans'], 4)
        self.assertTrue(all(r['plan_mode'] == 'parallel' for r in self.results()))
        self.assertEqual([r['plan_source'] for r in self.results()],
                         [str(t / '02_plan/04_parallel_plan.txt') for t in tasks])

    def test_missing_parallel_file_fails_cleans_old_code_and_continues(self):
        missing = self.add_run('a')
        self.assertEqual(self.convert(), 0)
        self.add_run('b', PARALLEL_PLAN)
        self.assertEqual(self.convert(), 1)
        self.assertFalse((missing / 'plan_to_code/executable_plan.py').exists())
        results = self.results()
        self.assertEqual([r['status'] for r in results], ['failed', 'success'])
        self.assertIn('04_parallel_plan.txt', results[0]['error'])

    def test_limit_excludes_parallel_trigger(self):
        self.add_run('a')
        self.add_run('b', PARALLEL_PLAN)
        self.assertEqual(self.convert('--limit', '1'), 0)
        self.assertEqual(self.results()[0]['plan_mode'], 'json')
        self.assertEqual(self.results()[0]['stage_count'], 3)

    def test_dry_run_does_not_write(self):
        task = self.add_run('a', PARALLEL_PLAN)
        self.assertEqual(self.convert('--dry-run'), 0)
        self.assertFalse((task / 'plan_to_code').exists())
        self.assertFalse((self.baseline / 'plan_to_code_results').exists())

    def test_floor_filter_excludes_parallel_trigger(self):
        self.add_run('a')
        run = self.baseline / 'parallel_runs/b'
        task, row = write_success_run(run)
        row['floor_plan'] = row['manifest']['floor_plan'] = 'FloorPlan2'
        write_parallel_summary(run, [row], floor_plan='FloorPlan2')
        (task / '02_plan/04_parallel_plan.txt').write_text(PARALLEL_PLAN)
        self.assertEqual(self.convert('--floor-plan', '1'), 0)
        self.assertEqual(len(self.results()), 1)
        self.assertEqual(self.results()[0]['plan_mode'], 'json')

    def test_skipped_source_still_triggers_batch_mode(self):
        self.add_run('a')
        run = self.baseline / 'parallel_runs/b'
        row = failed_task_row()
        write_parallel_summary(run, [row])
        plan = run / row['run_dir'] / '02_plan/04_parallel_plan.txt'
        plan.parent.mkdir(parents=True)
        plan.write_text(PARALLEL_PLAN)
        self.assertEqual(self.convert(), 1)
        self.assertEqual([r['status'] for r in self.results()], ['failed', 'skipped'])

    def test_failed_dry_run_preserves_previous_code(self):
        task = self.add_run('a')
        self.assertEqual(self.convert(), 0)
        old = task / 'plan_to_code/executable_plan.py'
        previous = old.read_bytes()
        self.add_run('b', PARALLEL_PLAN)
        self.assertEqual(self.convert('--dry-run'), 1)
        self.assertEqual(old.read_bytes(), previous)

    def test_invalid_text_never_falls_back_to_json(self):
        task = self.add_run('a', PARALLEL_PLAN)
        invalid = ['', 'garbage', 'Stage 1\n',
                   'Stage 1\nrobot1 (sequential):\n',
                   'Stage 1\n(GoToObject robot1 mug)',
                   PARALLEL_PLAN.replace('OpenObject robot1', 'InventedAction robot1'),
                   PARALLEL_PLAN.replace('OpenObject robot1', 'OpenObject robot9'),
                   PARALLEL_PLAN.replace('robot9', 'robot999'),
                   PARALLEL_PLAN.replace('Stage 2', 'Stage 1')]
        for text in invalid:
            with self.subTest(text=text):
                old = task / 'plan_to_code/executable_plan.py'
                old.parent.mkdir(exist_ok=True)
                old.write_text('# old code')
                (task / '02_plan/04_parallel_plan.txt').write_text(text)
                self.assertEqual(self.convert(), 1)
                self.assertFalse(old.exists())
                self.assertEqual(self.results()[0]['plan_mode'], 'parallel')


if __name__ == '__main__':
    unittest.main()
