# 执行系统第二批：调度、失败策略与资源语义修改计划

> **面向执行代理：** 使用 `superpowers:executing-plans` 按任务实施，使用复选框跟踪进度。本文件是修改计划，不代表其中的代码已经实现。

**目标：** 使条件等待能够推进、失败策略名实一致、阶段条件实际生效，并让共享物体和设备的复合操作受到资源仲裁。

**架构：** 保留每机器人一个执行线程和底层 controller 串行调用。阶段协调器统一处理就绪状态、资源准入与导航波次；普通、容错入口调用同一执行循环，仅通过策略改变失败后的处理。

**技术栈：** Python 3.9、标准库线程与不可变数据结构、现有 AI2-THOR 适配和联合导航；不新增第三方依赖。

**规格：** 本文件“范围与固定决策”“策略契约”“调度契约”构成自包含规格，源于 `070b808b` 全面评审。

**依赖：** [第一批](2026-09-07-executor-batch-1-reliability.md)全部验收通过后开始。使用其 `ExecutionControl`、`EvaluationContext`、`ActionLedger` 和 v2 结果字段。完成后交给[第三批](2026-09-07-executor-batch-3-architecture-performance.md)。

## 范围与固定决策

- 本批解决 wait 条件与动作波次互等、策略字段无效、阶段后置条件不检查、线程私有状态看不到其他机器人、资源管理未接入主链路的问题。
- 默认 `execution_policy="legacy"`，保持一般动作失败后继续；新增 `strict` 显式落实失败策略。两种模式都使用相同的取消、输入检查、资源互斥和可信评估。
- `Action.on_failure` 现有默认 `FAIL_STAGE` 不变；legacy 将它映射为历史继续行为，结果明确记录实际决策。不要修改生成脚本中的旧计划。
- 新增 `scheduler_version=2`，与失败策略、评估版本、移动模式一同写入运行结果；legacy 仅表示失败处理兼容，不承诺复制原来的线程竞争顺序。
- 保留预任务阶段只允许 robot1、先于其他阶段执行的约束。普通阶段之间仍有屏障。
- 不修改导航网格、间距、候选/重规划预算；不将 step 失败改为隐式传送；不增加 LLM 在线重规划。
- 全量动作注册表和大文件职责拆分留给第三批；本批只增加落实当前契约所需的动作参数检查与资源需求表。
- 实施前记录第一批提交 SHA。命令从仓库根目录运行，使用 `/home/dwb/.pyenv/bin/pyenv exec python`，保留无关修改。

## 策略契约

新增 `execution_policy.py`，提供：

```text
ExecutionPolicy(str, Enum): LEGACY = "legacy", STRICT = "strict"
FailureDecision(kind: str, error_code: str, retry_number: int)
resolve_failure(policy, action, *, attempts: int, effects_satisfied: Optional[bool]) -> FailureDecision
    kind: retry / wait_retry / skip / fail_robot / fail_stage
resolve_stage_outcome(policy, stage, robot_outcomes, condition_satisfied) -> StageOutcome
    StageOutcome(status: str, continue_task: bool, errors: Tuple[Dict[str, Any], ...],
                 snapshot: Optional[WorldSnapshot])
    status: completed / partial / failed / timeout / cancelled
ConditionEvaluationError(RuntimeError)
PlanExecutionError(report: Mapping[str, Any])  # RuntimeError 子类，保存 report 属性
```

### 动作失败处理

| 输入策略 | strict | legacy |
|---|---|---|
| `FAIL_STAGE` | 停止本阶段，取消本阶段其余动作 | 记录失败，推进当前机器人游标 |
| `FAIL_ROBOT` | 停止当前机器人本阶段队列，其他机器人继续 | 记录失败，推进当前机器人游标 |
| `SKIP` | 记录失败并继续后续动作 | 相同 |
| `RETRY`、`WAIT_AND_RETRY` | 仅 Teleport 可自动重试；耗尽后 fail_stage | 保持仅 Teleport 重试；耗尽后继续 |
| `SKIP_IF_EFFECT_ALREADY_TRUE` | 全部声明效果已满足时记成功；缺失效果或效果未知/不满足则 fail_stage | 同样检查效果；不能证明时记录失败后继续 |

