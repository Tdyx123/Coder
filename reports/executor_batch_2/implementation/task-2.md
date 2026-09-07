# Task 2 交付报告：全机器人共享不可变快照

状态：完成；实现提交 `c8a1d712`，实现基线 `317fb232`，第一批验收提交 `8f95c7ca`。未实现 scheduler。代码提交后 `git status --short` 为空；报告保存在仓库既有忽略目录中。

## 实现

- 新增 `WorldSnapshot`、`SnapshotStore.capture(runtime, control)`、`SnapshotReadError`。同一 controller 锁内读取一次 `last_event`，复制全部物理 agent 的位置、旋转、库存和对象；递归冻结为 mapping proxy / tuple / frozenset，原事件之后的修改不能改变旧快照。重复对象优先采用 active agent 的 metadata，其他 agent 补充对象。
- `WorldState` 保留 callback 适配，位置、旋转、持有物、对象和 version 属性均来自一个 snapshot；refresh 的 robot_states 仅决定回填哪些执行状态，不再决定世界包含哪些机器人。`refresh([])` 仍得到完整世界。删除吞掉 BaseException 的旧刷新逻辑。
- `ThorRuntime._step_direct` 在 controller 锁内递增真实状态提交版本；失败动作返回的事件也提交。真正 controller 异常仍取消 root/scoped control 并保留原异常；Task 1 的 `action_deadline_scope(control=child)` 和锁前/锁内取消检查保留。
- 手持覆盖只在成功低层 Pickup/Put/Throw/Drop 提交时合并。删除高层动作返回后及 Executor.submit 返回后的迟到补写。快照的 `held_object_sources[robot][object]` 记录 `inventory + version` 或 `action_commit + action + version`。真实库存确认后覆盖退休，后续空库存不会复活旧持有物。旧公开 override API 保留兼容，但未经过提交的手工 override 不进入快照。
- 提交后的 `runtime.stage_scheduler.notify_world_changed()` 始终在 controller 锁外运行，失败动作和帧保存失败后的已提交事件也通知。
- 快照结构读取错误保留原异常 cause，取消 root control，抛出 SnapshotReadError，不能被普通动作失败策略降级。可选对象属性缺失保持缺失，允许三值目标检查判为 unknown。
- Executor 的失败后效果检查使用同一 captured objects 集合供字典目标求值，保持新建的无历史 EvaluationContext，不再穿插读取 live object 状态。
- 新增共享完整测试夹具 `tests/snapshot_fakes.py`，替换 pre-task、parallel、retry-policy 中欠完整的轻量 runtime；丰富 Task 1 controller-boundary 回归夹具。

## TDD 证据

初始命令：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_world_snapshot.py
E
ERROR: test_world_snapshot (unittest.loader._FailedTest)
ImportError: Failed to import test module: test_world_snapshot
ModuleNotFoundError: No module named 'executor_system.world_snapshot'
Ran 1 test in 0.000s
FAILED (errors=1)
```

自查新增覆盖寿命回归的有效 RED：

```text
FAIL: test_inventory_confirmation_retires_override_in_later_events
AssertionError: frozenset({'Apple|1'}) is not false
```

自查新增帧保存异常的有效 RED：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_world_snapshot.WorldSnapshotTest.test_committed_event_notifies_even_if_frame_saving_fails
F
FAIL: test_committed_event_notifies_even_if_frame_saving_fails (tests.test_world_snapshot.WorldSnapshotTest)
AssertionError: Lists differ: [] != [1]
Ran 1 test in 0.001s
FAILED (failures=1)
```

开发过程还修正了测试夹具 API 拼写和必须含阶段的计划格式。第一次运行 fixture 替换脚本时裸 `python` 不在 PATH，脚本没有执行；随后用指定 pyenv 重跑了替换及验证，所有实际 Python 验证均通过显式 pyenv 执行。

最终专项 GREEN（退出码 0）：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_world_snapshot.py
.......stage started with 1 robot queue(s).
Tick 0: robot1 completed Pass.
........stage started with 1 robot queue(s).
....
----------------------------------------------------------------------
Ran 19 tests in 0.007s

