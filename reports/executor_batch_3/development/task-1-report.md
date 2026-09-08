# Task 1 report — action registry, validation, and dispatch

Status: implemented, self-reviewed, verified, and committed.
Commit: `760a6071` — Unify executor action registration validation and dispatch.
Worktree: `/tmp/coder-executor-batch-3`; branch `codex/executor-batch-3`.
Base / completed batch-2 handoff: `b1a679ec3cdc924eb176887e9b4b916629bb3eaa`.
Completed batch-1 acceptance: `8f95c7ca092d7025a5ca67def5015daa56f6c568`.
Only Task 1's seven checkboxes were changed in the batch-3 plan. Task 2/3 were not implemented. No subagents or new worktrees were created.

## Implementation

- Added fixed `ActionSpec` registrations for every supported helper and THOR action, plus `NormalizedAction`, `PreparedAction`, and `ActionRegistry` APIs. Registration controls helper arity, direct required fields, helper execution callables, positional resource demands, recovery reservations, and compound resource roles. No arbitrary helper lookup through getattr/globals remains in the adapter.
- The registry distinguishes explicit helper args from direct payloads, checks exact arities including zero-argument helpers, retains Egg restrictions, rejects conflicting targets, unknown actions, invalid object IDs, cross-agent overrides, and nonfinite movement/rotation/horizon values. Direct objectId must name an actual snapshot instance; helper names still use existing deterministic aliases/fallbacks.
- `AI2ThorAdapter.execute` retains its public signature and delegates dispatch to the registry; following action, coordinator, world state, and action wave are forwarded. Legacy public direct/Put/generated-helper methods remain compatibility delegates.
- `resolve_action_resources` delegates through registry preparation, which calls the registered low-level resolver directly (no recursion). Removed the duplicated action resource and navigation recovery sets, and made old symbolic ResourceInferencer queries derive from registry registration. Direct Put validates snapshot holding and leases its held object; direct Pickup uses the same mass and skill checks as helpers.
- `PlanValidator` now contains real implementation: pure structure validation before generated entrypoints create ThorRuntime/controller, then scene validation before any task action. Errors include stage/robot/cursor. Scene-only resource resolution uses the same exact resolver as admission, preserving Sink→SinkBasin fallback, stove knob selection, aliases, and implicit resources while postponing state-dependent holding/ownership checks until admission. A future Pickup→Put sequence remains valid.
- Added pure shared skill normalization, finite mass parsing, and first-failure capability rules. Generation retains its object mass lookup, wrappers, contextual details, and artifact deletion behavior. Runtime reads explicit Pickup mass from the admission snapshot, and existing resource scope's before-step boundary rechecks actual nested/recovery Pickup skill/mass from current coherent object evidence before effects start.
- Updated only fake metadata/state needed to satisfy the new real contracts: complete robot capabilities/mass capacity, object masses, scene navigation targets, snapshot robot positions, valid Teleport vectors, and valid plans at mocked metrics boundaries. The clipping accounting mock publishes held evidence for its following Put; original retry/concurrency/accounting assertions remain.

## Files

Production: `scripts/executor_system/action_registry.py`, `capability_checks.py`, `plan_validator.py`, `action_plan.py`, `action_resources.py`, `generated_plan_runtime.py`; `scripts/baseline_converters/generation_validation.py`.
New tests: `tests/test_action_registry.py` (20 tests, with subtests for all helper/direct forms and invalid shapes).
Related fake/test fixtures: `tests/snapshot_fakes.py`, `test_action_resource_leases.py`, `test_execution_policy.py`, `test_execution_policy_cli.py`, `test_executor_retry_policy.py`, `test_final_reliability_fixes.py`, `test_navigation_batch_failures.py`, `test_parallel_runner.py`, `test_pddlrun_executor_adapter.py`, `test_stage_conditions.py`, `test_stage_scheduler.py`, `test_world_snapshot.py`.
Plan: `docs/superpowers/plans/2026-09-07-executor-batch-3-architecture-performance.md` (Task 1 checkboxes only).

## TDD / verification evidence

All commands ran from the worktree root with `/home/dwb/.pyenv/bin/pyenv exec python`.

