# Task 6 实施报告

状态：任务实现和验证完成，等待独立审查；不合并、不推送。工作区 `/tmp/coder-executor-batch-2`，分支 `codex/executor-batch-2`。初始接入基线 c0791872；Task 5 并行 scoped 修复后的最终评审基线为 `1d243042e68eec9d05be262acd276254dde38b0f`。第一批已验收生产 SHA `3c2661b267b767386114ed8cad5c6330871306a9`，第二批起点为其后合入的 `8f95c7ca092d7025a5ca67def5015daa56f6c568`。

## 实现

- generated_plan_runtime、parallel_runner、benchmark CLI 接受 --execution-policy legacy|strict，默认 legacy，不增加环境变量。父重试/批处理/异常路径透传所选策略并保留 requested_execution_policy；strict 子参数显式传递，结果实际策略与父请求必须一致。
- 默认 legacy 不向子进程追加新参数：旧独立 argparse 文件按原接口继续运行，共享运行时的旧生成文件无需修改即可使用新策略。旧独立脚本不支持 strict 时显式失败，没有自动降级。未修改旧生成脚本或第一批报告。
- TaskRunner 和 tolerant API 沿用 Task 5 接口。另扩展 task_plan.run_action_plan 的关键字以支持 standalone 透传；执行仍是同一 TaskRunner/StageScheduler 循环，不另建路径。
- shared runner 保留 report.execution_status、阶段/global证据，不能由 action_counts.failed=0 覆盖语义失败。scheduler2 没有 execution_quiescent=True（包括 false/null/缺失）时不提交 Done/最终评估，取消/timeout 保留第一批中止行为。
- run_results 支持 strict v2，并要求该记录 scheduler_version=2；scheduler_version 只接受 int 1/2，拒绝 bool/string/未知值。scheduler2 valid 评估允许 quiescent 的 semantic failed，保留 timeout/cancel 禁止与全部第一批 fixed goal/TC/GCR/SR/RU/count 约束。历史缺失 scheduler_version 按 1 分组，不凭空升级；shared build_runner_result 不预先声明 scheduler2，真实 execution_report 提供版本，避免旧独立模板误标。
- 普通 summary、durable rebuild、benchmark result_groups 均增加 scheduler_version。benchmark 按政策放置输出目录，并区分 execution_failure / goal_failure；目标评分定义不变，报告明确 strict 停止变化不能直接解释为能力回退。
- README/scripts README 与 docs/executor_batch_2.md 包含完整入口、条件与策略契约、资源需求表、冻结快照/每-agent选择接口、snapshot_is_current、结果解读和第三批边界。
- reports/executor_batch_2/semantic_examples.py 实际调用生产执行器/调度器/资源租约及 StepMovementStrategy，替换 Unity/高层交互为 deterministic simulator fake；已保存真实运行生成的 JSON/log。legacy 抓取失败继续、strict FAIL_ROBOT 同伴完成、step A 等 B 三例全部有顺序/等待/波次/资源/阶段条件证据。清楚标明不是 Unity 验收。
- 可复用 verify.sh 包含第一批全部具名集合与本批新增/受影响集合，不 discover。run_real_validation.py 从 /tmp 预备脚本固化，使用原 source 固定 manifest/脚本、所选 code 的 PYTHONPATH、独立策略/模式目录，保存脚本 SHA256、父权威 validated_metrics、固定分母、scheduler2/实际策略检查、进程清理；不运行最终 48 次。
- tracked reviews 保存任务1–5独立审查与最终裁定原文；Task5 至 1d243042 规格/质量均 approved，任务6独立审查由主代理执行。

## RED / GREEN 与验证

1. `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_parallel_runner.py tests/test_movement_benchmark.py tests/test_run_result_contract.py`

   RED `/tmp/task6-red.log`（已保存 reports/executor_batch_2/task6-red.log）：100 tests，2 failures + 4 errors，均来自缺失策略CLI/父参数、strict被旧validator拒绝、scheduler分组字段缺失。GREEN `/tmp/task6-green.log`：100 tests in 3.562s，OK。

