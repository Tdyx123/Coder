import json, subprocess, pathlib
root=pathlib.Path('/home/dwb/thor/Coder')
s=json.load(open(root/'coderun_results/0908_02.json'))
ids=['FloorPlan1_task_0','FloorPlan1_task_26','FloorPlan1_task_17']
chosen=[r for r in s['results'] if r['task_id'] in ids]
controls=[r for r in s['results'] if not any(a['action_type']=='GoToObject' and a['status']!='succeeded' for a in r['actions']) and r.get('navigation_metrics',{}).get('requests',0)>0][:3]
chosen+=controls
out=root/'reports/step_recovery_20260908'
out.mkdir(exist_ok=True)
(out/'baseline.json').write_text(json.dumps([{'task_id':r['task_id'],'executable_path':r['executable_path'],'actions':r['actions'],'navigation_metrics':r['navigation_metrics'],'timed_out':r['timed_out'],'run_time_seconds':r['run_time_seconds']} for r in chosen],indent=2))
args=['/home/dwb/.pyenv/bin/pyenv','exec','python',str(root/'scripts/executor_system/parallel_runner.py'),*[r['executable_path'] for r in chosen],'--max-workers','1','--movement-mode','step','--execution-policy','legacy','--reachable-refresh-mode','full','--timeout-seconds','120','--output-dir',str(out/'replay')]
print('Replaying', [r['task_id'] for r in chosen],flush=True)
raise SystemExit(subprocess.call(args,cwd=root))
