# 整批独立审查

审查范围：`8f95c7ca092d7025a5ca67def5015daa56f6c568..8a4d97a74cd6b1691cfb7d2210b0103c4c2f1718`，工作树 `/tmp/coder-executor-batch-2`。审查依据：原始第二批完整计划、final-review-brief、final-diff、progress ledger 三条 Ruling 和 task-6-report。采用 requesting-code-review 的 code-reviewer 规范；未改源码、HEAD/index，未启动子代理，未重复大套件。

**规格 verdict：需要修复。质量 verdict：需要修复。Ready to merge：With fixes。**

没有发现需要立即阻断执行安全的 P0/P1；发现三个可复现的 P2 整合问题，以及一个 P2 明确验收测试缺口。修复并复审后，仍须 root 在最终生产 SHA 完成真实 48 次 Unity 验收；本次 pending 状态本身不是代码问题。

## Strengths

- 普通与容错入口实际共用 TaskRunner → StageScheduler → Executor 机器人循环；阶段 child 取消和根基础设施取消分开，worker 实际退出证明约束 finalization 和资源释放。
- 资源准入使用冻结 metadata 的 detached selector，保留每 agent 可见性/距离；转换身份在 controller 提交锁内链接，避免先发布新 ID 后补租约身份的窗口。导航恢复候选预租约的保守串行化与已接受 Ruling 一致。
- 最终收尾区分当前与诊断快照，成功效果证明不会使用取消后的旧入场快照；v2 scheduler2 对 quiescent 语义失败保留评分资格，对 timeout/cancel/unquiescent 保持不可信。
- 本次排查的资源冲突 SKIP 并未伪造计数：只执行 robot1、robot2 入场前跳过时，总账 attempts=1、robot2 attempts=0，两个位置一致。该怀疑已排除。

## Issues

### Critical

无。

### Important

#### F1 — P2：旧 WorldState.tick callback 永久停在 0

- 位置：`/tmp/coder-executor-batch-2/scripts/executor_system/action_plan.py:324`；调度侧相关位置 `/tmp/coder-executor-batch-2/scripts/executor_system/stage_scheduler.py:316` 和 `/tmp/coder-executor-batch-2/scripts/executor_system/executor.py:638`。
- 新实现保留公开 `WorldState.tick`，但整个执行模块只剩初始化赋值，没有调度推进。旧基线 Executor 在每动作开始赋 `world_state.tick = tick`，成功后增加 tick（基线 executor.py:502、539）。旧 callback 因而能观察高层队列进展；新循环只增加私有 `_tick`。
- 有界实际 probe：单机器人队列 `[Action('Wait'), Action('Wait', wait_until=lambda w: w.tick >= 1)]`，只替换 AI2ThorAdapter.execute 为无副作用记录器，调用真实 `run_action_plan_tolerant(..., timeout_seconds=.18)`。结果只有第一个动作执行，最终 `execution_status=timeout`，cursor=1/WAITING_CONDITION，虽然协调器已推进三次 Pass、`state_version=3`。这不是依赖环，第二个动作只依赖已完成的前项。
- 影响：旧 callable 在默认 legacy 下也会挂到任务期限；无期限库调用则持续等待。与旧 callback 适配及保留兼容入口的要求冲突。
- 建议：为 scheduler2 的兼容 tick 明确定义并在所有 callback 使用的 WorldState 适配器中推进。最小可观察要求是前项完成后后项 callback 能看到进展；不要未经定义就把实际低层 snapshot version 等同于旧高层 tick。覆盖 ordinary/tolerant、单机器人旧 callback，必要时说明多机器人共享 tick 的新语义。

#### F2 — P2：重试次数仍消耗等待 tick，提前触发条件/资源等待超时

- 位置：`/tmp/coder-executor-batch-2/scripts/executor_system/executor.py:790`（尤其 792）；消费者 `/tmp/coder-executor-batch-2/scripts/executor_system/stage_scheduler.py:320`。
- 统一失败处理在 RETRY/WAIT_AND_RETRY 时沿用旧逻辑 `state.wait_ticks += 1`。新调度契约则规定 timeout_ticks 只按协调器完成的等待 Pass 累计；重试年龄/次数与这种等待 tick 是不同计数。
- 有界 probe：Teleport 的 wait_until 初始 true，首次 adapter 调用将条件设 false 后抛普通 RuntimeError；动作配置 `on_failure='RETRY', max_retries=1, timeout_ticks=1`。实际输出第一次 `action_failed/retry` 后立即 `timed out waiting for Teleport/retry_exhausted`，`state_version=0`、零次等待 Pass。应至少允许协调器推进规定的一次等待轮；若该 Pass 使前置条件重新成立，当前实现仍已提前终止。
- 影响：在失败后需等待同伴或世界恢复条件的合法重试中，strict 提前停止阶段，legacy 提前放弃动作；WAIT_AND_RETRY 的墙钟延迟也不能代替仿真等待轮。
- 建议：重试计数、重试延迟和 timeout_ticks 分开，保留成功/跳过/失败后的重置语义。添加真实 scheduler probe，验证首次等待 Pass 前没有条件超时，并覆盖一次 Pass 恢复条件后能重试成功。

