# Task 3 — 主线程阶段调度交付

提交：`a9449229`（报告位于本地被忽略的 .superpowers 交付目录）。

## 基线与范围

- 实施基线：`22fa6601`。已验收 Task 1：`f25f0ec1` / `317fb232`；Task 2：`c8a1d712` / `22fa6601`。
- 新增 `StageScheduler`、`PendingAction`、`RobotAdmission`、`StageOutcome`。
- 不修改导航预算、网格、移动配置或隐式传送行为。没有子代理，没有 full unittest discover。
- 采用 executing-plans、test-driven-development、verification-before-completion；在已授权隔离工作树直接实施和自审。

## 实现

- `run_workers(..., drive=...)` 在启动每机器人固定工作线程后，由调用线程执行调度循环；复用原有取消、异常优先级、共享 shutdown 截止时间、真实 target-exit 证据与不可复用运行时保护。
- 两个阶段入口都调用 `StageScheduler.run()`；工作线程仍经原 `Executor.execute` / `TolerantExecutor.execute` 和 `execute_action`，保留公共兼容及错误注入接口。
- 主线程读取一份共享快照、判定 wait 条件、稳定排序、执行空资源准入、冻结导航成员、发放动作、处理结果和游标。工作线程仅执行获准动作，使用现有 child control 动作作用域；adapter 不在工作线程重复检查已经获准的条件。
- 就绪排序：`(-base_priority - wait_rounds * 10 - (50 if critical else 0), robot_id, cursor)`。获准才清等待年龄；延期保持原动作和游标，不消耗动作自动重试。
- `PhaseCoordinator.admit_wave` / `wait_admitted_navigation` 与独立波次状态映射允许晚到波次和前一批高层动作重叠。单独调用的旧 `before_action` API 继续可用。联合导航收齐请求后才执行现有 batch，仍保留完整批结果分区检查。
- 仅获准 GoToObject 属于 step 联合导航成员；同波次非导航动作等导航结束。等待机器人保留为物理障碍，只有真实终态机器人进入 completed 集合。
- 三种同步策略：END 可立即申请下一动作；EACH_STEP 等当前获准集合全部完成；EVENT 与 END 并发相同，但仅在世界通知、结果/资源释放、空闲调度 tick 后重评条件。通知在 condition 上合并，不持 controller 锁等待线程。
- 全部等待时仅协调器每至少 50ms 提交一次 Pass，并累积等待 tick；截止时间保存 `runtime.scheduler_diagnostics`（机器人状态、游标、动作、等待原因、年龄、世界版本、资源持有者占位）。
- `SnapshotStore.capture` 改为每 50ms 可取消的锁获取，避免主线程 admission 卡在长 controller 调用上而无法进入有界 shutdown。
- `StageScheduler.run` 正常返回 completed/partial StageOutcome；策略性阶段失败在停止工作线程后返回 failed StageOutcome，并用未取消的根控制器获取最终快照。基础设施故障、超时、中断保持原异常传播。子阶段取消不取消根。
- 修正旧导航 standalone executor 测试 fixture：补齐 Task 2 所需 controller lock、多机器人 metadata、robot map 和状态版本；未绕过快照校验。

## TDD 与验证证据

所有 Python 都通过 `/home/dwb/.pyenv/bin/pyenv exec python`。原始输出保留在 `/tmp/task3-red*.txt`、`/tmp/task3-green*.txt`、`/tmp/task3-extra.txt`。

### RED 1：核心互等、同步顺序、诊断

命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_scheduler.py
```

结果（`/tmp/task3-red.txt`）：

```text
FAIL test_waiting_robot_does_not_block_dependency (mode='step')
  AssertionError: True is not false  # report timed_out
FAIL test_synchronization_policies_have_observable_order (policy='BARRIER_AT_STAGE_END')
  AssertionError: 4 not less than 3
FAIL test_synchronization_policies_have_observable_order (policy='EVENT_CONDITION')
  AssertionError: 4 not less than 3
ERROR test_idle_dependency_timeout_saves_diagnostics_and_rate_limits_pass
  AttributeError: 'FakeRuntime' object has no attribute 'scheduler_diagnostics'
