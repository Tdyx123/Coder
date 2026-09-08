# Whole-branch final review

Reviewed base `b1a679ec` through head `46342de74d67a1b9373e6ed25fcda8d8d96d662e` against the self-contained architecture/performance plan, constraints and SDD ledger. Review was read-only except this requested report; no delegation or production edits. The large package was reviewed in passes, focusing on production changes and using archived reports/ledger for historical experiment evidence.

## Strengths

- The fixed registry joins shape validation, capability checks, resource resolution and explicit dispatch without arbitrary generated-name lookup. Scene checks use detached snapshot evidence, while nested pickups recheck capabilities at the controller boundary.
- The controller extraction preserves both cancellation checkpoints, serialized submission, committed state-version publication and failed-event notification. Event-cache query scopes defer scheduler notification until the outer controller lock is released.
- Object resolver extraction preserves the original selection algorithms; interaction implementations use an explicit runtime. ContextVar binding/reset and worker-entry binding remove the production global-runtime mutation, and compatibility recording validates a conservative AST subset before running a globals-isolated copy.
- Metrics remain bounded and describe overlapping categories. The benchmark preserves each outcome, rejects incomplete full baselines and dirty/unknown identities, compares policies separately and keeps full as the default. Documentation accurately states the existing failed/pending real acceptance evidence.

## Issues

### Critical

None found.

### Important

1. **[P2] Validate the generated module prefix before treating its exit path as verified.**
   - Location: `scripts/execute_plan.py:132-145`, especially line 133.
   - `_has_canonical_generated_bindings` checks only the interval after the shared-main import and before the main guard. Arbitrary executable statements before that import remain accepted. Thus a preferred generated file with `raise SystemExit(0)` before the canonical import is selected and reports success without executing any plan. Prefixing `__name__ = 'generated_plan'` similarly disables the guard while passing verification. This defeats the compatibility entrypoint's explicit rejection of generated no-ops and can shadow a usable fallback executable.
   - Minimal evidence: in a temporary directory, installed a local shared-runtime stub whose `main` prints `SHARED_MAIN_CALLED` and returns 23, and created the canonical literal bindings and `if __name__ == '__main__': raise SystemExit(main(...))` wrapper. For each prefix above, `verify_generated_runtime(path)` returned `None`; an actual Python subprocess returned **0** with **empty stdout**. No THOR dependency or simulation was used.
   - Fix the complete permitted module structure, including the prefix and control/exit-symbol bindings; a conservative allowlist matching the actual generated imports/path bootstrap is preferable to further isolated alias exceptions. Add end-to-end fallback-selection tests for these two prefix variants and preserve valid repository-generated wrappers.

### Minor

1. **[P3, previously deferred] Canonical plan annotations cannot resolve WorldState.**
   - Locations: `scripts/executor_system/plan_types.py:71`, `:161`, and `:245` (`MultiStageActionPlan.global_success_condition`).
   - Confirmed on this head: `typing.get_type_hints(Action)`, `typing.get_type_hints(StagePlan)` and `typing.get_type_hints(TaskPlan)` all raise `NameError: name 'WorldState' is not defined`. Ordinary construction/execution is unaffected and the earlier review found no repository annotation-introspection consumer, so this remains nonblocking. A resolvable pure protocol/type annotation would preserve the dependency boundary without importing runtime machinery.

## Deferred-item triage

- WorldState annotation: retain as P3 above, not an execution blocker.
- Prior process-supervisor SIGKILL fixture timing race: no changed supervisor implementation and exact-head suites pass; retain historical evidence, do not widen this branch to an unrelated timing repair.
- Expected alias warning and exploratory nonexistent-module invocation: diagnostic/log history, not current correctness findings. Keep them distinguished from final required verification.
- Prior event-cache outer-lock notification issue: fixed by `ControllerClient.world_read_scope`; no remaining instance established in the reviewed cache path.

## Verification and real acceptance

- Parent supplied fresh exact-head `bash reports/executor_batch_3/verify.sh`: **exit 0**, overlapping groups **462 / 588 / 317**, `/tmp/coder-batch3-verification-46342de7.log`. Parent also reports compileall success for the executor package and the two changed entrypoint/benchmark scripts. I did not rerun those suites.
- Independently ran only the two minimal subprocess no-op reproductions and the annotation-introspection check described above.
- Required final **240 step + 24 teleport** real runs remain **pending on final code**. This review does not convert test results or older smoke experiments into real acceptance.
- Historical full baseline at `e82e20f5`: 120 runs, 110 valid and ten case-03 mass failures (`0.57 > 0.08`). These are the specified strict capability rule exposing an immutable workload incompatibility, not evidence permitting relaxed validation or a reduced denominator. The corrected baseline gate failure is authoritative.
- Corrected 24-run smoke at `b3aa308f`: no paired action/GCR/navigation regression, but event execution P50 **3.673028 s** versus full **2.664341 s** fails the 1.05 threshold. It neither passes event performance nor covers final-head/strict/five-repeat acceptance.
- Case-06 navigation failure reproduced five times on old code in the same environment; that evidence argues against labeling it a new batch-3 regression, while leaving the physical/environment cause unresolved.

## Assessment

**Ready to merge? With fixes.** Address the P2 generated-entrypoint false-success gap before code approval. The architecture otherwise follows the intended boundaries and retains the cancellation/resource/default contracts; final real acceptance must still be completed and reported according to its actual outcome, with failed gates and immutable-workload conflicts preserved explicitly.
