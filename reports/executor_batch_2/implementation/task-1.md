# 任务 1 实施报告：显式失败策略

## 状态

完成。基线提交为 `8f95c7ca092d7025a5ca67def5015daa56f6c568`，工作位于隔离 worktree `/tmp/coder-executor-batch-2` 的 `codex/executor-batch-2` 分支。

## 实现内容

- 新增 `execution_policy.py`：
  - `ExecutionPolicy.LEGACY` / `ExecutionPolicy.STRICT`。
  - 不可变 `FailureDecision(kind, error_code, retry_number)`。
  - `resolve_failure(...)` 是动作失败策略的唯一决策表。
  - 决策 kind 限定为 `retry`、`wait_retry`、`skip`、`fail_robot`、`fail_stage`。
  - `attempts` 包含初次尝试；第一次失败的 `attempts=1` 对应第 1 次额外重试，最多允许 `max_retries` 次额外重试。
  - 稳定错误码：`action_failed`、`retry_exhausted`、`retry_not_supported`、`effects_missing`、`effects_unknown`、`effects_unsatisfied`；防御性处理已满足效果时使用 `effects_already_satisfied`。
- 普通 `TaskRunner`/`StageRunner`/`Executor` 和容错 `run_action_plan_tolerant`/`TolerantStageRunner`/`TolerantExecutor` 均接受 `execution_policy`，默认 `legacy`，并共同调用 `resolve_failure`。
- strict 行为已接入现有循环：
  - `FAIL_STAGE` 停止阶段并抛出保留原始原因的阶段失败。
  - `FAIL_ROBOT` 只终止该机器人当前阶段队列，其他机器人继续。
  - `SKIP` 推进游标。
  - 只有 `Teleport` 可按 `RETRY` / `WAIT_AND_RETRY` 重试，耗尽后 strict 失败阶段、legacy 跳过。
  - `SKIP_IF_EFFECT_ALREADY_TRUE` 先检查非空声明效果；全部可证明时直接按成功记账，不调用 `resolve_failure`。无法证明时再按策略决策。
- `FailureHandler` 删除独立动作失败决策，委托 `resolve_failure`。
- `ActionResult` 和容错 `robot_failures` 写入 `requested_failure_policy`、`failure_decision`、`failure_error_code`；容错报告写入实际 `execution_policy`。
- `ExecutionControl.child(...)` 实现单向取消继承和更早截止时间继承。每个现有阶段使用子控制器；阶段取消不会取消任务根控制器或下一个阶段子控制器。
- 未捕获工作线程错误、主线程中断和任务级超时仍取消根控制器。内部 `StageFailureDecisionError` 区分策略触发的阶段取消，避免把它错误升级为任务取消。

## TDD 证据

### RED 1：新策略模块和子控制器尚不存在

命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execution_policy.py
```

首次结果：退出码 1，`ModuleNotFoundError: No module named 'executor_system.execution_policy'`。

加入只可导入、`resolve_failure` 抛 `NotImplementedError` 的接口骨架后再次运行：退出码 1，8 个测试产生 23 个预期错误；策略测试命中 `NotImplementedError`，控制器测试命中 `ExecutionControl` 缺少 `child`。

### GREEN 1：纯决策表和子控制器

同一命令结果：

```text
........
----------------------------------------------------------------------
Ran 8 tests in 0.001s

OK
```

### RED 2：执行入口尚未透传策略

命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_parallel_runner.OrdinaryExecutorFailureContinuationTest.test_task_runner_continues_after_robot_action_failure \
  tests.test_parallel_runner.OrdinaryExecutorFailureContinuationTest.test_task_runner_strict_fail_stage_stops_later_actions
```

结果：退出码 1，2 个错误，均为 `TaskRunner.__init__() got an unexpected keyword argument 'execution_policy'`。

### RED 3：容错入口的 strict 透传变异检查

临时将 `TolerantStageRunner` 向执行器传入的策略变异为 `legacy`，运行：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_parallel_runner.TolerantExecutorTest.test_tolerant_runner_obeys_strict_fail_stage
```

结果：退出码 1，strict 测试观察到后续 `CloseObject` 继续执行且未抛异常，证明该测试可捕获容错入口丢失策略透传。随后恢复实现。

## GREEN 与回归验证

任务要求的三套测试在首次完整实现后：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_execution_policy.py \
  tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py
```

结果：136 项，0 失败，退出码 0。

完整 `unittest discover -s tests` 会发现 951 项，而控制器提供的基线只包含 442 项。该发现运行因 worktree 缺少未纳入仓库的 RAG/PDDL 数据、`parallel_plan_to_code` 模块等环境资产而产生既有失败；同时它有效发现了阶段子控制器最初未把真正的未捕获线程错误传给根控制器。修复取消分类后，受影响关闭测试通过。

