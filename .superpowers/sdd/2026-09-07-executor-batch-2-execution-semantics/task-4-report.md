# Task 4 实施报告

状态：完成实现、自查和指定验证，等待批次评审；未合并 main。基线 `14c1791c`（Tasks 1–3 已批准）。遵循 supplied 执行计划与 TDD；未启动子代理，未运行 discover。

## 接口与接入

- `resource_manager.ActionResourceManager.try_acquire(owner, keys)` 原子申请全部实例 key，失败不保留部分 key。`blockers(keys)` 返回 owner 元组；`ResourceLease.release()` 幂等。执行时不持有 manager 互斥锁；同 owner 的重叠 grant 引用独立，释放一个不会释放其他 grant。
- `ActionResourceManager.bind_identity(old_id, new_id)` 保存物理 ID 历史。运行时 `_set_object_alias_current_object` 在移除旧 reverse alias 之前链接身份；已有活跃租约也按当前 canonical identity 检查冲突。不同实例仍有独立 key。
- `action_resources.ResolvedActionResources(keys, bindings)` 使用不可变 bindings；`resolve_action_resources(runtime, snapshot, robot_id, action)` 在 scheduler 主线程解析显式参数、隐式设备和腾手容器。自定义 object_resources 增加需求，不能取消固定需求。
- scheduler `_admit_resources` 在 `admit_wave` 冻结成员之前完成资源审批，worker 通过 `action_resource_scope` 执行原 `Executor.execute_action`。结果边界释放；取消时只在 worker 已 quiescent 后释放。诊断 `resource_holders` 为可 JSON 序列化的 key → owner 元组映射。
- WAIT 保留已解析申请；RETRY_NEXT_TICK 下一轮重新解析；SKIP 写入 skipped 终态、推进游标；FAIL_STAGE 写入动作 failed/fail_stage，再经现有 `StageFailureDecisionError` 返回 StageOutcome。等待年龄继续使用 Tasks 3 已有排序，测试证明等待者能胜过重复高优先级竞争者。
- `context.runtime_scope` 使用 thread-local、支持嵌套恢复；`get_runtime` 保留原全局 fallback。adapter 调 helper 不再并发修改共享 context.runtime。
- 实例绑定贯穿 `runtime.find_objects`、SinkBasin/炉头/旋钮 helper、literal Faucet 和自动腾手 placement。自动腾手只尝试已批准容器；嵌套 helper 复用 owner/grant，不再选择未获准容器。
- `runtime.step` 预检；`_step_direct` 在实际 controller 调用前再次检查并标记副作用，覆盖恢复路径的直接调用。前置失效 `ResourceBindingDeferred` 释放并重排；副作用开始后的失效 `ResourceBindingInvalid` 报普通动作失败，不重播复合动作。低层检查也独立拒绝别的机器人手持对象（OBJECT_HELD_BY_OTHER），不调用 handoff。

## 固定资源表

| 动作 | 资源 |
|---|---|
| Pickup/TeleportObjectToHand/Open/Close/Break/BreakEgg/Slice/Clean/Dirty/EmptyLiquid/SwitchOn/SwitchOff | 已解析单对象实例 |
| direct ToggleObjectOn/Off | objectId 实例 |
| PutObject | 声明对象、实际手持物、容器 |
| ThrowObject | 实际手持物 |
| RunMicrowave / RunCoffeeMachine / ColdObject | 设备与目标 |
| RunToaster | Toaster 与 Bread |
| CookByStoveBurner | 实际炉头、对应旋钮、容器、食物 |
| HeatByStoveBurner / FireByStoveBurner | 实际炉头、对应旋钮、目标 |
| FillWater | 实际 Faucet、解析出的 SinkBasin、目标；保留 Sink 不存在时的原 basin fallback |
| PrepareEgg | Egg 与容器；仍只执行原 BreakObject 语义 |
| 可嵌套 Pickup 的动作 | 需要腾手时增加当前手持物与一个预先选定的兼容容器 |
| GoToObject | 不因 destination 自动申请对象租约；仅增加下述可能的导航恢复资源 |
| 不涉及物体的 direct 动作 | 无对象需求 |

## 导航与兼容性决策

所有直接移动/旋转/恢复路径进入已有 navigation_execution_scope；完整拾取 backoff、可见性恢复、腾手/返回序列保持导航作用域，先资源审批，再导航锁，再 controller 锁。GoTo wave 收集外部没有提前持有导航锁。未启用旧 PositionResourceMgr/ObjectResourceMgr 审批，原身份/hand-off API 保留。

