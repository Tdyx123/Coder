# 执行系统第三批：动作注册、职责拆分与性能修改计划

> **面向执行代理：** 使用 `superpowers:executing-plans` 按任务实施，使用复选框跟踪进度。本文件是修改计划，不代表其中的代码已经实现。

**目标：** 用统一动作定义连接校验、资源和分派，拆清运行时职责，去除生产路径的隐式全局上下文，并用可复现证据优化导航查询成本。

**架构：** 前两批的执行策略、协调器、评估与结果协议保持稳定。先将运行时整理为兼容门面与明确服务，再建立分项测量；可达点刷新优化通过显式模式与现有完整刷新实现对照。

**技术栈：** Python 3.9、标准库 `dataclasses/typing/contextvars/threading/time/hashlib/unittest`、AI2-THOR 和现有计划转换器；不增加第三方依赖。

**规格：** 本文件“范围与固定决策”“目标模块边界”“动作契约”构成自包含规格，源于 `070b808b` 全面评审和前两批的交付接口。

**依赖：** 先完成[第一批](2026-09-07-executor-batch-1-reliability.md)和[第二批](2026-09-07-executor-batch-2-execution-semantics.md)。实施前记录它们的提交 SHA，不能以重构名义回退已修复行为。

## 范围与固定决策

- 统一动作定义，复用第二批前置/后置条件和资源租约；完善高层参数形式与低层 THOR payload 的明确分派。
- 逐项提取服务，保持 `ThorRuntime` 公共方法和旧导入路径；不引入新的框架、异步事件循环或分布式任务系统。
- 生产执行使用显式 runtime/action context；兼容 helper 保留任务上下文适配，不再临时改写全局 runtime。支持同一进程中两个显式 runtime 并存。
- 移动默认仍为 step，执行策略默认仍为 legacy；使用第一批 `fixed_goals_v2` 和第二批 `scheduler_version=2`，不改动其指标语义。
- 新增 `--reachable-refresh-mode full|event`，默认 `full`。event 为本批交付的可选优化，即使满足性能门槛也不在本批切换默认值。
- 保留 0.25 m 网格、0.35 m 间距和既有规划预算。性能优化不得放宽碰撞检查、取消检查或使用 Teleport 代替 step。
- 不对未知收益承诺加速比例。确定性测试约束查询次数，真实实验同时检查正确性与耗时；两者分别报告。
- 不改变 controller 图像分辨率、渲染配置和 GPU 并发默认值，以避免把多种变量混入此次测量。
- 所有验证从仓库根目录使用 `/home/dwb/.pyenv/bin/pyenv exec python`。完整实验报告保存在新运行目录，不覆盖历史 `reports/movement_modes_benchmark.*`。

## 目标模块边界

| 模块 | 责任与接口 |
|---|---|
| 新建 `plan_types.py` | 迁移 Action、PlannedAction、StagePlan、TaskPlan、ActionResult、机器人状态与相关常量；无 runtime/CLI 导入 |
| 新建 `action_registry.py` | ActionSpec、输入形式规范化、参数校验、资源需求和执行入口登记 |
| 新建 `capability_checks.py` | 技能名称规范化、有限质量检查和纯能力判定，供生成与运行共用 |
| `plan_validator.py` | 从重导出改为真实实现，调用注册表；运行前检查结构与策略，场景初始化后检查具体对象与机器人能力 |
| 新建 `controller_client.py` | 单次 THOR 提交、锁、取消与版本提交边界；不负责高层目标选择 |
| 新建 `object_resolver.py` | 对象匹配、实例绑定、别名变化和容器关系；复用现有确定性规则 |
| 新建 `object_interactor.py` | 对象动作、腾手、复合 helper 与故障恢复；消费第二批的准入绑定与租约 |
| 新建 `runtime_artifacts.py` | 帧、视频、元数据与运行目录；消费第一批输出隔离 |
| 新建 `runtime_metrics.py` | 有界分项计时、调用计数与配置快照 |
| 新建 `reachable_map.py` | 静态地图、机器人占位、场景变化缓存及 full/event 刷新策略 |
| `runtime.py` | ThorRuntime 组装门面，保存依赖并向服务委托；保留原公开 API |
| `action_plan.py` | 旧导入兼容门面；从 plan_types、plan_validator、stage_scheduler 等重新导出 |
| 新建 `scripts/benchmark_executor_regression.py` | 固定样例重复实验、分组对照和验收检查 |

