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

## Round 1 评审修复（基线 a3a70a2c）

落实 `task-4-review.md` 的两个 P1 与 fixture P2。以下描述替代上文初版的 live resolver 和 `_set_object_alias_current_object` 身份合并说明。

### 实现变化

1. **准入完全读取已捕获快照。** `WorldSnapshot.resource_metadata` 冻结 aliases、operated_names、物理 ID 身份历史。SnapshotStore 读取附加元数据锁也使用 50ms acquire + control.check，而不是引入不可取消等待。`snapshot_resource_view` 创建独立 ThorRuntime 选择视图，`current_objects` 只返回该快照对象；复用原排序/别名/放置兼容规则，但 view 的 alias 和 manager 都独立。普通 bind、stove/sink helper、held_object_type 和 compatible_receptacle_candidates 都使用该视图。主线程不访问 live find_object/current_objects，也不会将陈旧快照写回真实 aliases。worker 的已绑定查找继续更新兼容别名。

2. **对象身份在低层事件提交时发布。** `_step_direct` 在持有 controller_lock 时捕获 step 前对象 ID 集合和必要的手持对象；在事件提交、解锁、通知 scheduler 之前，`_commit_transformation_identities` 连接该次 step 新产生的 Slice/Break 子对象。多个切片都继承同一 source key；其他已存在对象/其他 step 创建的同类型对象不会被误合并。

3. **合法物理 ID 替换与别名修复分离。** Pickup/Put/Throw/Drop 的原实例消失、同类型新实例唯一可辨识时，在同一提交边界连接身份；Put 优先使用目标容器关系确认替换。保留既有 PutObject ID 替换行为。高层 `_set_object_alias_current_object` 只维护兼容别名，不再有权将两个已提交的独立 lineage 合并。即使 step 报失败，只要它实际发布了匹配后代，身份仍与原实例连续。手持成功证据逻辑不因该身份映射而改变。

4. **真实准入 fixture 补齐。** 六个 OrdinaryExecutorFailureContinuationTest 场景增加 Cabinet/Apple/Table 对象，成功 Pickup/Put mock 同步手持状态。全文件回归发现五个 TolerantExecutorTest 场景也缺对象，作同类数据补齐。保留全部策略、执行次数和效果判断断言。原资源测试改为更新快照数据/别名元数据，不再依赖实时 finder stub 选择目标；腾手 fixture 提供真实兼容容器中心。

### 本轮 RED

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.AdmissionPublicationRaceTest
```

原始输出 `/tmp/task4-round1-red-races.log`：`Ran 2 tests in 0.373s; FAILED (failures=5)`。

- Pickup、FillWater、Stove/自动腾手三个 subcase：在已捕获 snapshot 后，另一线程持有 controller_lock；40ms deadline 之后的 120ms 检查仍未完成，报 `snapshot admission blocked on live controller after deadline`。
- BreakObject / SliceObject 两个 subcase：真实 object_action_by_object 与 _step_direct 发布后暂停高层 alias 修复，第二 owner 获得新物理 ID 租约，报 `new physical ID escaped source lease before high-level repair`。
- 每个线程都用 finally 释放 Event 并 bounded join，无遗留 worker。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_parallel_runner.OrdinaryExecutorFailureContinuationTest
```

退出 1：`Ran 6 tests in 0.317s; FAILED (failures=4, errors=2)`，均由缺少 Cabinet/Apple 等场景对象提前失败；补齐后相同命令退出 0：`Ran 6 tests in 0.120s; OK`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.ResourceTest.test_delayed_alias_repair_cannot_merge_independent_committed_lineages
```

退出 1：`('a', 'b') != ('a',)`。旧高层 alias repair 将两个活跃 owner 合并到一个 lineage；现只允许低层提交确立身份。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.AdmissionPublicationRaceTest.test_put_replacement_retains_lease_before_high_level_repair
```

退出 1：新 Mug ID 在 Put 高层修复前获第二租约，`PutObject replacement escaped source lease before alias repair`。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_action_resource_leases.AdmissionPublicationRaceTest.test_failed_step_with_visible_descendant_still_preserves_identity
```

退出 1：controller 报失败但实际生成 EggCracked 时新 ID 无 holder，`() != ('a',)`。

初次完整具名回归 `/tmp/task4-round1-first-suites.log`：211 tests、6 failures、2 errors。除上述五个 Tolerant fixture 外，其余三个旧 resource fixture 依赖 live stub/未捕获 aliases/无实际容器位置；均已按快照契约补齐，没有放松 production 检查。

### 本轮最终 GREEN

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_resource_leases.py tests/test_parallel_runner.py tests/test_world_snapshot.py tests/test_runtime_object_aliases.py tests/test_navigation_execution_scope.py tests/test_executor_retry_policy.py tests/test_stage_scheduler.py
```

退出 0，完整输出 `/tmp/task4-round1-green-suites.log`：

```text
Ran 216 tests in 5.214s
OK
```

包括 deterministic Break/Slice/Put 发布窗口、snapshot 后持锁的全部显式/隐式准入路径、失败 step 的可见后代、快照 alias 冻结且不回写 live、全部切片继承与不同实例保持独立。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/action_resources.py scripts/executor_system/resource_manager.py scripts/executor_system/runtime.py scripts/executor_system/world_snapshot.py tests/test_action_resource_leases.py tests/test_parallel_runner.py
git diff --check
```

均退出 0、无输出。未运行 discover、未启动子代理。此前保守导航恢复候选的已批准性能取舍不变。本轮没有修改失败策略、执行主循环或 Task 5/6 的契约。
