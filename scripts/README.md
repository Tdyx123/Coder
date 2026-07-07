# Scripts Directory Notes

This document summarizes the current script entry points, required paths, and output locations used by the repository.

## Directory Expectations

The scripts are expected to be launched from the project root.

```text
LaMMA-P/
├── data/
│   ├── aithor_connect/
│   └── final_test/
├── downward/
├── logs/
├── resources/
└── scripts/
```

Important runtime locations:

- `scripts/providers.yaml`: LiteLLM provider and model definitions
- `scripts/pddlrun_llmseparate_config.yaml`: runtime storage configuration
- `resources/generated_subtask/`: generated PDDL subtasks
- `logs/task_manager_runs/`: planner outputs
- `logs/intermediate_runs/`: intermediate artifacts

## Main Scripts

### `pddlrun_llmseparate.py`

Main planner entry point.

```bash
python scripts/pddlrun_llmseparate.py --floor-plan 1
```

Arguments currently supported by the script:

- `--bddl-file`
- `--floor-plan`
- `--model`
- `--prompt-decompse-set`
- `--prompt-allocation-set`
- `--test-set`
- `--task-index`
- `--decompose-rag`
- `--no-decompose-rag`

Current defaults in code:

- model: `deepseek-chat`
- prompt decomposition set: `pddl_train_task_decomposesep`
- prompt allocation set: `pddl_train_task_allocationsep`
- test set: `final_test`

### `run_pddlrun_llmseparate_parallel.py`

Parallel wrapper for launching multiple floor plans and tasks.

```bash
python scripts/run_pddlrun_llmseparate_parallel.py --floor-plans 1 2 3
```

Arguments:

- `--floor-plans`
- `--model`
- `--test-set`
- `--max-floor-plan-workers`
- `--max-task-workers`
- `--output-root`
- `--prompt-decompse-set`
- `--prompt-allocation-set`
- `--disable-log-results`
- `--decompose-rag`
- `--no-decompose-rag`

If `--output-root` is not provided, summary files are written under:

```text
parallel_runs/pddlrun_llmseparate_<timestamp>/
```

### `plantocode.py`

Translates planning outputs into AI2-THOR-executable Python code.

```bash
python scripts/plantocode.py --logs-dir ./logs/task_manager_runs --validate-code
```

Generate baseline-compatible summaries through the concrete baseline entrypoints.
The generated summaries can still be discovered by
`executor_system/parallel_runner.py --base-line`:

```bash
python scripts/baselines/LaMMA-P.py --root ./baselines/LaMMA-P

python scripts/baselines/SMART-LLM.py --root ./baselines/SMART-LLM

python scripts/baselines/Scale-Plan.py --root ./baselines/Scale-Plan

python scripts/executor_system/parallel_runner.py \
  --base-line LaMMA-P \
  --max-workers 4 \
  --timeout-seconds 30 \
  --output-dir ./parallel_runner_results
```

Arguments:

- `--logs-dir`
- `--parallel-run`
- `--floor-plan`
- `--output-dir`
- `--validate-code`
- `--no-validate-code`

Behavior worth knowing:

- `--validate-code` is enabled by default
- the script recursively scans complete pddlrun task folders
- `--parallel-run` accepts a `parallel_runs/...` directory or its `summary.json`
- `--floor-plan` restricts conversion to one floor, e.g. `6` or `FloorPlan6`
- it writes summary files to `--output-dir`
- it writes `plan_to_code/executable_plan.py` into each original log folder
- generated `plan_to_code/executable_plan.py` can also be called with
  `--runner-mode` for no-render metric collection

Baseline entrypoint defaults:

- `scripts/baselines/LaMMA-P.py --root ./baselines/LaMMA-P` reads
  `baselines/LaMMA-P/logs/intermediate_runs` and writes
  `baselines/LaMMA-P/plan_to_code_results`
- `scripts/baselines/SMART-LLM.py --root ./baselines/SMART-LLM` reads
  `baselines/SMART-LLM/logs` and writes `baselines/SMART-LLM`
- `scripts/baselines/Scale-Plan.py --root ./baselines/Scale-Plan` reads
  `baselines/Scale-Plan/logs/intermediate_runs` and writes
  `baselines/Scale-Plan/plan_to_code_results`

### `generate_single_subtask_code.py`

Generates standalone executable Python files for feasible single subtasks.