未带目录的生产模块均位于 `scripts/executor_system/`。第一批 `evaluation.py/execution_control.py/run_results.py/process_supervisor.py` 与第二批 `execution_policy.py/stage_scheduler.py/world_snapshot.py/action_resources.py` 保持职责，不再建立同功能副本。

## 动作契约

```text
ActionSpec(name: str, helper_arity: Optional[int], direct_required: Tuple[str, ...],
           direct_allowed: bool, executor, resource_resolver)
NormalizedAction(action: Action, form: str, parameters: Mapping[str, Any])
    form: helper / thor
ActionRegistry.get(name: str) -> ActionSpec
ActionRegistry.normalize(action: Action) -> NormalizedAction
ActionRegistry.validate_shape(action: Action) -> None
ActionRegistry.prepare(runtime, snapshot, robot_id, action) -> PreparedAction
PreparedAction(normalized: NormalizedAction, resources: ResolvedActionResources)
ActionRegistry.execute(runtime, robot_id, prepared, action_context) -> Any
```

`ActionRegistry` 使用固定本地登记表，不执行模型生成代码、不通过 `getattr` 搜索任意名字。资源解析复用第二批的底层解析函数，由 `ActionSpec.resource_resolver` 直接调用；不得维护两个不同的物体集合。旧 `resolve_action_resources` 变为查找 ActionSpec 后调用其 resolver 的兼容委托；registry 的 prepare 不反过来调用该兼容函数，避免递归依赖。

### 高层 helper 参数数目

| 参数数目，不含 robot | 动作 |
|---|---|
| 0 | WaitOneTick、ThrowObject、WaitUntil（条件来自 wait_until） |
| 1 | GoToObject、PickupObject、TeleportObjectToHand、SwitchOn、SwitchOff、OpenObject、CloseObject、BreakObject、BreakEgg、SliceObject、CleanObject、DirtyObject、EmptyLiquid |
| 2 | PutObject、PrepareEgg、RunMicrowave、RunCoffeeMachine、RunToaster、HeatByStoveBurner、FireByStoveBurner、FillWater、ColdObject |
| 3 | CookByStoveBurner |

保留 BreakEgg/PrepareEgg 的 Egg 限制。纯低层动作 MoveAhead、RotateLeft/Right、LookUp/Down、Pass、Wait、Done、Teleport、ToggleObjectOn/Off 沿用现有 payload 参数；Wait/WaitOneTick 继续转换为 Pass。

存在 helper `args` 时按 helper 解析并精确检查数目；只有低层参数时按 THOR 解析。两种形式混用且目标冲突时拒绝，不能悄悄选择一个。单对象 THOR 动作要求 objectId；Teleport 要求有限三维 position；角度/移动量要求有限数值；agentId 必须等于队列 robot 对应的物理 agent，不能跨机器人执行。

既有既可高层又可低层的 PickupObject、PutObject、OpenObject、CloseObject、BreakObject、SliceObject、CleanObject、DirtyObject、ThrowObject 加入显式 direct 登记。PutObject 的直接形式从当前快照确定手持对象并申请其租约，不能绕过持有物检查；Pickup 的直接形式同样接受能力和质量验证。

场景依赖校验复用现有生成校验的纯技能映射和有限质量规则，不能为了复用而从运行时导入整个转换 CLI。高层复合动作保持已声明的技能要求，不在本批改变 benchmark 的机器人能力定义。隐式恢复不能绕过技能/质量限制。

共用模块定义 `normalize_skill_name(value: str) -> str`、`finite_nonnegative_number(value) -> Optional[float]` 和 `capability_failure(robot, action_type, *, object_name=None, object_mass=None) -> Optional[Dict[str, Any]]`。最后一个函数返回首个错误的 `reason/message/details`，按先技能、后显式 Pickup 质量的既有顺序检查，不读取文件或启动场景。生成侧保留 `_ObjectMassLookup`、错误包装和结果删除行为；运行侧从准入快照提供质量，恢复中新增的抓取同样执行质量检查。测试 fake 应补充有效机器人和对象元数据，不能为让旧测试通过而跳过运行校验。

## 任务 1：建立动作注册表并统一校验与分派

**文件：** 新建 `action_registry.py`、`capability_checks.py`、`tests/test_action_registry.py`；修改 `plan_validator.py`、`action_plan.py`、`action_resources.py`、`generated_plan_runtime.py`、`scripts/baseline_converters/generation_validation.py` 和相关 fake 元数据。

