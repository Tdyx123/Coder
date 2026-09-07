"""Reproducible scheduler examples using deterministic simulator substitutes.

TaskRunner, StageScheduler, leases and StepMovementStrategy are production code.
Only the high-level adapter/Unity world are replaced. This is not Unity acceptance.
"""
import argparse
from dataclasses import replace
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT)]
from executor_system.action_plan import Action, StagePlan, TaskPlan
from executor_system.parallel_runner import run_action_plan_tolerant
from executor_system.movement import MovementConfig
from executor_system.run_results import atomic_write_json
from tests.snapshot_fakes import FakeRuntime
from tests.test_navigation_batch_failures import make_case


def policy_example(policy):
    runtime = FakeRuntime()
    runtime.movement_config = MovementConfig.resolve('step', environ={})
    runtime.objects = [dict(objectId='Mug|1',objectType='Mug',visible=True),
                       dict(objectId='Microwave|1',objectType='Microwave',visible=True,isOpen=False,openable=True)]
    order = []
    def execute(adapter, robot, action, **kwargs):
        with runtime.controller_lock:
            order.append([robot,action.action_type])
            runtime.state_version += 1
            if action.action_type == 'PickupObject':
                raise RuntimeError('deterministic pickup collision')
            runtime.objects[1]['isOpen'] = action.action_type == 'OpenObject'
    queue = [Action('PickupObject', {'args':('Mug',)}, on_failure='FAIL_ROBOT'),
             Action('OpenObject', {'args':('Microwave',)}),Action('CloseObject', {'args':('Microwave',)})]
    peer = [Action('OpenObject', {'args':('Microwave',)}),Action('CloseObject', {'args':('Microwave',)})]
    expected_calls = 5 if policy=='legacy' else 3
    condition = lambda world: len(order)==expected_calls and not world.snapshot.objects_by_id['Microwave|1']['isOpen']
    plan = TaskPlan(policy,[StagePlan('shared-device',{'robot1':queue,'robot2':peer},condition)])
    with patch('executor_system.action_plan.AI2ThorAdapter.execute',execute),redirect_stdout(io.StringIO()):
        report = run_action_plan_tolerant(runtime,plan,execution_policy=policy,timeout_seconds=2)
    assert len(order)==expected_calls, order
    assert report['execution_status']==('partial' if policy=='legacy' else 'failed'),report
    assert [r for r,a in order if r=='robot1']==(['robot1']*3 if policy=='legacy' else ['robot1'])
    assert [a for r,a in order if r=='robot2']==['OpenObject','CloseObject']
    holders, grants = {}, {}
    for event in report['stages'][0]['resources']:
        if event['event']=='acquired':
            grants[event['action_key']]=event['keys']
        for key in grants[event['action_key']]:
            if event['event']=='acquired':
                assert key not in holders
                holders[key]=event['action_key']
            else:
                assert holders.pop(key)==event['action_key']
    assert not holders
    return dict(example=policy+'_pickup_failure',adapter='deterministic simulator substitute',order=order,report=report)


def step_dependency_example():
    grid,strategy,requests=make_case(selected_agent_id=1)
    runtime=FakeRuntime()
    runtime.movement_config=MovementConfig.resolve('step',environ={})
    runtime.objects=list(grid.objects.values())
    for agent,event in enumerate(runtime.controller.last_event.events):
        event.metadata['agent']['position']=grid.current_agent_position(agent)
    order=[]
    def ready(world):
        return world.robot_positions['robot2']['x']==0.5
    def execute(adapter,robot,action,**kwargs):
        if action.action_type=='GoToObject':
            strategy.navigate(replace(requests[1],phase_coordinator=kwargs['phase_coordinator'],
                                      action_wave=kwargs['action_wave']))
            with runtime.controller_lock:
                runtime.controller.last_event.events[1].metadata['agent']['position']=grid.current_agent_position(1)
                runtime.state_version+=1
        order.append([robot,action.action_type])
    plan=TaskPlan('dependency',[StagePlan('A-waits-for-B',{
        'robot1':[Action('Wait',wait_until=ready)],
        'robot2':[Action('GoToObject',{'args':('Target|1',)})]},ready,
        synchronization_policy='BARRIER_EACH_STEP')])
    with patch('executor_system.action_plan.AI2ThorAdapter.execute',execute),redirect_stdout(io.StringIO()):
        report=run_action_plan_tolerant(runtime,plan,execution_policy='strict',timeout_seconds=2)
    assert report['execution_status']=='completed',report
    assert order==[['robot2','GoToObject'],['robot1','Wait']],order
    assert grid.successful_move_count==2
    assert not any(action[0]=='Teleport' for action in grid.actions)
    return dict(example='step_A_waits_for_B',adapter='GridThorRuntime + production StepMovementStrategy',
                order=order,controller_actions=grid.actions,report=report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('/tmp/executor-batch-2-examples.json'))
    args=parser.parse_args()
    examples=[policy_example('legacy'),policy_example('strict'),step_dependency_example()]
    atomic_write_json(args.output,{'kind':'deterministic semantic regression; not Unity acceptance','examples':examples})
    for example in examples:
        print(example['example'],example['order'],example['report']['execution_status'])
    print(args.output)

if __name__=='__main__':
    main()