第一批定义的任务超时、取消、不可用 controller、未捕获工作线程错误不进入此表，始终触发任务级失败/取消。不得捕获后标记成普通动作失败继续执行。

`stage_failure_policy` 仅接受现有 `FAIL_STAGE` 和 `SKIP`：strict 中阶段失败时前者停止任务，后者记录失败并进入下一阶段。动作 `FAIL_STAGE` 停止当前阶段，是否执行下一阶段再由阶段策略决定。legacy 保持动作级继续，但阶段条件不满足必须报告失败，默认继续下一阶段。显式全局条件不满足始终使任务报告失败。

`on_conflict` 四个现有取值必须生效：WAIT 保留申请等待资源释放；RETRY_NEXT_TICK 推迟至下一调度轮；SKIP 记录 skipped；FAIL_STAGE 按阶段失败处理。超时/取消不受这些策略覆盖。

### 条件契约

- `wait_until`、阶段条件、全局条件继续接受 Python callable。callback 异常为 `ConditionEvaluationError`，记录原异常；不得判定成条件为假。
- `expected_preconditions/expected_effects` 每项仅允许 callable 或现有目标字典 `{name, states/state, contains}`。字典复用第一批目标解析与三值判定，但前置/后置条件始终检查当前快照，不能用历史温度事件跳过当前状态要求。
- unknown 前置条件不就绪；unknown 后置条件使动作失败。`SKIP_IF_EFFECT_ALREADY_TRUE` 要求非空效果列表，避免空列表被当成证明。
- 阶段开始时条件已满足可以跳过阶段，未执行动作记 skipped；阶段结束必须再次检查，不能只刷新状态。全局条件使用包含全部机器人与对象的最终快照。

## 调度契约

新增 `stage_scheduler.py`，主线程负责调度和状态转移，机器人线程负责执行获准的动作；主线程不运行高层动作，也不持有 controller 锁等待工作线程。

```text
PendingAction(robot_id: str, cursor: int, action: Action)
RobotAdmission(status: str, pending: Optional[PendingAction], reason: Optional[str])
    status: READY / WAITING_CONDITION / WAITING_RESOURCE / EXECUTING / FINISHED / FAILED
StageScheduler(runtime, stage, *, control: ExecutionControl, policy: ExecutionPolicy)
StageScheduler.run() -> StageOutcome
StageScheduler.notify_world_changed() -> None

WorldSnapshot(version, robot_positions, robot_rotations, held_objects, objects_by_id)
    冻结快照，嵌套数据不可由调用方修改；包含所有物理机器人。
SnapshotStore.capture(runtime, control: ExecutionControl) -> WorldSnapshot
SnapshotReadError(RuntimeError)

ExecutionControl.child(*, deadline: Optional[float] = None) -> ExecutionControl
    子控制器继承父取消和更早截止时间；取消子控制器不取消父控制器。
```

`WorldState` 保留作为旧 callback 的适配器，它的公开位置、旋转、持有物映射来自同一 `WorldSnapshot`；不再由每个机器人单独刷新部分状态。snapshot version 是实际低层状态提交版本，不使用各机器人私有 tick 充当版本。

| `synchronization_policy` | 执行规则 |
|---|---|
| `BARRIER_AT_STAGE_END` | 获准动作完成后该机器人可申请下一动作，不等待其他机器人完成当前高层动作；阶段结束等待全部机器人终态 |
| `BARRIER_EACH_STEP` | 每个调度轮所有已发放动作结束后进入下一轮；WAITING_CONDITION/WAITING_RESOURCE 不作为必须完成的动作 |
| `EVENT_CONDITION` | 与阶段末屏障相同的执行并发；条件在世界版本变化、资源释放或截止时间事件后重新检查，不做每机器人忙轮询 |

任何策略均不得要求 WAITING_CONDITION 的机器人加入导航波次。没有执行中/可运行动作时，协调器以最多每 50 ms 一次的频率提交一个 Pass 推进仿真并重新检查条件；只有协调器可产生这类等待 tick。`timeout_ticks` 从当前动作第一次进入等待开始，按这种调度轮累积，成功、跳过或失败后归零。

同一阶段存在真实依赖环时，截止时间最终终止任务，并保存机器人、等待条件、资源持有者的诊断快照；不依赖无限等待解决。

## 文件分工

