# Task 2 report — canonical types, controller boundary, and runtime artifacts

Status: implemented, self-reviewed, verified, and committed; awaiting controller review.
Worktree: `/tmp/coder-executor-batch-3`; branch: `codex/executor-batch-3`.
Task base: `5c397b34` (reviewed Task 1 plus recovery-capability/flat-payload fixes).
Batch 1 production: `3c2661b267b767386114ed8cad5c6330871306a9`; acceptance `8f95c7ca092d7025a5ca67def5015daa56f6c568`.
Batch 2 production: `149637b28ffcb49b95ac8d177f0302c043942851`; handoff `b1a679ec3cdc924eb176887e9b4b916629bb3eaa`.
Only Task 2's seven checkboxes were updated in the tracked batch-3 plan and local task brief. No subagents/worktrees were created; no simulator/live experiments or all-repository discovery were run.

## Responsibility commits

1. `1d5a434a` — Extract canonical executor plan types with compatible imports.
2. `96c8ec26` — Extract serialized controller client behind runtime facade.
3. `49bc48ab` — Extract runtime artifacts with explicit media dependencies.
4. `0f4975d0` — Document completed executor boundary extraction checks (only Task 2 checkboxes).

## Implementation and boundaries

- `plan_types.py` owns `Action`, `PlannedAction`, `StagePlan`, `MultiStageActionPlan`/`TaskPlan`, `RobotExecutionState`, `ActionResult`, `ResourceRequest`, related constants, and the pure retry classification helper. It imports only `dataclasses` and `typing`. `DIRECT_PAYLOAD_FIELDS` moved here with the types; registry imports/re-exports that same constant. `Action.from_any` no longer lazily imports the registry. `action_plan` and existing compatibility modules export identical class objects, while production executor-system modules import foundational types from `plan_types` directly. Existing behavior-bearing runner/adapter/world-state classes remain in their existing modules.
- `ControllerClient` owns the sole controller submission algorithm, cancellation checkpoints, lock boundary, state-version publication, committed holding evidence, scheduler notification, and controller stop ownership. Runtime exposes thin `_step_direct`, `_commit_world_event`, and `stop` delegates with unchanged signatures/defaults. The client reads the injected runtime's current `controller` and lock rather than copying controller/event state; replacing the runtime's controller continues to work. Object identity/transformation algorithms remain runtime hooks for Task 3.
- The parent explicitly confirmed that the task phrase “one cancellation check/state commit” means one canonical boundary implementation. Both existing cancellation checks are retained: before lock acquisition and immediately before submission under the lock. Deleting either would regress cancellation safety. One returned event increments the version once, including failed action events; commit/frame exceptions cannot undo the published version, and notification remains outside the controller lock.
- `RuntimeArtifacts` owns output-root allocation, preparation/cleanup, frame and third-party frame handling, metadata, video generation, and preview closure. It consumes the existing owned-child output isolation. Runtime injects callable providers for `cv2`, frame conversion, metadata configuration, and logging, so late patches to `runtime.cv2`, `runtime.event_cv2_frame`, `runtime.GENERATE_METADATA`, and `runtime.log` still apply after service creation. Runtime retains `shutil`/`subprocess` compatibility module references, so existing `runtime.shutil.which`/`runtime.subprocess.run` patch paths remain effective. `runtime.Controller` construction stays in the assembly facade with identical settings.
- Runtime initializes both services normally; narrow lazy getters support existing `object.__new__(ThorRuntime)` fixtures/callers without separate implementations. The legacy central/synchronous executor aliases remain identical to `Executor`; their module documentation explicitly states that no central worker thread exists.
- No movement algorithm, controller render/resolution configuration, scheduling/resource/condition policy, evaluation/result metric semantics, global helper context, object resolution/recovery algorithm, or refresh/metrics feature was changed.

## Files

New production files:
- `scripts/executor_system/plan_types.py`
- `scripts/executor_system/controller_client.py`
- `scripts/executor_system/runtime_artifacts.py`

Runtime and compatibility/type imports:
- `scripts/executor_system/runtime.py`
- `scripts/executor_system/action_plan.py`
- `scripts/executor_system/action_registry.py`
- `scripts/executor_system/actions.py`
- `scripts/executor_system/executor.py`
- `scripts/executor_system/generated_plan_runtime.py`
- `scripts/executor_system/movement.py`
- `scripts/executor_system/parallel_runner.py`
- `scripts/executor_system/pddlrun_adapter.py`
- `scripts/executor_system/resource_inferencer.py`
- `scripts/executor_system/stage_runner.py`
- `scripts/executor_system/stage_scheduler.py`
- `scripts/executor_system/task_plan.py`
- `scripts/executor_system/central_executor.py`
- `scripts/executor_system/synchronous_executor.py`

Tests/documentation:
- `tests/test_runtime_facade.py` (13 tests)
- `docs/superpowers/plans/2026-09-07-executor-batch-3-architecture-performance.md` (only Task 2 checkboxes)
- Local ignored report, task brief, and evidence logs under `.superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/`.

## RED/GREEN evidence

