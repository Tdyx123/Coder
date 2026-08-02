# 强制语义顺序模式验证与筛查

## 验收结果

- Gold：52 条人工复核任务，41 条含强制顺序，119 条真实强制边。
- Precision：100.0%；95% Wilson 下界：96.9%。
- 强制边 recall：100.0%；有序任务完整解析覆盖率：100.0%。
- 门槛：precision ≥ 90.0%，置信下界 ≥ 90.0%，recall/coverage ≥ 40.0%。
- 最终门禁：**通过**。

## 已接受的高置信语言模式

| 模式 | 表面形式 | Precision | 95% 下界 | Recall |
|---|---|---:|---:|---:|
| explicit_then_stage_barrier | A then B<br>A then B then C<br>A and B then C<br>A then B and C | 100.0% | 96.9% | 100.0% |
| then_serial_chain | A then B<br>A then B then C | 100.0% | 95.0% | 100.0% |

解析规则：仅把显式 then 当作阶段 barrier；and 与逗号保留在同一阶段。动作与对象必须唯一对齐到某个阶段，最低词法分和 runner-up margin 均达标才输出强制边，否则主动弃权。整个过程不读取 PDDL 先决条件或机器人分配。

迭代记录：v1 未识别 switch it/them on，留出集 recall 为 97.5%；补充代词动作别名后的 v2 recall 为 100.0%，且 precision 未下降。

## 全量筛查

- 全量记录：148。
- 成功解析强制顺序：126 （85.1%）。
- 综合状态：{"conflict": 52, "no_forced_order": 22, "pass": 74}。
- 仅看任务分解文字：{"conflict": 48, "no_explicit_conflict": 78, "not_applicable": 22}。
- 机器人参考安排回测：49 条记录违反强制顺序，其中 89 条强制边被并行、13 条被反向安排。

状态解释：

- conflict：分解文字或最终 Sequence of Operations 明确违反强制边。
- pass：已识别强制边，且最终机器可读安排全部保持。
- no_forced_order：任务没有命中已接受的强制顺序模式。
- unable_to_screen：解析器弃权、验证门禁失败或安排不完整。
- no_explicit_conflict：任务分解文字未发现明示冲突；这不是正确性证明。

## 产物

- 验证明细：data/grpo/deepseek_v3_2_ordering_validation.json
- 逐任务筛查：data/grpo/deepseek_v3_2_decomposition_screening.jsonl
