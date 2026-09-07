import collections
from copy import deepcopy
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys
src=Path(sys.argv[1]); dst=Path(sys.argv[2]); dst.mkdir(parents=True,exist_ok=True)
raw=src.read_bytes(); report=json.loads(raw)
with (dst/'full-report.json.gz').open('wb') as f:
    with gzip.GzipFile(filename='',mode='wb',fileobj=f,mtime=0) as z:
        z.write(raw)
compact=deepcopy(report)
compact['full_report_sha256']=sha256(raw).hexdigest()
compact['evidence_note']='Full immutable snapshots and parent-validated metrics are preserved in full-report.json.gz. This readable copy retains action/wait/wave/resource evidence and snapshot versions; large object metadata omitted.'
for row in compact['results']:
    row.pop('validated_metrics',None)
    metrics=row['metrics']
    metrics.pop('final_snapshot',None)
    for stage in metrics.get('stages',[]):
        snap=stage.pop('snapshot',None)
        if snap:
            stage['snapshot_summary']={key:snap.get(key) for key in ('version','robot_positions','robot_rotations','held_objects')}
            stage['snapshot_summary']['object_count']=len(snap.get('objects_by_id',{}))
(dst/'report.json').write_text(json.dumps(compact,ensure_ascii=False,indent=2)+'\n')
(dst/'result_example.json').write_text(json.dumps(report['results'][0]['metrics'],ensure_ascii=False,indent=2)+'\n')
groups=[]
for policy in ('legacy','strict'):
    for mode in ('teleport','step'):
        rows=[r for r in report['results'] if (r['execution_policy'],r['movement_mode'])==(policy,mode)]
        groups.append({'execution_policy':policy,'movement_mode':mode,'scheduler_version':2,'evaluation_version':'fixed_goals_v2','runs':len(rows),
                       'exit0':sum(r['process']['returncode']==0 for r in rows),
                       'execution_status':dict(collections.Counter(r['metrics']['execution_status'] for r in rows)),
                       'evaluation_status':dict(collections.Counter(r['metrics']['evaluation_status'] for r in rows)),
                       'gcr_mean':sum(r['metrics']['gcr'] for r in rows)/len(rows),
                       'sr_successes':sum(r['metrics']['sr']==1 for r in rows),
                       'expected_stage_cancel_records':sum(len(r['metrics'].get('worker_errors',[])) for r in rows)})
(dst/'summary.json').write_text(json.dumps({'code_sha':report['code_sha'],'groups':groups},ensure_ascii=False,indent=2)+'\n')
print(json.dumps(groups,ensure_ascii=False,indent=2))