**输入/输出：** 使用本计划动作契约；第二批的 `tests/test_plan_contract.py` 成为行为约束，移交实现但保留测试。

- [x] 加入高层参数错误和低层参数合法的对照测试，导入 Action、ActionRegistry：

```python
def test_direct_pickup_is_not_dispatched_as_empty_helper(self):
    registry = ActionRegistry()
    action = Action.from_any({"action": "PickupObject", "objectId": "Apple|1"})
    normalized = registry.normalize(action)
    self.assertEqual(normalized.form, "thor")
    self.assertEqual(normalized.parameters["objectId"], "Apple|1")

def test_missing_pickup_target_fails_before_execution(self):
    with self.assertRaisesRegex(ValueError, "PickupObject.*target"):
        ActionRegistry().validate_shape(Action("PickupObject"))
```

- [x] 为动作参数表逐项建立 subTest；覆盖合法旧生成输入、冲突双形式、未知动作、跨 agentId、非有限数值、Put 未持有、质量超限、缺失技能、复合动作隐式资源绑定。
- [x] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_registry.py tests/test_plan_contract.py`，确认新增需求为 RED。
- [x] 实现固定登记表并验证所有当前支持的动作恰好登记一次。每条记录包含形状检查、执行入口、资源解析，不将技能和参数规则散落到 adapter 分支。
- [x] `PlanValidator` 分为纯结构检查和场景检查：前者在创建 controller 前运行，后者在初始化后、任何任务动作前运行。机器人与对象缺失给出 stage/robot/cursor 定位错误，不能让 IndexError 落入失败继续策略。
- [x] `AI2ThorAdapter.execute` 委托注册表，但保持公开签名和 next_action/action_wave 传递。旧资源推断 API 委托注册表，删除被替代的重复动作集合。
- [x] 运行注册表、计划契约、`test_generation_validation.py`、`test_pddlrun_executor_adapter.py`、`test_executor_retry_policy.py`、`test_parallel_runner.py`。

**交付：** 新动作只需登记一次，输入校验与实际执行接受相同形式。

## 任务 2：拆分类型、controller 与运行产物

**文件：** 新建 `plan_types.py/controller_client.py/runtime_artifacts.py`、`tests/test_runtime_facade.py`；修改 `runtime.py/action_plan.py` 与兼容导出模块。

**接口：** `ControllerClient.step(payload, *, check_success, save_frame)` 委托既有单步边界；`RuntimeArtifacts.prepare()/save_frames(event)/write_final_metadata()/generate_video()` 保留原行为。`ThorRuntime` 的公开方法仍可调用。

- [x] 先加兼容导入和委托行为测试，使用现有 FakeEvent/FakeRuntime，不启动仿真：

```python
def test_legacy_action_import_is_same_type(self):
    from executor_system.action_plan import Action as legacy_action
    from executor_system.plan_types import Action as canonical_action
    self.assertIs(legacy_action, canonical_action)
```

- [x] 添加一次 step 对应一次取消检查/状态提交、controller 锁互斥、重复 stop 幂等、输出目录隔离、异常时结果仍落盘的行为测试；避免只断言调用了某个新类。
- [x] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_facade.py`，确认新模块导入为 RED。
- [x] 先迁移无副作用类型和常量，再迁移 controller，最后迁移产物。每次迁移保持原方法签名与默认参数；不同时改变动作算法。生产内部模块改从 plan_types 导入类型，不能通过 action_plan 兼容门面形成环；基础类型不得导入 runtime、registry 或 CLI。
- [x] controller 的锁、取消、状态版本提交与计数只保留一个实现。`runtime._step_direct` 作为门面调用它，旧测试中对 runtime 的替换仍通过明确注入的 controller/事件支持。
- [x] 保存 `central_executor.py/synchronous_executor.py` 的兼容别名，并在文档标记已无中央工作线程；不重新实例化旧 worker。
- [x] 每完成一个职责提取就运行本批门面测试和第一批相关回归；整任务结束运行第二批的调度、资源、快照测试。

**交付：** 类型与基础运行边界可独立理解，旧生成脚本和旧导入路径仍可用。

## 任务 3：提取对象服务并消除隐式全局执行上下文

**文件：** 新建 `object_resolver.py/object_interactor.py`、`tests/test_runtime_context_isolation.py`；修改 `runtime.py/actions.py/context.py/demo_state.py/task_plan.py/action_registry.py`。

