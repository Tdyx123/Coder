# Task 6 implementation report

Base: `6fd96de9180a85d7e86a4be886968be8979e3609`

## RED

Command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py`

Initial result: exit 1, four tests with six failures. Importing `execute_plan.py`
parsed CLI arguments; generated runtime selection returned 1 instead of its child 23;
generated arguments were rejected; each omitted legacy field failed through a missing
repository-data traceback instead of naming `floor_no`, `robots`, or `ground_truth`.

The package-root export test then failed with `AttributeError: module
'executor_system' has no attribute 'Action'`. A later focused RED test confirmed the
remaining fabricated `no_trans_gt=0` and `max_trans=10`: the legacy compiler returned 0
when those values were absent.

## GREEN

`scripts/execute_plan.py` now has a main guard, resolves directory paths and old
`./logs/<name>` commands, verifies the shared generated runtime via its canonical
import and literal bundle assignments, forwards remaining runtime arguments, and
returns the child exit code. Legacy concatenation requires all recorded context and
does not create an executable when it is incomplete.

`executor_system.__init__` exposes canonical types/services lazily. The first eager
implementation was caught by `test_runtime_facade`: importing `executor_system.plan_types`
loaded execution modules. The final `__getattr__` implementation keeps package-root
compatibility without breaking the pure-types boundary.

Final commands:

- `bash reports/executor_batch_3/verify.sh`: exit 0; batch 1 583 OK, batch 2 588 OK,
  batch 3 focused 310 OK.
- `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py`:
  exit 0; 6 OK.
- Explicit py_compile for the changed Python files: exit 0.
- Four curated `.xz` archives decompressed to the SHA-256 recorded in their readable
  reports: exit 0.

## Files

- `.gitignore`
- `README.md`
- `scripts/README.md`
- `scripts/execute_plan.py`
- `scripts/executor_system/__init__.py`
- `tests/test_execute_plan_compatibility.py`
- `docs/superpowers/plans/2026-09-07-executor-batch-3-architecture-performance.md`
- `reports/executor_batch_3/` documentation, verification script/log, archive helper,
  and curated evidence

## Open acceptance concerns

The implementation suites are green. Real acceptance is not: the immutable full
baseline has 110 valid and 10 case-03 mass-invalid evaluations; the corrected event
smoke matches correctness but has event P50 3.673028 s versus full 2.664341 s and
fails the 1.05 performance gate. The final 240 step and 24 teleport runs are pending.
Case 06 `NO_PLAN_FOUND` also reproduces on old batch 2 code; no speculative fix was
made.

## Review round 1 fixes

The three Important findings in `task-6-review.md` were reproduced before changing
the implementation.

RED command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`

Result: exit 1; 28 tests ran with four failures and one error. The verifier accepted
an import-only wrapper, each of the three historical robot placeholders (`[]`,
`['robot1']`, and `['Robot2']`) overwrote the recorded robot context during an actual
minimal legacy execution, and the invalid preferred wrapper shadowed a valid fallback
used for benchmark plan identity.

The legacy compiler now parses `code_plan.py` and structurally replaces only those
three historical top-level assignments with the validated robots recorded in
`log.txt`. Generated-runtime verification now requires the imported shared `main`
alias to be called with `BUNDLE_DATA`, `TASK_FILE`, `TASK_INDEX`, and `__file__` from
the `__main__` exit path. Deferred function/class bodies do not satisfy that check.
The subprocess fixtures install a controlled shared runtime and prove argument,
selected-path, and return-code forwarding. The benchmark regression proves that an
import-only preferred candidate is rejected in favor of a verifiable fallback and
that the selected bundle supplies benchmark plan identity.

GREEN command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py tests.test_runtime_facade.PlanTypesFacadeTest.test_parsing_types_does_not_load_execution_or_cli_modules`

Result: exit 0; 29 tests ran, all OK. Explicit py_compile for the three changed Python
files and `git diff --check` also exited 0. No live simulation or full-suite rerun was
performed for this focused review fix. The real acceptance status above remains
unchanged and pending the root-owned final runs.

## Review round 2 fixes

RED command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py`

Result: exit 1; 10 tests ran with one failure and one error. A shared-runtime call
inside `if False` incorrectly verified, and executing `robots = [];
events.append('run-plan')` showed that the adjacent action had been deleted.

The verifier now accepts only a direct shared-runtime exit as the first statement of
the `__main__` body, or the same direct exit as the first statement of the repository's
known diagnostic `try` wrapper. Nested conditional calls and calls after an earlier
statement cannot satisfy verification. The placeholder rewrite now calculates exact
UTF-8 byte offsets from `lineno`, `col_offset`, `end_lineno`, and `end_col_offset`, and
replaces only the assignment node span, leaving adjacent statements intact.

GREEN command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py tests.test_runtime_facade.PlanTypesFacadeTest.test_parsing_types_does_not_load_execution_or_cli_modules`

Result: exit 0; 31 tests ran, all OK. The controlled subprocess test also executes the
current generated diagnostic `try` wrapper. Explicit py_compile and `git diff --check`
passed. No live simulation or full-suite run was performed; real acceptance remains
pending the root-owned final runs.

## Review round 3 fix

RED command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_execute_plan_compatibility.ExecutePlanCompatibilityTests.test_rebound_shared_runtime_alias_is_rejected_without_execution`

Result: exit 1; the canonical-looking wrapper rebound `run_generated_plan` to a
lambda, returned 0, and bypassed the controlled shared-runtime stub.

The verifier now requires exactly one canonical shared-main import and permits only
one binding each for `BUNDLE_DATA`, `TASK_FILE`, and `TASK_INDEX` between that import
and the sole `__main__` guard. Any intervening rebinding, reimport, definition, or
dynamic statement makes the generated bindings ambiguous and rejects the candidate.
This is a narrow generated-template binding check, not a Python sandbox.

GREEN command:

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`

Result: exit 0; 31 tests ran, all OK. The rebound wrapper is rejected without printing
the shared-stub marker, while existing controlled canonical wrappers still reach the
stub. Explicit py_compile and `git diff --check` passed. No broader or live suite was
run, and real acceptance remains pending the root-owned final runs.

## Review round 4 fix

Base: `59860f89`.

RED command (from `/tmp/coder-executor-batch-3`):

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`

Result: exit 1; 32 tests ran with nine subtest failures. Imported main aliases
`BUNDLE_DATA`, `TASK_FILE`, `TASK_INDEX`, `SystemExit`, `exit`, `sys`, `__file__`,
`__name__`, and `RuntimeError` all made the preferred candidate verify and shadow
the valid fallback returned by `find_generated_runtime`.

The canonical-binding check now rejects runtime aliases that collide with the three
generated data bindings or the guard, script-file, exit, and diagnostic-handler
names. This retains ordinary noncolliding aliases and the existing generated
entrypoint contract. It is a conservative template check, not a Python sandbox.
The regression checks candidate selection and executes the compatibility CLI with
the controlled shared runtime, confirming each rejected collision selects the
fallback task and forwards its exit code 23.

GREEN commands (from `/tmp/coder-executor-batch-3`):

- `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`:
  exit 0; 32 tests ran, all OK.
- `/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/execute_plan.py tests/test_execute_plan_compatibility.py`:
  exit 0.
- `git diff --check`: exit 0.

No full suite or live simulation was run. Real acceptance and its existing
performance concerns remain unchanged, pending the root-owned final runs.
