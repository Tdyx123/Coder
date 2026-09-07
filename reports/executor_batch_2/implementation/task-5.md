# Task 5 实施报告

基线：`ae6656627f8bc24dc61de2e595fa2e95e46324cd`（Task 1–4 已验收）。未合并。

## 实现与接口

- `Executor._execute_queue()` 是唯一机器人工作循环，接收调度器准入，在固定资源绑定作用域内执行，读取当前完整快照并检查效果，随后交给主线程记账、释放租约、推进队列。普通/容错入口、独立 `Executor.execute()` 均走 `StageScheduler.run()`；独立入口复用原 executor 实例，保留 mock/subclass 和 submit/navigation 兼容接口。
- `TolerantExecutor` 只保留兼容构造和统计属性。`TolerantStageRunner` 继承 `StageRunner.execute_stage()`。`TaskRunner.execute()` 统一控制整个计划和一次报告冻结；`run_action_plan_tolerant()` 调同一个 TaskRunner，捕获 `PlanExecutionError` 返回其同一 report 对象，并保留原 timeout 返回契约。
- `execution_policy.py` 提供 `PlanExecutionError(report)`（保留 `.report`）、`resolve_stage_outcome(policy, stage, robot_outcomes, condition_satisfied)`、条件证据/判定函数及显式 snapshot 序列化。robot_outcomes 接受 StageOutcome 序列或 robot→StageOutcome 映射；输出 snapshot 取最后可用的 immutable snapshot。
- 阶段：初始条件 true 记录每个动作 skipped/`stage_condition_already_satisfied`，零 attempts；正常结束和动作 FAIL_STAGE 后均检查最终阶段条件。严格 FAIL_ROBOT 让其他机器人完成后令阶段失败，后续阶段服从 stage_failure_policy。严格 SKIP 动作失败为 partial；严格阶段失败配 SKIP 可继续，但最终任务仍 failed。legacy 普通动作失败为 partial 并继续；阶段条件 false 使任务 failed 并继续下一阶段。显式全局条件 false 在两种策略都使报告 failed，strict 普通入口抛 PlanExecutionError。
- callable 异常保留 ConditionEvaluationError 和原 cause/traceback，不作为 false，不进入普通动作失败策略；StageScheduler 保留 Task 3 的父取消/异常传播行为，TaskRunner 在任务报告边界收集该错误（strict 抛结构化 PlanExecutionError，容错返回报告）。超时、取消、shutdown、未捕获 worker 异常继续遵循第一批的原异常传播；只容错包装的既有 timeout 会转换成返回报告。
- expected_preconditions/expected_effects 使用当前同一全机器人快照。每项为 callable 或 `{name, states/state, contains}`；字典用 fresh/no-history EvaluationContext + Task 4 的 detached snapshot_resource_view，保留别名/身份/contains 解析，不读取 live alias/controller。unknown 前置等待，unknown 效果失败。SKIP_IF_EFFECT_ALREADY_TRUE 只接受非空且全部已满足的效果证明。
- 唯一终态记账：重试和延期保留 attempt 证据但不重复 terminal；报告区分 succeeded/failed/skipped/cancelled/unexecuted。FAIL_ROBOT 尾部 reason=`robot_failed`；stage abort 尾部 `stage_failed`；后续未开始阶段 `previous_stage_failed`；超时、取消、条件错误有对应原因。已提交成功结果的同阶段机器人在阶段停止时仍保留成功，不被误计为取消。

## 报告/Task 6 接口

`runtime.execution_report` / 容错返回值保留既有 stats 和 ledger 字段，并增加：

- `execution_status`、`scheduler_version=2`、`task_id`、`errors`、`worker_errors`、`execution_quiescent`、`global_condition_satisfied`。
- `actions`：按计划阶段/机器人/游标排序的每动作终态、原因、动作 key、策略实际决定、attempts、当前条件证据。
- `stages`：status、continue_task、errors、initial/final condition 结果、actions、attempts（包含 retry/deferred）、waits、waves（含导航成员）、resources（acquired/released 和 keys）、diagnostics（含资源持有者）、snapshot。
- `final_snapshot`：全部物理机器人的位置/旋转/held objects、objects_by_id 和 inventory sources。报告里均为普通 dict/list/scalar；不存 controller events 或 MappingProxyType。StageOutcome.snapshot 仍不可变。
- semantic failed 可以 quiescent；Task 6 应按最终设计保留详细报告，允许这类执行的可信评估，并保证显式全局 false 不被最终评分覆盖。Task 6 仍负责 strict/legacy CLI 透传、版本 grouping 和 completed-v2 对 semantic failed 的 evaluation guard；本任务未放宽 timeout/unquiescent 评估限制。

