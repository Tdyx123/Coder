# Task 4 独立规格与质量审查

审查范围：`14c1791c..a3a70a2c`，任务 brief 中的任务 4 及固定契约。已阅读交付 diff、报告和与具体风险直接相关的既有执行路径；未修改源码、未启动子代理、未重跑已通过的 112 项测试。任务 5 的统一执行循环、阶段/全局条件及报告汇总不计为本任务遗漏。

**规格结论：不通过。质量结论：需要修改。**

## 必须修复

### 1. [P1] 准入解析不能在主调度线程无期限等待 controller 锁

位置：`scripts/executor_system/action_resources.py:78-80`（`bind` 调用实时 `runtime.find_object`；炉头、旋钮、水槽和腾手候选解析也读取实时 runtime）。

`StageScheduler._drive` 在主线程取得可取消的快照后调用本函数，但这里再次通过 `ThorRuntime.find_object → find_objects → current_objects` 进入普通阻塞式 `with controller_lock`。在阶段末屏障或 EVENT_CONDITION 下，另一个已运行的 worker 可以在快照释放锁之后进入 controller.step 并卡住，随后调度线程就卡在资源解析；`run_workers` 在主线程直接调用 drive，没有另外一个监督线程来执行截止时间检查，因此任务取消、截止时间及有界退出契约都失效。

定向复现：给已有快照的真实 ThorRuntime resolver 配置被另一线程占有的 controller_lock，安装 40ms ExecutionControl 截止时间后申请 PickupObject。100ms 时 resolver 仍未返回（输出 `resolver finished 60ms after deadline: False`），只有显式释放锁才结束。没有运行 Unity，也没有运行已有测试集。

修复要求：把显式/隐式对象选择限制在已捕获快照和快照适配器上，或者让准入阶段的所有实时读取通过同一可取消、有截止时间的锁入口；不能仅修复普通 bind，而遗漏 stove/sink/hand-placement helper。增加覆盖“快照已获取，worker 随后持锁，准入仍能按截止时间退出”的回归。

### 2. [P1] 转换身份必须在新物体状态可被再次准入之前连接

位置：`scripts/executor_system/runtime.py:1143-1144`，以及 `scripts/executor_system/resource_manager.py:661-668`。

目前 `bind_identity(old_id, new_id)` 只随高层 `_set_object_alias_current_object` 执行。真实 Break/Slice 路径先经 `_step_direct` 发布新 event、释放 controller_lock、通知 scheduler，然后 `object_action_by_object` 才调用 `update_object_alias_after_action`。在该窗口中，另一个机器人可按新物理 ID 解析并取得一个与旧 key 不冲突的租约；稍后 bind_identity 把两个 key 合并，却没有阻止或撤销已发出的冲突许可。这样原子申请本身正确，跨转换的互斥仍被破坏。

定向复现：真实 `object_action_by_object('BreakObject', ...)` 中，以 fake step 发布 `Egg|1 → EggCracked|2`，在 step 返回给高层前由第二线程调用真实 resolver 与 try_acquire。输出 `second lease granted before first helper returns: True`；高层完成身份修复后，`holders()` 输出 `{'Egg|1': ('a', 'b')}`。初始解析使用真实 find_object，会自动建立别名；不是缺少测试别名配置导致的假失败。

修复要求：在 controller 状态提交/再次准入的共同边界内完成转换身份连接，或以另一种机制在身份尚未确定时禁止新实体的许可；不能等两个 owner 已执行后再合并。增加对转换状态发布与高层返回之间窗口的并发回归，断言任何时刻同一逻辑身份最多一个 owner。

### 3. [P2] 更新受准入变更影响的普通入口测试 fixture

位置：`tests/test_parallel_runner.py:2484`（`OrdinaryExecutorFailureContinuationTest`），关联新增准入调用 `scripts/executor_system/stage_scheduler.py:167-170`。

父代理提供的 `/tmp/task4-entry-compat.log` 显示此类原先通过的六项测试现在全部失败：4 failures、2 errors。旧 FakeRuntime 没有 Cabinet/Apple 等动作所需物体，新增 resolver 在 patched adapter 之前报 `Could not find resource`，导致这些测试无法再检验原先的 legacy 继续、strict fail_robot/fail_stage、重试与效果已满足行为。这是受影响 fixture 的集成回归，不应通过关闭/绕过资源绑定来修。

修复要求：补齐这些入口场景的对象与持有状态 fixture，保留真实准入路径及原来的行为断言；重跑该类并完成父代理要求的 parallel_runner 相关覆盖。此项证据来自父代理提供的运行日志，本审查没有重复运行。

