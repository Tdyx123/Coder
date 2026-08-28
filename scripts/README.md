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
- `--problem-repair`
- `--no-problem-repair`

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
- `--problem-repair`
- `--no-problem-repair`

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

# Reproduce the legacy conservative 277/447 conversion baseline.
python scripts/baselines/SMART-LLM.py \
  --root ./baselines/SMART-LLM \
  --recovery-mode conservative

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
- SMART-LLM only: `--recovery-mode aggressive|conservative`

Behavior worth knowing:

- `--validate-code` is enabled by default
- SMART-LLM defaults to deterministic `aggressive` free-format recovery after
  conservative conversion fails; use `--recovery-mode conservative` for the
  legacy conversion contract
- SMART-LLM result rows identify direct versus recovered output through
  `conversion_kind` and record recovery confidence, rules, and line-level events
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
  --movement-mode step \
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
- `--movement-mode` accepts `teleport` or `step`; when omitted, the runner uses
  `LAMMAP_MOVEMENT_MODE`, then defaults to `step`
- default subprocess timeouts are 30 seconds for `teleport` and 120 seconds for
  `step`; an explicit `--timeout-seconds` overrides either default
- timed-out tasks are retried up to two times after each full round completes
- each completed round cleans up matching GPU processes before the next retry round
- summary metrics are written to a dated JSON filename, such as `0628_01.json`
  or `LaMMA-P_0628_01.json`
- `stdout` is kept only for results with `robot_failures` by default; pass
  `--save-all-stdout` to keep it for every result
- the summary records the effective movement mode, timeout, and each generated
  runtime's `navigation_metrics`

### Pluggable `GoToObject` movement

Generated plans keep the same `GoToObject(robot, object)` action format. The
runtime selects one of two movement plugins:

- `teleport` retains candidate teleport plus target-facing behavior
- `step` is the default and converts current THOR reachable positions to the
  0.25 m grid, jointly plans same-wave robot requests through
  `multi_robot_avoidance.py`, and submits real rotate/`MoveAhead` microsteps
  with 0.35 m clearance, dynamic replanning, failed-edge recovery, visibility
  confirmation, and completed-robot parking

Select the movement mode explicitly for a generated executable or for the
parallel runner:

```bash
python path/to/plan_to_code/executable_plan.py --movement-mode teleport

python scripts/executor_system/parallel_runner.py \
  --root logs/intermediate_runs \
  --movement-mode step \
  --output-dir ./parallel_runner_results
```

`--movement-mode` has priority over `LAMMAP_MOVEMENT_MODE`; the environment
variable has priority over the `step` default. Step navigation never falls
back to `Teleport`. Initialization teleports and explicit plan `Teleport`
actions remain available but are outside the navigation action metrics scope.

Select the deterministic 12-case manifest, then run and validate both modes:

```bash
python scripts/benchmark_movement_modes.py \
  --select-manifest \
  --root logs/intermediate_runs \
  --manifest tests/fixtures/movement_benchmark_plans.json

python scripts/benchmark_movement_modes.py \
  --manifest tests/fixtures/movement_benchmark_plans.json \
  --output-json reports/movement_modes_benchmark.json \
  --output-md reports/movement_modes_benchmark.md \
  --check
```

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

### `multi_robot_avoidance.py`

Standalone deterministic L0/L1 prototype for multi-robot endpoint assignment,
space-time path reservations, and failure injection. It does not import
AI2-THOR or `executor_system`, and it is not wired into generated plan
execution.

Run the built-in crossing demo:

```bash
python scripts/multi_robot_avoidance.py --demo crossing --pretty
```

Other demos are `joint-assignment`, `failure`, and `stale-world`. The failure
and stale-world demos intentionally exit with status 4 after emitting their
JSON result. Use `--plan-only` to skip FakeRuntime execution.

Run a JSON scenario:

```bash
python scripts/multi_robot_avoidance.py \
  --scenario-file /path/to/scenario.json \
  --pretty
```

Minimal input shape:

```json
{
  "grid_size_m": 0.25,
  "hard_clearance_m": 0.35,
  "max_ticks": 64,
  "max_assignment_trials": 256,
  "walkable": [[0, 0], [1, 0], [2, 0], [0, 2], [1, 2], [2, 2]],
  "conflicts": [],
  "blocked_transitions": [[[0, 0], [1, 0]]],
  "robots": [
    {
      "id": "A",
      "start": [0, 0],
      "candidates": [{"id": "A1", "position": [2, 0], "cost": 1.0}]
    },
    {
      "id": "B",
      "start": [0, 2],
      "candidates": [{"id": "B1", "position": [2, 2], "cost": 1.0}]
    }
  ],
  "execution": {
    "failure_at_micro_step": null,
    "external_version_bump_before_micro_step": null,
    "walkable_events": []
  }
}
```

