# 执行器第二批：策略、调度与资源

第二批保留每机器人一个工作线程、controller 串行调用及现有移动预算，使用同一个执行循环处理普通与容错入口。`scheduler_version=2` 表示新的准入调度；`execution_policy=legacy` 只兼容动作失败后继续的行为，不承诺旧线程竞争顺序。

## 入口

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m scripts.executor_system.parallel_runner \
  path/to/executable_plan.py --execution-policy strict --movement-mode step \
  --output-dir coderun_results/strict

/home/dwb/.pyenv/bin/pyenv exec python path/to/executable_plan.py \
  --runner-mode --execution-policy legacy --movement-mode step \
  --metrics-output /tmp/legacy-result.json

/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_movement_modes.py \
  --execution-policy strict --manifest tests/fixtures/movement_benchmark_plans.json
```

`--execution-policy` 只接受 `legacy|strict`，默认 legacy，没有对应环境变量。库入口 `TaskRunner(runtime, execution_policy="strict")`、`run_action_plan_tolerant(runtime, plan, execution_policy="strict")`、`run_action_plan(plan, execution_policy="strict")` 同样支持该关键字。生成脚本的共享运行时同时支持 runner 和 standalone 模式。

父进程在 strict 时追加参数，并检查子记录的实际策略。默认 legacy 不追加新参数，因此旧的独立 argparse 脚本仍可按原接口运行；共享入口的旧生成文件无需修改即可使用新策略。旧独立脚本不认识 strict 时会明确失败，不会静默回退。历史记录缺失 scheduler_version 时按 1 分组，不填成 2。未产生可信子记录的启动/解析失败保留 requested_execution_policy，不能作为已完成执行证据。

基准报告写入所选输出文件父目录下的 `legacy/` 或 `strict/` 子目录。例如默认 strict 输出为 `reports/strict/movement_modes_benchmark.json`。每个结果组同时包含 metrics_schema_version、evaluation_version、execution_policy、scheduler_version、movement_mode；重建 summary 使用同一维度。跨组的顶层兼容聚合不能作为策略或调度器比较的证据。

## 失败与条件

| 动作 on_failure | strict | legacy |
|---|---|---|
| FAIL_STAGE | 取消本阶段剩余工作 | 记录失败并继续该机器人 |
| FAIL_ROBOT | 停止该机器人本阶段队列，同伴完成后阶段失败 | 记录失败并继续 |
| SKIP | 记录失败并继续 | 相同 |
| RETRY / WAIT_AND_RETRY | 仅 Teleport 自动重试，耗尽 fail_stage | 仅 Teleport 重试，耗尽继续 |
| SKIP_IF_EFFECT_ALREADY_TRUE | 非空效果列表全部由当前状态证明时成功，否则阶段失败 | 不能证明时失败并继续 |

strict 的 stage_failure_policy=FAIL_STAGE 停止任务；SKIP 允许继续下一阶段，但最终任务报告仍 failed。legacy 阶段条件失败也报告 failed，默认继续下一阶段。初始阶段条件 true 时未开始的动作记 skipped，started/attempts 保持零；结束时再次检查阶段条件。显式 global_success_condition=false 在两种策略下都令执行报告 failed。

wait_until、阶段/全局 callback 接收完整 WorldState；callback 异常保留 ConditionEvaluationError 原因，不等价于 false。expected_preconditions/effects 接受 callable 或 `{name, states/state, contains}` 目标字典，始终检查当前快照；unknown 前置条件等待，unknown 效果失败，历史 HOT/COLD 证据不能替代当前状态要求。任务超时、取消、controller 不可用与未捕获工作线程错误仍是任务级终止。

BARRIER_AT_STAGE_END 允许机器人独立推进；BARRIER_EACH_STEP 只等待当前已发放动作完成；EVENT_CONDITION 在状态、资源或截止时间事件后重查。等待条件的机器人不参加导航波次，但仍是物理障碍。无可运行/执行中动作时由协调器最多每 50 ms 提交一次 Pass，推进条件时间；timeout_ticks 从当前动作首次等待起累计。真实依赖环最终由截止时间中止并保存等待条件、资源持有者和全机器人快照。

## 资源需求与快照接口

`resolve_action_resources(runtime, snapshot, robot_id, action)` 返回不可变的 `ResolvedActionResources(keys, bindings)`。资源以实际 objectId 实例申请；`ActionResourceManager.try_acquire(owner, keys)` 原子授予全部 keys，失败不持有部分资源。嵌套 helper 复用绑定和租约，在执行结果返回后释放；终止收尾先证明工作线程静止再释放。`on_conflict` 的 WAIT 保留申请，RETRY_NEXT_TICK 下一轮重新解析，SKIP 记 skipped，FAIL_STAGE 使阶段失败。超时/取消始终优先。

| 动作 | 固定资源需求 |
|---|---|
| Pickup、TeleportObjectToHand、Open/Close、Break/BreakEgg、Slice、Clean/Dirty、EmptyLiquid、SwitchOn/Off | 解析后的单个对象 |
| ToggleObjectOn/Off | objectId 对象 |
| PutObject / ThrowObject | 声明对象、实际手持物和容器 / 实际手持物 |
| RunMicrowave、RunCoffeeMachine、ColdObject | 设备和目标 |
| RunToaster | Toaster 和 Bread |
| CookByStoveBurner | 炉头、对应旋钮、容器、食物 |
| HeatByStoveBurner / FireByStoveBurner | 炉头、对应旋钮、目标 |
| FillWater | Faucet、解析出的 SinkBasin、目标 |
| PrepareEgg | Egg 与容器，保留原 BreakObject 语义 |
| 可嵌套拾取的复合动作 | 需要腾手时增加当前手持物与预选兼容容器 |
| GoToObject | 不因目的地自动锁对象；加入可能恢复时关闭/重开的当前 open/openable 对象 |
| 无对象的直接动作 | 无对象需求 |

自定义 object_resources 只能增加需求。所有可能导航恢复的动作也预先绑定当前打开的可开闭对象。别名、slice/破壳后的身份转换保持资源身份连续；不能未经批准改选同类型另一实例。首次副作用前绑定失效可释放重排，副作用开始后失效报告失败，不重播整个复合操作。另一机器人手持的目标被拒绝，不隐式交接。

`SnapshotStore.capture(runtime, control)` 在可取消 controller 锁内一次读取全部物理机器人的 metadata，返回深度不可变 `WorldSnapshot(version, robot_positions, robot_rotations, held_objects, objects_by_id, held_object_sources, resource_metadata)`。version 来源于真实低层状态提交，公开 WorldState 映射来自同一快照。resource_metadata 冻结 aliases、identities、operated_names 及每机器人 visible/distance 选择证据；资源解析使用 detached view，不重新读取 live controller。报告中的 snapshot 是可 JSON 序列化副本。阶段报告 snapshot_is_current 表示收尾快照是否在工作结束后捕获且版本仍等于 runtime.state_version；超时/取消时只剩旧诊断快照，则效果为 unknown，不调用 callback 证明成功。

## 结果解释

process_status 表示进程是否完成；execution_status 表示计划执行与阶段/全局条件是否完成；task_success、TC/GCR/SR/RU 保留第一批固定目标分母与评分定义。三者分别解释。举例：动作全部成功但显式 global 条件 false，execution_status=failed；若固定目标已全部满足，仍可能 task_success=true。

scheduler2 仅在 execution_quiescent=true 且没有超时/取消时允许有效目标评估，包括普通策略/条件导致的 failed。没有静止证明时不提交 Done 或最终评估。父进程非零退出/超时继续覆盖子成功声明并清空最终评分。validator 保留 fixed denominator、GCR、TC、SR/RU 一致性检查；scheduler2 允许未准入的 skipped/cancelled 终态，要求 succeeded+failed <= started <= planned、attempts >= started、全部终态+unexecuted=planned。历史 scheduler1 保留 terminal <= started 的约束。

报告包含 actions、stages、attempts、waits、waves、resources、errors、final_snapshot。resources 的 acquired 记录含 keys/action_key，released 按 action_key 对应原申请；actions 保留终态原因及实际 failure decision。benchmark 将 execution_failure 与 goal_failure 分开标记，但诊断时仍需同时阅读执行状态与目标评分：strict 更早停止导致更多失败，不能直接解释成模型或导航能力回退。

## 三个可复现语义例子

```bash
/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_2/semantic_examples.py \
  --output /tmp/executor-batch-2-examples.json
