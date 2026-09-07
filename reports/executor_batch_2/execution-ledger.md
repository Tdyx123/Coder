# SDD ledger — plan: /tmp/coder-executor-batch-2/docs/superpowers/plans/2026-09-07-executor-batch-2-execution-semantics.md

Baseline: 8f95c7ca092d7025a5ca67def5015daa56f6c568; batch 1 production: 3c2661b267b767386114ed8cad5c6330871306a9.

## Preflight
| Tasks | Producer / consumer or internal consistency | Finding |
|---|---|---|
| 1 | Task tests and implementation scope | Consistent; progressive integration per task |
| 1, 2 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 1, 3 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 1, 4 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 1, 5 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 1, 6 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 2 | Task tests and implementation scope | Consistent; progressive integration per task |
| 2, 3 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 2, 4 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 2, 5 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 2, 6 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 3 | Task tests and implementation scope | Consistent; progressive integration per task |
| 3, 4 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 3, 5 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 3, 6 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 4 | Task tests and implementation scope | Consistent; progressive integration per task |
| 4, 5 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 4, 6 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 5 | Task tests and implementation scope | Consistent; progressive integration per task |
| 5, 6 | Shared executor/runtime/plan interfaces; earlier task supplies primitives used by later task | Execute sequentially; preserve compatibility exports |
| 6 | Task tests and implementation scope | Consistent; progressive integration per task |

Ruling: SKIP_IF_EFFECT_ALREADY_TRUE success is applied by executor before resolve_failure; pure decision kinds remain the declared five kinds — avoids inventing a sixth success kind — incorrect interpretation would require interface revision.
Ruling: Snapshot read errors propagate as infrastructure failures; callbacks retain original exception as cause — required by condition and infrastructure contracts — may stop legacy runs that previously swallowed errors. Resource binding invalidation follows the specified pre-side-effect requeue / post-side-effect failure contract.

## Progress
- [x] Task 1
- [x] Task 2
- [x] Task 3
- [x] Task 4
- [x] Task 5
- [x] Task 6

Task 1: implemented f25f0ec1; 151 covering tests pass; independent review pending.
Task 1: fix round 1/5 in progress — P1 stage cancellation not observed by controller boundary. Deterministic review reproduction confirmed second substep submitted after child cancellation. Fix worker-local control propagation; preserve root infrastructure cancellation.
Task 1: fix round 1/5 implemented f25f0ec1..317fb232; deterministic RED/GREEN and 101 covering tests pass; scoped re-review pending.

Task 1: complete (commits 8f95c7ca..317fb232, review clean). Task 2 base: 317fb232.
Task 2: implemented 317fb232..c8a1d712; 190 covering tests pass; independent review pending.
Task 2: fix round 1/5 in progress — P1 malformed commit inventory raises ordinary TypeError and Teleport retries it; fix SnapshotReadError/root cancellation/lock-free notification with regression.

Task 2: complete (commits 317fb232..22fa6601, review clean). Task 3 base: 22fa6601.
Task 3 RED: original step dependency deadlock, END/EVENT progress ordering fail; missing waiting diagnostics. Architecture agreed: main-thread run_workers drive callback; workers scheduler.execute_worker grant-only; PhaseCoordinator independent admitted waves. Snapshot lock acquisition must check cancellation because coordinator now reads snapshots while workers execute.
Task 3 interface ruling: StageScheduler.run returns StageOutcome for policy stage failure now; old entry wrappers may temporarily rethrow StageFailureDecisionError until task5 report unification. Infrastructure failures retain existing propagation. This implements the declared interface without delaying dependent task4.
User authorization (2026-09-08): 完全完成后 合并到 main. After all six tasks, reviews, regression and real benchmark acceptance, merge into /home/dwb/thor/Coder main without asking again. No push requested. Preserve unrelated user changes; verify merge state.
Task 3: implemented 22fa6601..a9449229; specified88/affected121/pre-task5 passing; independent review pending.
Task 3: fix round 1/5 in progress — P1 synchronous idle Pass blocks main deadline/shutdown; P2 wait age only grows on idle Pass; P2 second pre-submission failure does not retire admitted wave. All reproduced by bounded review probes, delegated supervised tick/aging/retirement fixes.

