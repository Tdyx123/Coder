# Task 3 delivery report

## Scope and commits

Worktree: `/tmp/coder-executor-batch-3`; branch: `codex/executor-batch-3`; base: `0f4975d0` (Task 2 reviewed endpoint).

1. `c4fac22d` — extract deterministic ObjectResolver, preserving runtime methods/signatures and per-runtime aliases/history.
2. `2a7e31e0` — extract ObjectInteractor, compound helpers, hand clearance and recovery; registry/resources call explicit services.
3. `80610422` — ContextVar runtime ownership, safe AST recording, entrypoint cleanup isolation, regression coverage and Task 3 checkboxes.

No Task 4/5 implementation, new worktrees/subagents, or main-worktree edits. No live Unity runs or performance claims. Git writes used the authorized sandbox escalation for this isolated branch's commits; no rejection occurred.

## Implementation

- ObjectResolver owns the original deterministic matching, alias registration/repair/transformation and operation-history implementation. Existing ThorRuntime method signatures delegate to the injected service. Runtime instances retain their own state/locks; lazy service construction supports ThorRuntime.__new__ fixtures and snapshot views.
- ObjectInteractor owns object actions, inventory/hand clearance, door/visibility/slice recovery and compound helpers. Its execute entry consumes PreparedAction and the existing action context; the registry still owns the fixed dispatch table, input normalization and resources. Resource admission uses an explicit snapshot-view interactor instead of binding global helper context.
- The original runtime configuration/log patch points remain available through an explicit settings provider, preserving forceAction and log regression behavior. Pure legacy action utility helpers remain callable without any runtime binding. Canonical error predicates reside in the interaction module and are re-exported by runtime.
- context.bind_runtime uses ContextVar tokens and resets them in finally. runtime_scope remains a compatibility alias. The real execution-worker and cleanup-worker targets bind runtime themselves; no thread inheritance is assumed.
- Standalone generated execution and demo execution bind their local runtime. Runner input preparation and cleanup no longer write/clear global runtime or global goals. demo.runtime reads the scoped runtime before its legacy fallback. The former adapter globals swap was already eliminated by Task 1; it remains absent.
- Explicit services record observations against their injected runtime even when it has no EvaluationContext. Bound legacy goals/history are stored per runtime. Unbound single-runtime compatibility assignments/sets remain available, but production does not depend on them.
- Scene initialization constructs runtime.random = random.Random(0), without reseeding the process generator. Placement/grid/movement/default-policy behavior is unchanged.
- record_subtask immediately honors an existing planned_actions attribute, including []/None, without calling the function body. Dynamic recording validates its whole source/code body first, then executes a FunctionType copy with copied globals, original code/defaults/closure and copied kwdefaults. Only the copied helper names are replaced.
- AST validation accepts straight helper statements and inert local assignments. It rejects dynamic control flow, global/nonlocal writes, indirect/arbitrary calls, helper-name shadowing, attributes/subscripts, unsupported operators, unpacking and unavailable source. Referenced captured/global/default values must be exact builtin inert data, preventing hidden protocol side effects. The validator reads the code object's source, avoiding inspect's __wrapped__ unwrapping trap. Unsupported tasks get an explicit planned_actions request before body execution.

## RED / GREEN evidence

All Python commands were run from the worktree root through `/home/dwb/.pyenv/bin/pyenv exec python`. Logs are stored beside this report.

