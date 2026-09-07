# LaMMA-P: Generalizable Multi-Agent Long-Horizon Task Allocation and Planning with LM-Driven PDDL Planner

This is the official repository for the LaMMA-P codebase. It includes instructions for configuring and running LaMMA-P on the MAT-THOR datasets in the AI2-THOR simulator. It is accepted as a conference paper by the IEEE International Conference on Robotics and Automation (ICRA), Atlanta, 2025.

[Project Website](https://lamma-p.github.io/) | [Paper](https://arxiv.org/abs/2409.20560) | [Video](https://www.youtube.com/watch?v=1edDuJbk_uk)

<img src="docs/motivation.png" width="100%"/>

**Abstract:** Language models (LMs) possess a strong capability to comprehend natural language, making them effective in translating human instructions into detailed plans for simple robot tasks. Nevertheless, it remains a significant challenge to handle long-horizon tasks, especially in subtask identification and allocation for cooperative heterogeneous robot teams. To address this issue, we propose a Language Model-Driven Multi-Agent PDDL Planner (LaMMA-P), a novel multi-agent task planning framework that achieves state-of-the-art performance on long-horizon tasks. LaMMA-P integrates the strengths of LMs' reasoning capability and traditional heuristic search planning to achieve a high success rate and efficiency while demonstrating strong generalization across tasks. Additionally, we create MAT-THOR, a comprehensive benchmark that features household tasks with two different levels of complexity based on the AI2-THOR environment. The experimental results demonstrate that LaMMA-P achieves a 105% higher success rate and 36% higher efficiency than existing LM-based multi-agent planners.

## Code Organization

- `resources/`: Robot definitions, action definitions, PDDL domain files, and generated subtasks
- `scripts/`: Main planning, plan-to-code, execution, and utility scripts
- `data/`: AI2-THOR connectors, datasets, and prompt examples
- `downward/`: Fast Downward planner submodule
- `docs/`: Figures and documentation assets

## Datasets

- Test tasks: `data/final_test/`
- Robot definitions: `resources/robots.py`
- Floor plans: refer to [AI2-THOR Demo](https://ai2thor.allenai.org/demo) for layouts

The current scripts load floor-plan task files from:

```text
data/final_test/FloorPlan{N}.jsonl
```

## Environment Setup

### 1. Python Environment

Create a conda environment (or virtualenv):

```bash
conda create -n lammap python=3.9
conda activate lammap
pip install -r requirements.txt
```

### 2. Fast Downward Planner

The project requires the [Fast Downward Planner](https://github.com/aibasel/downward/).

This repository can also share one local Fast Downward build with sibling projects by setting:

```bash
export FAST_DOWNWARD_PATH=/home/dwb/thor/LaMMA-P/downward/fast-downward.py
```

When `FAST_DOWNWARD_PATH` is set, LaMMA-P uses it before the configured
`planner.executable` value in `scripts/pddlrun_llmseparate_config.yaml`.

```bash
git submodule update --init --recursive
cd downward
python build.py
python fast-downward.py --help
cd ..
```

### 3. LiteLLM Provider Setup

The scripts read model provider settings from `scripts/providers.yaml`.

1. Open `scripts/providers.yaml`
2. Fill in the `api_keys` list for the provider you want to use
3. Update `base_url` and `models` if you use a custom endpoint

The available model choices exposed by the CLI are loaded dynamically from this file.

Example:

```yaml
providers:
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_keys:
      - your_first_key
      - your_second_key
      - your_third_key
    models:
      - deepseek-chat
      - deepseek-reasoner
```

When multiple keys are configured for one provider, LaMMA-P rotates keys in round-robin order per request. If one key hits rate limits or another retryable upstream error, the same request will switch to the next configured key and retry up to 3 times, waiting 5, 10, and 15 seconds between retries.

### 4. Runtime Storage Setup

`scripts/pddlrun_llmseparate.py` also reads runtime storage settings from:

```text
scripts/pddlrun_llmseparate_config.yaml
```

By default, intermediate files are written to:

```text
logs/intermediate_runs
```

Final task logs are written under:

```text
logs/task_manager_runs/<instance_id>/
```

## Quickstart

### 1. Run the Planner for a Floor Plan

```bash
python scripts/pddlrun_llmseparate.py --floor-plan 1
```

Common options:

- `--model`: model name defined in `scripts/providers.yaml` (default: `deepseek-chat`)
- `--prompt-decompse-set`: prompt set for decomposition (default: `pddl_train_task_decomposesep`)
- `--prompt-allocation-set`: prompt set for allocation (default: `pddl_train_task_allocationsep`)
- `--test-set`: dataset split to use (default: `final_test`)
- `--plan-feedback` / `--no-plan-feedback`: enable or disable allocation and planner retries
- `--plan-feedback-max-retries`: maximum plan-feedback retry rounds after the first allocation attempt
- `--val-feedback`: run VAL after planning and retry failed PDDL Problem generation
- `--val-feedback-max-retries`: maximum VAL-driven Problem regeneration rounds
- `--problem-repair` / `--no-problem-repair`: enable or disable pure local checks and deterministic repairs after Problem generation (default: disabled)
- `--no-log-results`: disable per-task result logging
- `--bddl-file`: run from a BDDL file instead of a dataset floor plan

What the script does:

1. Decompose the high-level task into subtasks
2. Generate PDDL problem files for each subtask
3. Run Fast Downward on each subtask
4. Merge the subtask plans into a final combined plan
5. Save logs and artifacts for later plan-to-code conversion

When both plan feedback and VAL feedback are enabled, all plan-feedback attempts
finish before VAL starts. VAL validates only the subtasks that have a usable plan
from the final allocation attempt, so missing plans and non-contiguous subtask IDs
do not prevent the available subset from being validated. After VAL starts, a VAL
retry never returns to allocation feedback.

### 2. Run Multiple Floor Plans in Parallel

The repository now includes a parallel wrapper:

```bash
python scripts/run_pddlrun_llmseparate_parallel.py --floor-plans 1 2 3
```

Useful options:

- `--model`
- `--test-set`
- `--max-floor-plan-workers`
- `--max-task-workers`
- `--output-root`
- `--plan-feedback` / `--no-plan-feedback`
- `--plan-feedback-max-retries`
- `--val-feedback` / `--no-val-feedback`
- `--val-feedback-max-retries`
- `--problem-repair` / `--no-problem-repair`
- `--disable-log-results`

If `--output-root` is not provided, summaries are written to:

```text
parallel_runs/pddlrun_llmseparate_<timestamp>/
```

To print completion commands for eligible incomplete runs under `parallel_runs/`:

```bash
python scripts/generate_pddlrun_completion_commands.py
```

This no-argument generator only prints comment-and-command pairs; it does not
start jobs or write files. Each generated command uses the original run directory
as `--output-root` and enables `--merge-existing-floor-summaries`, so running that
command writes the missing floors back to the original output root and rebuilds
the top-level summary from both existing and newly generated floor summaries.

### 3. Convert Generated Plans into Executable AI2-THOR Code

For the newer `parallel_runs/pddlrun_llmseparate_<timestamp>/` layout, encode the allocation and planner artifacts deterministically into standalone AI2-THOR scripts:

```bash
python scripts/parallel_plan_to_code.py \
  --parallel-run parallel_runs/pddlrun_llmseparate_<timestamp> \
  --floor-plan 6 \
  --task-index 0
```

This writes `code_plan.py`, `executable_plan.py`, and `encoding_summary.json` into the task's `plan_to_code/` folder. Headless AI2-THOR execution is the default; add `--no-headless` when you want local rendering windows for visual debugging. Add `--execute` to run each generated standalone script immediately.

After planning, convert generated plans into executable Python code:

```bash
python scripts/plantocode.py --logs-dir ./logs/task_manager_runs --validate-code
```

For a `parallel_runs/pddlrun_llmseparate_<timestamp>/` directory, convert all successful task plans in the run, optionally filtering to one floor:

```bash
python scripts/plantocode.py \
  --parallel-run parallel_runs/pddlrun_llmseparate_<timestamp> \
  --floor-plan 6
```

Important notes:

- `--logs-dir` defaults to `./logs`
- `--parallel-run` accepts a parallel run directory or its `summary.json`
- `--floor-plan` restricts conversion to one floor, e.g. `6` or `FloorPlan6`
- `--output-dir` defaults to `./plan_to_code_results`
- `--validate-code` is enabled by default
- `plantocode.py` also writes `plan_to_code/executable_plan.py` back into each original log folder

PDDLRun and the LaMMA-P, SMART-LLM, Scale-Plan, KGLAMP, and COT converters
always check the assigned robot's skills before generating code. Explicit
`PickupObject` actions also require the object's mass to be no greater than
the robot's `mass_capacity`. Only the first validation failure is recorded
for each task, checking skills before mass on the same action. Missing metadata
is filled from the recorded robot identity or scene cache where possible;
unresolved or invalid required metadata fails generation. Failed validation removes any previous
generated `executable_plan.py` for that task; dry runs do not write or remove files.

`plan_to_code_summary.json` includes `mass_failed_generations` and
`skill_failed_generations` (task counts). `plan_to_code_results.json` records
`failure_reason` and `validation_error` with the failing stage, robot, and
zero-based action index. Reasons are `mass_exceeded`, `missing_skill`, or
`validation_data_missing`; the last contributes only to the overall failure count.

To skip Python compilation validation (skill and mass checks remain enabled):

```bash
python scripts/plantocode.py --logs-dir ./logs/task_manager_runs --no-validate-code
```

### 4. Execute a Generated Plan in AI2-THOR

After `plantocode.py` writes a task's `plan_to_code/executable_plan.py`, execute its
directory with the compatibility entry point:

```bash
python scripts/execute_plan.py \
  --command logs/path/to/task-run \
  --movement-mode step --execution-policy legacy --reachable-refresh-mode full
```

`--command` also accepts a directory name directly below `./logs`. The entry point
verifies and prefers the shared-runtime executable, forwards runtime options, and
returns the child's exit code. For an old directory with only `log.txt` and
`code_plan.py`, it assembles the historical executable only when the log records a
positive floor number, nonempty robot metadata, nonempty goals, and nonnegative
transition metrics. Missing context fails with a request to run `scripts/plantocode.py`;
no default scene, robot, goal list, or transition count is invented.

Current defaults are `step` movement, `legacy` execution policy, and `full` reachable
refresh. The movement environment variable `LAMMAP_MOVEMENT_MODE` is consulted only
when no movement option is supplied.

### 5. Versioned Executor Results and Isolated Runtime Media

`scripts/executor_system/parallel_runner.py` gives every generated executable
attempt a run identity and preserves its result independently.  The default
execution budget is 120 seconds for `step` movement and 30 seconds for
`teleport`; `--timeout-seconds` changes only that execution budget.  The parent
also allows 60 seconds for startup and 10 seconds for finalization, then spends
at most 5 seconds terminating only the child process group that it created.

Schema-v2 results use `metrics_schema_version: 2` and
`evaluation_version: "fixed_goals_v2"`.  They report parent-confirmed
`process_status`, runtime `execution_status` and `evaluation_status`, fixed
goal counts, `task_success`, raw action success rate and action counts, phase
durations, worker/cleanup errors, and `run_id`/`task_key`/`attempt`.  The older
compatibility fields (`status`, `action_sr`, `executed_actions`, and
`failed_actions`) remain available, but are not task-success metrics.  Existing
unversioned records remain `legacy_v1`; they are never rewritten to claim v2.

Attempts are stored under:

```text
<output-dir>/runs/<run-id>/<task-key>/attempt_<N>/
  child_metrics.json  stdout.log  stderr.log  result.json
  lammap-runtime-<unique>/
    agent_*/  top_view/  video_*.mp4  metadata.txt # when media is enabled
```

An explicit `ThorRuntime(output_root=...)` treats the supplied path as a
container and creates a fresh `lammap-runtime-<unique>` child; `runtime.output_root`
and media API paths point to that child. A standalone generated script uses a
distinct temporary directory keyed by the same run identity and attempt. The
runtime rejects its source directory and source-tree ancestors as output
containers. Cleanup removes only the runtime's own media-shaped children and
never cleans other runs or performs machine-wide GPU/process cleanup.

To rebuild a daily summary from completed attempt records without executing
plans again:

```bash
python scripts/executor_system/parallel_runner.py \
  --rebuild-summary ./parallel_runner_results/runs/<run-id>
```

Rebuilding reads durable `result.json` files, selects the latest valid
parent-completed attempt for each task, and writes a new top-level summary.

## Citation

If you find this work useful for your research, please consider citing:

```bibtex
@inproceedings{zhang2025lamma,
  title={LaMMA-P: Generalizable Multi-Agent Long-Horizon Task Allocation and Planning with LM-Driven PDDL Planner},
  author={Zhang, Xiaopan and Qin, Hao and Wang, Fuquan and Dong, Yue and Li, Jiachen},
  booktitle={2025 IEEE International Conference on Robotics and Automation (ICRA)},
  year={2025},
  organization={IEEE}
}
```

## Acknowledgement

We sincerely thank the researchers and developers for [SMART-LLM](https://github.com/SMARTlab-Purdue/SMART-LLM), [AI2THOR](https://github.com/allenai/ai2thor), and [Fast Downward](https://github.com/aibasel/downward/) for their amazing work.

执行器第二批支持 `--execution-policy legacy|strict`（默认 legacy）、条件准入和共享资源租约，结果记录实际策略与 `scheduler_version=2`。用法、资源表、可复现语义示例及验收状态见 [第二批执行语义说明](docs/executor_batch_2.md)。

执行器第三批将规范计划类型放在 `executor_system.plan_types`，并以固定
`ActionRegistry` 统一校验、资源和分派；控制器、对象解析/交互、输出、指标与可达图
缓存均为显式服务。旧 `action_plan` 导入保持对象身份兼容，生产路径显式传递 runtime
和 action context。当前协议为 `metrics_schema_version=2`、
`evaluation_version=fixed_goals_v2`、`scheduler_version=2`。`event` 刷新仍是可选项；
真实 smoke 的正确性对照匹配，但性能门槛失败，因此默认仍为 `full`，最终 240+24 次
真实验收尚待执行。动作表、诊断/恢复命令、三批验证入口和精选证据见
[第三批执行器交付说明](reports/executor_batch_3/README.md)。历史设计文档只作为背景，
当前默认值以运行时配置模块和 CLI 为准。