Ran 3 tests in 0.911s
FAILED (failures=3, errors=1)
```

Teleport 的同一依赖回归原本通过，step 原实现超时；修改后两种模式都通过。初次 GREEN：`Ran 3 tests in 0.338s / OK`。

### RED 2：主线程等待快照锁无法按时退出

同一调度套件新增测试后（`/tmp/task3-red2.txt`）：

```text
FAIL test_snapshot_lock_wait_respects_deadline
  AssertionError: 0.25025315396487713 not less than 0.15
```

此次另一个延期测试失败是测试构造 `NavigationBatchResult` 使用了错误位置参数，修正为字段关键字后测试通过，不将该失败算实现 RED。

### RED 3：工作线程重复执行 callback

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_scheduler.StageSchedulerTest.test_conditions_and_admission_order_belong_to_scheduler_thread
```

`/tmp/task3-red3.txt`：`Ran 1 test in 0.004s / FAILED (failures=1)`。断言发现 callback 线程集合多出三个 worker ident。修改 adapter 对 scheduler admission 的识别后通过。

### RED 4：StageOutcome 策略失败接口

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_scheduler.StageSchedulerTest.test_policy_stage_failure_returns_outcome_and_leaves_parent_live
```

`/tmp/task3-red4.txt`：`Ran 1 test in 0.054s / FAILED (errors=1)`，实际抛 `StageFailureDecisionError: Stage fail failed on robot1 Wait: action rejected`。加入已停机后的失败结果转换后通过。

### 最终 GREEN

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_scheduler.py tests/test_movement_coordinator.py tests/test_navigation_batch_failures.py tests/test_navigation_execution_scope.py tests/test_multi_robot_avoidance.py
```

真实退出码 0；`/tmp/task3-green-main-final.txt`：

```text
Ran 88 tests in 1.851s
OK
```

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execution_shutdown.py tests/test_execution_policy.py tests/test_world_snapshot.py tests/test_parallel_runner.py
```

`/tmp/task3-green-affected.txt`：

```text
Ran 121 tests in 7.947s
OK
```

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_scheduler.py tests/test_action_plan_pre_task.py
```

`/tmp/task3-extra.txt`：

```text
Ran 20 tests in 0.995s
OK
```

