# SDD ledger — plan: docs/superpowers/plans/2026-09-07-executor-batch-3-architecture-performance.md

Base: b1a679ec (main changed from 8f95c7ca after waiting as requested).
Batch 1 production: 3c2661b267b767386114ed8cad5c6330871306a9.
Batch 2 production: 149637b28ffcb49b95ac8d177f0302c043942851; handoff b1a679ec.
Workspace: /tmp/coder-executor-batch-3; branch codex/executor-batch-3.
Spec: self-contained scope, module boundaries and action contract in plan.
Task-brief helper attempted; it cannot parse Chinese 任务 headings. Equivalent literal extraction saved each brief, including global constraints.

## Preflight
|Tasks|Shared contract/files|Assessment|
|1|Registry shapes, capability checks, resources, validator|Direct/helper tests match contract; exact helper arity includes args=[]|
|2|Types, controller, artifacts|Identity-compatible exports; single step boundary preserved|
|3|Object services and context|Production explicit services; compatibility ContextVar; recorder AST rejects dynamic code|
|4|Metrics and benchmark|Bounded aggregates, raw repeated results, no overlapping-time sums|
|5|Reachable map and CLI|full default, event optional; topology and occupancy distinguished|
|6|Compatibility and docs|Existing generated runtime verified; no fabricated missing context|
|1/2|action_plan, runtime imports|Task 1 uses current types; Task 2 migrates canonical ownership without identity change|
|1/3|Registry execution and object services|Task 1 explicit execution integration; Task 3 extracts service implementations|
|1/4|generated_plan_runtime|Metrics preserve registered dispatch and validation|
|1/5|generated_plan_runtime|Refresh CLI preserves action validation|
|1/6|Exports/docs|Task 6 documents final registry and compatibility|
|2/3|runtime facade|Object extraction follows boundary extraction|
|2/4|controller_client/runtime_artifacts|Instrument canonical service boundaries only|
|2/5|runtime/controller|Cache consumes single commit boundary|
|2/6|Exports/docs|Retain all public identities and defaults|
|3/4|Object recovery|Recovery timing added around explicit service|
|3/5|runtime|Cache does not reintroduce implicit context|
|3/6|Context compatibility docs|Document final implementation|
|4/5|Metrics, CLI, benchmark|Save full baseline before event work; comparison uses same candidate SHA|
|4/6|Benchmark checks and docs|Missing inputs nonzero, preserve raw evidence|
|5/6|Refresh defaults and docs|full remains default irrespective of measured performance|

## Progress
- Task 1: pending
- Task 2: pending
- Task 3: pending
- Task 4: pending
- Task 5: pending
- Task 6: pending
- Whole-branch review: pending
- Real acceptance: pending

Task 1: in_progress (agent /root/task1; base b1a679ec).
Baseline: bash reports/executor_batch_2/verify.sh — 581 tests OK in 18.479s, exit 0; /tmp/coder-batch3-baseline.log.
Environment: copied ignored .python-version from source; Python miniconda3-3.9-25.9.1-3, ai2thor 5.0.0. GPU read required escalation and succeeded: eight RTX 4090, driver 580.173.02.
Fixed manifest: all 12 script files copied byte-for-byte from original checkout to ignored log paths; hashes in fixture-readiness.json. Scripts append hardcoded original root, so benchmarks must set PYTHONPATH to candidate scripts/root before child launch.

Task 1: implemented commit 760a6071; review /root/review1 pending. Evidence: task-1-report.md and task-1-logs; final 581 baseline + 220 focused + 14 isolated passed. Pre-existing supervisor timing race recorded; final full run passed.

Task 1: fix round 1/5 in_progress (2 important open: recovery Close/Open skill bypass; flat direct fields discarded; fix base 760a6071).
Task 1: minor (deferred): pre-existing process supervisor SIGKILL assertion timing race, final baseline passed; do not alter unrelated supervisor in registry task.

Task 1: fix round 1/5 (2 addressed, 0 open; commits 760a6071..5c397b34).
Task 1: complete (commits b1a679ec..5c397b34, review clean).
Task 2: in_progress (base 5c397b34).

Task 2: Ruling: retain cancellation check before controller lock AND immediately before submission inside lock; interpret task single-check phrasing as a single canonical boundary, not single control.check call — batch1 explicitly requires both checks and batch3 forbids regression — cost if wrong: interface/test wording needs adjustment, without weakening cancellation safety.