OK
```

最终指定及受影响覆盖 GREEN（退出码 0）：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_world_snapshot.py tests/test_runtime_object_aliases.py tests/test_action_plan_pre_task.py tests/test_parallel_runner.py tests/test_execution_policy.py tests/test_executor_retry_policy.py tests/test_execution_shutdown.py
----------------------------------------------------------------------
Ran 190 tests in 3.773s

OK
```

完整最后一次输出保存在 `/tmp/task-2-tests.log`。没有运行 full discover 或无关宽范围套件。

最终以下命令均退出 0，无输出：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/world_snapshot.py scripts/executor_system/runtime.py scripts/executor_system/action_plan.py scripts/executor_system/executor.py tests/test_world_snapshot.py tests/snapshot_fakes.py tests/test_action_plan_pre_task.py tests/test_parallel_runner.py tests/test_execution_policy.py tests/test_executor_retry_policy.py
git diff --check
```

## 自查与交接

- 检查了 controller → override 的锁顺序；调度通知在 controller 锁释放后调用。并发回归验证不会观察到新位置配旧版本。
- 真实库存仍是持有物首要来源；成功动作覆盖有明确提交来源，不读取旧线程 tick，不合并未经提交的手动覆盖。
- 结构错误、root cancellation、失败事件版本、单机器人阶段后的完整 global callback、快照不可变、效果共享 captured objects 均有测试。
- 后续 scheduler 注册 `runtime.stage_scheduler = scheduler` 并在退出时清理，即可接收提交通知。此任务未实现调度、资源仲裁、callback ConditionEvaluationError 或完整条件契约（后续任务负责）。
- 没有启动 Unity；验证基于可编程多 agent event 和现有执行/取消套件。生产 capture 要求完整 agent/position/rotation、objects、inventoryObjects 结构，轻量测试 runtime 需要采用完整夹具，不能依赖旧静默刷新。
- brief 中 `execution_control_scope` 是误称，实际已有接口是 `action_deadline_scope(control=child)`，已向 controller 确认并保留。

## 审查修复 round 1：提交元数据失败不能进入 Teleport 重试

修复提交：`22fa6601`。提交后工作区干净。

审查 P1 已复现并修复。低层 controller 返回事件后立即在同一 controller 锁内发布 `state_version` 并标记应通知；手持提交解析失败时取消当前 scoped child 与 root，以保留原异常 cause 的 `SnapshotReadError` 传播。既有 `_step_with_retries` 的基础设施异常保护因此直接终止，不再尝试下一次 Teleport。最终通知仍在 controller 锁外，即使该事件的 metadata 不可读，也会送达已发布版本及取消状态；之后捕获通过 control.check 拒绝继续读取损坏世界。

新增确定性回归按顺序准备 `inventoryObjects=None` 的事件与正常事件。断言实际 controller 调用恰好一次、last_event 仍是损坏事件、版本为 1、root/child 取消、cause 是 TypeError、通知在锁外且观察到 `(1, True, True)`。

精确 RED 输出（退出码 1）：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_world_snapshot.WorldSnapshotTest.test_malformed_teleport_commit_cancels_root_without_retry
Retrying Teleport for agent 1 (1/1); retrying serially.
F
======================================================================
FAIL: test_malformed_teleport_commit_cancels_root_without_retry (tests.test_world_snapshot.WorldSnapshotTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/tmp/coder-executor-batch-2/tests/test_world_snapshot.py", line 184, in test_malformed_teleport_commit_cancels_root_without_retry
    self.runtime._step_with_retries(
AssertionError: SnapshotReadError not raised

----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (failures=1)
```

精确专项 GREEN 输出（退出码 0）：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_world_snapshot.WorldSnapshotTest.test_malformed_teleport_commit_cancels_root_without_retry
.
----------------------------------------------------------------------
Ran 1 test in 0.000s

OK
```

限定覆盖 GREEN 末尾输出（退出码 0；完整输出 `/tmp/task-2-fix-round1-tests.log`）：

```text
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_world_snapshot.py tests/test_executor_retry_policy.py tests/test_execution_shutdown.py
.
----------------------------------------------------------------------
Ran 86 tests in 0.466s

OK
```

`/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/runtime.py tests/test_world_snapshot.py` 与 `git diff --check` 均退出 0、无输出。未扩大套件范围，未运行 Unity。