原移动恢复从 MoveAhead 错误的 object_name 选择 **openable 且 isOpen** 的对象，执行 Close → retry Move → Open。为在任何副作用前完整准入，同时保留恢复，GoTo 和会嵌套导航/恢复的 Pickup/Slice/Toaster/FillWater/Stove helper 保守声明当前快照中满足该条件的实例。这是可能被实际恢复操作的有限候选集，不是全场景对象锁；closed 对象和正常 unrelated 对象操作不被统一锁定。父代理明确批准这个实现选择。

代价：多个导航动作共享敞开恢复候选时可能串行准入；若 destination 本身属于这样的恢复候选，会因恢复需求而获租约。没有调整路径、间距、候选或重规划预算。新出现的未批准目标仍被低层防线拒绝，不能在已有副作用后扩展资源。

原 GoTo 的提前腾手移到后续实际 Pickup；next_action 交互目标选择保留。Pickup 准入持有物/容器后完成腾手、返回和实际交互。这样避免在导航 wave 收集前因隐式腾手持有导航锁。

## RED 证据

1. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_resource_leases.py`
   初始退出 1：`ImportError: cannot import name 'ActionResourceManager'`，`Ran 1 test`，`FAILED (errors=1)`（计划指定最小导入 RED）。
2. 同资源测试命令：新增晚到 held-by-other 和 auto-hand reselection 回归时退出 1：
   - `AssertionError: RuntimeError not raised`（未拒绝其他机器人新持有目标）；
   - attempted `['CounterTop|2', 'CounterTop|1'] != ['CounterTop|1']`（原 fallback 选择未批准容器）。
   `Ran 21 tests ... FAILED (failures=2)`；后来去除继承造成的重复测试。
3. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.MovementAdmissionTest`
   退出 1，`Ran 2 tests ... FAILED (failures=2)`：direct Teleport 只有 controller，无 navigation scope；拾取 backoff 移动和最终交互都未持有作用域 `[False, False]`。
4. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.DirectSideEffectTest.test_direct_recovery_cannot_bypass_object_grant`
   退出 1，`ResourceBindingDeferred not raised`：_step_direct 可绕过未批准对象检查。
5. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.AdmittedNavigationRecoveryTest`
   退出 1：`Tick 0: robot1 failed GoToObject: RESOURCE_BINDING_INVALID: unleased helper target Drawer|1`，`'partial' != 'completed'`。实现恢复候选预准入后相同测试 `Ran 1 test in 0.007s; OK`。
6. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.SchedulerResourceTest.test_conflict_skip_and_fail_stage_do_not_execute_loser`
   退出 1：FAIL_STAGE 未保存动作结果，`last_action_result` 为 None。现写 failed/fail_stage 结果和 ledger 终态。
7. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.HelperBindingTest.test_fillwater_sink_query_can_use_existing_basin_fallback`
   退出 1：`Could not find AI2-THOR object matching 'Sink'`。现保留原 SinkBasin fallback，并锁定实际 Faucet/Basin 而不要求不存在的 Sink 实例。

## GREEN 与自查

最终命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_resource_leases.py tests/test_navigation_execution_scope.py tests/test_runtime_object_aliases.py tests/test_executor_retry_policy.py tests/test_stage_scheduler.py
```

退出 0；完整最终输出 `/tmp/task4-tests.log`：

```text
Ran 112 tests in 1.696s
OK
```

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/action_resources.py scripts/executor_system/resource_manager.py scripts/executor_system/stage_scheduler.py scripts/executor_system/runtime.py scripts/executor_system/actions.py scripts/executor_system/context.py scripts/executor_system/action_plan.py tests/test_action_resource_leases.py tests/test_stage_scheduler.py
git diff --check
```

都退出 0，无输出。仅运行任务指定四类回归和直接受影响 scheduler suite；未运行全量测试。

自查覆盖：全部原子申请、反序多 key 无部分占有、不同实例并发、共享复合动作不交错、失败/取消释放、旧/新物理 ID 连续冲突、持有权独立检查、四种冲突策略、等待年龄、thread-local 嵌套、Faucet/Basin/knob/手部容器绑定、副作用前/后失效、导航锁及真实恢复、next_action 选择兼容回归。既有 scheduler 两个 admission spy 测试改为包装真实准入而不是跳过资源上下文。

限制：以 fake controller/真实执行链路验证，不包含 Unity 场景运行；保守恢复候选可能增加资源等待。结果总体汇总和阶段/全局条件仍由 Task 5 完成统一；本任务没有修改失败策略算法或重复执行主循环。