### Ledger 向后兼容修改

`ActionLedger.record_terminal(..., started=True)` 新增可选 kwarg。默认完全保持旧行为；`started=False` 只允许 skipped/cancelled，用于尚未准入的动作，不伪造 started 或 attempts。scheduler_version=2 的 count validator 下界为 succeeded+failed ≤ started ≤ planned，attempts ≥ started，所有终态 + unexecuted = planned；未指定/旧 scheduler 仍保留 terminal ≤ started 的旧校验。resource admission/condition timeout 的失败是一次失败的准入 attempt；初始条件 skip 和资源 conflict SKIP 是零 attempts。

## 验证证据

初始 RED 命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_plan_contract.py
```

输出（`/tmp/task5-red.log`）：`Ran 22 tests in 0.141s`，`FAILED (failures=14, errors=9)`。随后初版新增测试 GREEN：`Ran 22 tests in 0.227s`，`OK`。

最终 GREEN 命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_plan_contract.py tests/test_action_plan_pre_task.py tests/test_parallel_runner.py tests/test_executor_retry_policy.py tests/test_stage_scheduler.py tests/test_execution_policy.py tests/test_action_resource_leases.py tests/test_world_snapshot.py tests/test_run_result_contract.py tests/test_execution_shutdown.py tests/test_final_reliability_fixes.py
```

输出（`/tmp/task5-final-tests.log`）：`Ran 276 tests in 10.561s`，`OK`。没有 full discover。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/action_plan.py scripts/executor_system/execution_policy.py scripts/executor_system/executor.py scripts/executor_system/parallel_runner.py scripts/executor_system/run_results.py scripts/executor_system/stage_scheduler.py tests/test_stage_conditions.py tests/test_plan_contract.py
git diff --check
```

两项均退出 0，无输出。

新测试覆盖初始/最终阶段条件、global 全机器人、global false、legacy/strict 阶段继续策略、FAIL_ROBOT 同伴、动作 pre/effects 和 unknown、callback cause、历史 HOT 不可替代当前状态、serializable evidence、独立入口条件、缺参数/冲突 objectId/非法策略/负 retries/非有限 timeout/未知 robot、零 attempt skip/旧版本计数约束。

旧测试仅按本任务契约更新：补齐 OpenObject 参数和真实对象 fixture；阶段取消隔离测试改用无需对象资源的 Wait 保留其取消边界检查；strict 普通入口断言 PlanExecutionError，strict 容错入口断言返回同一个 failed report。现有 Task 4 资源绑定和全部 shutdown 测试继续通过。

## 自审与限制

- 已复核 controller 等待不在主线程执行高层动作；WAIT_AND_RETRY 的等待由调度器发放；资源租约仅在 worker 退出证明后清理；报告与 ledger 只冻结一次。
- 未增加第三方依赖，未修改导航预算或使用隐式传送，未修改 generated_plan_runtime/CLI（Task 6）。
- 实机 Unity 和跨进程 durable report 验证留给本批 Task 6；此任务完成范围内无已知阻塞。

## 评审修复 Round 1

依据 `task-5-review.md` 三项确认问题修复，基线 `2ca2a8d1`。

1. **共享独立入口生命周期**：显式传入同一 PhaseCoordinator 的 Executor/TolerantExecutor 在进入 execute 时向该协调器注册，等待 active_agent_ids 对应调用方全部进入；只选一个调用方驱动一个 StageScheduler，复用原协调器实例，并由 run_workers 创建唯一受监督机器人工作组。其他兼容调用方等待同一生命周期完成并取得结果/原异常；仅 run_workers 标记的工作线程能进入 Executor 唯一动作循环，防止外部调用方变成额外消费者。独立单机器人入口和 TaskRunner 路径仍走同一循环。注册等待受原协调器 control/deadline 限制；所有 active 参与方须并发进入 execute，协调器对应一次共享阶段（沿用原联合波次的参与方契约）。桥接以发起者的阶段 ID/index、执行策略、logger/stats 建立共享阶段上下文，常规容错调用方应像原测试一样共享同一 TolerantRunStats；没有扩展为一个协调器内混合多个阶段策略/独立统计生命周期。
2. **阶段终止已返回结果**：收尾从只保留 success 改为处理全部已返回结果。完成失败进入相同 failure-resolution/terminal/statistics 路径，保留原异常和策略证据；只有已证明 quiescent 才允许 finalizing 模式。retry/wait_retry 在阶段停止后转换为明确的 `stage_stopped_before_retry` 失败终态，不发动作或 Pass；deferred 保留 attempt 证据并撤销兼容 executed 计数，逻辑动作仍取消。收尾新增错误同步写入最终 StageOutcome 和顶层 report.errors，不仅写 stages。
3. **显式阶段截止时间**：StageRunner 共用路径通过 `root_control.child(deadline=getattr(self, 'deadline', None))` 保留 TolerantStageRunner 更早的显式 deadline，既不改任务根截止时间，也不恢复旧执行循环。

导航测试只迁移私有波次观察点为 `_state_for_wave(action_wave)`、用相对 wave ID 替代旧起始常量；新增 original coordinator identity、联合参与方和 completed_agent_ids 断言，保留 exact three requests、same cursor、two logical actions、failed robot 和 navigation metrics 原断言。Grid fake 的既有对象元数据补了 `current_objects()` 访问器，使 mandatory binding guard 真实运行；没有绕过资源绑定。

### 修复 RED/GREEN

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_conditions.StageConditionsTest.test_shutdown_drain_keeps_peer_failure_and_attempt tests.test_stage_conditions.StageConditionsTest.test_tolerant_stage_deadline_tightens_explicit_control tests.test_navigation_batch_failures.NavigationBatchFailureTest.test_tolerant_execution_retries_deferred_navigation_at_same_cursor
```