**接口：** `ObjectResolver(runtime).find_objects/find_object/register_object_id_bindings/resolve_object_alias` 保持对应 runtime 方法的参数；`ObjectInteractor(runtime)` 接收 PreparedAction 和第二批动作上下文。`context.bind_runtime(runtime)` 提供有作用域的兼容 ContextVar 绑定。

- [x] 添加同进程两个 runtime 的线程隔离测试；每个线程显式绑定自己的上下文，调用 helper 后只能改变所属 runtime 的动作记录和评估证据。

```python
def test_context_binding_is_restored(self):
    from executor_system.context import bind_runtime, get_runtime
    first, second = object(), object()
    with bind_runtime(first):
        self.assertIs(get_runtime(), first)
        with bind_runtime(second):
            self.assertIs(get_runtime(), second)
        self.assertIs(get_runtime(), first)
```

- [x] 覆盖 helper 抛异常仍恢复上下文、切片别名不跨任务、两任务相同对象名不共享历史、并行解析不修改同一个函数 globals。
- [x] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_context_isolation.py`，确认 RED。
- [x] 按顺序提取对象查找/绑定，再提取交互/恢复。保留 runtime 门面，服务始终访问注入的 runtime，不访问模块全局 runtime。
- [x] 注册表生产入口直接调用显式服务。为兼容 actions helper，在每个实际工作线程入口使用 `bind_runtime`；ContextVar 不依赖线程自动继承。删除 `AI2ThorAdapter.call_generated_helper` 临时写模块 globals 的行为。初始化随机数使用 runtime 自己的 `random.Random(seed)`，默认 seed 仍为 0，不调用全局 random.seed 干扰其他 runtime。
- [x] `TaskPlanParser.record_subtask` 优先读取已存在的 `planned_actions`。没有显式动作时，创建共享原函数代码但拥有复制 globals 的 `types.FunctionType`，只在该副本替换 recorder，保留 defaults/closure/kwdefaults；若使用不支持的间接 helper/动态控制流则明确拒绝并要求显式计划，不能静默执行原函数产生真实副作用。
- [x] 动态函数录制兼容层在执行前用 AST 约束：仅允许直线动作 helper 调用和无副作用的局部赋值；拒绝循环、条件、任意函数调用、全局写入及无法取得源码的函数。已有 `planned_actions` 不需要动态录制。
- [x] 运行上下文测试、`test_runtime_object_aliases.py`、`test_action_plan_pre_task.py`、`test_executor_retry_policy.py`、`test_pddlrun_executor_adapter.py` 和资源租约测试。

**交付：** 显式 runtime 隔离成为生产路径，旧 helper 兼容不再依赖共享可变全局值。

## 任务 4：加入分项计时和可复现实验记录

**文件：** 新建 `runtime_metrics.py`、`scripts/benchmark_executor_regression.py`、`tests/test_runtime_metrics.py`、`tests/test_executor_regression_benchmark.py`；修改 `controller_client.py/movement_coordinator.py/generated_plan_runtime.py/parallel_runner.py`。

**公共接口：**

```text
RuntimeMetrics.observe(category: str, seconds: float) -> None
RuntimeMetrics.increment(name: str, amount: int = 1) -> None
RuntimeMetrics.snapshot() -> Dict[str, Any]
RuntimeMetrics.measure(category: str) -> ContextManager[None]
```

- [ ] 写计时和有界存储测试，导入 RuntimeMetrics：

```python
def test_metrics_keep_counts_without_unbounded_samples(self):
    metrics = RuntimeMetrics()
    for _ in range(10000):
        metrics.observe("controller", 0.01)
    snapshot = metrics.snapshot()
    self.assertEqual(snapshot["controller"]["count"], 10000)
    self.assertAlmostEqual(snapshot["controller"]["total_seconds"], 100.0)
    self.assertNotIn("samples", snapshot["controller"])
