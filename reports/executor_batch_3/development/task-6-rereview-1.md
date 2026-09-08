### Spec Compliance

- ❌ Issues remain: round-1 finding 3 is fixed by the benchmark regression hunk, and the normal forms of findings 1 and 2 are covered, but generated-runtime verification can still accept a wrapper that never invokes the shared runtime (`scripts/execute_plan.py:121-163`), while the new legacy placeholder rewrite can delete unrelated plan code (`scripts/execute_plan.py:239-262`).

### Strengths

- Round-1 finding 1 is fixed for the three documented standalone top-level placeholders: `scripts/execute_plan.py:239-262` replaces them from validated log context, and `tests/test_execute_plan_compatibility.py:195-228` executes all three forms and verifies the effective robots.
- Round-1 finding 2 is fixed for canonical wrappers: `scripts/execute_plan.py:86-179` requires a top-level shared-main import and a four-argument call under a main guard; `tests/test_execute_plan_compatibility.py:101-193` now proves the controlled shared runtime receives forwarded arguments and supplies the child exit code.
- Round-1 finding 3 is fixed: `tests/test_executor_regression_benchmark.py:113-138` verifies that an import-only preferred candidate is rejected, the valid fallback is selected, and benchmark identity uses the selected bundle.
- The appended RED/GREEN report clearly scopes the 29-test focused run and retains the pending real-acceptance status (`.superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/task-6-report.md:61-84`).

### Issues

#### Critical (Must Fix)

- None.

#### Important (Should Fix)

- `scripts/execute_plan.py:121-163`: `_walk_executable_nodes` recursively visits every syntactic descendant of the main guard without control-flow analysis, so an unreachable branch such as `if False: raise SystemExit(main(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__))` makes an otherwise import-only wrapper verifiable. The actual path can then exit 0 without running the plan, preserving the silent behavior the round-1 fix was meant to reject. Restrict acceptance to the canonical direct exit statement (including its known `try` wrapper) or validate that all reachable normal paths terminate through the shared call; add an unreachable-branch regression.
- `scripts/execute_plan.py:247-262`: the structural rewrite records only `lineno/end_lineno` and replaces those complete physical lines. A valid historical line such as `robots = []; run_plan()` therefore loses `run_plan()` entirely; the old textual replacement preserved it. Because legacy plans are generated Python and may place another statement after the placeholder, this can silently remove actions. Replace only the AST node's `col_offset/end_col_offset` source span (with correct multiline handling), or reject non-standalone placeholder statements explicitly; add a same-line preservation test.

#### Minor (Nice to Have)

- None.

### Assessment

**Task quality:** Needs fixes

**Reasoning:** The requested regressions now cover the ordinary compatibility cases and benchmark selection, but the verifier remains bypassable and the new source rewrite can silently erase executable plan statements.

**Checks run:** Read the scoped incremental diff and appended report; no test suite or subagent was run.