RED（`/tmp/task5-fix1-red.log`）：`Ran 3 tests in 0.068s`，`FAILED (failures=3)`。
GREEN（`/tmp/task5-fix1-green.log`）：`Ran 3 tests in 0.066s`，`OK`。
新增有界收尾测试还覆盖未执行任何自动重试、deferred attempt 证据、顶层 peer 原异常及 deadline 前零动作。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_stage_scheduler.py tests/test_navigation_batch_failures.py tests/test_parallel_runner.py tests/test_execution_shutdown.py tests/test_plan_contract.py
```

最终指定回归（`/tmp/task5-fix1-targeted.log`）：`Ran 153 tests in 9.830s`，`OK`。

```bash
bash reports/executor_batch_1/verify.sh
```

修复后执行一次第一批兼容集合（`/tmp/task5-fix1-firstbatch.log`）：`Ran 449 tests in 14.369s`，`OK`，退出 0。日志中的模拟 disk-full ERROR 行是既有故障注入测试输出，并无失败测试。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/executor.py scripts/executor_system/stage_scheduler.py scripts/executor_system/action_plan.py scripts/executor_system/execution_control.py tests/test_stage_conditions.py tests/test_navigation_batch_failures.py
git diff --check
```

均退出 0；未 full discover，未启动子代理，未合并。

## 评审修复 Round 2

依据 Fix1 复审新增的 P2，基线 `c0791872`。本轮仅修改 executor.py、stage_scheduler.py、test_stage_conditions.py；Task 6 并行文件未修改或暂存。

- 收尾调用 `effects_satisfied_after_failure(action, refresh=False)`；仍使用相同 callable/目标字典判定器，但只读 scheduler 已取得的退出后快照，不重新刷新，不提交 controller 动作，也不重试。非空全部 true 的效果恢复 `succeeded/effects_already_satisfied`；缺失/unknown/false 保留现有决定。
- 经主代理确认，追加最小 scheduler finalization guard：效果 callback 异常仍保留 ConditionEvaluationError 的原 cause，继续处理已返回结果并完成 tails、report、资源释放和 runtime.stage_scheduler 还原后再传播。已有任务级原异常优先于收尾次要异常；后者仍保存到报告证据。
- 定向测试用有界双结果队列固定机器人 1 FAIL_STAGE 先出队；机器人 2 执行时将对象从 RoomTemp 改为 Hot 后返回失败，分别以 callable 和字典证明**最终**快照效果。断言一次效果 callback、2 attempts、failed=1/succeeded=1、原始 stage failed、尾项 unexecuted，且整个收尾测试禁止 runtime.step。异常测试断言 PlanExecutionError → ConditionEvaluationError → 原 ValueError 身份、3 条动作记录和租约清空。另验证原 PlanExecutionTimeout 对象不被次要 callback 错误替换。