Task 2: implemented 5c397b34..0f4975d0 (types1d5a434a,controller96c8ec26,artifacts49bc48ab,docs0f4975d0); review /root/review2 pending. Evidence task-2-report.md: 583 baseline,115 batch2 targeted,77 facade/registry/artifacts passed; 157 existing runtime signatures preserved.

Task 2: minor (deferred): plan_types WorldState forward annotations unresolved for typing.get_type_hints; no repository consumer; final review must triage.
Task 2: minor (deferred): exploratory nonexistent test module loader error recorded transparently; corrected intended suites passed; retain evidence.
Task 2: complete (commits 5c397b34..0f4975d0, review approved; 2 nonblocking minor observations).
Task 3: in_progress (base 0f4975d0).

Task 3: implemented 0f4975d0..80610422 (resolver c4fac22d, interactor2a7e31e0, context/recorder80610422); review /root/review3 pending. 583 baseline+216 focused incl17 new passed, runtime signatures unchanged.

Task 3: fix round 1/5 in_progress (2 important open: missing require_break_egg_target import in Egg helpers; missing raise_if_execution_aborted masks cancellation in resolver).
Task 3: minor (deferred): expected alias warning in passing logs, not a behavior failure.

Task 3: fix round 1/5 (2 addressed,0 open;80610422..b6b48685).
Task 3: complete (commits0f4975d0..b6b48685,review clean).
Task 4: in_progress (base b6b48685).

Task 4: implemented e82e20f5; review found 2 Important gates: full failed/incomplete/no-timing accepted; dirty/unknown code identity accepted. Fix deferred until live baseline exits to keep every child code identity clean.
Live baseline e82e20f5 running: fixed12*2policies*5, all code_dirty=false. Case03 FloorPlan1_task_0 all10 reject stagePhase1 robot1 cursor5 Pickup Pot mass0.57 > capacity0.08 under required Task1 capability validation. Preserve fixed workload and strict checks, report actual incomplete evaluations; do not weaken checks/change samples to force acceptance.

Live full baseline complete:120 rows,110valid,10incomplete,0timeouts/missing/unstarted; SHA e82e20f5 dirtyfalse every row. Old checker erroneously pass; original report preserved, task4fix rereads.
Before/after comparison saved full-baseline-batch2-comparison.json. Beyond required case03 mass rejection, case06 FloorPlan201_task_1 legacy all5 now partial due1:robot1:0 GoToObject NO_PLAN_FOUND, same GCR2/3 vs batch2 completed. Read-only diagnostic /root/task3 requested, no production edits until cause established.

Task 4: fix round1/5 (2important +noise minor addressed,0open;e82e20f5..57a89ef2).
Task 4: complete implementation+saved full baseline (b6b48685..57a89ef2,review clean); real acceptance FAILED due immutable case03 mass conflict. Corrected task4-gate-recheck.json exit1 authoritative; original e82 report preserved.
Task 5: in_progress (base57a89ef2).


## Same-environment old-code replay
Old SHA: b1a679ec3cdc924eb176887e9b4b916629bb3eaa
Loaded runtime: /tmp/coder-executor-batch-2/scripts/executor_system/generated_plan_runtime.py
Five diagnostic step/legacy repeats using identical original case06 script all valid/partial, GCR2/3, same1:robot1:0 GoToObject NO_PLAN_FOUND. Saved old-navigation-replay/report.json and raw child results. This establishes failure occurs without third-batch code; exact live-physics/environment trigger remains unproven. No speculative algorithm correction.

Task5 implemented21019fc0,review /root/review5 pending;130focused588baseline70finalnavigationpassed;corridor13→4queries sameactions. Plan checkbox bookkeeping may still need task6 (agent updated ignored brief only).
Live event smoke24 (fixed12*legacy*full/event*1) running clean21019fc0 output reports/executor_batch_3/21019fc0_event_smoke; no source changes until finishes. Not final240/24 acceptance.
Archive prebaseline prepared in ignored evidence-archive/full-baseline: lossless xz with originalsha verified,readable report correctedcheckfalse+originaltrue,gate-recheck,oldcomparison. Final task6 will curate artifacts, not commit480MB raw directory.

Task5 smoke24 complete exit1:22valid2knownmassinvalid0timeouts;7paired/11action regressions plusGCR/nav/perf. Source21019clean.
Task5 fixround1/5 inprogress: P1 metadata-only changes cause topologyversion/replan eachstep; carried-object12stepfake fails9 event vs12full, livecase01 same failure. Preserve query invalidation but only invalidate actually affected topology/request.
Task5 minor(deferred): cache outer controllerlock held through scheduler notify violates release-before-notify convention; no current deadlock shown; final review must triage.

