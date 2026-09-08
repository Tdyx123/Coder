# Task 5 — optional event-driven reachable map cache

Status: implementation and deterministic verification complete; controller review and fixed-workload live comparison remain pending. Base: `57a89ef2113871d0327a31ce031eadb2d65e1acb`. Worktree `/tmp/coder-executor-batch-3`, branch `codex/executor-batch-3`. Implementation commit: `21019fc0` (`feat(executor): add optional event-driven reachable map refresh`).

## Implementation

- Added `scripts/executor_system/reachable_map.py`: canonical frozen RuntimeWorldSnapshot (the same four fields; old movement_coordinator import reexports the identical class), ReachableMapCache, navigation-only metadata fingerprint and current physical occupancy. The module does not import the coordinator.
- Event cache retains initial static candidates separately from current known topology and temporarily withheld segments. All-agent query unions confirm additions. First suspected omission triggers an immediate second all-agent query. Only points absent in both and outside every actual 0.35m robot neighborhood are removed from known topology; confirmed removals advance the version even if the point was already temporarily withheld. Unconfirmed missing points remain in static evidence but are excluded from entry; current occupied source nodes remain represented so their robots can depart under the existing geometry/reservation rules. Occupancy changes include physical subgrid x/z changes and force reevaluation of withheld segments.
- `movement_coordinator.py`: full refresh retains its original every-microstep query and historical-union behavior. Event refresh checks every agent's latest position before and after each microstep and queries all agents at batch start and at most four successful microsteps apart. Scene fingerprint/action invalidation, failures, displacement and invisibility force immediate fresh evidence. Topology/position boundaries release existing reservations and replan using the unchanged budgets. Failed directed edges remain blocked for the whole batch; a subsequent batch cannot retry before its mandatory fresh full query. No TTL reopening exists.
- `controller_client.py`: committed object/door/hand transformation actions invalidate an attached event cache under the existing publication lock, including two opposing events between cache reads. Metadata fingerprints also detect object movement, AABB/OBB/rotation, holder and instance changes. Temperature, visibility, frames and camera rotation are excluded. Cache reads/query publication share the runtime RLock. Query exceptions cancel the execution control (and parent control when distinct) and rethrow the original error, preventing stale-cache reuse.
- `movement.py` and `runtime.py`: validated `reachable_refresh_mode=full|event`, default full, full refresh interval default4 and maximum4; ThorRuntime now enables event. Grid0.25m, clearance0.35m, planning/replan/edge budgets, default step and default legacy are preserved.
- `parallel_runner.py`: added missing parent CLI option and carried it through retries, workers and durable summary mode metadata. `generated_plan_runtime.py` already had the child CLI and runtime forwarding in Task4; its existing end-to-end wiring is exercised, so no redundant source edit was necessary. Child runtime configuration serializes MovementConfig with its new interval automatically.
- Counters in runtime_metrics: `reachable_cache_hits`, `reachable_full_refreshes` (all-agent rounds, including confirmation rounds; compatibility full path also counts), `reachable_suspected_removals` and `reachable_confirmed_removals` (point events). Existing controller `reachable_queries` continues to count actual GetReachablePositions submissions separately.
- Tests: expanded programmable `GridThorRuntime` at the simulator boundary, new `tests/test_reachable_map_cache.py` (22 tests), and parent CLI integration in `tests/test_parallel_runner.py`. Tests use the real StepMovementCoordinator/planner. Existing navigation tests and acceptance thresholds were not weakened.

## RED/GREEN evidence

All test/compile commands used `/home/dwb/.pyenv/bin/pyenv exec python` at the worktree root. Active environment: `miniconda3-3.9-25.9.1-3`, Python3.9 selected by `.python-version`. Logs are in this same ignored task directory.

1. Required initial RED:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_reachable_map_cache.py`
   `task5-red.log`: 13 tests, 11 expected assertion/subtest failures, no errors. Corridor observed event13 queries vs full13 (required <=6); cancellation, query-error poisoning, second query, permanent removal and temporary occupancy exclusion also failed. Initial fixture instrumentation shell invocation used unavailable bare `python`; it was corrected with explicit pyenv before this authoritative RED run.
2. First GREEN, same command: `task5-green-first.log`, 13 tests OK.
3. Additional integration/boundary RED:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_reachable_map_cache.py tests.test_parallel_runner.ParallelRunnerCliTest.test_reachable_refresh_mode_reaches_child_through_parent_cli`
   `task5-boundaries-red.log`: 19 tests, four expected failures for passive-agent drift not invalidating a plan, previously withheld removal not advancing version, missing full-refresh counter, and missing parent CLI mode.
   Same command GREEN: `task5-boundaries-green.log`, 19 tests OK.
