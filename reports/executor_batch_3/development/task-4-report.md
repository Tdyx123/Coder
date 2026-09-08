# Task 4 — bounded metrics and reproducible benchmark

Implementation commit: `e82e20f5` (`feat(executor): add bounded runtime metrics and regression benchmark`). Postcommit tracked worktree is clean.

Implementation base: `b6b48685f7e77d7ab82110ecc21e20720c41fb7e`, branch `codex/executor-batch-3`, worktree `/tmp/coder-executor-batch-3`.
Tasks 1–3 and their reviewed contracts were retained. Task 4 implementation and fake validation are complete. The controller subsequently saved the real full baseline; its corrected acceptance check FAILED (110 valid / 10 case03 mass-exceeded). Saving the baseline checkbox is complete; overall real acceptance remains incomplete. No simulator was started by this agent and no cache optimization was implemented.

## Changes and interfaces

- `scripts/executor_system/runtime_metrics.py`: thread-safe `RuntimeMetrics.observe/increment/snapshot/measure`. Each timing category keeps only count/total_seconds/max_seconds. At most 64 named timing categories plus one overflow bucket and 128 named counters plus one overflow bucket; names are bounded to 128 characters. Snapshots copy state; exceptional exits are timed; nonfinite/negative timings are rejected.
- `controller_client.py`: `lock_wait` measures acquisition before the second cancellation check; `controller` measures the actual THOR submission; `reachable_query` measures GetReachablePositions within controller time. Counters: controller_calls, reachable_queries, controller_exceptions, controller_action_failures. State publication and scheduler notifications retain their existing atomic/finally boundary.
- `movement_coordinator.py`: `navigation_planning` includes scenario construction, planner execution and result assembly; navigation_plans counts planning calls.
- `object_interactor.py`: `recovery` and recovery_calls cover recovery/hand-preparation helper entries, including eligibility checks and exceptions. Nested calls overlap; these counters count helper calls, not successful recoveries.
- `runtime_artifacts.py`: `artifacts` and artifact_calls cover prepare, frame saving, metadata, and video methods, including disabled-output checks. Actual controller rendering configuration remains unchanged.
- `runtime.py`: eagerly owns RuntimeMetrics, records effective controller constructor parameters, exposes reachable_refresh_mode with default full and seed 0. At this task boundary event is explicitly rejected until Task 5 implements it.
- `generated_plan_runtime.py`: runner JSON includes runtime_metrics, reachable_refresh_mode and reproducibility. Metadata records actual module-root code SHA/dirty status, Python/AI2-THOR versions, platform, GPU UUID/name/driver (null when unavailable), BUNDLE_DATA content hash, executable hash, task-record hash, scene, robot count, seed, resolved movement and controller settings, rendering flags, timeout, policy and selected runtime constants. Three protocol versions remain top-level result fields. The shared result builder's contract is unchanged; generated runner identifies scheduler 2 explicitly. CLI exposes --reachable-refresh-mode full|event; default full paths retain compatibility with old constructor fakes.
- `parallel_runner.py`: optional `reachable_refresh_mode='full'` and `pythonpath_prepend=None` arguments. Benchmark prepends candidate scripts/root in each child's PYTHONPATH without changing the parent environment. This takes precedence over the fixed12 executables' hardcoded original-root **append** calls. No process supervision or result validation rule was relaxed.
- `scripts/benchmark_executor_regression.py`: fixed-manifest CLI, default repetitions=5, step/legacy/full, max_workers=1. Every job starts a new process/controller; no automatic retries and repetition numbering starts at 1. Results are grouped by movement, policy, refresh mode and all three protocol versions. Each job preserves the parent-validated child result in child_result and the untouched child_metrics.json alongside request/result/log files. Input plan/hash failures become per-repetition failures. Missing fixed files list all missing cases and the complete unstarted denominator, return nonzero, and launch no smaller replacement workload. Nonempty output directories cannot overwrite evidence. JSON writes reuse the established atomic writer.
- `movement.py` and `benchmark_movement_modes.py`: old planning_durations_seconds remains a list for import/result compatibility, capped to the latest 128 entries, with explicit count, limit, truncation and preview semantics. Full accumulated planning_time_seconds remains unchanged. The legacy movement benchmark emits null/incomplete for its planner P95 when input previews were truncated and its check rejects incomplete evidence. The new benchmark never derives percentiles from this preview.
- Tests: `tests/test_runtime_metrics.py` and `tests/test_executor_regression_benchmark.py` (21 tests).