#### F3 — P2：准入侧失败没有同步逐动作 attempt 计数，报告自相矛盾

- 位置：`/tmp/coder-executor-batch-2/scripts/executor_system/stage_scheduler.py:323`，同类资源准入异常分支在 `:197`；覆盖输出位置 `:233`。
- 条件等待超时和资源解析异常会向 ActionLedger `record_attempt()`，但没有更新 `attempt_counts[key]`。一旦该 action 曾获准，`_observe_result` 用已有的较小计数覆盖 ActionResult.attempts。因此首次失败/等待的测试能通过，而重试/延期之后再发生准入失败的跨路径报告会失真。
- 上述 F2 的同一个生产 probe 已独立观察到：总账 `action_counts.attempts=2`，阶段 attempts 有两个失败记录，而唯一动作终态 `actions[0].attempts=1`，第二条尝试记录仍为 attempts=1。即使修复 F2、等待一次 Pass 后再超时，此处漏更新仍然存在。
- 影响：整批保存的动作证据无法与总账核对；重试耗尽显示发生于第二次逻辑尝试，而逐动作报告只宣称一次。结果可信性契约要求延期/重试/等待失败在各报告入口保持一致。
- 建议：统一记录一次 attempt 的入口，或在所有准入失败记账分支同步逐动作计数。明确保持当前总账对等待超时的既定计数定义，避免修复一个字段却改变 first-batch 评分分母。至少覆盖已获准后发生条件超时及资源解析失败的场景。

#### F4 — P2（验收测试缺口）：没有无 deadline 依赖环的外部看门狗与清理回归

- 位置：`/tmp/coder-executor-batch-2/tests/test_stage_scheduler.py:79`；绑定要求为原计划“批次验收与交接”的第一条。
- 现有 `test_idle_dependency_timeout_saves_diagnostics_and_rate_limits_pass` 传入内部 timeout=.18；其他无期限测试在 test_execution_shutdown 中检查动作中断、worker 异常、SIGINT 等，没有显式循环条件依赖。不能用内部 deadline 测试证明没有 deadline 时外层超时/取消与清理机制可靠。
- 本次有界只读 probe 使用 `install_control(runtime, None)`、两个相互等待对方完成标记的机器人，待第一次等待 Pass 到达后由外层取消 root 并 `join(1)`：得到 `ExecutionCancelled`、`execution_quiescent=True`、空资源 holders、两机器人 WAITING_CONDITION 诊断，正常退出。因此目前没有证据表明该场景产品代码出错，问题是明确要求的持久回归未交付。
- 建议：补入一个使用 Event 和有界外层 join 的具名测试；finally 始终取消/释放并 join 实际 worker，断言诊断、零动作误执行、quiescence 与无遗留租约。覆盖 step/teleport；加入现有具名门禁。

### Minor

无单独提出的风格或优化建议。

## 验证范围和证据

- 已逐段核对整批生产改动：策略、共享快照、调度/波次、资源解析/租约、runtime commit/恢复/线程本地 scope、普通/容错/standalone 接口、CLI 父子策略透传、v2 验证与聚合；定向检查相关条件、资源、调度及 shutdown 测试。
- task-6-report 及已保存 verification.log 记录 575 个具名测试通过；这是已有门禁证据，本审查没有重新运行或声称重新通过这套测试。
- 本审查通过 `/home/dwb/.pyenv/bin/pyenv exec python -`，设置 `PYTHONDONTWRITEBYTECODE=1`，运行三个有界脚本：资源 SKIP 反证、tick/重试计数复现、无 deadline 取消清理。均正常退出；源码和测试文件未修改。执行器/调度器使用生产实现，替换 Unity metadata/adapter 仅为确定性控制场景，不宣称真实 Unity 验收。
- F1 最小复现输出：`status=timeout; executed=['robot1']; state_version=3; action_counts={planned:2, started:1, succeeded:1, unexecuted:1, attempts:1}`。
- F2/F3 最小复现输出：`executed=['robot1']; state_version=0; counts.attempts=2; actions[0].attempts=1; attempts decisions=[retry,skip]; final reason=retry_exhausted; final error='robot1 timed out waiting for Teleport.'`。
- 无 deadline 取消反证输出：`deadline=null; errors=['ExecutionCancelled']; quiescent=true; holders={}`。

## Assessment

**Ready to merge：With fixes。** 共享执行与资源生命周期主体已整合，既有关键基础设施修复有效；但兼容 callback 的进度、重试与等待计数、逐动作报告一致性三个交界仍有真实可触发回归，需要一轮限定修复及对应复审。补齐明确要求的无 deadline 依赖环回归后，再由 root 执行最终 SHA 的真实 48 次验收。
