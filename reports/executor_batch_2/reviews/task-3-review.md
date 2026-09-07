# Task 3 review

Reviewed base `22fa6601` → head `a9449229` against `task-3-brief.md` and `task-3-report.md`.

**Spec verdict: changes required.** Main-thread admission, admitted navigation membership, the three synchronization modes, shared snapshots, cancellation checks, and temporary failed-Outcome wrapper behavior are implemented within the stated Task 3 boundary. Bounded shutdown and waiting-age semantics have actionable gaps.

**Quality verdict: changes required.** Three findings below. Resource leases/holders, expected conditions, stage/global condition policy, wrapper outcome integration, and report unification remain accepted Task 4/5 work and are not counted as findings.

## Findings

1. **[P1] Keep idle Pass submission under bounded supervision** — `scripts/executor_system/stage_scheduler.py:281-282`.

   `_drive` calls `runtime.step` synchronously on the task driver. If the simulator call hangs, the driver cannot return to `control.check()` or enter `run_workers`' bounded shutdown. Workers can stop, but the task remains hung and the runtime remains apparently reusable. This regresses the inherited guarantee for controller calls: an in-flight call may finish, but an unquiescent call must not prevent bounded task exit and runtime invalidation. Have the coordinator authorize the tick while a supervised execution target performs the potentially blocking call; include that target in actual-exit/quiescence accounting and propagate the stage control. The driver must remain able to enforce the deadline and shutdown budget.

   Focused bounded probe: a single condition-false action, root deadline 80 ms, patched shutdown allowance 30 ms, and an Event-blocked `runtime.step`. After Pass entered and a further 200 ms join, the scheduler thread was still alive and `runtime.reusable` remained true. Releasing the Event then allowed `PlanExecutionTimeout`. The probe always released/joined its target; no reported suite was rerun.

2. **[P2] Age waiting admissions during active scheduling rounds** — `scripts/executor_system/stage_scheduler.py:277-287`.

   `wait_rounds` is incremented exclusively inside the idle-Pass branch, which only runs when no action is in flight. A robot that waits while peers continually receive/finish actions retains age zero regardless of how many admission rounds it has waited. The required priority formula therefore does not provide its specified waiting-age adjustment during the normal active schedule, and Task 4 resource arbitration would inherit starvation when a competing robot continually wins the same resource. Advance admission age once per actual scheduling round for admissions still waiting, independently of idle simulation ticks; preserve the separate Pass-only `timeout_ticks` contract and clear age on admission.

   Focused probe: robot1 waits for robot2's fourth action; four robot2 actions complete in successive END-policy admissions. The stage completes, but all nine condition/admission observations of robot1's `wait_rounds` are `0`. This is not a thread-order issue.

3. **[P2] Retire every participant after an already-aborted admitted wave** — `scripts/executor_system/executor.py:350-357`.

   The first pre-submission navigation failure sets `navigation_complete` and departs its member. If another member also fails while constructing its request, its `abort_action_wave` returns at `navigation_complete` without recording departure. No other scheduler path departs that member. With the new `_admitted_waves` map, the finished wave and its exceptions/request state therefore stay live for the remainder of the stage; repeated recoverable navigation failures accumulate retained waves. Preserve the first root error, but ensure every admitted participant is marked departed exactly once even when the wave was already aborted, so the final participant removes the map entry.

   Focused probe: admit two GoToObject members, call `abort_action_wave` for member 0 and member 1. Both actions have failed before submission, yet `_admitted_waves` still contains wave 1 and its departed set is only `{0}`.

## Review evidence and scope

- Read the full submitted diff and the implementation report; inspected unchanged code only for the named navigation-lock, worker-shutdown, control-propagation, and action-failure paths.
- Confirmed actual step navigation collects the wave before `StepMovementCoordinator.execute_batch` acquires the runtime navigation execution lock. Waiting robots are excluded from admitted members and not marked done solely for waiting.
- Reused reported test evidence without rerunning those suites. Ran only the three bounded, isolated probes described above, with `/home/dwb/.pyenv/bin/pyenv exec python` and bytecode writes disabled.
- No source/test edits, no subagents, no broad repository crawl.

## Fix round 1 re-review — `14c1791c`

Scope: read the full `a9449229..14c1791c` fix diff and appended RED/GREEN report. Rechecked the three previous findings and regressions arising from these edits only; no suites, extra probes, subagents, or source changes.

**Spec verdict: approved within the agreed Task 3 boundary. Quality verdict: approved. No remaining actionable findings in this scoped re-review.**

- **Finding 1 closed:** idle Pass is now a coordinator-issued internal permit handled by an existing supervised robot target. The driver remains responsive while `_tick_inflight` prevents overlapping admission/snapshot capture. The tick runs under child control and does not count as a plan action. `run_workers` already supervises the actual target exit, so a blocked tick is covered by shutdown timeout and runtime invalidation. The new regression checks the deadline, bounded task return, nonquiescence, and unusable runtime and releases/joins its blocked target afterward.
- **Finding 2 closed:** waiting age increments at admission evaluation rounds, independently of completed idle-Pass `wait_ticks`; age still clears on admission. The regression observes age accumulating across peer progress while timeout ticks remain zero.
- **Finding 3 closed:** aborting an already-complete navigation wave now departs each remaining member while preserving the first root error. The final departure retires the map entry; duplicate aborts remain harmless. The regression verifies retirement, both departures, first-error identity, and idempotence.
- Reviewed tick-result handling, notification consumption, terminal worker mailboxes, and wave departure behavior for regressions introduced by this fix. No additional actionable defect identified.

Validation relied on the reported three-test RED→GREEN sequence and 122-test passing focused coverage; these were not rerun during review. Task 4/5 exclusions above still apply.
