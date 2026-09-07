import json
import math
import sys
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
assert r['status']=='completed' and len(r['results'])==48
assert r['contract_pass']
assert not r['scope_cleanup']
seen=set()
for row in r['results']:
    m=row['metrics']; checked=row['validated_metrics']
    key=(row['case']['path'],row['movement_mode'],row['execution_policy'])
    assert key not in seen
    seen.add(key)
    assert row['contract_valid'] and row['fixed_denominator']
    assert row['process']['returncode']==0 and not row['process']['timed_out'], key
    assert m['execution_policy']==row['execution_policy'] and m['scheduler_version']==2
    assert m['original_goal_count']==row['expected_goal_count']
    assert checked['evaluation_status']==m['evaluation_status']=='valid', key
    assert m['execution_quiescent'] is True, key
    assert not m['cleanup_errors'], key
    stage_cancel=any(e.get('exception_type')=='StageFailureDecisionError' for e in m.get('errors',[]))
    assert all(e.get('exception_type')=='ExecutionCancelled' and stage_cancel and m['execution_status']=='failed' for e in m['worker_errors']), key
    assert len(m['actions'])==m['action_counts']['planned'], key
    for stage in m['stages']:
        assert all(k in stage for k in ('actions','attempts','waits','waves','resources','snapshot','condition')), key
    ri=m['ru_inputs']
    upper=ri['max_trans']+1; lower=ri['no_trans_gt']+1
    expected=(1.0 if upper==lower==ri['no_trans'] else 0.0) if upper==lower else (upper-ri['no_trans'])/(upper-lower)
    assert math.isclose(m['ru'], expected), key
print('48 runs: unique cases/policy/mode; process exit; contract/identity; fixed denominator; current scheduler/policy; quiescent evaluation; no unexpected worker/cleanup errors (stage child cancellations retained); full audit fields: PASS')
