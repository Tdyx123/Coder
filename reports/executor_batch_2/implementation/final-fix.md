# 整批最终统一修复报告

工作树 `/tmp/coder-executor-batch-2`，分支 `codex/executor-batch-2`；修复基线 `8a4d97a74cd6b1691cfb7d2210b0103c4c2f1718`。本轮一次处理 final-review.md 的 F1–F4，使用 systematic-debugging、test-driven-development、verification-before-completion；未启动子代理，未 merge/push。提交 SHA 见末尾。

## Finding 裁定与实现

- **F1 已修复。** 根因是统一调度只推进私有 `_tick`，所有公开 WorldState 仍为 0。`_observe_result` 在首次确认动作终态时推进 `scheduler.world.tick`；每个 worker 的准入消息包含当时共享 tick，失败复核同步协调器当前 tick。StageRunner、独立 Executor 及共享 PhaseCoordinator 的返回 WorldState 同步最终 tick；TaskRunner/global callback 使用最后执行阶段的 world。
- **F2 已修复。** 根因是 Executor 的 retry/wait_retry 分支直接 `wait_ticks += 1`。移除该增加，仍仅由 `_receive_results` 消费已完成 idle Pass 时增加条件/资源等待预算；retry_number、墙钟 retry_after 及准入等待年龄保持独立，既有动作终态重置不变。没有调整重试资格、次数上限或导航语义。
- **F3 已修复。** 根因是准入失败只更新 ActionLedger，遗漏 `attempt_counts`。新增统一 `_record_attempt`，用于 worker 获准、资源冲突 FAIL_STAGE、资源解析/等待失败、条件等待超时这四条记账路径；`_observe_result` 将权威累计值同步至 ActionResult 和序列化动作/尝试结果。已获准后再发生准入失败、延期后连续准入失败均保持总账/逐动作一致。失败仍按原定义算一次准入 attempt；没有改变计划动作数、目标评分分母，也没有给未执行尾项或初始/冲突 SKIP 增加 attempt。
- **F4 已补齐并验证。** 这是持久验收测试缺口，基线产品 probe 和新增正向测试均能正确取消，故没有为它修改取消生产代码。新增具名 `test_no_deadline_dependency_cycle_external_watchdog_cleans_workers`，step/teleport 都使用两个机器人相互等待对方完成 Event 的显式依赖环，`install_control(runtime, None)` 且 child 同样无 deadline。独立 watchdog 等待首个 Pass（最多 .5s）后取消 root，外层 join 最多 1s。测试断言 ExecutionCancelled、两机器人 WAITING_CONDITION/cursor0 诊断、零高层动作执行与 attempts、tick0、quiescence、空租约和两个真实 worker 已退出。finally 无论断言是否通过，都取消 root/child、唤醒协调器、投递停止消息，并在共享 1s 清理预算内 join task/watchdog/所有捕获的真实 worker。

## 材料语义裁定（root 已确认）

公开 `WorldState.tick` 定义为**本阶段全部机器人已由协调器确认的高层动作终态数**：成功、最终失败、资源冲突 SKIP 各增加一次。重试、延期、idle Pass、实际底层提交、未执行尾项及整阶段初始条件满足的 skip 不增加。每阶段从 0 重置；全局 callback 保留最后执行阶段的 tick。协调器 callback 读取当前确认进度；worker 动作/效果 callback 采样准入时进度，当前动作尚未被确认，因此不计入。失败复核在协调器上使用当前进度。`WorldState.version` 继续是实际低层提交版本，独立于 tick。

该定义恢复“前项完成后后项 callback 可见进展”的兼容能力；代价是不会复刻旧多机器人私有线程计数及竞争顺序。此裁定已同步 root，写入 progress ledger、WorldState docstring 和 docs/executor_batch_2.md。没有修改旧生成脚本、导航网格/间距/候选或重规划预算，没有新增隐式传送或在线规划。

## RED / GREEN

以下同一命令先在无生产修复的基线上运行，再在最小修复后运行；真实 TaskRunner/StageScheduler/Executor/资源解析和租约保留，仅 Unity adapter/metadata 使用现有 deterministic fake。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_stage_conditions.StageConditionsTest.test_legacy_tick_callbacks_observe_completed_queue_progress \
  tests.test_stage_conditions.StageConditionsTest.test_tick_is_shared_by_robots_and_resets_at_stage_boundary \
  tests.test_stage_conditions.StageConditionsTest.test_standalone_world_returns_confirmed_tick \
  tests.test_stage_scheduler.StageSchedulerTest.test_retry_preserves_one_idle_pass_before_wait_timeout \
  tests.test_stage_scheduler.StageSchedulerTest.test_admission_failure_after_attempt_keeps_reports_consistent \
  tests.test_stage_scheduler.StageSchedulerTest.test_no_deadline_dependency_cycle_external_watchdog_cleans_workers