```

此驱动使用真实 TaskRunner、StageScheduler、lease 和 StepMovementStrategy；Unity/高层交互由确定性仿真替身提供。它不是真实 Unity 验收。已保存本次运行的完整[机器可读证据](../reports/executor_batch_2/semantic_examples.json)。共享设备同伴的线程交错可能不同；驱动检查资源不重叠及每机器人的语义顺序。

1. legacy 抓取失败后继续：robot1 PickupObject 失败，随后仍 OpenObject、CloseObject；robot2 完成共享 Microwave 的 Open/Close。实录为 r1 Pickup → r2 Open → r1 Open → r2 Close → r1 Close，资源 acquire/release 从不重叠。阶段条件 false→true，5 attempts、4 succeeded、1 failed，执行 partial。
2. strict FAIL_ROBOT：robot1 PickupObject 失败后两项尾部动作未执行；robot2 Open/Close 完成。实录为 r1 Pickup → r2 Open → r2 Close。阶段条件 false→true，但失败机器人仍使阶段 failed；3 attempts、2 succeeded、1 failed、2 unexecuted。
3. step 中 A 等 B：robot1 的 wait_until 检查同一快照内 robot2.x==0.5，robot2 GoToObject 经真实 StepMovementStrategy 产生两次 MoveAhead；导航波次只有 robot2，完成后 robot1 Wait 执行。阶段条件 false→true，2 succeeded、零 Teleport，执行 completed。

## 验证与交接

从仓库根目录运行 `bash reports/executor_batch_2/verify.sh`，它明确列出第一批全部集合和第二批新增/受影响集合，不使用 discover。第一批报告不修改。任务审查历史保存在 [reviews](../reports/executor_batch_2/reviews/)，后续修复与最终裁定应一并阅读。

真实 Unity 验收：**pending**，由主代理在最后审查和最终 production SHA 确定后运行。固定 manifest 12 例 × 两策略 × 两移动模式 = 48 次；每个组合独立目录/identity，使用原始目标数和原生成样本，保存 stdout/stderr、源码摘要、child metrics、父进程权威结果及清理事件。可复用命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_2/run_real_validation.py \
  /tmp/coder-executor-batch-2 /home/dwb/thor/Coder /tmp/executor-batch-2-real-final
```

source 参数指含固定 untracked 生成脚本的原仓库；code 参数选择已审查执行器。`--limit 1` 只做预检，不算最终验收；输出目录不可复用已有 report。第三批继续处理动作注册表、大文件拆分和架构/性能工作，不改本批移动预算。