- Baseline: `bash reports/executor_batch_2/verify.sh` — 583 tests, OK, 18.292 s (`task3-baseline.log`).
- Initial required RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_context_isolation.py` — 8 tests, FAILED (11 expected assertion failures, no fixture errors after correcting the alias fixture call). Fails for absent bind_runtime, executing existing explicit plans, mutating original function globals, and accepting unsafe bodies (`task3-red.log`).
- Resolver extraction GREEN: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_object_aliases.py tests/test_action_resource_leases.py` — 47 tests, OK (`task3-resolver-green.log`).
- Interactor extraction GREEN: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_plan_pre_task.py tests/test_executor_retry_policy.py tests/test_action_resource_leases.py` — 89 tests, OK (`task3-interactor-green.log`). An earlier group including alias tests also passed 98 tests.
- First context/AST GREEN: required context command — 8 tests, OK; then 13 tests, OK after entrypoint fixes (`task3-context-green.log`, latest saved version is 13 tests).
- Entrypoint RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_runtime_context_isolation.RuntimeEntrypointIsolationTest` — 5 tests, FAILED (4 assertion failures and the expected unbound-runtime RuntimeError from an actual worker). Demonstrated global RNG reseeding, clearing another legacy runtime during cleanup, shared demo history, hidden unsafe decorated code, and worker context not inherited (`task3-entry-red.log`). Fixed by scoped worker bindings/local ownership/code-object validation.
- Explicit service/value-safety RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_runtime_context_isolation.ExplicitServiceIsolationTest` — 2 tests, FAILED (2 expected failures). One runtime's helper polluted the other bound EvaluationContext; a custom parameters mapping executed keys() during recording (`task3-explicit-red.log`). Both fixed and covered by final GREEN.
- Pure-helper compatibility RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_runtime_context_isolation.ExplicitServiceIsolationTest.test_pure_compatibility_helpers_need_no_runtime_binding` — 1 test, FAILED with the expected missing-runtime exception after extraction (`task3-pure-red.log`). Fixed by static service utilities and context-free compatibility wrappers.
- Demo facade RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_runtime_context_isolation.RuntimeEntrypointIsolationTest.test_demo_runtime_facade_prefers_scoped_runtime` — 1 test, FAILED (1 expected identity mismatch), then final GREEN (`task3-demo-red.log`).
- Intermediate all-baseline regression: 583 tests, OK, 18.447 s (`task3-baseline-after.log`).
- Focused self-review regression: 145 tests, OK (`task3-focused-green.log`).

Final focused command:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
 tests/test_runtime_context_isolation.py tests/test_runtime_facade.py \
 tests/test_action_registry.py tests/test_generation_validation.py \
 tests/test_runtime_object_aliases.py tests/test_action_plan_pre_task.py \
 tests/test_executor_retry_policy.py tests/test_pddlrun_executor_adapter.py \
 tests/test_action_resource_leases.py
```

Result: **216 tests, OK**, including all **17 new isolation/recorder tests**, Task 1/2 coverage and all Task 3 named regressions (`task3-final-focused.log`).

Final baseline after the last implementation commit: `bash reports/executor_batch_2/verify.sh` — **583 tests, OK** (`task3-final-baseline.log`; exact elapsed time is in the log).

Compilation: explicit pyenv `python -m py_compile` for all changed production/test Python files passed. `git diff --check` passed. An AST comparison of every ThorRuntime method argument signature against base `0f4975d0` reported `[]` changed/missing signatures. Git status was clean after the commits; only the ignored report/logs were subsequently written.

## Self-review and concerns

- Preserved Task 1 recovery gates: actual door recovery preflights CloseObject and restoration OpenObject before effects; implicit hand clearance requires GoToObject+PutObject; slice reposition requires GoToObject; actual nested Pickup still checks skill/mass. The existing registry/retry/lease tests verify these behaviors. Compound declared-skill contracts were not broadened.
- Two existing test fixtures were adapted to explicit ownership: the blocker test now mixes the real ThorRuntime service facade into its FakeRuntime (no recovery checks removed), and the Egg history assertion binds the runtime whose history it reads (no copying evidence into globals).
- Initial self-review found the decorated-source and custom-mapping holes, and both now have behavioral RED/GREEN tests. Shared function globals are observed from synchronized concurrent recordings, not merely inspected as source text.
- Pure utility imports were pruned after extraction and no duplicate interaction implementation remains in actions/runtime. Compatibility constant/log injection is deliberate; service state is per runtime.
- Legacy module runtime assignment remains a single-runtime compatibility escape hatch. Concurrent callers must use explicit runtimes/bind_runtime; all production queue/cleanup/standalone entrances do so. Unsupported dynamic recording intentionally requires explicit planned_actions.
- No unresolved implementation concern identified within Task 3. Verification is deterministic/fake-controller based; live simulator/performance acceptance belongs to later tasks and was not attempted here.


