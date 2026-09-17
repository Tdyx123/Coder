# GCR 评价协议：atomic_goals_v3

评价以原始任务记录的 `object_states` 为准。动作计划中的实例选择、执行别名的重绑定、旧 bundle 中被具体化的 `gcr` 均不能改变评分要求。已有共享运行入口的生成脚本重跑时会重新读取原始任务目标；不会自动重算历史结果。

## 子目标与实例

每个 `states` 状态和每个 `contains` 元素各计一项。没有状态和包含关系时，目标作为一项对象存在性条件。GCR 为已满足子目标数除以子目标总数。

类型名表示独立的存在性要求。例如 Shelf 包含 Pen、CD、Mug，会拆成三条关系，各自允许由不同 Shelf 实例满足；Mug 的 HOT、CLEANED 也可以由不同 Mug 满足。包含关系必须由容器当前的 `receptacleObjectIds` 证明。

编号实例（如 `Shelf_1`、`CD2`）使用评价上下文创建时传入的 `object_id_bindings` 快照。直接 objectId 按 ID 匹配。显式引用没有绑定或绑定冲突时，子目标为 unknown，整次评价无效；已知 ID 在完整场景中不存在则为不满足。类型目标不使用执行器推断的别名绑定。

直接使用 ThorRuntime 时，应在执行前一次注册完整任务绑定。未预建评价上下文的调用使用首次注册时保存的快照，不使用执行后变化的别名表。也可以显式调用 `EvaluationContext.from_goals(goals, object_id_bindings=bindings)`。

HOT、COLD、SLICED、BROKEN 延续历史证据规则，但证据必须带有符合子目标约束的实际物体 ID。类型状态继续识别切片食物和破裂鸡蛋；显式实例不会因为其他同类物体变换而替换评分对象。动作前置条件和阶段条件仍沿用完整条件判断，不使用拆分计分语义。

## 结果字段

指标结构仍为 `metrics_schema_version=2`，评价版本为 `evaluation_version=atomic_goals_v3`。

- `original_goal_count`：原始目标条数。
- `atomic_goal_count`：拆分后子目标总数，也是 GCR 分母。
- `satisfied_goal_count`：满足的子目标数。
- `subgoal_results`：每个子目标的条件、原目标索引、子目标索引、状态、候选实例、匹配实例 ID 和诊断原因；索引从 0 开始。
- `goal_results`：原目标级状态和对应子目标索引。

TC 在全部子目标满足时为 1。SR 继续根据 TC、RU 计算。缺少必要元数据或显式绑定时，不输出可用的最终 GCR；保留 unknown 子目标明细。空目标仅在调用方明确允许已验证的空操作任务时计满分。

| 原始目标 | 实际结果 | GCR |
|---|---|---|
| Shelf contains Pen、CD、Mug | Pen、Mug 在 Shelf_1；CD 在 Shelf_5 | 1 |
| Shelf_1 contains Pen、CD、Mug | 相同场景 | 2/3 |

## 兼容与比较

旧 `fixed_goals_v2` 结果继续使用原始目标条数作分母，仍可读取。新版结果校验要求子目标明细、索引、状态、计数和分数一致。

导航基准按评价版本分组；跨版本总计不提供混合 GCR，跨版本比较不通过验收。单行汇总工具拒绝混合评价版本，需先按 `evaluation_version` 分开输入。新旧分数不可直接当成同一口径比较。
