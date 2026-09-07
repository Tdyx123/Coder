# Task 1 review

Reviewed baseline `8f95c7ca` through head `f25f0ec1`, the task brief, the complete supplied diff, and implementation report. Review was read-only except for this report. Existing passing suites were not rerun.

**Spec verdict: Needs fixes. Quality verdict: Needs fixes.**

The pure decision table, default legacy behavior, strict failure choices, Teleport-only retry limit, executor policy propagation, and standalone one-way parent/child cancellation meet the Task 1 scope. Later scheduler, condition, stage-result aggregation, and resource work were not treated as missing Task 1 deliverables.

## Actionable finding

**[P1 / Important] Propagate stage cancellation to the actual controller-call boundary.**

- Changed location: `scripts/executor_system/action_plan.py:1118`; same issue at `scripts/executor_system/parallel_runner.py:499`.
- Relevant existing boundary: `scripts/executor_system/runtime.py:656-661`.
- Both stage runners now pass a child control to executors, while `ThorRuntime._step_direct()` still obtains and checks only `runtime.execution_control`, the task root. On strict `FAIL_STAGE`, the child is cancelled and the root correctly remains active. A sibling already inside a composite high-level action can therefore issue further controller calls after stage cancellation, until that action returns to the executor's next child-control check. This violates the binding cancellation contract and can also prevent workers from quiescing within the shutdown budget.
- Focused in-memory reproduction used a two-robot strict `TaskRunner`: robot 2 executed a first real `ThorRuntime._step_direct` call, robot 1 then failed, robot 2 waited until the stage child reported cancelled, and attempted another `_step_direct` call. The controller accepted both calls. Output was `['FirstSubstep', 'SubstepAfterStageCancellation']`, with `stage cancelled: True` and `task cancelled: False`; the task ultimately raised `StageFailureDecisionError`.
- Fix by making the worker's stage control available to the runtime/controller boundary, for example through a thread-local execution-control scope, and checking it both before and after acquiring the controller lock. Keep actual infrastructure failures task-scoped and preserve one-way child cancellation. Add a deterministic regression showing that a sibling composite action cannot submit another controller step after strict stage cancellation, while a subsequent stage child remains usable.

No other actionable Task 1 findings were identified. The reported 151 passing tests are accepted as existing verification evidence; the focused reproduction above exposes a cancellation case those tests do not cover.

## Scoped re-review: fix round 1

Reviewed only `f25f0ec1..317fb232`, the supplied fix diff, and appended RED/GREEN evidence.

**Prior finding: Addressed. Final Task 1 spec verdict: Pass. Quality verdict: Approved.**

`Executor.execute_action()` now supplies its stage control to the runtime action scope, which installs and restores that control in thread-local state. `ensure_control()` resolves the active scoped control, so the existing checks before and inside the controller lock now reject the cancelled sibling's subsequent substep. The controller exception path also explicitly cancels the task root, preserving task-level infrastructure failure semantics. Scope restoration and per-run scope initialization preserve one-way cancellation and next-stage reuse.

The added deterministic regression reproduces the exact reported failure before the fix and checks both blocked post-cancellation controller calls and successful controller access under a fresh stage child afterward. Accepted the implementation report's 101 passing focused/regression tests and clean syntax/diff checks; no suites were rerun. No new actionable regressions were identified in this scoped fix review.
