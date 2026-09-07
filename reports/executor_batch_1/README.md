# 执行系统第一批可靠性验收与交接

执行日期：2026-09-07 至 2026-09-08。计划：[第一批修改计划](../../docs/superpowers/plans/2026-09-07-executor-batch-1-reliability.md)。

实现已完成，六项任务分别经过独立审查，整批交叉审查发现的五项问题也已修复并[复审通过](review.md)。最终生产代码提交为 `3c2661b267b767386114ed8cad5c6330871306a9`，工作分支为 `codex/executor-batch-1-reliability`，工作区为 `/tmp/coder-executor-batch-1`。

## 验证结果

- 本批验收文件、受影响生成入口和最终新增回归共 **442 项测试通过**，耗时 9.912 秒，退出码 0。[完整输出](verification.log)，[验证命令](verify.sh)。从仓库根目录运行 `bash reports/executor_batch_1/verify.sh`，统一使用显式 pyenv Python。
- 固定 manifest 的 **12 样例 × 2 移动模式**已在原代码和最终代码上分别真实执行。两组均 24 次正常退出。最终组 24 份评估均有效，目标分母均与生成脚本原始 `BUNDLE_DATA.gcr` 一致，父进程结果契约校验通过，`worker_errors` 和 `cleanup_errors` 均为空，RU 均可从保存的 `ru_inputs` 复算。
- 原基线为 `cd853222`；计划所列 `070b808b` 到该提交在执行系统、相关汇总/基准脚本及测试范围内无差异。原版本初始回归为 293 项通过。

| 代码 / 评估版本 | 移动模式 | 运行数 | 正常退出 | 平均 GCR | SR=1 数 |
|---|---|---:|---:|---:|---:|
| cd853222 / legacy_v1 | teleport | 12 | 12 | 0.6875 | 0 |
| cd853222 / legacy_v1 | step | 12 | 12 | 0.6875 | 0 |
| 3c2661b2 / fixed_goals_v2 | teleport | 12 | 12 | 0.6875 | 0 |
| 3c2661b2 / fixed_goals_v2 | step | 12 | 12 | 0.6875 | 0 |

这些样例的评分未变化；不能把正常退出或有效评估解释为任务成功。固定分母、元数据未知、跨实例历史证据、真实 SIGINT、中断回收及损坏结果恢复由针对性回归覆盖。

## 报告与复现

- [原版本真实报告](cd853222_legacy_v1/report.json)
- [最终版本真实报告](3c2661b2_fixed_goals_v2/report.json)
- [最终 v2 结果实例](3c2661b2_fixed_goals_v2/result_example.json)，含 `ru_inputs` 的 `no_trans`、`no_trans_gt`、`max_trans`。
- [真实运行脚本](run_real_validation.py)：参数依次为代码根目录、独立输出目录、固定样例所在仓库。使用已存在的 `tests/fixtures/movement_benchmark_plans.json`，不重选样例；只终止本脚本创建的进程组。

```bash
/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_1/run_real_validation.py \
  /tmp/coder-executor-batch-1 /tmp/executor-batch-final-repeat /home/dwb/thor/Coder
```

原始日志分别保留于 `/tmp/executor-batch-real-reports/cd853222_legacy_v1_simulation/` 和 `/tmp/executor-batch-real-reports/3c2661b2_fixed_goals_v2/`。真实仿真需要 AI2-THOR 缓存锁及进程权限；首次沙箱运行因缓存锁只读失败，随后通过自动审批在隔离代码/输出目录中完成。中间提交 `8766acae` 的报告独立保留用于追踪，不作为最终验收依据。

## 分任务提交

| 任务 | 实现与审查修复提交 |
|---|---|
| 固定目标、历史证据 | df164db0、5b2edeee |
| 动作账本、v2 结果与分组 | 04efa8d1、f3e616c5 |
| 取消、线程静止、异常与收尾 | faf2083e、f3c6315f |
| 所属进程组与预算 | fcb970c4、96fdeae4 |
| 原子存储、增量汇总与恢复 | 7be0c970、57c9263b |
| 运行输出隔离与文档 | d29e6ad5、8766acae |
| 整批交叉审查修复 | 3c2661b2 |

## 验收边界与第二批交接

固定目标不会被观察追加；缺失字段保持未知；同一目标的证据不能跨实例拼接。工作线程错误和清理错误分别留存；取消后禁止新的 controller 提交；无法确认工作函数退出时禁止终态评估与控制器关闭，并封存结果。进程回收只作用于已登记的所属进程组。每个父进程确认完成的尝试均单独原子落盘，重建仅使用完成标记，损坏或未完成尝试保留为 interrupted。

Python 线程中已提交的仿真调用仍不能被强制中断，最终硬停止依赖外层所属进程组。真实验证范围仅为固定 12 样例双模式，不代表全部场景或严格执行策略已验证。额外早期全仓 `unittest discover -s tests` 因该 worktree 缺少未跟踪的 RAG/PDDL/脚本资源出现 4 failures、15 errors，未宣称全仓通过；本批明确列出的 442 项验收回归通过。

第二批沿用本批公共字段与 `execution_policy="legacy"`，不得重新引入全局目标缓存；严格执行策略和调度器重写不在本批。历史报告保持原样，回退不删除新报告，也不恢复全机 GPU 清理。