`robots` accepts one to four entries. `blocked_transitions` is optional and
contains directed `[source, target]` grid pairs that the planner must not
traverse in this planning batch. The reverse direction remains available
unless it is listed separately. Both endpoints must be distinct members of
`walkable`; wait transitions cannot be blocked.

`walkable_events` enables strict dynamic-walkable execution in `FakeRuntime`.
The first event must be a full refresh at committed step 0. Every successful
robot movement must then have exactly one matching `robot_step` event. An
optional `active_refresh` may follow the robot event at the same boundary:

```json
{
  "execution": {
    "walkable_events": [
      {
        "at_committed_step": 0,
        "type": "active_refresh",
        "reachable_positions_by_robot": {
          "A": [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 0.25, "y": 0.0, "z": 0.0},
            {"x": 0.5, "y": 0.0, "z": 0.0}
          ],
          "B": [
            {"x": 0.0, "y": 0.0, "z": 0.5},
            {"x": 0.25, "y": 0.0, "z": 0.5},
            {"x": 0.5, "y": 0.0, "z": 0.5}
          ]
        }
      },
      {
        "at_committed_step": 1,
        "type": "robot_step",
        "robot_id": "A",
        "reachable_positions": [
          {"x": 0.0, "y": 0.0, "z": 0.0},
          {"x": 0.25, "y": 0.0, "z": 0.0},
          {"x": 0.5, "y": 0.0, "z": 0.0}
        ]
      },
      {
        "at_committed_step": 1,
        "type": "active_refresh",
        "reachable_positions_by_robot": {
          "A": [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 0.25, "y": 0.0, "z": 0.0},
            {"x": 0.5, "y": 0.0, "z": 0.0}
          ],
          "B": [
            {"x": 0.0, "y": 0.0, "z": 0.5},
            {"x": 0.25, "y": 0.0, "z": 0.5},
            {"x": 0.5, "y": 0.0, "z": 0.5}
          ]
        }
      }
    ]
  }
}
```

Each robot's newest snapshot replaces its previous snapshot. Global
`walkable` is the union of those newest snapshots, not an accumulated history.
Coordinates are converted to integer grid keys with
`round(coordinate / grid_size_m)`; `y` is ignored. Same-boundary updates are
atomic. Missing updates, malformed snapshots, or a union that omits a current
robot position stop execution with `INVALID_WALKABLE_UPDATE`.
Event step numbers always refer to global successful commits, including commits
made by a replacement plan after replanning.

When an update removes a remaining path point or assigned endpoint,
`FakeRuntime` releases the old reservations and replans from the current world
state. Failure to find a replacement plan returns `REPLAN_FAILED`. Execution
JSON includes the final `walkable`, `walkable_version`, and `replan_count`.

Process exit codes:

- `0`: `PLANNED` or `EXECUTED`
- `2`: `INVALID_SCENARIO`
- `3`: `NO_PLAN_FOUND` or `SEARCH_LIMIT_REACHED`
- `4`: `COMMIT_REJECTED_STALE_WORLD`, `EXECUTION_FAILED`,
  `INVALID_WALKABLE_UPDATE`, or `REPLAN_FAILED`

AI2-THOR alignment and limitations:

- The defaults mirror this repository's 0.25 m navigation step and 0.35 m
  center-clearance policy. The latter is an application constraint, not a
  guarantee from Unity's collision geometry.
- The current runtime uses `snapToGrid=False` and rounds world coordinates to
  0.25 m grid keys. The static scenario uses integer grid keys, while dynamic
  reachable-position events accept AI2-THOR-style `{x, y, z}` objects. This
  prototype still does not model floating-point drift or real colliders.
- Scenarios accept one to four robots. Directed `blocked_transitions` are
  batch-local evidence about failed movement edges; they are not persisted
  between independent scenario loads.
- `GetReachablePositions` alone does not prove that an object is interactable
  from a point. The runtime adapter therefore rotates toward the target and
  verifies visibility after reaching an assigned candidate; held-object
  collider geometry is still delegated to THOR action results.
- `GoToObject` uses the selected movement plugin. Teleport mode reserves its
  endpoint candidate; step mode uses time-indexed paths and serial microsteps.
- The world version is synthetic. A real adapter would have to increment it
  after every relevant simulator state change.
- Prioritized space-time A* is intentionally bounded and incomplete. A
  `NO_PLAN_FOUND` result means this planner found no plan; it is not a proof
  that the MAPF instance is unsatisfiable.
- Action-wave deadlock handling, completed-robot parking, rotations, and
  dynamic recovery are runtime concerns implemented outside this pure planner;
  detailed held-object footprints remain a THOR-level limitation.

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

problem_repair:
  enabled: false
```

The optional Problem repair pass is disabled by default. It uses only generated
Problem text and the corresponding in-memory Domain text; it does not invoke a
planner, VAL, subprocess, or another LLM call.