## Review round 1 fixes — b6b48685

Parent review `task-3-review.md` reproduced two missing extraction dependencies that the original passing suites did not cover. Both findings were verified against base `80610422` and fixed in **b6b48685** (`fix(executor): restore extracted Egg and cancellation dependencies`). Production changes are exactly two imports:

- `object_interactor.py` now imports the existing `require_break_egg_target` from utils. Compatibility BreakEgg/PrepareEgg and direct ObjectInteractor helpers execute valid Egg actions and reject non-Egg targets before effects.
- `object_resolver.py` now imports the existing `raise_if_execution_aborted`. Both `_current_object_by_id_optional` and `repair_object_alias` preserve the exact original ExecutionCancelled/PlanExecutionTimeout exception; ordinary read failures retain their None/warning fallback.

Behavioral tests added:

- Two tests, each with four helper/path combinations: BreakEgg and PrepareEgg through actions compatibility wrappers and explicit ObjectInteractor, using the actual object-action implementation with only the simulator step faked. Valid targets submit BreakObject with the Egg resource and record success/BROKEN evidence; non-Egg targets fail with no submission or execution-stat increment. Explicit calls run while a different runtime is bound to protect ownership.
- Four resolver tests cover cancellation and timeout in both exception branches (asserting exception identity), ordinary optional-lookup fallback, and ordinary alias-repair fallback plus one-time warning behavior. Alias IDs remain intact after failures.
- Expected warning output in these newly added tests is captured and asserted. The existing unrelated Tomato warning/logging path was not modified.

RED command, replayed with the final payload fixture while removing only the two fixed imports and restoring them in a finally block:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
 tests.test_runtime_context_isolation.EggHelperExtractionRegressionTest \
 tests.test_runtime_object_aliases.ResolverExceptionRegressionTest
```

Result: **6 tests, FAILED (errors=14)**, subprocess exit **1**. Every failing subcase exposed one of the two reviewed NameErrors (`task3-review-red.log`). The initial valid-Egg assertion was corrected to include the existing `objectResources: ['Egg|1']` payload before this final RED replay; no production payload change was made.

The same command after fixing the imports: **6 tests, OK**, 0.005 s (`task3-review-green.log`). Each import was also verified separately while applying the fixes (`task3-review-egg-green.log`, `task3-review-resolver-green.log`).

Bounded unresolved-reference inspection:

```bash
/home/dwb/.pyenv/bin/pyenv exec python /tmp/task3_scan_extracted_globals.py
```

The script is copied beside this report as `task3_scan_extracted_globals.py` for rerunning from the worktree root. It inspects LOAD_GLOBAL instructions in functions/methods (including static/class methods and nested code) defined only in object_resolver/object_interactor, checking module globals and builtins. Before the fix it reported exactly four unresolved call sites: BreakEgg/PrepareEgg -> require_break_egg_target, and optional lookup/alias repair -> raise_if_execution_aborted (`task3-review-static-red.log`). After the fix: **Unresolved global call sites: 0**, exit 0 (`task3-review-static-green.log`). No other extracted unresolved reference was found.

Final related regression command:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
 tests/test_runtime_context_isolation.py tests/test_runtime_object_aliases.py \
 tests/test_action_plan_pre_task.py tests/test_action_registry.py \
 tests/test_executor_retry_policy.py tests/test_action_resource_leases.py
```

Result: **150 tests, OK**, 0.336 s (`task3-review-regression.log`). This includes all requested review-fix files and the related recovery/lease checks. Tests were not broadened further after this passed.

Final syntax/diff checks:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
 scripts/executor_system/object_interactor.py scripts/executor_system/object_resolver.py \
 tests/test_runtime_context_isolation.py tests/test_runtime_object_aliases.py
git diff --check
```

Both passed without output. The committed production diff consists only of the two missing imports, so facade signatures, recovery ordering, capability policy and runtime ownership are unchanged. No unresolved review finding remains in this fix scope. No live simulator or Task 4/5 work was performed.
