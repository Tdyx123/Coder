# Step GoToObject 恢复验证

6 个固定样本的 GoToObject 失败：9 → 8。本报告是样本重放结果，不代表原批次全部 339 个任务的改善幅度。

|任务|导航失败（原→现）|耗时秒（原→现）|视角恢复成功|现超时|导航瞬移|
|---|---:|---:|---:|---|---:|
|FloorPlan1_task_0|4 → 4|21.7 → 58.2|0|False|0|
|FloorPlan1_task_3|0 → 0|12.2 → 9.4|0|False|0|
|FloorPlan1_task_5|0 → 0|12.0 → 9.4|0|False|0|
|FloorPlan1_task_8|0 → 0|8.9 → 6.0|0|False|0|
|FloorPlan1_task_17|4 → 3|25.4 → 102.3|2|False|0|
|FloorPlan1_task_26|1 → 1|24.5 → 62.9|1|False|0|

## 实现和边界

- 到达后目标不可见时最多尝试六个俯仰角；失败恢复原视角，超时和取消直接传播。
- step 候选默认从 10 点一次扩展到总上限 30 点，保留对象绑定及排除位置。
- 逐机器人返回候选、到达和 event 目标刷新失败，保留其他机器人的成功结果。
- NavigationMetrics 新增 visibility_look_attempts、visibility_recoveries、candidate_expansions；robot_failures 保留 navigation_decision_trace。
- 纯 step；保留既有失败边、移动重规划和任务时限；未修改 LLM/PDDL 生成或评估语义。

## 验证

- 158 个导航相关测试通过，编译检查及 git diff --check 通过。独立审查发现的问题均补充回归并修正。
- 扩展测试曾运行 239 项，其中 238 项通过，1 项现有评估断言失败：test_temperature_check_records_only_satisfied_state_without_mutating_goals 预期 GCR 0、实际 0.5。该测试只执行 Wait，换回 HEAD evaluate 方法仍复现；未在本次修改评估口径。
- 最终重放设置：step / legacy / full / 120 秒 / 单 worker；原 executable_plan.py 未改写。
- 最终六样本批次中 FloorPlan1_task_17 出现一次 BrokenPipeError，属于模拟器通信中断；其未完成动作不计作改善。仅该样本单独重跑，完整重跑结果用于该行比较，原始中断记录保留。
- 最初沙箱内重放在创建 ~/.ai2thor 锁文件时失败，未执行动作，不计入比较；其产物保留在 replay/0908_01.json。
- 恢复搜索增加了困难案例的耗时。不可见或物理不可达目标仍可能失败；这次改善不能解读为保证导航成功。

## 产物

- 最终结果：[0908_03.json](replay/0908_03.json)
- 单样本重跑结果：[retry/0908_01.json](retry/0908_01.json)
- [逐任务比较](comparison.json)
- [测试日志](unit-tests.log)
- [重放脚本](replay.py)（从仓库根目录使用 pyenv exec python 运行；模拟器需要可写 AI2-THOR 缓存和 GPU）
- [汇总脚本](summarize.py)
