import json,os,signal,subprocess,sys,time
from pathlib import Path
source=Path(sys.argv[3]).resolve(); code=Path(sys.argv[1]).resolve(); output=Path(sys.argv[2]).resolve(); output.mkdir(parents=True,exist_ok=True)
cases=json.loads((source/'tests/fixtures/movement_benchmark_plans.json').read_text())['cases']; rows=[]
for index,case in enumerate(cases):
 for mode,budget in [('teleport',30),('step',120)]:
  attempt=output/f'{index:02d}_{mode}';attempt.mkdir(exist_ok=True)
  env=os.environ.copy();env['PYTHONPATH']=str(code/'scripts')+os.pathsep+str(code);env['PYTHONDONTWRITEBYTECODE']='1'
  command=[sys.executable,str(source/case['path']),'--runner-mode','--metrics-output',str(attempt/'metrics.json'),'--movement-mode',mode,'--timeout-seconds',str(budget)]
  started=time.monotonic();timed_out=False
  with (attempt/'stdout.log').open('w') as out,(attempt/'stderr.log').open('w') as err:
   proc=subprocess.Popen(command,cwd=code,env=env,stdout=out,stderr=err,start_new_session=True)
   try:
    try:proc.wait(timeout=budget+70)
    except subprocess.TimeoutExpired:timed_out=True
   finally:
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:proc.wait(timeout=2)
    except subprocess.TimeoutExpired:pass
    try:os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    proc.wait(timeout=3)
  row={'case':case,'movement_mode':mode,'returncode':proc.returncode,'timed_out':timed_out,'elapsed':time.monotonic()-started,'directory':str(attempt)}
  if (attempt/'metrics.json').exists():
   try:row['metrics']=json.loads((attempt/'metrics.json').read_text())
   except ValueError:row['invalid_metrics']=True
  rows.append(row);(output/'report.json').write_text(json.dumps({'code_root':str(code),'results':rows},indent=2,ensure_ascii=False));print(index,mode,proc.returncode,flush=True)
