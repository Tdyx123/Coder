# Task 5 只读审查

审查范围：`ae6656627f8bc24dc61de2e595fa2e95e46324cd` → `2ca2a8d1`；依据 task-5-brief.md、task-5-report.md 和完整 task-5-diff.txt。未修改源代码，未启动子代理，未重跑报告中的 276 项或全量测试。对具名跨入口、收尾风险读取了必要的未修改实现，并运行了下面的少量定向探针。

**规格结论：需修改。质量结论：需修改。** 统一循环、条件判定及主要报告结构已落实，但保留的并发兼容入口和终态证据仍有实质回归。

## 发现

### [P1] 独立执行入口丢弃调用方提供的共享 PhaseCoordinator

位置：`scripts/executor_system/executor.py:536-538`；对应覆盖发生在 `scripts/executor_system/stage_scheduler.py:88-91`。

`Executor.execute()` 无条件创建仅含当前机器人的 StageScheduler，后者把原 `executor.phase_coordinator` 替换为新的一机器人协调器。既有用法会为多个 Executor/TolerantExecutor 提供同一个 PhaseCoordinator 并并发调用 execute；现在这些队列被拆成互不知情的阶段，无法形成原共享导航波次，也不会向原协调器报告完成。多个调度器还会分别写同一 runtime 的 stage_scheduler、execution_quiescent 等状态，削弱统一阶段监督的保证。这不是方法签名仍可调用就能覆盖的兼容行为。

定向证据：FakeRuntime，step 模式，共享 `PhaseCoordinator(runtime, [0, 1])`，两实例分别执行不同目标的 GoToObject；在适配器处观察 `(robot, phase is original, active_agent_ids, wave_id)`，结果为 `('robot1', False, [0], 1)` 和 `('robot2', False, [1], 1)`，执行完成后原协调器 `completed_agent_ids == set()`。

主代理另外提供第一批 verify.sh 的交叉入口验证：449 项中 1 项失败，`tests.test_navigation_batch_failures.NavigationBatchFailureTest.test_tolerant_execution_retries_deferred_navigation_at_same_cursor` 的 seen 实际为空、预期三个请求。其对象快照已填充；该测试显式共享 PhaseCoordinator，不能把它归因于缺少对象 fixture。测试还读取旧 `_action_wave` 存储，迁移该观察点时应保留真实共享波次、延期后同 cursor、三次请求/两项逻辑动作的断言，不能只改成两个独立执行成功。

建议：让同一兼容协调器下的队列加入同一个 StageScheduler/监督生命周期，或提供保持该既有并发调用语义的兼容桥接；保持 Executor 中唯一实际动作循环。

### [P2] 阶段终止收尾丢弃已提交的同伴失败

位置：`scripts/executor_system/stage_scheduler.py:429-430`。

当机器人 A 的 FAIL_STAGE 首先被主线程处理时，其他机器人可能已经向 results 队列提交动作失败。`_harvest_completed_successes()` 在已经证明 quiescent 后只接收成功，直接丢弃所有 `exc is not None` 的结果；随后 `_tail_records()` 把这些实际已完成失败的动作标成 cancelled。原异常、失败策略决定及 attempt 证据消失，failed/cancelled 计数被改写。修复应覆盖已完成失败和延期证据，而不仅保留成功结果；收尾时不要重新启动动作或复活被停止的阶段。

定向证据：严格模式中 robot1 的 Wait 默认 FAIL_STAGE，robot2 的 Wait 为 SKIP；两个适配器都实际抛出 RuntimeError。用有界的 `_receive_results` 包装等待队列已有两个结果再调用原实现，以固定合法的竞态顺序。报告得到 `planned=2, started=2, attempts=2, failed=1, cancelled=1`，robot2 为 `cancelled/stage_failed`，stages[0].attempts 只有 robot1，找不到 robot2 原错误。预期两个完成的动作失败均保留，阶段仍 failed，且无重复终态。

