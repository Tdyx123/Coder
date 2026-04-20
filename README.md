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

When multiple keys are configured for one provider, LaMMA-P rotates keys in round-robin order per request. If one key hits rate limits or another retryable upstream error, the same request will immediately try the next configured key.

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
- `--no-log-results`: disable per-task result logging
- `--bddl-file`: run from a BDDL file instead of a dataset floor plan

What the script does:

1. Decompose the high-level task into subtasks
2. Generate PDDL problem files for each subtask
3. Run Fast Downward on each subtask
4. Merge the subtask plans into a final combined plan
5. Save logs and artifacts for later plan-to-code conversion

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
- `--disable-log-results`

If `--output-root` is not provided, summaries are written to:

```text
parallel_runs/pddlrun_llmseparate_<timestamp>/
```

### 3. Convert Generated Plans into Executable AI2-THOR Code

After planning, convert generated plans into executable Python code:

```bash
python scripts/plantocode.py --logs-dir ./logs/task_manager_runs --validate-code
```

Important notes:

- `--input-source` defaults to `pddl_logs`
- `--logs-dir` defaults to `./logs`
- `--output-dir` defaults to `./plan_to_code_results`
- `--validate-code` is enabled by default
- `plantocode.py` also writes a `code_plan.py` file back into each original log folder for `execute_plan.py`

To skip validation:

```bash
python scripts/plantocode.py --logs-dir ./logs/task_manager_runs --no-validate-code
```

### 4. Execute a Generated Plan in AI2-THOR

Once `code_plan.py` exists in a specific log folder, execute it with:

```bash
python scripts/execute_plan.py --command <log_folder_name>
```

`<log_folder_name>` should be the folder name directly under `logs/`, because `execute_plan.py` resolves the target as:

```text
logs/<log_folder_name>/
```

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