- 新建 `scripts/executor_system/execution_policy.py`、`stage_scheduler.py`、`world_snapshot.py`、`action_resources.py`。
- 修改 `executor.py`、`action_plan.py`、`parallel_runner.py`：统一执行循环与阶段驱动；原类继续导出。
- 修改 `runtime.py`、`movement.py`、`movement_coordinator.py`：快照版本、动作准入上下文、保留导航汇合与恢复。
- 修改 `resource_manager.py`：增加活跃的动作资源租约层，保留原对象身份解析函数。
- 修改第一批 `execution_control.py`：支持阶段子控制器。
- 修改 `generated_plan_runtime.py`、`scripts/benchmark_movement_modes.py`、说明文档：策略透传及版本记录。

## 任务 1：失败策略变成可验证的显式决策

**文件：** 新建 `execution_policy.py`、`tests/test_execution_policy.py`；修改 `executor.py`、`parallel_runner.py`、`execution_control.py`。

**输入/输出：** 输入 Action、已尝试次数、效果判定；输出 `FailureDecision`，由执行循环应用游标、停止范围及账本更新。

- [ ] 将下面的测试放入 `unittest.TestCase`，导入 `Action`、`ExecutionPolicy`、`resolve_failure`：

```python
def test_fail_stage_is_explicit_in_strict_mode(self):
    action = Action("OpenObject", {"args": ("Cabinet",)})
    strict = resolve_failure(ExecutionPolicy.STRICT, action,
                             attempts=1, effects_satisfied=None)
    legacy = resolve_failure(ExecutionPolicy.LEGACY, action,
                             attempts=1, effects_satisfied=None)
    self.assertEqual(strict.kind, "fail_stage")
    self.assertEqual(legacy.kind, "skip")
```

- [ ] 为策略表每行建立参数化 subTest；覆盖重试上限、非 Teleport 不重试、空效果不能证明成功、父取消传到子控制器、阶段取消不影响下一阶段控制器。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execution_policy.py`，确认 RED。
- [ ] 实现纯函数决策表，重试数定义为“初次尝试之后额外尝试的次数”，与 `max_retries` 一致。普通循环和容错循环先共同使用它，不在本任务改变调度。
- [ ] 移除 `FailureHandler` 中独立决策逻辑，使其委托纯函数；所有结构化日志记录 requested policy 与实际 `FailureDecision.kind`。
- [ ] 运行新测试及 `test_executor_retry_policy.py`、`test_parallel_runner.py`。现有失败继续测试显式指定 legacy，另加 strict 对照，不删除旧行为断言。

**交付：** 失败策略有唯一解释，可先在原调度下独立验收。

## 任务 2：创建全机器人共享快照

**文件：** 新建 `world_snapshot.py`、`tests/test_world_snapshot.py`；修改 `runtime.py` 和 `action_plan.py` 的 `WorldState`。

**输入/输出：** 在 controller 锁内复制同一次 `last_event` 的所有 agent metadata，产出 `WorldSnapshot`。对象状态和持有物以该事件为依据；现有手持覆盖状态只可在对应动作提交完成时同步合并，并记录来源。

- [ ] 测试两个机器人共享状态：B 的持有物变化后，A 的 callback 从新快照可见；同一快照中版本和位置一致；读取异常不会保留旧位置并假装成功；修改返回映射被拒绝。

```python
def test_snapshot_is_immutable(self):
    snapshot = self.store.capture(self.runtime, self.control)
    with self.assertRaises(TypeError):
        snapshot.robot_positions["robot1"]["x"] = 99.0
    self.assertEqual(snapshot.version, self.runtime.state_version)
```

测试夹具的 `self.runtime` 使用可编程多 agent event；设置完整对象和 agent metadata，不启动 Unity。`self.store`、`self.control` 分别是 `SnapshotStore()` 和 `ExecutionControl()`。

- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_world_snapshot.py`，确认 RED。
- [ ] `_step_direct` 成功获得事件后，在同一提交边界更新 `state_version` 和相关持有物记录，再通知协调器；不在持有 controller 锁时获取调度条件锁。使用提交后通知避免锁顺序反转。
- [ ] 快照读取失败抛出 `SnapshotReadError` 并走第一批基础设施异常路径。字段可选导致的单目标 unknown 与整个快照无法读取分别处理。
- [ ] 删除 `WorldState.refresh` 的 `except BaseException: continue`，旧 callback 适配到完整快照。最终全局条件不得通过 `refresh([])` 得到空世界。
- [ ] 运行快照测试及 `test_runtime_object_aliases.py`、`test_action_plan_pre_task.py`、`test_parallel_runner.py`。