### [P2] TolerantStageRunner 在显式 control 下忽略自己的 deadline

位置：`scripts/executor_system/parallel_runner.py:277-281`。

新兼容构造只在 control 为空时调用 ensure_control(runtime, deadline)，否则只保存 self.deadline。继承的 StageRunner.execute_stage 使用 `root_control.child()`，不再把该 deadline 传给子控制器；旧实现的入口 `_check_deadline(self.deadline)` 和 `child(deadline=self.deadline)` 均被删除。调用方提供较长任务期限和更早阶段期限时，阶段会越过明确传入的期限继续运行。

定向证据：root control 截止时间为当前时间 +1 秒，TolerantStageRunner 的 deadline 为当前时间 -1 秒，同时显式传入该 root control；执行一个 Wait 得到 `completed`、runtime.state_version 为 1，子 control 的 deadline 等于 root deadline。应在任何动作前抛出 PlanExecutionTimeout。建议在共享 StageRunner 的阶段子控制器构造中保留兼容入口的更早期限，而不是引入第二套执行逻辑。

## 已核对的实现与边界

- Executor._execute_queue 为唯一实际机器人工作循环；TolerantExecutor、TolerantStageRunner 为薄兼容类，容错任务函数复用 TaskRunner 并返回 PlanExecutionError 的同一 report。
- 普通任务路径有统一预执行校验；报告测试覆盖缺 Pickup 参数、冲突参数、非法策略、负重试、非有限期限和未知机器人。未对所有动作建立第三批才负责的注册表要求。
- 当前条件使用不可变全机器人快照；字典条件通过 fresh EvaluationContext 和 detached snapshot_resource_view 判定，不复用历史 HOT/COLD 证据。unknown 前置条件等待、unknown 效果失败；callable 异常经 ConditionEvaluationError 保留 cause，不作为普通 false。
- 主要严格/legacy 动作、FAIL_ROBOT、阶段 SKIP/FAIL_STAGE、初始条件跳过、结束条件及显式全局条件的路径符合规格。条件异常/基础设施异常的监督仍通过 run_workers；SnapshotStore 自身取消 control 并向根传播，未发现新加效果快照错误被普通失败策略吞掉的问题。
- 正常 worker 完成后的顺序为效果验证、主线程终态记账、释放租约、推进下一动作；重试/延期无重复 terminal。失败停止后的剩余租约仅在真实 worker 退出证明后清理。上述第二项说明其已提交失败结果的收尾仍不完整。
- 单次 TaskRunner 报告冻结、`ActionLedger.record_terminal(started=True)` 默认语义以及新 started=False 仅允许 skipped/cancelled 的兼容约束已核对。额外资源冲突 SKIP 探针得到 started=0、attempts=0、skipped=1，详细 action.attempts 同为 0，没有虚构尝试。
- stages/actions/attempts/waits/waves/resources/diagnostics、最终快照与全局条件字段均已提供；快照报告显式序列化，不暴露 controller event 或 MappingProxyType。执行路径中 quiescent 和语义 failed 可区分。共享独立入口对监督状态的覆盖风险属于第一项。
- CLI、generated runtime 参数透传和 semantic failed 的可信评估 guard 明确留给 Task 6，本审查不把它们当作 Task 5 遗漏。

## 验证可信度

task-5-report.md 的 276 项 GREEN、py_compile 和 diff --check 为实施方证据，本审查没有重新声明独立跑过。主代理的额外 449 项结果按其提供的日志报告引用。独立定向探针复现了上述三个问题，并排除了资源 SKIP 伪造 attempts 的疑虑。没有执行真实 Unity，也没有对本任务范围以外代码做宽泛扫描。

## Fix1 复审：2ca2a8d1 → c0791872

**结论：需修改，暂不 approved。** 本轮只读取完整 task-5-fix1-diff.txt、实施报告修复章节及前述问题涉及的必要代码，没有扩大重审范围，没有修改源码、启动子代理或重跑大套件。

