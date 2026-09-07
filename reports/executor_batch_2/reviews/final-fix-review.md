# 最终统一修复限定复审

范围：`8a4d97a74cd6b1691cfb7d2210b0103c4c2f1718..149637b28ffcb49b95ac8d177f0302c043942851`。只核对原 final-review.md 的 F1–F4、对应生产/测试/文档 diff 和修复直接影响；读取 final-fix-report.md、final-fix-diff.txt、progress.md 的新增 tick Ruling。未扩大整批审查，未修改源码、测试或 Git 状态，未启动子代理，未重复大套件。

**规格 verdict：APPROVED。质量 verdict：APPROVED。F1–F4 全部 ADDRESSED。**

## 逐项结论

| Finding | 结论 | 核对结果 |
|---|---|---|
| F1：WorldState.tick 永久为 0 | ADDRESSED | `_observe_result` 只在首次确认终态时增加阶段共享 tick；retry/deferred/idle Pass/未执行尾项/初始阶段 skip 不进入该增加路径。worker 准入消息携带采样 tick，失败复核使用协调器当前 tick，StageRunner/独立 Executor/共享 PhaseCoordinator 返回值同步最终 tick。TaskRunner 保留最终阶段 WorldState 供 global callback。行为与已接受 Ruling、docstring、文档一致，没有混用 snapshot version。 |
| F2：重试消耗等待 tick | ADDRESSED | 删除 Executor retry/wait_retry 的 wait_ticks 增加；统一循环中唯一剩余增加处为调度器消费完成的 idle Pass。retry_number 和 retry_after 保留独立语义。新增测试覆盖 legacy/strict × RETRY/WAIT_AND_RETRY × condition/resource，确认首个 Pass 前预算为 0、一次 Pass 恢复条件/释放租约后再次获准。 |
| F3：准入失败逐动作计数遗漏 | ADDRESSED | 四类入口统一调用 `_record_attempt`：获准、资源冲突 FAIL_STAGE、资源解析/等待异常、条件等待超时。总账、stats 与 attempt_counts 在同一入口更新；ActionResult 及报告读取一致累计值。SKIP/初始跳过/未执行尾项没有增加 attempt。新增测试覆盖重试后条件超时、资源消失、延期后连续准入失败及两种 policy 的尾项差异。 |
| F4：无期限依赖环回归缺失 | ADDRESSED | 新增具名测试在 step/teleport 下构造互等完成 Event 的显式环；root/child deadline 均为 None，独立 watchdog 外部取消，有界等待退出并核对诊断、零高层执行与 attempt、quiescence、空租约和两个实际 worker 的退出。finally 对成功/断言失败均取消、唤醒并在共享预算内 join 捕获线程。测试位于现有具名门禁模块。 |

## 直接回归核对及验证

- 已核对新 mailbox 四元组的唯一生产发送/接收路径匹配；高层 tick 不影响底层 state_version、波次编号或现有 `_tick` 调度日志。多机器人 tick 采用 Ruling 的共享终态定义，不宣称复刻旧私有线程计数顺序。
- ActionResult 是可变 dataclass，ExecutionLogger 保存该对象引用；统一累计 attempts 同步后，日志对象与序列化记录保持一致。未修改 ActionLedger 评分/计划分母或 retry 资格与预算。
- 独立重放原审查最小 probe，使用 `PYTHONDONTWRITEBYTECODE=1 /home/dwb/.pyenv/bin/pyenv exec python -`，总耗时不足 1 秒，退出 0：F1 两个动作完成，准入 tick `[0,1]`，返回 `tick=2/version=0`；F2/F3 在条件持续 false 时，先恰好完成一次 idle Pass 再超时，总账与逐动作 `attempts=2`，逐次累计序列 `[1,2]`。真实生产调度/执行器保留，Unity adapter 使用确定性 fake。
- 阅读并确认保存日志：`/tmp/final-fix-targeted.log` 为 195 tests/11.543s/OK；`reports/executor_batch_2/verification.log` 为 581 tests/18.946s/OK。本次复审没有重跑这些套件。
- 阅读 `/tmp/final-fix-watchdog-mutation.log`：忽略 watchdog 指定取消后两模式按预期断言失败，测试 finally 清理后剩余线程为 0。这是有意的反向清理验证，不是当前产品测试失败。

## Issues / Assessment

Critical：无。Important：无未处理项。Minor：无新增项。未发现本次限定修复引入的新直接回归。

**Ready to merge：Yes，代码审查通过；仍须 root 完成最终生产 SHA 的真实 48 次 Unity 验收。** 本报告关闭原 final-review.md 的 F1–F4 并取代其中“需要修复”的代码 verdict，不把 fake/单测当作真实验收，也不提前宣称整批合并完成。