最终覆盖验证：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_execution_policy.py \
  tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py \
  tests/test_execution_shutdown.py
```

结果：

```text
----------------------------------------------------------------------
Ran 151 tests in 3.737s

OK
```

语法与补丁检查：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/executor_system/execution_policy.py \
  scripts/executor_system/execution_control.py \
  scripts/executor_system/action_plan.py \
  scripts/executor_system/executor.py \
  scripts/executor_system/parallel_runner.py \
  tests/test_execution_policy.py \
  tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py \
  tests/test_execution_shutdown.py
git diff --check
```

两条命令均退出码 0，无输出。

## 自审

- 决策表每一行均由字面量期望的 `subTest` 覆盖；另有重试边界、非 Teleport、空效果、父子取消方向和截止时间测试。
- 普通入口覆盖 legacy 继续、strict `FAIL_STAGE`、strict `FAIL_ROBOT` 和效果已满足成功；容错入口覆盖 legacy 重试/继续及 strict `FAIL_STAGE`。
- 失败日志记录请求策略和实际决策；重试日志也携带相同字段。
- strict `FAIL_ROBOT` 的账本只把失败动作记为 failed，未执行的同机器人后续动作保持 unexecuted；测试核对完整计数映射。
- legacy 默认保持现有动作失败后继续，现有继续行为测试显式选择 legacy。
- 未引入后续 `StageOutcome`、阶段条件裁决、资源管理或 scheduler 实现。

## 关注事项与后续边界

- 当前 strict `fail_stage` 会结束现有阶段并向调用者抛出阶段失败，但 `stage_failure_policy=SKIP` 的阶段结果归并和进入下一阶段属于后续阶段策略任务。本任务已保证阶段子取消本身不会污染根控制器或下一阶段控制器。
- 声明效果字典目前通过新建的无历史 `EvaluationContext` 检查当前对象元数据；后续统一 `WorldSnapshot` 后应改为同一冻结快照上的三值判定，避免一次失败处理读取多个时刻。
- 全量 discover 的非任务失败依赖缺失的仓库外资产；本次没有修改或绕过这些测试。

## 决策分歧

无。按控制器决定，已满足效果在调用 `resolve_failure` 前记成功，`resolve_failure` 保持五种声明 decision kind。

## Review 修复 round 1：控制器边界的阶段取消

Review 指出 P1：执行器使用阶段 child control，但 `ThorRuntime._step_direct()` 仍只检查任务根 control。复合动作中的兄弟线程可能在 strict `FAIL_STAGE` 取消后继续提交低层步骤。

### 根因与修复

- `Executor.execute_action()` 进入 `action_deadline_scope` 时没有传入执行器的阶段 control。
- `action_deadline_scope()` 和 `ensure_control(runtime)` 没有线程局部的活跃 control 概念，因此 `_step_direct()` 锁前和锁内检查的都是根 control。
- 修复后，`Executor` 把自身 control 传入 action scope；scope 将它安装在线程局部状态中，`ensure_control()` 优先返回当前线程的 scoped control。
- `_step_direct()` 保留锁前、锁内两次检查，所以等待锁和复合动作后续步骤都会观察阶段取消。
- `controller.step()` 自身抛出的基础设施异常仍同时取消 scoped child 与任务根 control；普通动作失败返回 event 后仍交给失败策略。
- 离开 scope 会恢复此前的嵌套 control。`install_control()` 为每次任务执行重建线程局部容器，避免复用旧执行状态。

### RED

新增确定性双机器人回归：robot2 通过真实 `_step_direct()` 提交 `FirstSubstep`；robot1 随后触发 strict `FAIL_STAGE`；robot2 等待阶段 child 确认取消后尝试 `SubstepAfterStageCancellation`。

命令：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_execution_policy.ChildExecutionControlTest.test_strict_stage_cancel_blocks_sibling_substep_at_controller_boundary
```

修复前结果：退出码 1，1 个失败。controller 实际调用为：

```text
['FirstSubstep', 'SubstepAfterStageCancellation']
```

期望只有：

```text
['FirstSubstep']
```

### GREEN

同一聚焦命令修复后：

```text
----------------------------------------------------------------------
Ran 1 test in 0.010s

OK
```

要求的覆盖验证：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_execution_policy.py \
  tests/test_parallel_runner.py \
  tests/test_execution_shutdown.py
```

结果：

```text
----------------------------------------------------------------------
Ran 101 tests in 3.669s

OK
```

回归同时验证：controller 未接收取消后的第二步，根 control 未取消，随后创建的阶段 child 能通过相同 action scope 和真实 `_step_direct()` 成功提交 `NextStageSubstep`。

语法检查与 `git diff --check` 均退出码 0，无输出。本轮按要求未运行 discover 或无关测试。