原三项的主路径修复已确认：

- 共享 PhaseCoordinator 现在注册全部兼容调用方，选一个驱动者建立一个共享 StageScheduler，并复用原协调器；受监督 worker 的线程标识避免等待中的外部调用方消费 mailbox。导航回归测试保留三次请求、同 cursor、两项逻辑动作、失败机器人和 metrics，并新增原协调器身份及联合成员断言，未弱化成独立队列测试。
- 阶段收尾现在保留已经返回的普通失败及 deferred 证据，停止自动重试，并将新增错误合入 StageOutcome/任务报告。原丢失普通 peer failure 的问题已修复，但下面的效果证明分支仍有遗漏。
- StageRunner 的统一子控制器构造传入兼容 deadline，明确期限不再因已有 root control 而丢失；对应回归测试要求过期前零动作。

已核对日志：`/tmp/task5-fix1-targeted.log` 记录 `Ran 153 tests in 9.830s / OK`；`/tmp/task5-fix1-firstbatch.log:344-346` 记录 `Ran 449 tests in 14.369s / OK`。这些是已存在的实施方测试日志，本轮未重复运行。

### [P2] 收尾模式绕过 SKIP_IF_EFFECT_ALREADY_TRUE 的效果证明

位置：`scripts/executor_system/executor.py:732`，由本次修复新增的 `finalizing` 分支引入。

`handle_failure(..., finalizing=True)` 无条件令 effects_satisfied=None，即使动作声明了非空、在当前快照上可证明为 true 的效果，也不调用判定器。严格模式因此把已满足效果的动作记为 failed/effects_unknown，而正常完成收集会记为 succeeded/effects_already_satisfied；仅由 results 出队顺序决定逻辑结果，仍不满足原评审要求的“完成结果经过相同失败策略”。阶段停止可以阻止重试，不应取消既有的效果成功判定。在普通 FAIL_STAGE 收尾中根控制器仍有效，调度器已取得退出后的当前全局快照，可以用该快照验证而不恢复动作执行。

独立有界探针沿用新增 shutdown-drain 测试的两结果排队方式：robot1 为默认 FAIL_STAGE 的 Wait；robot2 为 `Wait(on_failure='SKIP_IF_EFFECT_ALREADY_TRUE', expected_effects=(effect,))`，effect 恒返回 True 并记录调用；两个适配器都抛 RuntimeError，固定 robot1 先出队。实际输出 `planned=2, started=2, attempts=2, failed=2, succeeded=0`，robot2 reason=`effects_unknown`、effects=[]，效果 callback 的调用列表为空。预期保留阶段 failed，同时 robot2 为 succeeded、效果证据为 true，且仍不再执行任何动作或重试。

建议：收尾时基于已经取得的可信当前快照保留效果证明及条件异常语义，避免走会因已取消阶段 control 失败的旧刷新路径；补充上述非空效果已满足的确定性排队回归。修复后无需重新审查无关 Task 5 范围。

## Fix2 复审：c0791872 → 841e02f3

**规格 verdict：需修改。质量 verdict：需修改。** 本轮严格审提交 diff 中 executor.py、stage_scheduler.py、test_stage_conditions.py 三个文件；未审查 Task 6 未提交改动，未重跑大套件，未修改源码，未启动子代理。

原 Fix1 遗留 P2 的正常 FAIL_STAGE 路径已修复：收尾使用已有退出后快照进行 callable/字典条件判定，满足时恢复 succeeded/effects_already_satisfied；新增测试同时证明最终温度变化、单次 callback、计数及不执行 controller step。新增 callback 收尾 guard 在已出队异常后继续排空，再生成 tails/report、释放租约和恢复 runtime.stage_scheduler；随后保留原异常优先级，结构上未发现独立的直接回归。`/tmp/task5-fix2-targeted.log` 已核对为 `Ran 93 tests in 1.971s / OK`，compile 与 diff --check 的退出 0 为实施报告证据。