Timing categories overlap. GetReachablePositions time is included in controller time; recovery and planning can contain other categories. Neither report adds overlapping categories to claim a total. Benchmark P50 is statistics.median and P95 is nearest rank over raw per-run execution phase durations. Missing durations fail event/full performance checks.

No API key or complete environment mapping is persisted. GPU query stderr/environment are not copied into metadata. Configuration input is explicitly constructed by the runner and benchmark.

## --check scope and acceptance

`--check` checks recorded evidence, **not complete real acceptance**. A zero exit does not certify external collision equivalence, cancellation, lease, deadlock or result-protocol regression tests; report.external_validation explicitly marks external validation pending. Missing hard-safety counters are unknown, never reported as observed zero. Positive collisions/lease_leaks/wait_deadlocks evidence and any step-navigation Teleport evidence are rejected.

For full baselines, every run must have a completed parent process, valid finite-GCR evaluation and finite nonnegative execution duration, in addition to the missing/protocol/timeout/reproducibility checks. Known partial/failed task execution or unsuccessful goal scores remain baseline evidence only when the process completed with valid evaluation. Unknown/dirty code identity is rejected for the parent and each child.

For event/full comparisons, each policy and movement mode remains separate. Checks reject mismatched/missing pairs, code roots/SHA/plan hashes, protocol versions, new timeout/incomplete/process/overall execution/task-success/navigation/GCR regressions, and each formerly successful action becoming failed or missing (case/repetition/action_key included). Thus equal aggregate GCR or two partial statuses cannot hide a new action failure. Group checks enforce nondecreasing valid evaluation count/GCR/navigation completion, nonincreasing timeouts, and execution median **and** P95 <= 1.05 × full. Protocol groups are never automatically combined. Raw timeouts, missing results, incomplete evaluations, missing cases and unstarted runs remain visible.

## RED/GREEN evidence

All commands were run from `/tmp/coder-executor-batch-3`, with Python explicitly selected by pyenv. Environment: miniconda3-3.9-25.9.1-3, `/home/dwb/.pyenv/versions/miniconda3-3.9-25.9.1-3/bin/python`. Log paths below are relative to this report's directory.

1. Required initial RED:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py`
   — missing new module imports, 2 loader errors; `task4-red.log`.
2. Initial integration attempt with the same command: 13 tests, 3 errors (`task4-first-green.log`). Two were a missing explicit_timeout=None argument to an existing helper; one was an incomplete recovery fake settings fixture. Both were fixed without bypassing runtime validation. GREEN: 13 tests, OK (`task4-green.log`).
3. Additional RED, same command: 18 tests, 1 failure/2 errors (`task4-integration-red.log`). Covered incomplete legacy percentile labelling and per-repetition malformed source retention. The real fake-subprocess fixture initially lacked required action-ledger/quiescent evidence; corrected the fixture, not the result validator. GREEN: 18 tests, OK (`task4-integration-green.log`).
4. `bash reports/executor_batch_2/verify.sh` initially ran 587 tests with one error (`task4-baseline.log`). Adding scheduler_version=2 to shared build_runner_result affected the legacy template entry. Removed that shared change and set scheduler2 only in generated run_runner_mode. Regression GREEN:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_final_reliability_fixes.py tests/test_executor_regression_benchmark.py tests/test_runtime_metrics.py`
   — 27 tests, OK (`task4-protocol-green.log`). Existing tests were not changed.
5. Pair/denominator RED:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_executor_regression_benchmark.py`
   — 14 tests, 1 failure/1 error for missing paired-action failure and missing-case raw denominator (`task4-pair-red.log`). Implemented both.
6. Final named GREEN:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py`
   — **21 tests, OK**, 1.562s (`task4-final-named.log`). Includes FakeClock lock/controller/query/planning/recovery/artifact boundaries, bounded counts, concurrent counters, policy/version splits, repeated failures, missing/invalid fixed inputs, output preservation, paired failures/performance gates and real OS fake child execution proving candidate import precedence.