4. Expanded stable two-agent spacing, additions and config guard coverage plus Tasks1–4 focused verification:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_reachable_map_cache.py tests/test_action_registry.py tests/test_generation_validation.py tests/test_runtime_facade.py tests/test_runtime_context_isolation.py tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py`
   `task5-focused.log`: 130 tests OK, 2.078s. Synthetic subprocess tests only, no simulator run.
5. Required prior-batch/navigation/conditions/leases verification:
   `bash reports/executor_batch_2/verify.sh`
   `task5-batch2.log`: 588 tests OK, 19.612s. This names all existing movement, coordinator, step, geometry, navigation failure/scope suites and second-batch stage conditions/resource leases, plus evaluation, result, shutdown and process contracts.
6. Self-review RED for continuous occupancy within a snapped grid cell:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_reachable_map_cache.ReachableMapCacheTest.test_subgrid_occupancy_change_rechecks_temporary_segment`
   `task5-occupancy-red.log`: one expected failure, query count stayed6 instead of refreshing. Root cause: occupancy invalidation compared only snapped GridPoints. Fixed to compare actual x/z coordinates.
   Final relevant GREEN:
   `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_reachable_map_cache.py tests/test_movement_coordinator.py tests/test_step_movement.py tests/test_navigation_execution_scope.py tests/test_navigation_batch_failures.py`
   `task5-occupancy-green.log`: 70 tests OK, 0.576s, after the final event-cache change. No broad rerun was needed for the event-only occupancy correction; this repeats every affected navigation integration suite.
7. `python -m py_compile` via explicit pyenv for all nine changed/new Python files and `git diff --check`: exit0 after final implementation changes.

Deterministic corridor evidence: single robot executes exactly12 MoveAhead microsteps with identical full/event action sequences and endpoint `{x:3.0,y:0.0,z:0.0}`. Full queries13 times at successful counts0–12. Event queries4 times at counts `[0,4,8,12]` (69.2% fewer queries). Both report zero recorded collisions. Separate two-robot test executes24 microsteps, compares complete action sequences, verifies both endpoints and every recorded pairwise distance >=0.35m, and requires event query count <=half full. Neither test asserts timing/speedup; all step cases exclude Teleport.

## Self-review and limits

- Full's refresh body is preserved with only an added runtime counter. Its existing removal suppression and per-step query tests pass. Event is explicitly optional; no default, skill/capability, mass, workload, evaluator, lease, rendering, GPU concurrency or navigation budget change was made.
- Confirmed snapshot import identity and no reachable_map -> coordinator dependency; production runtime configuration reaches the coordinator, and real controller commit hooks invalidate event cache. Parent CLI mode is tested through the real scheduler/retry worker and a real lightweight child process, whose simulator-independent output records event.
- Confirmed complete dynamic coverage: door open/close, moved/bounded/picked/sliced objects, instance insertion/removal and inventories, temporary/confirmed/restored cells, actual subgrid occupancy changes, different per-agent query unions, failed edges and next-batch evidence, moving/passive agent displacement, unmappable position, invisible terminal candidates, cancellation even on hits, and query failure forbidding stale retry.
- Event can refresh/replan more frequently for scenes with changing navigation metadata or persistent occupancy ambiguity. Initial static evidence remains for diagnosis, but confirmed removed points cannot remain in effective planning topology. Query confirmation is bounded to a second all-agent round per refresh, and newly ambiguous omissions remain temporarily withheld. No runtime performance ratio is claimed.
- No Task6 docs or execute_plan changes. Only Task5 brief checkboxes were checked; global live acceptance remains pending.
- Existing controller-owned live baseline is untouched:120 runs,110 valid and10 required case03 mass-limit rejections, no timeouts. Corrected Task4 checker rejects that baseline; neither skills nor mass/workload were changed to force acceptance. Independent controller diagnostic established case06 legacy NO_PLAN_FOUND also occurs in old batch2 code; this task makes no speculative planning-algorithm fix.
- Controller will run/review the exact240 step comparisons and24 teleport regressions. Real correctness/performance acceptance is pending and must not be inferred from deterministic query savings.