1. Initial RED:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_plan_contract.py`
   Output: `Ran 11 tests ... FAILED (errors=1)`. The 10 existing plan-contract tests passed; the new test module failed importing the absent `executor_system.action_registry`. This initial RED is an expected missing-public-API import error, not an assertion failure. Log: `task-1-logs/red.log`.
2. Initial GREEN after implementation: same command, `Ran 24 tests ... OK` (`green-initial.log`).
3. Refinement RED:
   `... python -m unittest tests.test_action_registry.ActionRegistryTest.test_registry_can_be_imported_before_action_plan tests.test_action_registry.ActionRegistryTest.test_teleport_angles_are_finite`
   `Ran 2 tests ... FAILED (failures=3)`: fresh-import circular initialization and both nonfinite rotation/horizon checks failed. Fixed lazy fixed helper references and numeric validation; these now pass in final focused suite (`red-refinement.log`).
4. Target-admission RED:
   `... python -m unittest tests.test_action_registry.ActionRegistryTest.test_direct_object_id_is_a_concrete_snapshot_id tests.test_action_registry.ActionRegistryTest.test_navigation_target_missing_at_admission_fails_without_step`
   `Ran 2 tests ... FAILED (failures=2)`: direct alias IDs and disappeared navigation targets were accepted. Fixed exact direct ID validation and navigation target existence without adding a navigation destination object lease (`red-targets.log`).
5. Existing resource regressions exposed the naive separate scene target lookup breaking the already-supported `FillWater('Sink', ...)` basin fallback. Scene validation now invokes the registered resolver in scene-only mode. Registry + resource lease suite: `Ran 50 tests in 0.285s ... OK` (`resource.log`, before final two target tests).
6. Final required focused suite:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_plan_contract.py tests/test_generation_validation.py tests/test_pddlrun_executor_adapter.py tests/test_executor_retry_policy.py tests/test_parallel_runner.py`
   `Ran 220 tests in 3.930s ... OK` (`focused.log`).
7. Final prior-batch regression:
   `bash reports/executor_batch_2/verify.sh`
   `Ran 581 tests in 18.827s ... OK` (`baseline-final.log`).
8. Self-review found newly added metrics-fixture imports depended on earlier modules initializing sys.path. Isolated `... python -m unittest tests/test_final_reliability_fixes.py` reproduced missing `executor_system` import; fixed import ordering. Isolated `... python -m unittest tests/test_final_reliability_fixes.py tests/test_execution_policy_cli.py`: `Ran 14 tests in 0.095s ... OK` (`isolated-fixture*.log`).
9. `... python -m py_compile` on all seven changed/new production files and `tests/test_action_registry.py`: exit 0. `git diff --check`: exit 0.

## Concerns and limits

- Two intermediate 581-test runs (18.534s and 18.664s) failed only the existing `test_parent_exit_does_not_leave_term_ignoring_descendant` SIGKILL-event assertion. The fixture launches a child and exits without waiting for the child to install SIGTERM-ignore; a child receiving TERM first can exit without KILL. Its isolated rerun passed (`Ran 1 test in 0.222s ... OK`, `supervisor-rerun.log`), and the final full 581 run passed. No process-supervisor production/test changes were made. The second failing run is retained in `baseline.log`.
- An exploratory full discovery ran 1092 tests and failed on both then-incomplete executor fixtures and unavailable live/data integration dependencies. This is not the accepted regression command; full output is retained in `full.log`. The intended 581-test offline baseline and all requested Task 1 tests are green. No Unity/live performance experiments were claimed.
- Explicit action_context services and canonical type moves belong to Tasks 2/3. This task uses a dictionary for registry execution context and keeps existing compatibility runtime scopes for generated helpers. Existing resource admission scopes are used to preserve bindings and nested Pickup checks. No new implicit global runtime fallback was added.

Git staging initially hit read-only shared `.git/worktrees` metadata; the authorized escalation succeeded and the task commit was created. Working tree is clean. Report/log artifacts remain in the pre-existing ignored `.superpowers/sdd` handoff directory.


## Review round 1 resolution

Base: `760a60710b94a286643f8d1c2c5e7de66703d4e6`. Both important findings in `task-1-review.md` were verified against real production paths, then covered with failing tests before fixes. Follow-up commit: `5c397b34` — Enforce recovery capabilities and preserve flat action payloads.

