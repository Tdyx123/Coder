"""Compatibility and observable boundaries of the runtime services; no simulator."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from executor_system.runtime import ThorRuntime
from executor_system.execution_control import ExecutionCancelled, close_runtime
from tests.test_world_snapshot import multi_event, snapshot_runtime


class PlanTypesFacadeTest(unittest.TestCase):
    def test_legacy_imports_are_identical_types(self):
        from executor_system import action_plan, plan_types
        for name in ('Action', 'PlannedAction', 'StagePlan', 'TaskPlan',
                     'MultiStageActionPlan', 'ActionResult', 'RobotExecutionState',
                     'ResourceRequest'):
            with self.subTest(name=name):
                self.assertIs(getattr(action_plan, name), getattr(plan_types, name))
        from executor_system.task_plan import TaskPlan
        self.assertIs(TaskPlan, plan_types.TaskPlan)

    def test_parsing_types_does_not_load_execution_or_cli_modules(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
sys.path.insert(0, 'scripts')
from executor_system.plan_types import Action, TaskPlan
value = Action.from_any({'action': 'ThrowObject', 'throwMagnitude': 7})
assert value.parameters == {'throwMagnitude': 7}
plan = TaskPlan.from_dict({'stages': [{'robot_action_queues': {'robot1': ['Pass']}}]})
assert plan.stages[0].robot_action_queues['robot1'][0].robot_id == 'robot1'
assert not any(name in sys.modules for name in (
    'executor_system.runtime', 'executor_system.action_plan',
    'executor_system.action_registry', 'executor_system.parallel_runner',
    'executor_system.generated_plan_runtime'))
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
