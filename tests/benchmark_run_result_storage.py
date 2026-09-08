import argparse
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from tests.test_run_result_storage import complete_result
parser = argparse.ArgumentParser(description="Manual 340-task/361-attempt storage benchmark; not a unit test.")
parser.add_argument('mode', choices=('before', 'after'))
parser.add_argument('--baseline-revision', default='a35a60b097b3e945f6e99c1ff9f2b9617f7f6ed7')
parser.add_argument('--report', type=Path)
args = parser.parse_args()
mode = args.mode
if mode == 'before':
    source = subprocess.check_output(['git', 'show', f'{args.baseline_revision}:scripts/executor_system/run_results.py'], cwd=repo, text=True)
    import types
    module = types.ModuleType('old_run_results')
    exec(compile(source, 'old_run_results.py', 'exec'), module.__dict__)
else:
    from executor_system import run_results as module
with tempfile.TemporaryDirectory(prefix='storage-benchmark-') as temp:
    root = Path(temp)
    store = module.RunResultStore(root, 'benchmark')
    store.summary_path = root / 'summary.json'
    module.atomic_write_json(store.summary_path, {'run_status': 'in_progress'})
    samples = []
    payload = 'x' * 1004000
    rebuilds = [0]
    original = store.rebuild_summary
    def rebuild():
        rebuilds[0] += 1
        return original()
    store.rebuild_summary = rebuild
    def record(item):
        index, attempt = item
        key = module.task_key_for_executable(root / f'task-{index:03d}.py')
        result = complete_result('benchmark', key, attempt, stdout=payload)
        start = time.monotonic()
        store.record_attempt(key, attempt, result)
        samples.append(time.monotonic() - start)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, [(i, 1) for i in range(340)]))
        list(pool.map(record, [(i, 2) for i in range(11)]))
        list(pool.map(record, [(i, 3) for i in range(10)]))
    stored = time.monotonic()
    store.write_summary('completed')
    report = dict(mode=mode, tasks=340, attempts=len(samples), summary_bytes=store.summary_path.stat().st_size,
        first_20_mean=statistics.mean(samples[:20]), last_20_mean=statistics.mean(samples[-20:]),
        max_record_seconds=max(samples), persistence_seconds=stored-started,
        final_summary_seconds=time.monotonic()-stored, total_seconds=time.monotonic()-started,
        peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, rebuilds=rebuilds[0])
    print(json.dumps(report), flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