**交付：** 条件和效果检查有一致、可解释的世界状态输入。

## 任务 3：消除等待条件与导航波次互等

**文件：** 新建 `stage_scheduler.py`、`tests/test_stage_scheduler.py`；修改 `executor.py` 的 `PhaseCoordinator`、`movement.py`、阶段执行入口。

**输入/输出：** 机器人先提交下一动作；协调器检查条件并发放许可；只将本轮获准的 GoToObject 放入已有联合导航接口。

- [ ] 写两机器人回归：A 等待 B 的动作设置标志，B 的动作不依赖 A；step 与 teleport 均须完成。测试用 Event 和有界 join，不依赖线程启动先后。

```python
def test_waiting_robot_does_not_block_dependency(self):
    ready = threading.Event()
    plan = TaskPlan("dependency", [StagePlan("s", {
        "robot1": [Action("Wait", wait_until=lambda world: ready.is_set())],
        "robot2": [Action("Wait")],
    })])
    def execute(adapter, robot_id, action, **kwargs):
        if robot_id == "robot2":
            ready.set()
    with patch.object(AI2ThorAdapter, "execute", execute):
        report = run_action_plan_tolerant(self.runtime, plan, timeout_seconds=1)
    self.assertFalse(report["timed_out"])
    self.assertTrue(ready.is_set())
```

导入标准库 `threading`、`patch` 和现有计划类型。`self.runtime` 采用现有 `FakeRuntime` 并显式设置 `MovementConfig.resolve("step")`，同样参数化验证 teleport。

- [ ] 增加：一个机器人等待、两个机器人联合导航；晚到就绪请求进入下一波次；某成员失败唤醒同批其他成员；延期请求保留动作游标；全部等待到截止时间能输出依赖诊断。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_scheduler.py`，确认现有互等回归为 RED。
- [ ] 用任务主线程驱动协调器：收集 pending → 读取共享快照 → 判定条件 → 资源准入（本任务先使用空资源集合）→ 冻结获准导航成员 → 发放许可 → 接收结果和再次调度。
- [ ] 就绪选择按 `(-base_priority - wait_rounds * 10 - critical_bonus, robot_id, cursor)` 稳定排序，`critical_bonus=50`。等待年龄直到获准才清零。不要用线程抢锁顺序选择获准者。
- [ ] 非导航获准动作继续等待同波次导航结束，保持现有交互前导航屏障；等待条件的机器人作为占位参与空间约束，但不能被误标记成已完成并停车。
- [ ] 联合导航始终先收齐成员请求，再获取导航执行锁。取消时唤醒所有许可和波次等待者，取消后的请求不得进入新波次。
- [ ] 实现三种同步策略表；给每种策略加入可观察动作开始/结束顺序的断言，不只检查最终成功。
- [ ] 运行调度测试及 `test_movement_coordinator.py`、`test_navigation_batch_failures.py`、`test_navigation_execution_scope.py`、`test_multi_robot_avoidance.py`。

**交付：** 条件等待不会阻塞产生条件的动作，联合导航成员集合可确定。

## 任务 4：为物体与设备操作接入动作租约

**文件：** 新建 `action_resources.py`、`tests/test_action_resource_leases.py`；修改 `resource_manager.py`、`stage_scheduler.py`、`runtime.py`、`actions.py`。

**公共接口：**

```text
ResolvedActionResources(keys: Tuple[str, ...], bindings: Mapping[str, str])
resolve_action_resources(runtime, snapshot, robot_id, action) -> ResolvedActionResources
ActionResourceManager.try_acquire(owner: str, keys: Sequence[str]) -> Optional[ResourceLease]
ActionResourceManager.blockers(keys: Sequence[str]) -> Tuple[str, ...]
ResourceLease.release() -> None
```

owner 为第一批动作键。一次申请所有 key：可同时授予才成功，否则一个也不授予，避免持有一半资源等待另一半。资源 key 使用实例 ID 绑定的逻辑身份；切片/破蛋后通过已有 alias 映射保持身份连续。

- [ ] 加入最小租约失败测试，导入 `ActionResourceManager`：

```python
def test_resource_is_released_before_next_owner_enters(self):
    manager = ActionResourceManager()
    first = manager.try_acquire("0:robot1:0", ("Microwave|1", "Mug|1"))
    self.assertIsNotNone(first)
    self.assertIsNone(manager.try_acquire("0:robot2:0", ("Microwave|1",)))
    first.release()
    first.release()
    self.assertIsNotNone(manager.try_acquire("0:robot2:0", ("Microwave|1",)))