Task 3: complete (commits 22fa6601..14c1791c, review clean). Task 4 base: 14c1791c.
Task 4 design integration: thread-local helper runtime/action scope replaces unsafe temporary shared context.runtime mutation; automatic hand preparation moves from GoTo to actual leased Pickup interaction (target selection semantics preserved). Required for helper bindings without pre-wave navigation lock.
Ruling: GoTo pre-admission leases exact currently open/openable recovery candidates, including destination only if it is such a recovery candidate — preserves existing dynamic Close/Move/Open recovery and pre-side-effect resource/lock ordering without normal destination reservation — cost: conservative serialization of navigation sharing possible open blockers. Post-motion lease expansion or failing all existing recovery was rejected.
Task 4: implemented 14c1791c..a3a70a2c; 112 covering tests passing (23 resource tests subset); independent review pending.
Task 4: fix round 1/5 in progress — P1 live controller reads from main-thread resource resolver bypass bounded snapshot lock; P1 transformed ID published before lease identity linkage permits duplicate owners; P2 affected ordinary entry fixtures lack resource metadata (6/6 fail). Review reproduced P1s; targeted entry log /tmp/task4-entry-compat.log. Delegated snapshot-based admission/atomic identity commit/fake enrichment fixes.
Task 4: fix round 1/5 reviewed original 3 findings addressed (commit3e59d841,216 tests). New P2 detached snapshot selection loses per-agent visible/distance, changes robot1 instance choice using active robot2 metadata. Fix round 2/5 delegated per-agent frozen selection views and deterministic two-Mug regression.

Task 4: complete (commits14c1791c..ae665662, review clean after2fix rounds). Task5 base: ae665662.
Task 5: implemented ae665662..2ca2a8d1; 276 covering tests pass; independent review pending.
Task5 broad firstbatch gate:449tests,1 failure shared standalone deferred-navigation compatibility; log /tmp/batch2-after-task5-regression.log. Review confirms P1 shared PhaseCoordinator replaced, plus P2 completed peer failure dropped on stage shutdown and P2 explicit control ignores earlier TolerantStageRunner deadline. Fix round1/5 delegated preserving unified loop/shared wave/outcome evidence/min deadline.
Task5 fix round1/5 reviewed: original3 findings addressed (c0791872;153targeted+449firstbatch pass); new P2 finalizing skips completed SKIP_IF_EFFECT_ALREADY_TRUE proof. Fix round2/5 delegated executor-only and bounded tests while task6 independent CLI/docs work runs.
Task6 base c0791872; implementation agent /root/task6, task5 fix2 will be excluded from task6 review diff.
Task5 fix round2/5 (841e02f3;93targeted pass) restores proof and cleanup. Scoped review identifies direct P2 stale entry snapshot used during timeout/cancelled finalization (no final capture), producing false succeeded action evidence. Fix round3/5 delegated: only evaluate on proven post-exit current snapshot, else unknown; no capture past cancelled control.
Task6 development smoke (uncommitted CLI tree atop841e02f3): one real Unity legacy/teleport normal exit, valid contract/evaluation/fixed denominator/no worker or cleanup errors; /tmp/executor-batch-2-smoke-01/report.json. Not final acceptance.

Task 5: complete (commits ae665662..1d243042, review clean after3fix rounds). Task6 final review base1d243042; shared standalone lifecycle, completed outcome proofs, current finalization snapshot all verified.
Task6 implemented b9dd85c3;575 tests/17.797s GREEN; task review requests one P2 new docs direct parallel_runner.py invocation fails baseline relative import. Fix1/5 delegated use verified python -m scripts.executor_system.parallel_runner command, doc/help-only check (no repeated suite).

Task 6: complete (commits1d243042..8a4d97a7, review clean after1doc fix). All6task gates approved; 575namedtests passed on finalproduction code; final whole-branch review and real48 acceptance pending.
Final whole-branch review 8f95c7ca..8a4d97a7:3 P2 integration findings plus1 P2 missing persistent acceptance test. F1 WorldState.tick never advances legacy callbacks; F2 retry increments wait_ticks without actual idle Pass; F3 admission-failure ledger attempt missing per-action counter update; F4 no-deadline cycle external watchdog cleanup regression absent (bounded review probe product behavior passes). Dispatch ONE final fix worker for all4, then ONE scoped re-review. No known infrastructure P0/P1.
Ruling: scheduler2 compatibility WorldState.tick is the stage-wide count of coordinator-confirmed high-level action terminals (success/final failure/conflict skip), reset each stage; retry/defer/idle Pass/low-level commits do not increment it, initial-condition stage skip stays0, worker callbacks sample admission-time progress — restores legacy callback forward progress without confusing actual snapshot version or private robot cursors — cost: does not reproduce old multi-robot private-thread counter order; callbacks that depended on that race need adjustment.

Final fix wave implemented149637b2: all4 findings addressed,195targeted+581fullnamed GREEN, mutation proves0 leftoverthreads. Final scoped re-review pending; real48 will use149637b2 if approved.

Final scoped review149637b2: F1-F4 ADDRESSED, spec/quality APPROVED, no new direct regressions. Final real48 running /tmp/executor-batch-2-real-149637b2 from production149637b2.

Final real48 complete on149637b2:48exit0/48contract/48valid/48fixeddenominator; RU and quiescence/audit checks pass, cleanup empty,7expectedstrictchildcancel records retained. Legacy24GCR unchanged vsfirstbatch; strictteleport mean0.6388889, strictstep0.6111111 (early stop), allSR0. Report reports/executor_batch_2/149637b2_real/. Ready authorized localmain merge after evidence commit.
