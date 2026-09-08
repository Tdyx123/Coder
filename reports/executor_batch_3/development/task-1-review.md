### Spec Compliance

- ❌ Issues found: implicit recovery can still bypass non-Pickup skill restrictions (`scripts/executor_system/action_resources.py:232`), and public flat THOR payload ingestion drops fields that the new registry promises to validate and forward (`scripts/executor_system/action_registry.py:198`, `scripts/executor_system/action_plan.py:102`). See Important findings.
- ✅ All Task 1 named production/test files have corresponding changes; fixed registration, helper/direct normalization, resource delegation, shared capability rules, and structural/scene separation are present (`scripts/executor_system/action_registry.py:108`, `scripts/executor_system/action_resources.py:93`, `scripts/executor_system/capability_checks.py:22`, `scripts/executor_system/plan_validator.py:15`).
- ⚠️ Explicit context services and canonical type moves are deferred to Tasks 2/3 as instructed; this review makes no whole-batch completion claim. Live performance, movement/default invariants, and two-runtime isolation are outside this task diff.

### Strengths

- `scripts/executor_system/action_registry.py:249`: preparation calls registered resource resolvers directly, while the old resource API delegates inward (`scripts/executor_system/action_resources.py:93`); this avoids the prohibited compatibility recursion and a second resource-definition table.
- `scripts/executor_system/plan_validator.py:38`: scene errors receive stage/robot/cursor context, and scene-only resolution preserves future Pickup→Put plans without weakening actual Put admission (`tests/test_action_registry.py:118`, `scripts/executor_system/action_resources.py:155`).
- `scripts/executor_system/generated_plan_runtime.py:352` and `:449`: both generated entrypoints structurally validate before runtime construction; the controller-creation sentinel test checks this ordering (`tests/test_action_registry.py:153`).
- `tests/test_action_registry.py:164`: forwarding coverage checks actual next-action arguments and coordinator/wave identity; `scripts/executor_system/capability_checks.py:22` preserves skill-first, then Pickup capacity/mass validation.

### Issues

#### Critical (Must Fix)

- None identified.

#### Important (Should Fix)

- **Recovery Close/Open still bypass robot skills.** `scripts/executor_system/action_resources.py:232` runs capability checks only for PickupObject. A GoToObject admission reserves open objects (`scripts/executor_system/action_registry.py:116`), and unchanged blocker recovery submits CloseObject and OpenObject through `_step_direct` (`scripts/executor_system/runtime.py:2326`, `:2342`). A robot with only GoToObject therefore passes both operations' resource boundary despite lacking the corresponding skills. This violates the explicit requirement that implicit recovery cannot bypass skill restrictions. Check the skills required by a recovery sequence before its first effect, including restoration, while retaining the declared capability policy for ordinary compound helpers. Add a focused recovery test with a leased open blocker and a robot missing CloseObject/OpenObject. Directed reproduction prepared GoToObject for a robot with `skills=['GoToObject']`, then called its real ActionResourceScope.before_step with CloseObject on the leased cabinet; it was accepted.
- **Flat THOR payload fields bypass the new numeric validation and are silently lost.** `scripts/executor_system/action_registry.py:198` and `:203` validate horizon/throwMagnitude/rotation only after `Action` construction. The supported dictionary boundary `Action.from_any` copies only objectId/agentId/degrees/moveMagnitude/position (`scripts/executor_system/action_plan.py:102`), so a flat Teleport payload containing rotation with infinity and horizon with NaN is accepted after both fields disappear. Finite rotation/horizon values and direct ThrowObject throwMagnitude are likewise dropped rather than forwarded. Extend flat payload ingestion consistently with the registered direct payload contract and cover the public `Action.from_any({...})` path, rather than only constructing `Action` with nested parameters as the new angle tests do. Directed reproduction normalized a flat Teleport with infinite rotation and NaN horizon successfully to `{'position': {'x': 0, 'y': 0, 'z': 0}}`.

#### Minor (Nice to Have)

- `task-1-report.md:53`: the documented intermittent supervisor SIGKILL assertion remains a validation reliability concern, but the final accepted regression log passes and neither supervisor implementation nor its test changed in this task. Track separately; it is not a Task 1 blocker.

### Assessment

- **Task quality: Needs fixes.** Registration and delegation are coherent, but recovery capability enforcement and public payload ingestion do not yet satisfy the end-to-end action contract.
- **Checks performed:** read the provided diff once in sequential sections; inspected unchanged Action.from_any, generated helper wrappers, TaskRunner/Executor validation calls, runtime step boundaries, and blocker recovery only to assess the named input-compatibility, invocation-order, and recovery-enforcement risks. No changed function was reread to regenerate diff context; no git operation, code mutation, suite rerun, or subagent dispatch occurred.
- **Verification evidence:** inspected final focused/regression log tails: 220 tests OK and 581 tests OK. Ran one small `/home/dwb/.pyenv/bin/pyenv exec python -B` reproduction from the worktree root for the two specific gaps above; both reproduced without submitting controller actions. The only file written is this requested report.
