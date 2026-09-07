"""Archive immutable raw benchmark evidence with verified lossless compression."""
from pathlib import Path
import argparse, hashlib, json, lzma, shutil
parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('destination',type=Path);a=parser.parse_args()
a.destination.mkdir(parents=True,exist_ok=False)
with a.source.open('rb') as src,lzma.open(a.destination/'full-report.json.xz','wb',preset=6) as dst:shutil.copyfileobj(src,dst)
raw=a.source.read_bytes();digest=hashlib.sha256(raw).hexdigest();report=json.loads(raw)
keys=('case','case_index','movement_mode','execution_policy','reachable_refresh_mode','repetition','process_status','execution_status','evaluation_status','gcr','sr','original_goal_count','satisfied_goal_count','timed_out','error','actions','navigation_metrics','runtime_metrics','phase_durations_seconds','reproducibility','cleanup_errors','worker_errors')
summary={k:v for k,v in report.items() if k!='results'}
summary.update(original_report_sha256=digest,full_report_archive='full-report.json.xz',raw_source=str(a.source.resolve()))
summary['results']=[{k:row[k] for k in keys if k in row} for row in report.get('results',[])]
(a.destination/'report.json').write_text(json.dumps(summary,indent=2))
h=hashlib.sha256()
with lzma.open(a.destination/'full-report.json.xz','rb') as src:
 while True:
  chunk=src.read(1048576)
  if not chunk:break
  h.update(chunk)
if h.hexdigest()!=digest:raise RuntimeError('Archive verification failed')
print(json.dumps({'source_sha256':digest,'archive_bytes':(a.destination/'full-report.json.xz').stat().st_size,'runs':len(report.get('results',[])),'check_passed':report.get('check_passed')}))