2. 新增 tests/test_execution_policy_cli.py 覆盖 standalone 实际 strict 执行、共享 wrapper 保留 semantic failed 与目标评分、父子策略不一致、benchmark透传/目录/失败分类，以及未静止禁止 Done/evaluate。初始 `/tmp/task6-red2.log` 暴露缺 standalone 参数和 execution_failure 分类；该轮测试曾导入 TestCase 导致额外发现旧测试，并有错误 RU fixture，随后改为模块导入、修正 no_trans=2 后验证，不把 fixture 错误当产品 RED。还因此发现预先 shared_build_runner_result stamp scheduler2 会误标旧独立模板，已取消预先 stamp。

3. 未静止边界单测 RED `/tmp/task6-red3.log`（tracked task6-quiescence-red.log）：1 failure，发现仍提交 Done；null quiescence 再次 RED `/tmp/task6-red4.log`：1 failure。最终 `/tmp/task6-green4.log`：5 tests in 0.068s，OK。

4. 最终完整门禁（Task5 fix3 基线 1d243042 上运行一次）：

   `bash reports/executor_batch_2/verify.sh`

   `/tmp/task6-full-gate.log`，tracked `reports/executor_batch_2/verification.log`：**Ran 575 tests in 17.797s，OK，退出 0**。覆盖第一批全部集合和本批所有新增/受影响集合。日志的模拟 disk-full ERROR 为已有故障注入用例，无失败测试。

5. `/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_2/semantic_examples.py --output reports/executor_batch_2/semantic_examples.json`

   最终 `/tmp/task6-examples-final.log`（tracked semantic_examples.log），退出 0。legacy partial（5 attempts /4成功/1失败），strict failed（3 attempts/2成功/1失败/2未执行），step completed（2成功、两次MoveAhead、零Teleport）。三例阶段条件均 initial=false/final=true，资源持有不重叠；线程交错只按实际日志说明，不承诺复现同一跨机器人竞争顺序。

6. `/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/executor_system/generated_plan_runtime.py scripts/executor_system/parallel_runner.py scripts/executor_system/run_results.py scripts/executor_system/task_plan.py scripts/benchmark_movement_modes.py tests/test_execution_policy_cli.py tests/test_parallel_runner.py tests/test_movement_benchmark.py tests/test_run_result_contract.py reports/executor_batch_2/semantic_examples.py reports/executor_batch_2/run_real_validation.py`

   退出 0；`git diff --check` 退出 0。

## 限制与最终验收

- 最终真实 48 次 Unity 仿真 **pending**，由主代理在独立审查通过和最终 production SHA 确定后运行。本任务没有伪造该验收。主代理报告 /tmp/executor-batch-2-smoke-01/report.json 的单例 legacy/teleport 预检成功（exit0、completed、valid、固定分母/契约成立、无worker/cleanup errors）；它使用尚未提交树，仅预检，不计最终验收。
- benchmark 顶层 compatibility aggregates 保留旧接口；跨策略/版本比较应读取 result_groups。输出策略分目录是新增行为，文档说明指定文件名也位于政策子目录。
- 未改导航预算、网格、隐式传送策略；未新增依赖，未启动子代理，未 merge/push。

## Task6 Fix1：可运行文档入口

独立审查唯一 P2：新增直接脚本启动示例触发基线相对导入错误。已将 docs/executor_batch_2.md 中父CLI改为 `/home/dwb/.pyenv/bin/pyenv exec python -m scripts.executor_system.parallel_runner`；未改旧生产入口。

从仓库根目录定向验证以下四条命令，退出码全部为0：模块入口 `-m scripts.executor_system.parallel_runner --help`、`scripts/benchmark_movement_modes.py --help`、`reports/executor_batch_2/semantic_examples.py --help`、`reports/executor_batch_2/run_real_validation.py --help`；统一使用显式pyenv Python。前两项help包含 execution-policy选项。`git diff --check` 退出0。只改文档/报告，没有重复575测试或真实Unity。