### [P2] 超时/取消收尾把旧快照当成当前效果证明

位置：`scripts/executor_system/executor.py:732`，即新增 `effects_satisfied_after_failure(action, refresh=not finalizing)` 调用；关联的 finalizing 说明假定 scheduler 提供的是退出后快照。

该假定只对普通 StageFailureDecisionError 且根控制器仍有效的分支成立。StageScheduler.run 的 timeout/cancelled/BaseException 分支不会在 worker 退出后 capture，self.world 可以仍是准入时的旧快照；新代码却在所有 finalizing 情况下无条件对它执行效果证明。于是已失败动作会因执行前满足的效果被改记成功，违背“前置/后置条件始终检查当前快照”的契约。任务级 timeout 保留正确并不能使该动作成功证据可信。

独立有界探针复用新测试的“两结果均已返回后抛主 timeout”路径：准入时 Mug=Hot、version=0；robot2 动作把 Mug 改为 RoomTemp、version=1 后抛 RuntimeError，声明 `SKIP_IF_EFFECT_ALREADY_TRUE` 和非空 `{'name': 'Mug', 'state': 'HOT'}`；results 尚未处理时主线程抛 PlanExecutionTimeout。实际最终任务为 timeout，但 robot2 为 succeeded/effects_already_satisfied，效果证据 satisfied=true/world_version=0；真实退出后状态为 RoomTemp/version=1。这个假阳性由本次恢复收尾效果证明直接引入，Fix1 的该路径尚不会宣称成功。

建议：显式区分“已取得可信当前退出后快照”和“仅有旧诊断快照”，仅前者可用于当前效果成功证明；没有可信当前快照时保守保留 unknown/未证明，不能越过取消控制器刷新。补充上述 Hot→RoomTemp + 主 timeout 的定向回归，并保留已有正常阶段失败 Hot 证明成功与 callback 清理测试即可，无需扩大旧代码重审。

## Fix3 复审：841e02f3 → 1d243042

**规格 verdict：approved。质量 verdict：approved。** 原三项问题及 Fix1/Fix2 直接引入的遗留问题，在本次限定范围内均已闭合，没有新增可操作发现。

本轮仅审 task-5-fix3-diff.txt 的 executor.py、stage_scheduler.py、test_stage_conditions.py 三个提交文件，并核对实施报告末尾和已有日志；未混入 Task 6 未提交文件，未重新扫描旧范围，未运行套件、修改源码或启动子代理。

- `_final_snapshot_version` 只在退出后 capture 成功时设置，证明还要求该版本与 scheduler.world.version、runtime.state_version 一致。因此准入旧快照不会因版本数字偶然相同就自动成为退出后证明，之后提交版本变化也会使证明失效。
- finalizing 仅在 `final_snapshot_current=True` 时调用现有 callable/字典效果判定器。无可信当前快照时清空效果证据并返回 unknown，不调用 callback、不刷新、不执行 controller；原 Hot→RoomTemp + timeout/cancel 的错误成功路径已被封闭。
- 普通阶段失败的退出后快照仍可证明 effects_already_satisfied；既有 callback 原 cause、收尾租约清理、原异常优先级和禁止重试的行为未被改写。阶段 `snapshot_is_current` 明确区分当前证明与诊断快照。
- 新测试覆盖 timeout/cancel × callable/字典四种组合，断言 succeeded=0、原异常对象不变、无 callback/旧效果证据、租约释放；原正常最终快照成功和 callback 错误清理测试仍保留。旧 timeout 次要 callback 测试改为不调用 callback，符合本次明确的快照可信度契约，并非弱化证明要求。

已核对 `/tmp/task5-fix3-targeted.log` 为 `Ran 94 tests in 2.157s / OK`。RED 四个子情况失败、四项定向 GREEN、compile 与 diff --check 退出 0 为实施报告记录；本轮未重复执行。批准限定于 Task 5 至 `1d243042` 的审查范围，不替代 Task 6 集成及真实驱动验收。