Git staging initially hit the worktree index read-only filesystem boundary. Authorized escalation succeeded for staging/checking/commit; no automatic approval review rejection occurred. All nine implementation/test files are committed; controller-owned reports/executor_batch_3 remains untracked and untouched.

## Review round 1 — metadata refresh versus replanning, notification ordering

Review source: `task-5-review.md`; base `21019fc0`. This section supersedes the initial report's statement that all scene fingerprints advance the planning-map version or automatically cause a replan. Fix commit: `b3aa308f` (`fix(executor): replan only invalidated event navigation paths`).

P1 was reproduced on the real StepMovementCoordinator: both a carried Mug and an unrelated continuously moving Mug exhausted the unchanged max_replans=8 after9 successful microsteps on a12-step unobstructed route. A separately relocated target reached the stale3.0m candidate instead of its new0.75m candidate. Controller-owned same-commit live smoke under `reports/executor_batch_3/21019fc0_event_smoke` also reported replan-budget regressions; those logs were read only and no simulator was run by this implementer.

The fix preserves immediate all-agent queries for every navigation-relevant metadata/action change. Metadata now advances a separate scene revision; only effective reachable topology changes or confirmed removals advance RuntimeWorldSnapshot.version. The coordinator validates the remaining path/endpoint instead of spending a replan for every version/metadata event. For changed destination or interaction-target metadata it reuses ThorRuntime.build_navigation_request to regenerate target candidates with the original robot, destination, next action and phase/wave context. It updates target centers/destination metadata and retains the current trajectory when its assigned candidate remains applicable. A displaced target whose new candidates omit the current assignment invalidates the plan before further stale motion. The initial static corridor remains4 event queries versus13 full; continuous object-motion corridors still query every step, complete12 steps with zero replans and match full action sequences. No object-motion field is ignored, no planning budget is raised, and full's original refresh/failed-edge behavior remains unchanged.

P2 was reproduced independently: GetReachablePositions callbacks observed the outer controller RLock still owned at versions1 and2. ControllerClient now owns a nested, thread-local world_read_scope: it keeps atomic map/query evidence under that lock and coalesces committed-event notifications until the outer scope exits. Ordinary submissions still notify after their own unlock. A real separate thread successfully acquires the controller lock from the callback on both normal completion and an unsuccessful second query; the latter preserves version2 publication, cancellation, original query error and final notification. No scheduler/condition algorithm or cancellation rule changed.

P3: the existing slice-visibility retry test now captures its expected Tomato alias warning and asserts it, avoiding the noisy diagnostic identified by review.

Verification (explicit pyenv3.9, repository root):

- `python -m unittest tests.test_reachable_map_cache.ReachableMapCacheTest.test_continuous_object_motion_refreshes_without_replanning_unchanged_route tests.test_reachable_map_cache.ReachableMapCacheTest.test_target_relocation_rebuilds_candidates_before_following_stale_route tests.test_reachable_map_cache.ReachableMapCacheTest.test_changed_target_retains_path_when_current_candidate_is_still_valid`
  RED `task5-review-red.log`:3 tests, one wrong-endpoint assertion and three expected product replan-budget exceptions (carried/noncarried subcases plus moving target), not loader/fixture errors.
- `python -m unittest tests/test_reachable_map_cache.py`
  GREEN `task5-review-green.log`:25 tests OK,0.423s. Old metadata-version assertions were changed to require unchanged versions when fresh query topology is unchanged; immediate-query assertions remain. Existing real-removal/version/occupancy tests remain intact. One intermediate run identified an unset programmable candidate list in the existing door-change fixture; the fixture now supplies its original candidates.
- `python -m unittest tests.test_reachable_map_cache.ReachableMapCacheTest.test_all_agent_query_notifications_happen_after_outer_controller_unlock`
  RED `task5-notify-red.log`: callback observed owned locks `[(True,1),(True,2)]`.
- `python -m unittest tests/test_reachable_map_cache.py tests/test_world_snapshot.py tests/test_stage_scheduler.py tests/test_action_resource_leases.py tests/test_stage_conditions.py`
  GREEN `task5-notify-green.log`:129 tests OK,3.784s. Includes genuine changed-path removal and unrelated confirmed removal that retains a valid path.
- `python -m unittest tests/test_reachable_map_cache.py tests/test_movement_coordinator.py tests/test_step_movement.py tests/test_navigation_execution_scope.py tests/test_navigation_batch_failures.py tests/test_movement_config.py tests/test_movement_strategies.py tests/test_executor_retry_policy.py tests/test_runtime_metrics.py tests/test_runtime_facade.py`
  GREEN `task5-review-navigation.log`:158 tests OK,0.804s; expected Tomato alias warning is captured. No broad588-test repeat or live run was needed.
