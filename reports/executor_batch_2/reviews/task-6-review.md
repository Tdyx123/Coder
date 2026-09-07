# Task 6 独立规格与质量审查

评审范围：`1d243042..b9dd85c3`，工作树 `/tmp/coder-executor-batch-2`。依据 `task-6-brief.md` 的自包含绑定规格、`task-6-report.md`、`task-6-diff.txt` 与对应提交实际 diff。未扩大为任务 1–5 全局重审，未修改生产代码，未启动子代理。

## 规格 verdict：changes requested

策略透传、版本/评估契约、三例与验收交接实现符合任务要求；新增使用说明中的父 CLI 命令无法运行，需修复下面的 P2 后才能批准完整交付。

- generated、parallel、benchmark parser 均默认 legacy 并拒绝非法策略；strict 沿父任务、重试、子命令和 standalone convenience API 透传。不增加策略环境变量。
- legacy 子命令不附加新参数，保留旧独立 argparse 脚本兼容；父端检查实际子策略，strict 不静默降级。
- 共享 wrapper 保留阶段/全局失败状态，scheduler2 未明确静止时阻止 Done/评估；validator 允许明确静止的普通 failed 目标评估，同时拒绝 timeout/cancelled 有效评分，保留第一批固定分母和计数约束。
- strict v2 要求 scheduler2，旧结果缺失版本按 scheduler1 分组；普通 summary、durable rebuild 和 benchmark 同时区分 policy/scheduler/movement/evaluation/schema。
- benchmark 输出按策略隔离，执行失败与目标失败分别提供诊断；文档提醒不能将 strict 提前停止直接视为能力回退。
- 三个确定性例子实际使用生产调度器、资源租约和 StepMovementStrategy，并保存顺序、资源和阶段条件证据；文档明确这些不是 Unity 验收。
- verify.sh 保留第一批具名集合并加入第二批集合；48 次驱动保存脚本摘要、父验证结果、固定分母与进程清理。最终 Unity 运行由 root 执行，pending 不作为缺陷。

## 质量 verdict：changes requested

### [P2] 新增父 CLI 示例在参数解析前失败

位置：`docs/executor_batch_2.md:8`（命令至第 10 行）。

复现：在仓库根目录执行 `/home/dwb/.pyenv/bin/pyenv exec python scripts/executor_system/parallel_runner.py --help`，退出码 1，报 `scripts/executor_system/parallel_runner.py:123: ImportError: attempted relative import with no known parent package`。因此按新增 strict 示例运行时，父进程根本不能解析或透传新参数，也不能创建结果。任务现有测试直接导入 parser/main，未覆盖该启动形式。

相对导入本身已存在于 `1d243042`，这里没有把历史源码问题算成新引入的执行器回归；本次新增的交接文档明确推荐了不可用形式，属于任务 6 的可运行文档范围。可修复直接脚本入口并增加有界启动检查，或将新增文档改成已验证可运行的模块入口。已独立验证 `/home/dwb/.pyenv/bin/pyenv exec python -m scripts.executor_system.parallel_runner --help` 退出 0，输出包含 `--execution-policy {legacy,strict}`。

未发现其他本范围内需要修改的问题。

## 验证证据与边界

- 已检查 tracked `reports/executor_batch_2/verification.log`：`Ran 575 tests in 17.797s`、`OK`；按任务指示未重复该全套。
- 本审查只运行上述两条 `--help` 有界只读探针，并执行 `git diff --check 1d243042..b9dd85c3`（退出 0）。
- 已读取三例脚本、保存的报告/日志、验收驱动及说明；未启动 Unity，未改第一批报告。

最终结论：修复 P2 的文档入口或实际入口后做定向复核，无需因该问题重复完整 575 测试或提前运行最终 48 次。

## Fix1 定向复审与最终裁定

复审范围：`b9dd85c3..8a4d97a7`。已读取 `task-6-fix1-diff.txt`、实施报告追加记录，并核对实际提交差异：仅修改新增文档中的父 CLI 命令，另追加 tracked 实施报告，无生产或测试源码变更。

唯一 P2 已修复：`docs/executor_batch_2.md:8` 改为 `/home/dwb/.pyenv/bin/pyenv exec python -m scripts.executor_system.parallel_runner`。独立重跑该模块命令的 `--help`，退出 0，帮助内容包含 `--execution-policy {legacy,strict}`；`git diff --check b9dd85c3..8a4d97a7` 退出 0。新增文档的其余三个具体脚本入口已由实施方记录 `--help` 全部退出 0；本次差异未改变这些命令。

该修复消除新增推荐命令的启动失败，没有引入本差异直接相关的新问题。未重跑 575 套测试，未运行 Unity，未修改源码，未启动子代理。

- **规格最终 verdict：approved。** 原审查唯一阻塞已消除，任务 6 绑定要求在本评审范围内满足。
- **质量最终 verdict：approved。** 无剩余本范围可操作问题。

最终 48 次真实 Unity 验收仍由 root 在整体最终审查后执行；该 pending 状态不影响本次任务 6 代码与文档的审查裁定。