```

- [ ] 补齐共享微波炉复合动作不交错、两个抓取竞争同一实例、不同实例可并发、反序双资源申请不死锁、失败与取消释放租约、别名变化仍冲突、等待年龄防长期饥饿。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_action_resource_leases.py`，确认 RED。
- [ ] 资源需求固定：单对象交互占用对象；Put 占用手持对象和容器；Microwave/Coffee/Cold 占用设备与目标；Toaster 占用面包与烤面包机；Stove 系列占用炉头、对应旋钮、容器/目标；FillWater 占用水源、解析出的 SinkBasin 与目标；PrepareEgg 占用蛋与容器但不改变其现有动作语义。
- [ ] GoToObject 单独导航不长期占有目标对象；执行实际交互前重新解析并申请对象租约。导航位置资源继续由联合规划器管理，不启用旧位置仲裁器的另一套审批。
- [ ] 高层辅助动作在许可发放前解析隐式目标：旋钮、SinkBasin、自动腾手的放置容器。将解析结果通过动作执行上下文传给 helper，执行时不得重新选择另一个未获租约的对象。副作用开始前发现绑定失效则释放全部租约并重新排队；已经发生副作用后发现失效则报告动作失败，不自动重播整个复合动作。
- [ ] 嵌套 helper 复用当前动作 owner 与租约，不重复申请；需要扩展资源必须在任何副作用之前完成。租约的申请/释放只短暂持有资源管理锁，执行期间不持有该互斥锁。
- [ ] 所有会直接移动或恢复机器人位置的动作进入现有导航执行作用域；遵守“短暂资源审批 → 导航锁 → controller 锁”，不持有调度条件锁等待导航。批次所有成员完成资源准入后才冻结导航成员，避免一成员占锁等另一成员申请。
- [ ] 持有物归属独立于动作租约：不能因租约释放就允许另一机器人操作对方手持物。没有显式交接动作时返回可解释的 `OBJECT_HELD_BY_OTHER`；不自动传递物体。
- [ ] 运行资源测试、导航作用域测试、对象别名测试及执行重试测试。

**交付：** 对象复合动作的互斥覆盖完整生命周期，已有导航避障仍是唯一位置规划来源。

## 任务 5：统一执行循环并落实阶段、动作条件

**文件：** 修改 `executor.py`、`action_plan.py`、`parallel_runner.py`、`stage_runner.py`；新建 `tests/test_stage_conditions.py`、`tests/test_plan_contract.py`。

**输入/输出：** `Executor` 保留唯一动作循环；`TolerantExecutor` 为指定 legacy 策略和统计适配的兼容子类；两个阶段入口均委托 `StageScheduler.run()`。

- [ ] 添加阶段结束条件测试：初始 false、执行后 true 通过；执行后仍 false 返回失败；初始 true 跳过；全局条件可以读取所有机器人。

```python
def test_stage_condition_is_checked_after_execution(self):
    calls = []
    def never_satisfied(world):
        calls.append(world)
        return False
    stage = StagePlan("s", {"robot1": [Action("Wait")]},
                      stage_success_condition=never_satisfied)
    report = run_action_plan_tolerant(self.runtime, TaskPlan("t", [stage]),
                                      timeout_seconds=1, execution_policy="strict")
    self.assertEqual(report["execution_status"], "failed")
    self.assertGreaterEqual(len(calls), 2)
```

测试复用现有 `FakeRuntime`，导入计划类型和运行入口；新参数由本任务实现。