- Parent-requested strengthened callback test (actual independent lock-acquiring thread plus committed failure path):
  `python -m unittest tests.test_reachable_map_cache.ReachableMapCacheTest.test_all_agent_query_notifications_happen_after_outer_controller_unlock tests.test_reachable_map_cache.ReachableMapCacheTest.test_failed_query_still_notifies_after_outer_controller_unlock`
  GREEN `task5-notify-thread-green.log`:2 tests OK,0.340s. The earlier owner-state check was replaced by this actual thread test; it adds one new exceptional-path test, bringing the cache file to28 tests.
- Explicit pyenv `python -m py_compile` on all six changed Python files; `git diff --check`: exit0.

Tracked `docs/superpowers/plans/2026-09-07-executor-batch-3-architecture-performance.md` now has Task5's ten checkboxes marked. Its other tasks and real-acceptance checkboxes remain unchanged/unchecked. No Task6 or execution-plan implementation work was added. Controller must review and smoke the new commit before the fixed240 step/24 teleport comparisons. Live correctness/performance remains pending; defaults remain full/step/legacy.

## Review round 2 — preserve explicit reposition constraints

Review source: `task-5-rereview-1.md`; base `b3aa308f`. Fix commit: `6fd96de9` (`fix(executor): retain explicit pose exclusions across replanning`).

The review probe was reproduced through the actual `ThorRuntime.navigate_to_object(..., exclude_current_position=True)` entrypoint and real StepMovementCoordinator, using GridThorRuntime only for scene/query/candidate responses. A target change between the runtime's original filtering and the coordinator's first plan regenerated x=0 as an eligible endpoint, so the old event path completed without moving. Two allowed-target cases (x=0.25 and a new x=0.5 not present in the original candidate list) both incorrectly finished at x=0; a regenerated list containing only x=0 also incorrectly succeeded.

The fix stores a durable immutable `NavigationRequest.excluded_pose_keys` constraint with a backward-compatible empty default. The existing runtime option records the explicitly excluded current grid pose as well as filtering its initial candidates. The coordinator preserves that constraint when rebuilding target candidates, filters the regenerated candidates by it, and independently checks it during candidate assignment. Ordinary target relocation may still introduce entirely new candidates; the old candidate list is not frozen. When all regenerated candidates are explicitly forbidden, existing NoInteractionPoseError handling fails safely. Temporary visibility exclusions remain separate. No public ThorRuntime method signature, full-refresh behavior, grid, spacing, budget, cancellation or default was changed.

RED:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
 tests.test_reachable_map_cache.ReachableMapCacheTest.test_reposition_exclusion_survives_target_change_before_first_plan \
 tests.test_reachable_map_cache.ReachableMapCacheTest.test_reposition_cannot_accept_only_regenerated_forbidden_pose
```

`task5-exclusion-red.log`:2 tests,3 expected assertion/subtest failures, no fixture errors. Both valid destinations incorrectly stayed at0, and the forbidden-only case raised no error.

GREEN:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
 tests/test_reachable_map_cache.py tests/test_navigation_execution_scope.py \
 tests/test_executor_retry_policy.py tests/test_movement_config.py \
 tests/test_movement_strategies.py tests/test_movement_coordinator.py tests/test_step_movement.py
```

`task5-exclusion-green.log`:128 tests OK,0.550s. The new cases require actual one/two-microstep movement to0.25/0.5 with no Teleport, and require failure when only the excluded origin remains. This also repeats actual Slice recovery/scope, target-relocation, request/config compatibility and navigation suites. Explicit pyenv `python -m py_compile` on the four changed Python files and `git diff --check` passed. No broad rerun or new simulator smoke was performed.

Controller-owned live smoke read-only evidence: `reports/executor_batch_3/b3aa308f_event_smoke/report.json` has24 results,2 incomplete evaluations,0 timeouts/missing/unstarted results, and check_passed=false. Controller reports functional results match full after round1, with the two known case03 mass-limit failures retained. The saved checker also rejects event execution median3.6730282506905496s versus full2.6643414504360408s (allowed ratio1.05). Event therefore has **not passed performance acceptance** and remains explicitly optional; full remains default. No threshold or workload change was made. This bounded constraint fix does not require another24-run smoke per controller instruction; final240 step/24 teleport comparisons remain controller-owned and pending.
