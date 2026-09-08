# Executor batch 3: architecture and reachable-map evidence

Batch 3 centralizes action validation, resource resolution, and dispatch; extracts
runtime services behind `ThorRuntime`; replaces production global runtime mutation
with explicit runtime/action context; adds bounded runtime metrics; and adds the
optional event-driven reachable-map refresh mode. The compatibility defaults remain
`step`, `legacy`, and `full`.

## Acceptance status

Implementation verification and the final real workload used clean code commit
`939be0ba48b9b9d058cc64eb036f9639ddda3ff0` (`code_dirty=false`). The implementation
suites passed, but real AI2-THOR acceptance **failed**. `event` remains opt-in and
`full` remains the default.

The final step comparison completed all 240 requested runs with 220 valid evaluations,
20 known case-03 mass-invalid evaluations, and no timeout, missing result, or unstarted
run. Each policy/mode group contains 60 distinct runs:

| Policy | Metric | full | event | Result |
|---|---:|---:|---:|---|
| legacy | valid evaluations | 55 | 55 | equal |
| legacy | mean GCR | 0.704545 | 0.698485 | failed |
| legacy | navigation completion | 0.971429 | 0.977143 | event higher |
| legacy | execution P50 (s) | 2.753241 | 3.918104 | failed 1.05 gate |
| legacy | execution P95 (s) | 6.367630 | 6.650296 | passed 1.05 gate |
| strict | valid evaluations | 55 | 55 | equal |
| strict | mean GCR | 0.643939 | 0.643939 | equal |
| strict | navigation completion | 0.965517 | 0.979592 | event higher |
| strict | execution P50 (s) | 2.323457 | 3.263614 | failed 1.05 gate |
| strict | execution P95 (s) | 6.314382 | 6.944234 | failed 1.05 gate |

The legacy GCR failure is the case-06 repetition-05 pair: event put Newspaper in
`Drawer_2` while the fixed goal requires `Drawer_1`, producing GCR 1/3 versus full
2/3. The generic plan target is `Drawer`; all other nine legacy case-06 rows selected
`Drawer_1`. Event repetition 05 reported 7/7 successful actions and 3/3 successful
navigations, so the first cause of the different trajectory remains unresolved. This
does not establish a deterministic event-cache defect, and the five old-code
`NO_PLAN_FOUND` replays do not waive the new paired GCR loss. See
[`development/final-case06-diagnosis.md`](development/final-case06-diagnosis.md).

The final teleport regression completed all 24 requested runs with 22 valid
evaluations, two known case-03 mass-invalid evaluations, and no timeout, missing
result, or unstarted run. Its nonzero check result contains only those baseline
process/evaluation failures. Across both final workloads, 242 of 264 runs were valid
and all 22 invalid runs have the same recorded cause: robot capacity `0.08` is below
Pot mass `0.57`.

The real reports do not establish the external collision, lease, deadlock, or
cancellation acceptance checks; those items remain unknown rather than passed.
Optional `max_workers=1/2/4` throughput, memory, and GPU measurements were not run.
The retained cancellation ruling keeps checks both before the controller lock and
immediately before submission inside it. The known nonblocking P3 remains:
`typing.get_type_hints` on canonical plan types cannot resolve the `WorldState`
forward annotation; no repository consumer was found and ordinary execution is
unaffected.

Earlier evidence remains relevant: the immutable 120-run pre-optimization baseline
had 110 valid and ten case-03 mass-invalid evaluations, and the corrected 24-run smoke
had 22 valid and two mass-invalid evaluations while failing the event P50 gate.

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
review. Implementation reports, review rulings, focused logs, and the exact final
verification log are in [`development/README.md`](development/README.md). The raw run
directories are intentionally ignored and remain available in the original workspace.