1. **Implicit recovery Close/Open skills:** `retry_move_past_open_object_blocker` now preflights both CloseObject and restoration OpenObject using the shared capability checker before its first Close submission. A robot with only GoToObject, with GoToObject+CloseObject, or with GoToObject+OpenObject leaves the leased blocker open and submits no recovery action. The existing real scheduler/controller recovery test still verifies successful Close→Move→Open and complete lease release when capabilities are present. Checks are at the concrete recovery entrance; ordinary declared compound helper primitive steps do not gain blanket skill requirements. A regression confirms RunMicrowave-only capability still permits its ordinary primitive Open through the existing resource scope.
2. **Flat payload ingestion:** `Action.from_any` now copies the shared registry's supported direct payload fields before normalization. Rotation, horizon, throwMagnitude and standing survive flat dictionaries; forceAction and placeStationary are also retained. Tests go through public `Action.from_any({...})` and the adapter's actual payload submission, rejecting nonfinite flat rotation/horizon/throwMagnitude and preserving finite Teleport/ThrowObject values. A flat ThrowObject payload therefore retains its direct form rather than becoming an empty helper.
3. **Other actual implicit paths inspected, as requested by controller:**
   - `prepare_hand_for_goto_if_needed` / `prepare_hand_for_pickup` converge on `place_held_objects_for_pickup`, which navigates to a leased receptacle and invokes `put_held_object_in_receptacle`. Added GoToObject+PutObject preflight before the first navigation/placement effect; missing either skill produces no effects. Existing hand-container lease and candidate stability assertions remain green.
   - `retry_object_action_after_target_visibility_error` can reach `retry_slice_after_interaction_reposition`, which introduces a new GoToObject navigation before retrying Slice. Added GoToObject preflight before navigation, with missing-capability/no-effect and existing serialization/deadline/cleanup coverage.
   - `retry_pickup_after_clip_error` and `retry_pickup_after_target_visibility_error` retry the same Pickup payload; admitted actual Pickup still goes through existing `ActionResourceScope.before_step` skill/mass validation. Their camera/backoff movement remains existing navigation implementation; no primitive movement-skill requirements were imposed (robot catalog describes GoToObject, not each internal turn/look/microstep).
   - `handoff_held_object_direct` was inspected: only the old adapter forwarding method references it; no automatic production caller was found. Its compatibility path was not expanded in this review fix.

Production follow-up files: `action_registry.py`, `action_plan.py`, `runtime.py`. Test follow-up files: `test_action_registry.py`, `test_action_resource_leases.py`, `test_navigation_execution_scope.py`, `test_executor_retry_policy.py`. Recovery fake metadata was completed; no checks were bypassed and no unrelated supervisor code/test was changed.

### Review TDD evidence

- RED command: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_action_resource_leases.py tests/test_navigation_execution_scope.py`.
  Result: `Ran 64 tests in 0.646s ... FAILED (failures=11, errors=1)`. Assertion failures show missing recovery skills were accepted and flat fields were lost/nonfinite values bypassed validation; the one error was the dropped ThrowObject magnitude incorrectly routing to a missing fake helper (`review-red.log`).
- First affected run after production fixes exposed only incomplete legacy recovery fake robot metadata (`review-first-green.log`). Completed the positive-path fixtures' robot definitions/mapping, retaining their original concurrency, lock, cancellation, and retry assertions.
- GREEN affected command: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_action_resource_leases.py tests/test_navigation_execution_scope.py tests/test_executor_retry_policy.py`.
  Result: `Ran 115 tests in 0.654s ... OK` (`review-green.log`).
- Full baseline command: `bash reports/executor_batch_2/verify.sh`.
  Result: `Ran 583 tests in 18.541s ... OK` (`review-baseline.log`); two new non-registry recovery test methods increase the previous 581 count. No supervisor flake in this run.
- Original requested focused command: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_plan_contract.py tests/test_generation_validation.py tests/test_pddlrun_executor_adapter.py tests/test_executor_retry_policy.py tests/test_parallel_runner.py`.
  Result: `Ran 224 tests in 4.068s ... OK` (`review-focused.log`).
- `python -m py_compile` via explicit pyenv on all seven review-modified production/test files and `git diff --check`: exit 0.

Review fix commit created successfully; working tree clean. No unresolved review findings identified in self-review.