```bash
python scripts/generate_single_subtask_code.py \
  --floor-plans 1 \
  --output-dir ./data/single_subtask_code
```

The generator reads `data/bad_single_subtasks.json` by default, when present,
and excludes matching bad subtasks before applying `--limit`.

Bad subtask config format:

```json
{
  "version": 1,
  "bad_subtasks": [
    {
      "floor_plan": 1,
      "subtask": {"skill": "Break", "objects": ["WineBottle"]},
      "reason": "Known runner failure in FloorPlan1"
    }
  ]
}
```

Behavior worth knowing:

- pass `--bad-subtasks-config PATH` to use a different JSON file
- `floor_plan` accepts either `1` or `"FloorPlan1"`
- omit `floor_plan` or set it to `null` to filter the subtask on every floor
- matching is exact on `skill` and ordered `objects`
- filtering affects only the current generation run; old files already in an
  output directory are not deleted, so regenerate into an empty directory or
  clear stale outputs when changing the bad subtask list
- generated summaries include `excluded_subtasks` and `bad_subtasks_config`

### `executor_system/parallel_runner.py`

Runs multiple `plantocode.py` generated `plan_to_code/executable_plan.py`
files concurrently.

```bash
python scripts/executor_system/parallel_runner.py \
  --root ./logs/task_manager_runs \
  --max-workers 4 \
  --timeout-seconds 30 \
  --output-dir ./parallel_runner_results
```

Run generated code for one `parallel_runs/...` run. Generate the code first:

```bash
python scripts/plantocode.py \
  --parallel-run parallel_runs/pddlrun_llmseparate_<timestamp> \
  --output-dir ./plan_to_code_results

python scripts/executor_system/parallel_runner.py \
  --parallel-run parallel_runs/pddlrun_llmseparate_<timestamp> \
  --max-workers 4 \
  --timeout-seconds 30 \
  --output-dir ./parallel_runner_results
```

Run every Python file directly under a directory:

```bash
python scripts/executor_system/parallel_runner.py \
  --py-dir ./data/single_subtask_code \
  --max-workers 4 \
  --timeout-seconds 30 \
  --output-dir ./parallel_runner_results
```

Behavior worth knowing:

- each generated file is run in a subprocess with `--runner-mode`
- `--root` recursively discovers `plan_to_code/executable_plan.py` files
- `--parallel-run` uses one parallel run summary to find that run's generated
  `task_run_dir/plan_to_code/executable_plan.py` files; it does not generate
  code, so run `scripts/plantocode.py --parallel-run ...` first
- `--py-dir` runs direct child `*.py` files from the given directory
- `--parallel-run` cannot be combined with `--root`, `--base-line`, `--py-dir`,
  or positional executable paths
- runner mode sets `renderImage=False` and skips video/metadata output
- each subprocess has a 30 second timeout by default
- timed-out tasks are retried up to two times after each full round completes
- each completed round cleans up matching GPU processes before the next retry round
- summary metrics are written to a dated JSON filename, such as `0628_01.json`
  or `LaMMA-P_0628_01.json`
- `stdout` is kept only for results with `robot_failures` by default; pass
  `--save-all-stdout` to keep it for every result

### `execute_plan.py`

Executes a generated `code_plan.py` by assembling an `executable_plan.py`.

```bash
python scripts/execute_plan.py --command <log_folder_name>
```

The script currently resolves the target folder as:

```text
logs/<log_folder_name>/
```

So the command value should be a directory name directly inside `logs/`.

## Dataset Format

The parallel runner currently loads task files from:

```text
data/<test_set>/FloorPlan{N}.jsonl
```

Each line is expected to be a JSON object describing one task.

## Provider and Storage Configuration

### Provider File

`scripts/providers.yaml` should define the LiteLLM providers and model names you want exposed to the scripts.

Each provider must now use an `api_keys` list, even when only one key is configured.

Example:

```yaml
providers:
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_keys:
      - your_first_key
      - your_second_key
    models:
      - deepseek-chat
      - deepseek-reasoner
```

Requests rotate across the configured keys in round-robin order. If a key encounters a rate limit or another retryable API error, the current request switches to the next key from the same provider and retries up to 3 times, waiting 5, 10, and 15 seconds between retries.

### Storage File

`scripts/pddlrun_llmseparate_config.yaml` currently contains:

```yaml
storage:
  base_dir: logs/intermediate_runs
```

Update this file if you want to redirect intermediate artifacts to another location.