- [ ] 覆盖动作前置条件不满足、后置效果失败、callback 抛异常、stage SKIP 继续下一阶段、FAIL_STAGE 阻止下一阶段、FAIL_ROBOT 保留其他机器人执行结果、预任务阶段屏障。
- [ ] 添加校验测试：缺少 Pickup 参数、动作参数同时给 args 和冲突 objectId、非法策略、负重试数、非有限超时、未知机器人在执行前失败。此处建立小型明确检查，第三批注册表接管同一测试。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_stage_conditions.py tests/test_plan_contract.py`，确认 RED。
- [ ] 统一循环顺序：获准 → 检查取消与绑定 → 执行 → 当前效果验证 → 一次终态记账 → 释放租约 → 提交下一动作。延期和重试不生成重复终态。
- [ ] 分离队列耗尽、机器人失败、阶段条件满足三个概念；不要在 `finally` 把失败机器人强制改为 `FINISHED_STAGE`。未执行尾部动作分别记录 skipped/cancelled/unexecuted 原因。
- [ ] 删除已被统一循环替代的内部实现，保留旧模块的导出与调用签名；普通 TaskRunner 从 StageOutcome.snapshot 构造兼容 WorldState 并返回，strict 阶段/全局失败抛出含结构化报告的 `PlanExecutionError`，容错包装捕获它并返回同一报告。ConditionEvaluationError 和 PlanExecutionError 均定义于 execution_policy.py。
- [ ] 运行新测试、`test_action_plan_pre_task.py`、`test_parallel_runner.py`、`test_executor_retry_policy.py`。

**交付：** 两个入口只在接口返回形式与所选策略上有差异，执行行为不再分叉。

## 任务 6：接入策略配置、回归与交接

**文件：** 修改 `generated_plan_runtime.py`、`parallel_runner.py`、`scripts/benchmark_movement_modes.py`、`README.md`、`scripts/README.md`；修改相关 CLI 测试。

**接口：** 新增 `--execution-policy legacy|strict`，默认 legacy；`run_action_plan_tolerant(..., execution_policy="legacy")` 和 `TaskRunner(..., execution_policy="legacy")` 新增关键字参数。不增加同名环境变量，避免隐式覆盖。

- [ ] CLI 测试验证父进程传递策略、旧生成脚本无参数仍运行、非法策略拒绝、结果记录实际策略及 `scheduler_version=2`。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_parallel_runner.py tests/test_movement_benchmark.py`，验证新增参数测试先失败再通过。
- [ ] 基准脚本新增同名策略选项，输出目录区分策略；不把 strict 失败较多直接解释为能力回退，逐项区分停止策略变化和任务目标变化。
- [ ] 文档给出三个实际例子：legacy 抓取失败后继续；strict FAIL_ROBOT 保留另一个机器人；A 等 B 的条件在 step 模式完成。记录共享资源操作顺序和阶段条件结果。
- [ ] 每个任务单独审查、提交；批次验证沿用第一批全套测试，加本批新增测试。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_execution_policy.py tests/test_world_snapshot.py \
  tests/test_stage_scheduler.py tests/test_action_resource_leases.py \
  tests/test_stage_conditions.py tests/test_plan_contract.py \
  tests/test_action_plan_pre_task.py tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py tests/test_navigation_batch_failures.py \
  tests/test_navigation_execution_scope.py tests/test_movement_coordinator.py \
  tests/test_runtime_object_aliases.py tests/test_execution_shutdown.py
```

## 批次验收与交接

- [ ] 双模式的跨机器人依赖用例完成；无 deadline 时显式依赖环测试使用测试外层超时并清理，生产默认仍有任务期限。
- [ ] strict 每个策略与声明一致；legacy 的既有失败继续行为通过；所有基础设施故障在两模式都可见。
- [ ] 快照不静默陈旧；阶段、全局、前置和后置条件均有真实状态断言。
- [ ] 同物体/设备租约无交错；别名变化、嵌套 helper、取消和异常均不遗留租约。
- [ ] 固定 12 样例 × 2 移动模式 × 2 执行策略生成独立真实报告；第一批评分口径保持一致。保存动作结果、等待原因、波次成员和资源拥有者，不只保存汇总成功率。
- [ ] 交给第三批：本批提交 SHA、两个策略的基准、动作参数检查表、资源需求表、快照接口和全部回归命令。

**回退：** 以 legacy/strict 显式选择失败策略，不用切回旧线程循环绕过调度错误。确需回退提交时保留第一批评估、取消、进程隔离修正；结果中的 scheduler_version 必须反映实际实现。
