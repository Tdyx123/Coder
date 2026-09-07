# Executor batch 3: architecture and reachable-map evidence

Batch 3 centralizes action validation, resource resolution, and dispatch; extracts
runtime services behind `ThorRuntime`; replaces production global runtime mutation
with explicit runtime/action context; adds bounded runtime metrics; and adds the
optional event-driven reachable-map refresh mode. The compatibility defaults remain
`step`, `legacy`, and `full`.

## Acceptance status

Implementation tests are collected by `verify.sh`. Real AI2-THOR acceptance is not
complete and must not be reported as passed:

- The immutable 120-run pre-optimization full baseline at `e82e20f5` produced 110
  valid evaluations and 10 invalid evaluations. All ten are case 03, where robot
  capacity `0.08` is below Pot mass `0.57`. The authoritative gate recheck failed.
- The corrected 24-run full/event smoke at `b3aa308f` produced 22 valid evaluations,
  the same two known mass-invalid runs, no timeouts, and no paired action, GCR, or
  navigation regression.
- That smoke did not meet the performance gate: event execution P50 was 3.673028 s
  versus full 2.664341 s, above the allowed 1.05 ratio. No speedup is claimed and
  `full` remains the default.
- The required final 240 step comparisons and 24 teleport regressions are pending.
- The case 06 `NO_PLAN_FOUND` diagnostic also reproduces five times on the old batch
  2 code in the same environment. It is diagnostic evidence, not an acceptance run,
  and no speculative navigation change was made.

## Public boundaries

| Boundary | Canonical implementation | Responsibility |
|---|---|---|
| Plans and results | `executor_system.plan_types` | Side-effect-free `Action`, `TaskPlan`, state and result types |
| Action contract | `executor_system.action_registry` | Fixed `ActionSpec` table, normalization, admission resources and dispatch |
| Controller | `executor_system.controller_client` | Serialized THOR submission, cancellation and version publication |
| Objects | `executor_system.object_resolver`, `object_interactor` | Deterministic binding and explicit interactions/recovery |
| Artifacts and metrics | `runtime_artifacts`, `runtime_metrics` | Owned output paths and bounded counters/timers |
| Reachability | `reachable_map` | Full/event map evidence, topology invalidation and occupancy |
| Compatibility facade | `action_plan`, `runtime` | Stable old imports and `ThorRuntime` public methods |

The package root re-exports the canonical plan types and extracted services. Existing
imports such as `from executor_system.action_plan import Action, TaskPlan` retain the
same object identities. Production code passes a runtime and action context explicitly;
`executor_system.context.runtime_scope` remains a `ContextVar` compatibility adapter
for helper callers and supports independent contexts in one process.

## Registered actions

| Form | Actions |
|---|---|
| helper, 0 targets | `WaitOneTick`, `ThrowObject`, `WaitUntil` |
| helper, 1 target | `GoToObject`, `PickupObject`, `TeleportObjectToHand`, `SwitchOn`, `SwitchOff`, `OpenObject`, `CloseObject`, `BreakObject`, `BreakEgg`, `SliceObject`, `CleanObject`, `DirtyObject`, `EmptyLiquid` |
| helper, 2 targets | `PutObject`, `PrepareEgg`, `RunMicrowave`, `RunCoffeeMachine`, `RunToaster`, `HeatByStoveBurner`, `FireByStoveBurner`, `FillWater`, `ColdObject` |
| helper, 3 targets | `CookByStoveBurner` |
| direct THOR | `MoveAhead`, `RotateLeft`, `RotateRight`, `LookUp`, `LookDown`, `Pass`, `Wait`, `Done`, `Teleport`, `ToggleObjectOn`, `ToggleObjectOff`; explicit direct forms are also registered for supported object actions |

New actions are added as fixed `ActionSpec` entries with an executor and the existing
resource resolver. Shape validation, scene/capability admission, resource preparation,
and execution all use that entry. The registry does not evaluate generated code or use
arbitrary `getattr` dispatch.

## Versions and defaults

Defaults come from the runtime CLI and configuration modules:

- `MovementConfig.resolve`: explicit movement mode, then `LAMMAP_MOVEMENT_MODE`, then
  `step`; 0.25 m grid and 0.35 m hard clearance.
- `generated_plan_runtime` and `parallel_runner`: execution policy `legacy` and
  reachable refresh `full`; `event` is opt-in.
- Result protocol: `metrics_schema_version=2`, `evaluation_version=fixed_goals_v2`,
  and `scheduler_version=2` for current generated runs.

`event` retains static reachability evidence and refreshes after relevant world changes
or its bounded full-refresh interval. `full` performs the compatible all-agent refresh
and is the recovery switch when event diagnostics or performance are unacceptable.

## Verification and diagnostics

Run the three batch suites and batch 3 focused checks:

```bash
bash reports/executor_batch_3/verify.sh
```

Run one generated task directory through the compatibility entry point:

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/execute_plan.py \
  --command logs/path/to/task-run \
  --movement-mode step --execution-policy legacy --reachable-refresh-mode full
```

The entry point first verifies and runs `plan_to_code/executable_plan.py` (or a
verified generated `executable_plan.py`) and returns its exit code. A legacy `log.txt`
and `code_plan.py` are concatenated only when the log contains a positive `floor_no`,
nonempty `robots`, nonempty `ground_truth`, and nonnegative `no_trans_gt`/`max_trans`.
Missing context exits nonzero and asks the operator to run `scripts/plantocode.py`; no
FloorPlan1, robot, goal, or transition default is synthesized.

Rebuild a summary from durable attempts after an interrupted parent process:

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/executor_system/parallel_runner.py \
  --rebuild-summary coderun_results/runs/<run-id>
```

Inspect stage `diagnostics` plus top-level `runtime_metrics`, `navigation_metrics`,
`worker_errors`, and `cleanup_errors` in `result.json` before retrying. Re-run with
`--reachable-refresh-mode full` to isolate event-refresh behavior. Use
`scripts/benchmark_executor_regression.py --check` for paired correctness/performance
gates; a nonzero result is evidence of a failed gate and must be preserved.

The curated artifact catalog and integrity hashes are in
[`evidence/README.md`](evidence/README.md). The `.xz` files are lossless copies of the
original reports; the adjacent readable reports retain only the evidence needed for
review. The raw run directories are intentionally ignored and remain available in the
original workspace.