7. Final baseline GREEN: `bash reports/executor_batch_2/verify.sh` — **587 tests, OK**, 21.145s (`task4-final-baseline.log`). No unittest discover or live simulator run was used.
8. Final Task 1–4 focused GREEN:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_action_registry.py tests/test_generation_validation.py \
  tests/test_runtime_facade.py tests/test_runtime_context_isolation.py \
  tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py
```

**103 tests, OK**, 2.025s (`task4-final-focused.log`). A final counter-label clarification (recovery_calls) and import placement cleanup were followed by the final named GREEN above.

9. Compile: `/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/runtime_metrics.py scripts/benchmark_executor_regression.py scripts/executor_system/controller_client.py scripts/executor_system/runtime.py scripts/executor_system/runtime_artifacts.py scripts/executor_system/movement.py scripts/executor_system/movement_coordinator.py scripts/executor_system/object_interactor.py scripts/executor_system/generated_plan_runtime.py scripts/executor_system/parallel_runner.py scripts/benchmark_movement_modes.py tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py` — exit 0. `git diff --check` — exit 0.

10. Durable synthetic subprocess CLI smoke (no AI2-THOR controller):

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_executor_regression.py \
  --manifest .superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/task4-fake/manifest.json \
  --output-dir .superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/task4-fake/output \
  --repetitions 2 --movement-modes step --execution-policies legacy \
  --reachable-refresh-modes full --max-workers 1 --check
```

Exit 0 (`task4-fake-cli.log`); 2/2 per-run records, no timeout/missing/incomplete metrics. `task4-fake/output/report.json` and its raw runs are retained locally. This synthetic one-case protocol smoke uses fabricated unit-test execution times and is **not** the fixed12 real baseline, performance evidence, or final acceptance. Its recorded code_dirty=true correctly reflects precommit implementation verification.

## Real full baseline saved; corrected acceptance failed

主任务已在干净 e82e20f5 上运行下列固定清单命令。120 次原始记录完整保留；首次 full 基准已保存，但修正后的门槛判为失败。任务 5 可在明确这一限制后实施，不得削弱质量检查或改选固定样例。

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_executor_regression.py \
  --manifest tests/fixtures/movement_benchmark_plans.json \
  --output-dir reports/executor_batch_3/e82e20f5_full_baseline \
  --repetitions 5 --movement-modes step \
  --execution-policies legacy strict --reachable-refresh-modes full \
  --max-workers 1 --check
```

This is 12 × 2 × 1 × 5 = 120 new simulator instances. If --check rejects a real timeout/protocol/missing result, all available raw evidence remains in the new directory; investigate rather than shrinking the manifest. Compare functionality/action errors with `reports/executor_batch_2/149637b2_real/` separately before proceeding.

Output layout:

```text
reports/executor_batch_3/e82e20f5_full_baseline/
  run_config.json
  report.json
  runs/case-01/step/legacy/full/repetition-01/
    request.json
    child_metrics.json
    result.json
    stdout.log
    stderr.log
    lammap-runtime-*/  (owned runtime artifact directory)
  ... case-12 / strict / repetition-05 ...