Task5 fixround1 implementedb3aa308f,review5 scopedpending. Changed query-vs-topology semantics, target-candidate refresh; outerlocknotification minoralsofixed wthreads. Newfixed24smoke running cleanb3aa308f pathreports/executor_batch_3/b3aa308f_event_smoke. No tracked edits until finishes.

Task5 fixround1 reviewed: originalP1 +lockminorresolved;newP2candidate-rebuilddropsforced-current-poseexclusion. Round2requested,baseb3aa308f.
Fixedsmoke24 b3aa308f done22valid2massinvalid0timeouts;NO paired/action/GCR/navregressions. Failedlegacy executionP50 event3.673028s vsfull2.664341s (>1.05). Preserveperformancefail;defaultfull. Final240+24stillpending.

Task5 fixround1(originalP1+lockminor addressed,newP2excludedposeopen);fixround2 b3aa308f..6fd96de9 P2addressed0open.
Task5 complete implementation(commits57a89ef2..6fd96de9,reviewclean). RealperformanceNOTpassed smoke;final240/24pending.
Task6 inprogress(base6fd96de9).

Task6 implementedb87e0991;review6 needsfix3important: legacyrobotplaceholderoverwrite;importonlygeneratedno-opaccepted;requiredbenchmarkrelatedcoverageabsent. Round1requested. Finalprecommitverify583/588/310groupspassed,6compatpassed,4archivesverified.

Task6 fixround1 6fb9ad6e ordinary3findingsaddressed;re-reviewfound2Importantdeadbranchsharedmainacceptance,wholelineplaceholderdeletessemicolonstatement. Round2requested.

Task6 fixround2 ab96382f previousdeadbranch/semicolonaddressed;newImportantimportedmainaliasrebindingaccepted. Round3requested (baseab96382f),boundedbindingverification.

Task6 fixround3 59860f89 normalaliasrebindaddressed;newImportantsharedmainaliascollideswithrequiredliteralidentitybindings. Round4escalatedfreshgpt6astra implementer. Freshverify59860f89 allpass groups462/588/316 (overlap), /tmp/coder-batch3-final-verification.log.

Task6 fixround4/5 59860f89..46342de7 aliascollisionsaddressed0open;32focusedpassed,reviewapproved.
Task6: complete implementation (6fd96de9..46342de7,reviewclean);finalrealacceptancepending.
Finalwholebranchreviewinprogress baseb1a679ec head46342de7.

Freshverification46342de7 allpass462/588/317groups, compileallpassed; /tmp/coder-batch3-verification-46342de7.log. Finalreviewinprogress foundP2prefixpreimportno-opaccepted;waitfullfindingssinglefixwave.

Finalreviewdone: oneP2generatedprefixfalsepositive, P3WorldStateannotationnonblocking, otherdeferredminorstriaged. Singlecombinedfixwave dispatched task6_round4 modelgpt6astra, base46342de7. Reportfinal-review.md.

Finalfix46342de7..939be0ba P2addressed, scopedreviewapproved noimportantopen;P3annotationdeferred. Freshverify939be0ba462/588/320groupspassed,compilepassed log/tmp/coder-batch3-verification-939be0ba.log.
Finalstep240running clean939be0ba reports/executor_batch_3/939be0ba_step_comparison;session2380 logfile/tmp/coder-batch3-final-step.log. No tracked edits until step+teleport finish.

Finalstep939be0ba complete240 exit1,220valid20case03incomplete,0timeout/missing/unstarted. Othergates:case06legacyrep5pairedGCRdrop,legacyaggregateGCRdrop;bothpolicyP50fail,strictP95fail. Readonlycase06diagnosisdispatchedtask6_round4. Teleport24running939be0ba session27928 logfile/tmp/coder-batch3-final-teleport.log, reports/executor_batch_3/939be0ba_teleport_regression. No tracked edits yet.

Finalteleport939be0ba complete24 exit1,22valid2case03massinvalid,0timeout/missing/unstarted. All264 rowsverifiedcodeSHA939be0ba cleanfalse. RealacceptanceFAILED. Evidence-onlycurationtask6running aftersimulationcomplete. FinalstepP50legacyfull2.7532411166466773 event3.9181036790832877;strictfull2.3234566354658455 event3.2636138731613755;P95strictfull6.314381753094494 event6.944234422873706. LegacyGCRfull0.7045454545454546 event0.6984848484848485.