All commands ran from `/tmp/coder-executor-batch-3` with `/home/dwb/.pyenv/bin/pyenv exec python`; the prescribed script also uses that exact interpreter. Active environment: `miniconda3-3.9-25.9.1-3` from the worktree `.python-version`.
Logs are in `.superpowers/sdd/2026-09-07-executor-batch-3-architecture-performance/task-2-logs/`.

1. Baseline command: `bash reports/executor_batch_2/verify.sh`.
   Result: **583 tests, OK** (`baseline.log`).
2. Initial required RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py`.
   Result: **11 tests, 1 failure and 10 errors**, all missing canonical service/type imports; subprocess purity check failed because `plan_types` did not exist (`initial-red.log`). Tests were written before extraction; only each responsibility's tests were included in that responsibility's green commit. The metadata failure test was corrected before artifact implementation to exercise actual existing result finalization rather than assuming `close_runtime` writes metadata; it does not.
3. Type GREEN: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py tests/test_action_registry.py`.
   Result: **26 tests, OK** (`types-green-final.log`). `bash reports/executor_batch_2/verify.sh`: **583 tests, OK** (`types-regression.log`).
4. Controller RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py`.
   Result: **8 tests, 6 errors**, missing `controller_client` (`controller-red.log`).
5. Controller GREEN: same facade command: **8 tests, OK** (`controller-green.log`). Facade + registry: **32 tests, OK** (`controller-task1.log`). Full prescribed script: **583 tests, OK** (`controller-regression.log`).
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py tests/test_world_snapshot.py tests/test_execution_shutdown.py`: **43 tests, OK** (`controller-final.log`).
6. Artifacts RED: `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py`.
   Result: **13 tests, 5 errors**, missing `runtime_artifacts` (`artifacts-red.log`).
7. Artifacts GREEN: same facade command: **13 tests, OK** (`artifacts-green.log`). Full prescribed script: **583 tests, OK** (`artifacts-regression.log`).
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py tests/test_runtime_metadata.py tests/test_runtime_output_isolation.py tests/test_runtime_third_party_views.py tests/test_action_registry.py tests/test_generation_validation.py`: **77 tests, OK** (`artifacts-final.log`).
8. Final Task 2 / batch 2 focused verification:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py tests/test_stage_scheduler.py tests/test_action_resource_leases.py tests/test_world_snapshot.py tests/test_stage_conditions.py`
   Result: **115 tests, OK** (`final-batch2-focused.log`).
9. Final full prescribed script on completed code: `bash reports/executor_batch_2/verify.sh`.
   Result: **583 tests, OK** (`final-regression.log`).
10. Compile command: `/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/plan_types.py scripts/executor_system/controller_client.py scripts/executor_system/runtime_artifacts.py scripts/executor_system/runtime.py scripts/executor_system/action_plan.py tests/test_runtime_facade.py`.
    Result: exit **0**. `git diff --check`: exit **0**.

One exploratory test command mistakenly included nonexistent `tests/test_capability_checks.py` and produced a test-loader error (`types-task1.log`). The actual Task 1 capability tests live in `test_generation_validation.py`; the corrected commands above passed. This was not a product regression and no test or validation rule was disabled.

## Self-review

- Compared AST signatures against task base `5c397b34`: all **157** original `ThorRuntime` method signatures, defaults, return annotations, and decorators are unchanged. `signature-review.log` records the result.
- Confirmed `plan_types` imports only standard-library `dataclasses`/`typing`, including method-local imports. A fresh interpreter parses both a direct flat action and a complete plan without loading registry/runtime/legacy action facade/CLI modules.
- Compared the extracted algorithms against base: controller event publication, exception cancellation, holding evidence, lock ordering, and navigation action accounting moved without algorithm changes. There is one `controller.step` submission implementation, in `controller_client.py`, and none in `runtime.py`.
- Confirmed service accesses the current injected controller; returned unsuccessful events still commit before raising. Concurrency tests use actual threads and an observed real RLock to guarantee the second worker reached lock acquisition while the first submission remains blocked. Cancellation during that wait records zero submissions and zero version changes. The repeat-stop test includes a throwing controller to prove it is not retried after partial shutdown.
- Media tests create real owned directories and files, preserve another runtime's frames, validate exact existing ffmpeg frame-rate/input/pixel-format/output arguments with an injected external encoder, and verify late frame/metadata dependency patches. The error path produces a real metadata filesystem error and a throwing controller stop, then checks that existing finalization still writes the failed result plus cleanup evidence to disk. Result persistence remains in its existing result/finalization modules, not duplicated in RuntimeArtifacts.
- Confirmed existing Controller factory patch and old `__new__` test fixtures through the existing rendering/output/snapshot/shutdown regressions. Legacy import identity and executor aliases remain tested.
- Only the new facade test file changed; no existing tests/fakes were weakened. Task 1 registered dispatch, shape preservation, capabilities, resource preparation, and recovery gates were retained. No Task 3 or Task 4 work was started.

## Concerns / limits

No known blocking implementation issues. Tests are deterministic fake-controller and filesystem/encoder-boundary checks; they do not claim live AI2-THOR, real OpenCV image encoding, ffmpeg execution, GPU, or performance acceptance. Those remain later batch responsibilities. The worktree and branch are intentionally retained for controller review.