```

- RED `/tmp/final-fix-red.log`：**Ran 6 tests in 2.258s，FAILED (failures=21)，退出 1**。F1 ordinary/tolerant 在第一项完成后无法推进、返回 tick0；F2 RETRY 条件/资源提前超时，WAIT_AND_RETRY 首个 Pass 前等待预算已为1；F3 逐动作 attempts1 与预期2/3 不一致。无 fixture errors。F4 正向测试在基线通过，符合其测试缺口裁定。
- GREEN `/tmp/final-fix-green.log`：**Ran 6 tests in 1.265s，OK，退出 0**。随后增加了 retry/idle tick 不增加及逐次尝试序列断言，统一由下面定向/完整门禁验证，没有再改变生产实现。
- F1 同时覆盖两个入口、每高层动作0或3次底层提交、前置/效果/阶段/全局 callback、多机器人共享进展、阶段重置、独立 stage/executor 返回值，防止错误地把 tick 设为 snapshot version 或 robot cursor。
- F2 同时覆盖 legacy/strict × RETRY/WAIT_AND_RETRY × condition/resource，首个 Pass 前 wait_ticks=0，完成一次 Pass 后恢复条件或释放真实资源，第二次执行成功；两次 worker 准入高层 tick 均0。
- F3 同时覆盖 legacy/strict × 首次执行失败后条件超时/资源消失/延期后资源消失；最后一种保留原语义共3 attempts（延期1、准入失败后retry1、重试准入失败1），正常失败路径2 attempts。尾项只在 legacy 执行，strict 未执行尾项 attempts0，planned固定2。

## F4 失败清理反向验证

日志 `/tmp/final-fix-watchdog-mutation.log`。通过测试进程内 patch `ExecutionControl.cancel` **仅忽略** reason 为 `external dependency watchdog` 的调用，保留其他取消行为，运行该具名测试。两个模式都在外层 1s join 断言处按预期失败；finally 仍完成 child/root 取消及线程 join。最后检查 `threading.enumerate()` 除 main 外为空。

结果：`Ran 1 test in 2.009s，FAILED (failures=2)`，接着 `Expected watchdog-cancellation mutation failures: 2; threads left after failure cleanup: 0`。验证脚本对预期失败数/零线程作断言，退出0。该故障注入没有修改生产文件，不是正常测试门禁失败，也不用于声称新增产品缺陷。

## 最终验证

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_stage_scheduler.py tests/test_stage_conditions.py \
  tests/test_executor_retry_policy.py tests/test_execution_shutdown.py tests/test_parallel_runner.py
```

日志 `/tmp/final-fix-targeted.log`：**Ran 195 tests in 11.543s，OK，退出0**。

```bash
bash reports/executor_batch_2/verify.sh
```

按 root 要求完整具名门禁仅运行一次，无 discover。最终日志 `reports/executor_batch_2/verification.log`：**Ran 581 tests in 18.946s，OK，退出0**，原575加本轮6个具名方法。日志保留原有故障注入用例输出，不代表测试失败。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/executor_system/action_plan.py scripts/executor_system/executor.py \
  scripts/executor_system/stage_scheduler.py \
  tests/test_stage_scheduler.py tests/test_stage_conditions.py
git diff --check
```

均退出0。自审已核对全部 tick 传播/返回路径、retry counter 写入、四个实际 attempt 入口、零 attempt skip 与未执行尾项路径、取消后的真实 worker 清理，以及现有门禁所覆盖的共享 standalone/finalization 兼容性。

## 提交与交接

仅提交3个源码、2个测试、docs/executor_batch_2.md、更新的 verification.log。本报告保持在 SDD scratch 供 root 归档；root 原有未跟踪的 `reports/executor_batch_2/execution-ledger.md`、`implementation/`、`reviews/task-6-review.md` 未覆盖、未 stage、未提交。

修复提交：`149637b28ffcb49b95ac8d177f0302c043942851`（Fix scheduler progress and admission attempt accounting）。提交后只剩上述 root 原有未跟踪归档，本轮源码/测试/文档均已提交。F1–F4 均已处理，等待 root 一次 scoped 复审。真实 Unity48 仍由 root 在最终生产 SHA 执行，本轮所有 fake/单测证据均不冒充真实验收。