### Round 2 验证

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_conditions.StageConditionsTest.test_drained_effect_proof_uses_final_snapshot tests.test_stage_conditions.StageConditionsTest.test_drained_effect_callback_error_preserves_cause_and_cleanup
```

RED（`/tmp/task5-fix2-red.log`）：`Ran 2 tests in 0.167s`，`FAILED (failures=3)`。
GREEN（`/tmp/task5-fix2-green.log`）：`Ran 2 tests in 0.179s`，`OK`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_executor_retry_policy.py tests/test_stage_scheduler.py
```

最终指定回归（`/tmp/task5-fix2-targeted.log`）：`Ran 93 tests in 1.971s`，`OK`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/executor.py scripts/executor_system/stage_scheduler.py tests/test_stage_conditions.py
git diff --check -- scripts/executor_system/executor.py scripts/executor_system/stage_scheduler.py tests/test_stage_conditions.py
```

均退出 0。按主代理要求，本轮没有重跑 449 项、没有 discover、没有子代理、未合并。

## 评审修复 Round 3

依据 Fix2 复审 P2：timeout/cancel 收尾没有退出后 capture，不能把准入快照用作当前效果证明。本轮仅改 executor.py、stage_scheduler.py、test_stage_conditions.py，未动 Task 6 并行文件。

- Scheduler 仅在正常退出后的 capture 或普通阶段失败、根 control 仍可用时的退出后 capture 成功后记录 `_final_snapshot_version`。收尾证明还要求该版本同时等于所传 snapshot.version 和 runtime 当前提交版本。
- Executor 的 finalizing 效果证明显式接收 `final_snapshot_current`；未确认则效果为 unknown、清空旧效果证据，不调用 callback、不刷新、不提交 controller。重试禁用不变。
- 阶段报告增加 `snapshot_is_current`：true 表示已有且仍匹配当前提交版本的退出后快照；false 表示该 snapshot 仅是诊断证据，不能证明当前动作效果。普通 FAIL_STAGE 的可信证明及条件异常 cause/租约清理继续保留。
- 新有界测试：入场 Mug=Hot/version0，机器人2改为 RoomTemp/version1 后返回失败；两个结果全部返回后主线程分别抛 timeout/cancel。callable/字典四种组合均要求 failed=2、succeeded=0、effects_unknown、无旧证据/无 callback 调用、原任务异常身份不变和租约释放。原“timeout 时次要 callback 不覆盖原异常”测试按当前快照契约强化为**不调用该 callback**，仍保留原 timeout 身份。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_conditions.StageConditionsTest.test_abort_drain_cannot_prove_effects_with_admission_snapshot
```

RED（`/tmp/task5-fix3-red.log`）：`Ran 1 test in 0.219s`，`FAILED (failures=4)`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_stage_conditions.StageConditionsTest.test_abort_drain_cannot_prove_effects_with_admission_snapshot tests.test_stage_conditions.StageConditionsTest.test_drained_effect_proof_uses_final_snapshot tests.test_stage_conditions.StageConditionsTest.test_drained_effect_callback_error_preserves_cause_and_cleanup tests.test_stage_conditions.StageConditionsTest.test_drained_callback_cannot_replace_primary_task_timeout
```

GREEN（`/tmp/task5-fix3-green.log`）：`Ran 4 tests in 0.439s`，`OK`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_executor_retry_policy.py tests/test_stage_scheduler.py
```

指定回归（`/tmp/task5-fix3-targeted.log`）：`Ran 94 tests in 2.157s`，`OK`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/executor.py scripts/executor_system/stage_scheduler.py tests/test_stage_conditions.py
git diff --check -- scripts/executor_system/executor.py scripts/executor_system/stage_scheduler.py tests/test_stage_conditions.py
```

均退出 0。本轮未重跑 449 项、未 discover、未启动子代理，未合并。