## 已核对且没有另报问题的范围

- 原子全部资源申请、失败不占部分 key、同 owner 多 grant 与幂等释放。
- 资源冲突四种策略、等待年龄、正常/失败/取消后资源释放与未退出 worker 的保守保留。
- helper 角色绑定、Faucet/Basin/knob/腾手容器限制、持有物独立检查及前后副作用失效分类。
- 资源审批后冻结导航成员、导航恢复作用域及资源管理锁不跨动作持有。

保守预申请当前敞开恢复候选的吞吐代价已在实施报告说明；本审查没有把这一设计说明当作上述两个并发缺陷的豁免理由。

## Fix 1 范围复审：a3a70a2c..3e59d841

**既有三项结论：已修复。修订版规格/质量结论：仍需修改，新增一项 P2。**

本轮只检查 fix1 diff、相关选择函数和新增定向回归；未运行任何 suite、未启动代理、未修改源码。

- 原 P1 主线程阻塞：解析使用独立 ThorRuntime 视图，普通对象、stove/sink 与腾手容器选择均不再读取真实 controller；快照附加 alias/operated 元数据锁采用可取消 acquire。
- 原 P1 身份发布空窗：转换和合法 Put 等实例替换的 identity 在 `_step_direct` 持有 controller_lock 时提交，早于解锁及 scheduler 通知；高层 alias 更新已不能合并已提交身份。新增测试覆盖 Break/Slice/Put 发布后暂停高层修复、多个切片、失败 step 已产生后代。
- 原 P2 入口 fixture：普通及容错入口补齐物体，并更新 mock Pickup/Put 持有状态；原策略断言保留。报告记录全部具名 216 项验证通过，本次复审不重复运行。

### 4. [P2] 快照选择视图需保留请求机器人的可见性和距离

位置：`scripts/executor_system/action_resources.py:78`（`view.current_objects = lambda agent_id=None: list(snapshot.objects_by_id.values())`）。

新视图丢弃 `agent_id`，而 `SnapshotStore.objects_by_id` 对重复对象 ID 使用当前活动 agent 的完整对象元数据。既有 `find_objects(..., agent_id=...)` 和 `compatible_receptacle_candidates` 都根据每个机器人的 `visible` 和 `distance` 排序。现在一个机器人的 Pickup/交互目标与自动腾手容器可能按另一个机器人的视角选择，并作为强绑定带入执行，导致原本附近可交互的对象被远处/不可见对象替代。这是从 live resolver 改为 snapshot resolver 引入的选择行为回归。

定向检查（未运行 suite）：构造相同两个 Mug 实例，robot1 元数据中 Mug|1 可见且距离 0.5、Mug|2 不可见且距离 5；robot2 元数据反之，并使当前活动 event 为 robot2。先捕获无旧 alias 的快照，再比较同一 robot1 的真实选择与 snapshot resolver。输出：`robot1 prior selection: Mug|1`；`robot1 snapshot selection: Mug|2`。

修复要求：在同一次可取消快照捕获中保存每个物理机器人的选择证据（至少 visible/distance，或独立的每-agent object view），并让 detached view 按 agent_id 使用对应证据；保持世界事实来自同一次快照，不能恢复 live controller 读取。增加活动 agent 与请求 agent 不同的目标选择及腾手容器选择回归。

## Fix 2 范围复审：3e59d841..ae665662

**原新增 P2 已修复。任务 4 规格与质量审查通过，无剩余 actionable finding。**

本轮只阅读 fix2 diff，并核对既有 `object_distance` 对未知距离的处理及具名测试日志；未运行 suite、未启动代理、未修改源码。

`SnapshotStore` 在原 controller 锁保护范围内捕获每个物理 agent 的 visible/distance，并随 resource_metadata 深度冻结。detached view 按 agent_id 叠加对应选择证据，其余对象事实继续使用同一快照的 authoritative objects_by_id。该 agent 未观察到的对象使用不可见/未知距离，既有排序函数使用大距离哨兵值 999999.0 处理未知距离；未恢复实时 controller 读取。

新增两项回归覆盖请求 agent 与活动 agent 不同的 Mug 选择、自动腾手容器选择、authoritative 状态保留、选择证据不可变及捕获后 live 可见性变化不影响解析。已核对 `/tmp/task4-round2-green-suites.log`：`Ran 143 tests in 3.733s; OK`，本轮没有重复运行。

本次修复范围内未发现新的行为回归。先前 fix1 关闭的两项 P1 与入口 fixture P2 保持关闭；任务 5 的后续契约仍按原任务边界处理。
