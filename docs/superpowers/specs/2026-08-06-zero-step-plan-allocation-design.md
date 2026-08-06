# 合法 0 步计划与机器人分配设计

## 背景与目标

Fast Downward 在子任务目标已由初始状态满足时会生成仅含 `cost = 0` 注释的合法 0 步计划。当前流程把“未解析出动作”统一视为错误，导致合法 no-op 子任务终止整个任务。

目标是允许合法 0 步计划，并确保这类子任务不参与机器人能力过滤或 CP-SAT 整数规划，也不分配机器人；同时保留子任务记录与编号，避免完成率和下游产物错位。

## 行为设计

- 计划文件存在、没有动作、包含 Fast Downward 的 `cost = 0` 标记，且 planner record 未报告失败时，认定为合法 no-op。
- 没有动作但不满足上述条件的空文件或异常输出继续抛出 `No plan actions found`，不掩盖规划器故障。
- `_build_subtask_requirements` 为 no-op 生成正常 requirement 记录，并设置：
  - `requires_execution: false`
  - `required_skills: []`
  - `min_mass_capacity: 0`
  - `duration: 0`
  - `required_locations: []`
  - `unresolved_location_actions: []`
- 有动作的子任务设置 `requires_execution: true`，其现有需求提取行为保持不变。

## 分配与输出

- `_allocate_subtasks_with_cpsat` 将 requirements 分为 actionable 和 no-op 两组。
- 只有 actionable requirements 进入候选机器人过滤和 `_solve_subtask_assignment`。
- 全部为 no-op 时不调用 OR-Tools；混合任务的 precedence 中指向 no-op 的依赖自然视为在时间 0 已完成。
- 输出仍按原始子任务顺序包含所有记录。no-op 记录使用：
  - `candidate_robots: []`
  - `assigned_robot: null`
  - `assigned_robot_domain: null`
  - `start: 0`
  - `end: 0`
  - `requires_execution: false`
- actionable 输出继续包含实际候选、机器人和调度时间，并设置 `requires_execution: true`。
- `requirements.json`、`candidate_robots.json` 和 `subtasks.json` 继续生成；候选机器人产物为 no-op 子任务记录空列表。

## 错误处理与兼容性

- 缺少 planner record、计划文件不存在、规划器明确失败、以及无法证明为合法 0 步计划的空输出仍保持现有错误语义。
- 混合任务中没有可执行子任务可用机器人的情况仍抛出 `No feasible robot`。
- 纯 no-op 任务即使机器人列表为空也可成功，因为不需要执行或分配。
- 现有非空计划的 CP-SAT 目标函数、能力/载重约束、位置互斥和前驱约束不变。

## 测试设计

- 合法 `cost = 0` 计划可生成 no-op requirement，不再抛错。
- 无动作但没有零成本标记的计划仍抛错。
- 混合任务只把 actionable requirement 传给候选过滤和 CP-SAT，并按原顺序合并输出。
- no-op 输出字段为未分配状态，actionable 输出保持现状。
- 全 no-op 且机器人列表为空时成功，并确认未调用 CP-SAT。
- 现有需求提取、能力过滤和调度测试继续通过。
