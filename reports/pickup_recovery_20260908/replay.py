"""Replay fixed original plans against a frozen executor or the working tree."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
from executor_system.parallel_runner import run_generated_executable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('variant', choices=('baseline', 'updated'))
    args = parser.parse_args()
    destination = OUT / args.variant
    destination.mkdir(exist_ok=True)
    code = OUT / (args.variant + '_code')
    expected = json.loads((OUT / 'code-hashes.json').read_text())[args.variant]
    for filename, digest in expected.items():
        assert hashlib.sha256((code / filename).read_bytes()).hexdigest() == digest
    for sample in json.loads((OUT / 'samples.json').read_text()):
        executable = Path(sample['executable_path'])
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == sample['sha256']
        result_path = destination / (sample['task_id'] + '.json')
        if result_path.exists():
            continue
        print('Starting', args.variant, sample['task_id'], flush=True)
        result = run_generated_executable(
            executable, metrics_output=destination / (sample['task_id'] + '.metrics.json'),
            timeout_seconds=120, movement_mode='step', execution_policy='legacy',
            reachable_refresh_mode='full', save_all_stdout=True,
            pythonpath_prepend=[str(code), str(ROOT / 'scripts')],
        )
        result['replay_executor_sha256'] = expected['executor_system/object_interactor.py']
        result_path.write_text(json.dumps(result, indent=2) + '\n')
        print('Finished', sample['task_id'], result.get('execution_status'),
              result.get('run_time_seconds'), flush=True)


if __name__ == '__main__':
    main()