```

After Task 5, use the exact step/full-event and teleport commands in constraints.md on the same candidate commit for the full/event comparison. No event speedup claim or default change is made here.

## Self-review / remaining limits

- Confirmed the candidate import path is tested through real subprocesses with the original checkout append present; report SHA is checked against the actual loaded module root and child plan hash.
- Confirmed controller cancellation checks, commit version advancement and notification ordering remain unchanged. Instrumentation metadata errors occur after published version and inside the existing commit failure handler.
- Confirmed no scheduler, evaluation, resource lease, action result or process validation implementation was weakened; the discovered shared-builder regression was repaired and the full baseline passed.
- Confirmed missing cases, source parsing failures and individual partial/action failures cannot disappear into a mean. Existing child metrics and parent outcomes remain separately reviewable.
- Explicit limits: event implementation and complete hard-safety certification remain pending. The controller saved the live full baseline afterward; its corrected acceptance failed for case03, as detailed in the review section below. GPU identity can be null under restricted GPU access and is not fabricated. Startup failures before a runtime object returns can lack runtime category measurements; their raw failure, phase duration and reproducibility metadata remain available. Multiple timer categories and nested recovery helper calls overlap by design.
- Only Task 4 checkboxes were updated. The saved-baseline checkbox was subsequently completed with explicit failed acceptance status; the global real-acceptance section remains unchecked.

Git staging initially hit the configured read-only .git worktree index; authorized escalation succeeded for git add and commit. No automatic approval review rejection occurred. The report, checkboxes, raw logs and synthetic smoke artifacts remain in the existing ignored .superpowers task directory, consistent with previous tasks.


## Review round 1 — baseline gate and code provenance

Fix commit: `57a89ef2113871d0327a31ce031eadb2d65e1acb` (`fix(benchmark): reject unusable baselines and unknown code identity`). Review source: `task-4-review.md`.

Both Important findings were reproduced and fixed:

- Every full run (full-only or paired comparison) now requires process_status=completed, evaluation_status=valid with finite GCR, and a finite nonnegative execution-phase duration. Ordinary partial/failed task execution with valid evaluation remains permitted. Incomplete scene-validation/startup failures cannot certify a baseline.
- Parent and child provenance each require a full 40/64-hex Git SHA, known absolute nonempty code root and literal boolean code_dirty=False. Dirty, nonboolean, missing and unknown metadata fail. Each child SHA/root must match the recorded parent, rather than the currently checked-out gate commit.
- A metadata-only correction preserves failed git-status queries as code_dirty=None; it no longer coerces unknown to clean. The RuntimeMetrics timer implementation, controller/runtime/task/movement execution, fixed plans and robot capabilities are unchanged. This narrow provenance correction was explicitly approved by the controller.
- Minor test-output finding fixed: CLI stdout/stderr is captured; expected diagnostics are asserted. The fake real-subprocess test explicitly expects rejection while the tested source is dirty instead of fabricating clean metadata.

Targeted RED/GREEN command (no redundant broad suite or simulator rerun):

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_executor_regression_benchmark.py tests/test_runtime_metrics.py
```

RED: 27 tests, 26 expected assertion/subtest failures (`task4-review-red.log`). GREEN: **27 tests, OK**, 1.568s (`task4-review-green.log`). Compiled the four changed Python files with explicit pyenv; exit 0. `git diff --check` passed. No parent-result validation implementation was changed, so no additional parent-contract suite was required.

Controller-owned live evidence verified read-only:

- Original `reports/executor_batch_3/e82e20f5_full_baseline/report.json`: exactly 120 records, 110 valid/completed and 10 incomplete/failed, zero timeouts/missing/unstarted runs. All parent/120 child code metadata reports clean e82e20f5 and the candidate root.
- All 10 unusable runs are manifest case03 (`FloorPlan1_task_0`), legacy repetitions1–5 and strict repetitions1–5. Scene validation rejects robot1 picking up Pot: mass 0.5699999928474426 exceeds capacity 0.08. This is the immutable fixed-workload capability conflict introduced into visibility by required Task1 validation; no plan, robot, capability rule, mass check or denominator was changed.
- Original check_passed=true is a known invalid gate conclusion. The original report and all raw outputs remain preserved. The corrected separate recheck is authoritative: **exit 1**, 10 baseline_process_failed + 10 baseline_evaluation_invalid findings, identifying the 10 affected runs. No timing/provenance failures were found in the recorded e82 evidence.

Exact recheck command:

```bash
/home/dwb/.pyenv/bin/pyenv exec python \
  .superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/task4-recheck-saved-baseline.py
```

The script only reads saved evidence and invokes acceptance_failures. Output: `task4-gate-recheck.json`, console evidence: `task4-gate-recheck.log`. It records checker commit 57a89ef2 and the original report SHA256 `0cd34b68dbd5d80f0dbe9b5e6aa9c03ddc7a1ac9d21a74429bc3dba47e3fc08b`, and verifies that source hash is unchanged after reading. It does not compare e82 runtime rows against the current gate HEAD, overwrite the old report, or start AI2-THOR.

`git diff --name-only e82e20f5..57a89ef2` contains only benchmark_executor_regression.py, runtime_metrics.py (provenance-only lines) and their two tests. The prior clean e82 simulator measurements remain valid historical runtime evidence; no 120-run repeat is necessary for this gate correction. Fixed-workload real acceptance is failed/incomplete, not complete. The synthetic dirty-code smoke recorded earlier also remains historical only and would now correctly fail clean-code certification.

Saved-baseline checkbox is complete with explicit failed acceptance annotation. Global real acceptance remains unchecked. Real raw output directory is untracked and excluded from this commit; the controller owns evidence archival.