```

- [ ] 增加 FakeClock 测试，分别覆盖锁等待、controller 调用、导航规划、可达点查询、动作恢复和产物输出计时。运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py`，观察 RED。
- [ ] 每类计时只保存 count/total/max；单任务不保存无界样本。P50/P95 从基准的逐任务、逐次重复记录计算。GetReachablePositions 计入 controller 总时间，也作为其子类单列，禁止把重叠计时相加声称总耗时。
- [ ] 记录代码 SHA、Python/AI2-THOR 版本、平台、GPU 标识、场景、机器人数量、种子、计划内容哈希、manifest 哈希、三个协议版本和所有有效运行参数；不保存 API key 或完整环境变量。
- [ ] 基准脚本实现 `--manifest`、`--output-dir`、`--repetitions`（默认 5）、`--movement-modes`、`--execution-policies`、`--reachable-refresh-modes`、`--max-workers`（默认 1）、`--check`。重复编号从 1 开始，每次运行一个新仿真实例。
- [ ] 保留每个 `(case, mode, policy, refresh_mode, repetition)` 结果，平均值不覆盖单次失败。不同评估/调度版本不能自动聚合，原始缺失/超时数量必须展示。
- [ ] 使用固定 manifest 路径，不重新挑选更容易的样例。样例缺失时列出缺失项并返回非零，不缩小分母。
- [ ] 先保存完整刷新下的功能和耗时基准，再开始下一任务；没有真实环境时先完成 fake 基准，真实验收状态明确保持未完成。

**交付：** 性能问题能归因到调用和等待成本，后续优化有固定对照。

## 任务 5：实现可选的事件驱动可达点刷新

**文件：** 新建 `reachable_map.py`、`tests/test_reachable_map_cache.py`；修改 `movement.py/movement_coordinator.py/runtime.py/controller_client.py/generated_plan_runtime.py/parallel_runner.py` 和现有移动测试。

**接口：** `ReachableMapCache.refresh(runtime, *, force: bool) -> RuntimeWorldSnapshot`；MovementConfig 新增 `reachable_refresh_mode="full"`、`full_refresh_interval_steps=4`，CLI 透传 refresh_mode。

`RuntimeWorldSnapshot` 从现有 movement_coordinator.py 迁移到 reachable_map.py，字段和类身份兼容，原路径重导出；reachable_map 不反向导入协调器。父子运行入口均增加 `--reachable-refresh-mode`，未指定时使用 full。

- [x] 添加可编程 GridThorRuntime 场景：走 12 个无障碍微步，event 与 full 的终点和碰撞约束相同，event 的 GetReachablePositions 查询数不超过 full 的一半。查询次数是确定性验收，不以机器速度断言。

```python
def test_event_refresh_reuses_unchanged_map(self):
    full = self.run_corridor(refresh_mode="full", micro_steps=12)
    event = self.run_corridor(refresh_mode="event", micro_steps=12)
    self.assertEqual(event.final_positions, full.final_positions)
    self.assertEqual(event.collisions, 0)
    self.assertLessEqual(event.reachable_queries, full.reachable_queries // 2)
```

测试类实现 `run_corridor`，使用现有 `tests.movement_fakes.GridThorRuntime` 和真实 StepMovementCoordinator，在 fake 的 `refresh_reachable_positions` 入口计数；返回上述三个字段的 SimpleNamespace。

