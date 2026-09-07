# Task 2 规格与质量审查

审查范围：基线 `317fb232` 至实现 `c8a1d712`，以 task-2-brief.md 为规格；完整阅读交付差异和 task-2-report.md。未修改代码，未启动 Unity，未重跑报告中的 190 项通过测试；仅针对提交元数据异常这一具体风险运行了两次最小复现。未审查后续 scheduler/conditions 实现。

## 结论

- 规格审查：**未通过，需修复一项基础设施异常传播问题。**
- 质量审查：**需修改。** 快照递归冻结与脱离源事件、全部物理机器人补全、WorldState 完整刷新、正常事件/失败动作的版本提交、库存确认后的覆盖退休以及正常路径的锁外通知均符合 Task 2 目标；以下异常路径允许读取损坏被重试后完全隐藏。

## [P1] 将提交元数据读取异常纳入任务级失败路径

位置：`scripts/executor_system/runtime.py:672`，相关解析位于 `runtime.py:699-704`。

`controller.step()` 成功返回后，`_commit_world_event()` 在已有 controller 异常处理块之外执行。它先递增 `state_version`，随后读取所有 agent 的 `inventoryObjects`；当某个 agent 的该字段为 `None`（或其他不可迭代结构）时会抛出普通 `TypeError`。这既没有取消根控制器，也没有包装成保留 cause 的 `SnapshotReadError`。而且 `committed = True` 尚未执行，因此实际已发布的新 last_event / version 不会触发通知。

这不是仅影响错误名称：现有 `_step_with_retries()` 会把该读取失败当成普通 Teleport 动作异常重试。可编程 controller 第一次返回上述损坏事件、第二次返回完整事件时，调用成功返回，任务仍未取消，第一次读取失败完全消失。这违反 Task 2“快照读取失败抛 SnapshotReadError 并走基础设施异常路径”的要求，以及基础设施错误不得被普通动作策略降级的固定契约。

最小复现均使用简报指定的 `/home/dwb/.pyenv/bin/pyenv exec python`，复用 `tests.test_world_snapshot.snapshot_runtime` / `multi_event`，不修改文件：

1. 将 `runtime.controller.next_event.events[1].metadata['inventoryObjects']` 设为 `None` 后调用 `_step_direct(..., save_frame=False)`：

   ```text
   error_type: TypeError
   error: 'NoneType' object is not iterable
   state_version: 1
   root_cancelled: False
   notifications: []
   last_event_x: 17
   ```

2. controller 按顺序返回上述损坏事件和完整事件，调用 `_step_with_retries({'action': 'Teleport', 'agentId': 1}, check_success=True, save_frame=False, retry_on_failure=True, max_retries=1)`：

   ```text
   Retrying Teleport for agent 1 (1/1); retrying serially.
   controller_calls: ['Teleport', 'Teleport']
   root_cancelled: False
   notifications: [2]
   returned_successfully: True
   ```

建议在同一提交边界处理元数据结构错误：取消当前和根控制器，并以保留原异常 cause 的 `SnapshotReadError` 传播，阻止底层重试或高层策略吞掉错误；同时明确已经获得新事件/提交版本之后的通知语义，确保通知仍在 controller 锁外。新增回归应覆盖损坏提交在 Teleport 可重试路径中只调用 controller 一次、根取消且保留 TypeError cause，并覆盖失败后的通知约定。

## 其他具体核查

- `_freeze` 对映射、序列和集合递归复制并冻结，源事件后续改动不影响已有快照；缺少单对象可选状态字段保持 unknown 输入。
- SnapshotStore 在同一 controller 锁内读取一个 last_event 和 version；WorldState 的公开映射来自同一 snapshot，refresh([]) 保持完整世界。
- 提交覆盖与真实 inventory 有来源和版本，成功放下/投掷释放覆盖，inventory 确认后退休；高层返回后补写已移除。
- 仅针对新增锁与 API 变化核查了 runtime 的 controller/override 锁调用处：已有 override 读取先释放自己的锁再取 controller 锁；正常 _step_direct 通知在 controller 锁释放后进行。
- Executor 的字典效果求值使用 captured objects，并使用新建的无历史 EvaluationContext。

除上述一项外，未发现本次范围内的其他可操作问题。

## fix1 限定复审：`c8a1d712..22fa6601`

**先前 P1：已解决。Task 2 规格审查通过，质量审查批准。** 本节更新上方初次审查结论。

完整阅读 task-2-fix1-diff.txt 及交付报告新增修复证据，仅审查原问题和修复引入的变化。未重跑任何套件，未修改代码。

- 新事件的版本递增与应通知标记在 controller 锁内先完成；原 hand override 提交仍在同一锁内使用当前版本。
- 提交元数据解析异常会取消当前 scoped control 和根 control，普通异常转换为保留原始 cause 的 SnapshotReadError；既有重试保护因此拒绝再次调用 controller。
- finally 仍在 controller 锁退出后通知，畸形事件也能发布其已提交版本及取消状态。
- 新增回归直接覆盖原复现：先提供损坏事件、再准备正常事件，断言仅一次 Teleport、TypeError cause、根/子取消、版本 1、锁外通知，以及后续 capture 被取消阻止。
- 报告给出了该回归修复前 RED、修复后 GREEN 和 86 项受影响覆盖通过证据。

未发现 fix1 新引入的可操作问题；无剩余审查发现。