这里包括 15 项调度测试及 5 项 pre-task 测试；与前面的调度套件有重叠，不将重叠数另计。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/stage_scheduler.py scripts/executor_system/execution_control.py scripts/executor_system/execution_policy.py scripts/executor_system/executor.py scripts/executor_system/action_plan.py scripts/executor_system/parallel_runner.py scripts/executor_system/world_snapshot.py tests/test_stage_scheduler.py tests/test_navigation_batch_failures.py
```

退出码 0、无输出。`git diff --check` 通过。

## Task 4 接口与 Task 5 边界

- `StageScheduler._admit_resources(pending) -> bool`：主线程按稳定优先级逐项调用；当前无资源、总是 True。Task 4 在此进行原子 lease 请求，并落实冲突策略。`_dispatch` 返回 False 时 admission 为 WAITING_RESOURCE，原 pending 不丢失。
- `_release_resources(pending)`：每个动作结果（包括失败/延期）调用；finally 仅在运行时已证明 quiescent 时清理未完成 admission。租约清理应幂等；不可对仍活跃的不可复用运行时提前释放资源。
- `notify_world_changed()`：可用于资源释放唤醒；coalesced 世界通知 hook 安装于 `runtime.stage_scheduler`，退出恢复此前值。
- `admissions`、`wait_rounds`、`executors[robot].state` 提供诊断和资源策略状态；`_diagnose()` 的 `resource_holders` 当前为空，Task 4 填充持有者。
- `coordinator.admit_wave({agent_id: (action_type, cursor)})` 冻结本次获准动作；只应在所有资源准入决定之后调用。
- **Task 5 待接续**：完整 expected_preconditions/effects、阶段开始跳过/阶段末与全局条件、统一普通/容错报告、统一失败处理与旧队列循环去重、scheduler_version 报告字段。当前 `StageRunner` / `TolerantStageRunner` 在 failed Outcome 上临时重抛 StageFailureDecisionError，保持 Task 1 已验收入口行为；Task 5 应按 outcome.continue_task 应用阶段策略，不能把基础设施异常吞成正常失败。
- 当前 StageOutcome.partial 仅表示本阶段有终结动作失败；完整 stage policy 汇总交给 Task 5。

## 自审

检查了主线程无高层动作、获准动作完整快照、controller 锁等待可取消、取消后不能发新波次、波次错误通知、实际 target-exit shutdown、严格子阶段不污染根、原 standalone PhaseCoordinator API、测试有界 Event/join、等待 tick 频率、重复通知不忙轮询、资源释放在 quiescence 后执行。未改变导航预算或生成脚本。

## Review fix round 1

修复提交：`14c1791c`。

针对 `task-3-review.md` 的三个发现完成有界、可重复回归；使用 receiving-code-review 先核实代码与失败证据，再修复。

1. **P1 空闲 Pass 纳入有界线程监督**：协调器不再同步调用潜在阻塞的 `runtime.step`，改为给现有机器人工作线程发送内部 `_IDLE_TICK` 许可。该目标已经包含在 `run_workers` 的 target-exit/quiescence 证明中，不创建额外 daemon。工作线程在阶段 child control 作用域提交 Pass；成功后只发送内部 tick 完成信号，不推进动作游标、不计动作 ledger。主调度线程在 tick 执行期间继续检查截止时间，可进入共享 shutdown 预算；卡死 tick 会使运行时不可复用。
2. **P2 等待年龄与等待超时分离**：每个实际条件/资源准入轮开始时给仍 WAITING 的 admission 增加一次 `wait_rounds`；它不依赖是否有同伴动作在执行。获准时仍清零。`wait_ticks` 仅在监督中的空闲 Pass 完成后增加；同伴连续四个动作不会消耗 timeout_ticks。
3. **P2 已失败波次成员清理**：`abort_action_wave` 在波次已结束导航后仍记录尚未退出成员的 departure。只由第一次失败设根错误，后续成员失败不覆盖；最后成员退出即删除 `_admitted_waves` 条目，重复 abort 幂等。

这些修复替代上文初版对空闲 tick 提交和等待年龄实现的描述。Task 4 资源钩子及 Task 5 交付边界保持原样。

### RED

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_scheduler.StageSchedulerTest.test_idle_pass_is_supervised_with_bounded_shutdown tests.test_stage_scheduler.StageSchedulerTest.test_waiting_age_advances_during_peer_actions_without_timeout_ticks tests.test_stage_scheduler.StageSchedulerTest.test_already_aborted_wave_retires_all_members_preserving_first_error
```

退出码 1；原始输出 `/tmp/task3-round1-red.txt`：

```text
FAIL test_idle_pass_is_supervised_with_bounded_shutdown
AssertionError: True is not false : blocked idle tick prevented bounded task exit
FAIL test_waiting_age_advances_during_peer_actions_without_timeout_ticks
AssertionError: 0 not greater than or equal to 3
FAIL test_already_aborted_wave_retires_all_members_preserving_first_error
AssertionError: 1 unexpectedly found in admitted wave map (departed_agent_ids={0})
Ran 3 tests in 0.308s
FAILED (failures=3)
```

阻塞 Pass 回归使用 80ms 任务截止时间、30ms shutdown 预算、250ms 有界 join；finally 必定释放 Event 并 join 所有实际目标，避免遗留线程。

### GREEN

上述完全相同的三个测试命令退出码 0；`/tmp/task3-round1-green-focused.txt`：

```text
Ran 3 tests in 0.142s
OK
```

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_scheduler.py tests/test_execution_shutdown.py tests/test_navigation_batch_failures.py tests/test_parallel_runner.py
```

退出码 0；`/tmp/task3-round1-green-suites.txt`：

```text
Ran 122 tests in 9.110s
OK
```

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/stage_scheduler.py scripts/executor_system/executor.py tests/test_stage_scheduler.py
```

退出码 0，无输出。`git diff --check` 通过。未运行 broad discover，未启动子代理。