- [x] 补齐门打开/关闭、物体移动/切片、临时机器人占位、失败边、位置偏差、候选不可见、取消和不同机器人可达集合的测试；不得只测试静态空网格。
- [x] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_reachable_map_cache.py`，确认 RED。
- [x] full 模式保持本批优化前的每微步完整刷新路径，作为兼容对照；event 模式每微步从最新事件验证实际位置，但最多每 4 个成功微步强制查询所有 agent 可达点一次。
- [x] 以下事件立即全量刷新并失效相关规划：门开闭、物体位置/包围盒变化、持有状态变化、实例集合变化、移动失败、位置偏差、候选不可见、开始新导航批次。指纹只来自导航有关元数据，不因温度或渲染帧变化刷新。
- [x] 地图分三层：初始化静态候选点、当前机器人占位、场景变化造成的有效点增删。移除点必须在两次完整查询中都消失，且不属于任何机器人的 0.35 m 占位邻域；新增点由新查询确认后加入。确认移除后提升地图版本并使经过该点的路径失效，避免永久保留历史可达点。
- [x] 首次发现疑似移除点立即进行第二次查询，不允许继续沿疑似失效路径执行；暂时无法区分占位与拓扑变化时保留静态点但把受影响段作为暂时不可进入，等待占位变化或下一次完整查询。
- [x] 失败有向边仍由现有规划预算限制；在当前批次内保持禁止，跨批次仅在新的完整地图证据后重新尝试。不因 TTL 到期直接放开尚未验证的阻碍。
- [x] 添加缓存命中、全量刷新、疑似移除、确认移除计数；可达点查询出错走第一批失败路径，不返回上次缓存假装最新。
- [x] 跑全部导航测试及第二批条件/资源测试。未满足真实性能门槛时保留 event 显式可选并报告结果，不修改正确性阈值。

**交付：** 默认行为可回归，新模式减少可证明的冗余查询，并处理真实场景变化。

## 任务 6：收敛兼容入口、文档与整体验收

**文件：** 修改 `scripts/execute_plan.py`、`scripts/README.md`、`README.md`、`executor_system/__init__.py`；新建 `tests/test_execute_plan_compatibility.py`，修改基准相关测试。

- [ ] 添加旧入口缺少楼层、机器人或目标时失败的测试：退出非零，错误列出缺失字段，不创建默认 FloorPlan1/空目标脚本。存在共享 generated runtime 脚本时直接运行并透传返回码。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py`，确认 RED。
- [ ] `execute_plan.py` 增加 main guard，兼容 `--command` 选择目录；优先运行目录中可验证的 generated runtime 脚本。旧日志不能确定必要上下文时明确要求先执行现有 plantocode 转换，不推测任务内容、不继续拼接默认参数。
- [ ] 删除 README 中已经失效的入口示例和“多机器人导航原型未接入”的说法；保留现有历史设计文档，标注它们不是当前默认值的来源。当前默认值说明引用实际配置模块。
- [ ] 文档说明兼容导入、显式上下文、动作登记步骤、策略/评分/调度版本、完整与事件刷新模式，以及诊断与恢复命令。
- [ ] 仅删除经过 `rg` 确认无生产调用的重复内部实现；公共旧导入保留重导出。测试关注行为和兼容性，不为每个一行委托写镜像测试。
- [ ] 每项职责提取和优化独立审查、提交；通过后运行前两批完整测试与本批新增测试。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_action_registry.py tests/test_plan_contract.py \
  tests/test_runtime_facade.py tests/test_runtime_context_isolation.py \
  tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py \
  tests/test_reachable_map_cache.py tests/test_execute_plan_compatibility.py \
  tests/test_generation_validation.py tests/test_pddlrun_executor_adapter.py \
  tests/test_movement_coordinator.py tests/test_navigation_execution_scope.py \
  tests/test_runtime_object_aliases.py tests/test_parallel_runner.py
```

## 真实验收与回退

先对 full 模式运行固定样例，核对职责拆分前后目标判定、动作结果和错误分类；再在同一候选提交上比较 full/event，隔离优化变量。

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_executor_regression.py \
  --manifest tests/fixtures/movement_benchmark_plans.json \
  --output-dir reports/executor_batch3_step_comparison \
  --repetitions 5 --movement-modes step \
  --execution-policies legacy strict --reachable-refresh-modes full event \
  --max-workers 1 --check

/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_executor_regression.py \
  --manifest tests/fixtures/movement_benchmark_plans.json \
  --output-dir reports/executor_batch3_teleport_regression \
  --repetitions 1 --movement-modes teleport \
  --execution-policies legacy strict --reachable-refresh-modes full \
  --max-workers 1 --check
```

- [ ] step 对照共 12 样例 × 2 策略 × 2 刷新模式 × 5 次，共 240 次；teleport 回归 24 次。不同策略分别比较，不合并 strict 与 legacy 的均值。
- [ ] 硬正确性门槛：新增碰撞为零、step 导航 Teleport 为零、无租约泄漏、无等待死锁、取消和结果协议测试全部通过。稳定 fake 场景必须逐动作等价。
- [ ] 真实 event/full 对照每种策略分别满足：有效评估数量不减少、超时数量不增加、平均 GCR 不降低、导航完成率不降低。出现差异必须定位到具体样例与重复编号，不以总均值掩盖单个新增确定性失败。
- [ ] 性能门槛：event 的执行时间中位数及 P95 均不得高于 full 的 1.05 倍；若未满足则 `--check` 非零，文档记录 event 尚未通过性能验收，默认仍是 full。
- [ ] 单任务验证通过后，可在同一固定工作负载对 `max_workers=1/2/4` 分别测量吞吐、峰值内存和 GPU 使用。仅输出数据与建议，不自动改变默认并发数。
- [ ] 最终交付实现提交、动作登记表、模块边界、三批全部测试记录、真实基准文件及未完成验证项。真实仿真不可用时不能将批次标记为已完成真实验收。

**回退：** 性能问题通过 `--reachable-refresh-mode full` 回到兼容刷新；结构问题按服务提取提交回退。三批评分、取消、资源与策略契约不随性能开关回退，历史报告始终保留。
