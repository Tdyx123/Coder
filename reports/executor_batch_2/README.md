# 执行系统第二批验收与第三批交接

[第二批计划](../../docs/superpowers/plans/2026-09-07-executor-batch-2-execution-semantics.md)六项任务已完成，分别经过独立规格与质量审查。整批交叉审查发现的三个整合问题及一个验收测试缺口已统一修复并[复审通过](reviews/final-fix-review.md)。最终生产代码 SHA：`149637b28ffcb49b95ac8d177f0302c043942851`；其后提交仅整理验收证据。第一批生产 SHA 为 `3c2661b267b767386114ed8cad5c6330871306a9`，本批分支起点为 `8f95c7ca092d7025a5ca67def5015daa56f6c568`。

## 验证结果

- 第一批全部具名集合及第二批新增/受影响集合：**581 项通过，18.946 秒，退出码 0**。[完整日志](verification.log)，[可复用命令](verify.sh)。另有 195 项定向回归通过；最终修复的 RED/GREEN 和 watchdog 故障注入证据在 [final-fix-evidence](final-fix-evidence/)。无期限依赖环测试在 step/teleport 两模式验证外部取消与 finally 清理；故意忽略 watchdog 取消时测试按预期失败，结束后残留线程为 0。
- 固定 manifest **12 样例 × 2 移动模式 × 2 执行策略，共 48 次真实 Unity 运行**，全部正常退出、父进程结果契约通过、评估有效、线程静止、原始目标分母不变。运行标识、策略、scheduler_version=2、所有计划动作审计字段、RU 复算及所属进程清理检查通过。[检查输出](real-verification.log)。
- 没有非预期工作线程故障或 cleanup_errors。strict 的 7 次阶段失败各保留一条预期 child `ExecutionCancelled` 记录：原始普通动作失败 → StageFailureDecisionError → 阶段取消，根控制仍允许静止后当前快照与 Done/评估。这些取消证据原样保留，不能说 worker_errors 全为空。

| 策略 | 移动模式 | 运行/正常退出/有效评估 | 执行 completed / partial / failed | 平均 GCR | SR=1 |
|---|---|---|---|---:|---:|
| legacy | teleport | 12 / 12 / 12 | 9 / 3 / 0 | 0.687500 | 0 |
| legacy | step | 12 / 12 / 12 | 9 / 3 / 0 | 0.687500 | 0 |
| strict | teleport | 12 / 12 / 12 | 9 / 0 / 3 | 0.638889 | 0 |
| strict | step | 12 / 12 / 12 | 8 / 0 / 4 | 0.611111 | 0 |

48 次正常退出和有效评分不代表任务成功；这些固定样例均没有 SR=1。legacy 的 24 份 GCR 与第一批逐例一致。strict 的目标变化来自已有失败触发停止策略，不能直接解释为导航或模型能力回退：

- FloorPlan1_task_0：Egg 导航失败后停止，teleport/step 的 GCR 均由 0.5 变为 0.25，13 项后续动作未执行。
- FloorPlan201_task_1：step 导航 NO_PLAN_FOUND 后停止，GCR 从 2/3 变为 1/3；teleport 不变。
- FloorPlan401_task_10：Cloth 已 Clean，CleanObject 返回失败，未声明效果证明的默认 FAIL_STAGE 在 strict 中停止，双模式 GCR 从 2/3 变为 1/3。
- 另一个 strict 失败样例因向关闭的 Drawer 放置物体而停止，GCR 与第一批相同。每个组合的完整首个失败、动作计数及固定目标证据见 [逐例基线比较](149637b2_real/baseline-comparison.json)。

## 报告与复现

- [可读真实报告](149637b2_real/report.json)：保留动作结果、尝试、等待原因、波次成员与资源拥有者，省略重复的大型对象元数据。
- [完整真实报告（gzip）](149637b2_real/full-report.json.gz)：原始快照、child metrics 和父进程校验结果全部保留；可读报告内有未压缩原文 SHA-256。
- [完整结果实例](149637b2_real/result_example.json)，[四组汇总](149637b2_real/summary.json)。
- [语义接口与参数/资源需求表](../../docs/executor_batch_2.md)，[三个可运行确定性语义示例](semantic_examples.py)及[实录](semantic_examples.json)。示例使用真实执行器和 step 算法、确定性仿真替身；与 48 次 Unity 验收分开。
- [各任务与整批审查](reviews/)，[各任务实现报告](implementation/)，[执行与裁定记录](execution-ledger.md)。

```bash
bash reports/executor_batch_2/verify.sh
/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_2/run_real_validation.py \
  /tmp/coder-executor-batch-2 /home/dwb/thor/Coder /tmp/executor-batch-2-real-repeat
/home/dwb/.pyenv/bin/pyenv exec python reports/executor_batch_2/check_real_report.py \
  /tmp/executor-batch-2-real-repeat/report.json
```

真实验收原始 stdout/stderr、metrics 与 report 在 `/tmp/executor-batch-2-real-149637b2/`。驱动仅拥有并回收自己创建的进程组，输出目录不覆盖历史报告。Unity 使用本地 AI2-THOR 缓存锁与进程权限，通过自动审批执行。此前单例 smoke 使用未提交 CLI 开发树，保留于 `/tmp/executor-batch-2-smoke-01/`，不作为本次验收依据。

## 实施裁定与边界

以下四项裁定按作出顺序保存，未隐去成本：

1. SKIP_IF_EFFECT_ALREADY_TRUE 的成功证明先于纯失败决策函数，保持其声明的五种 decision kind；若接口解释不符，需要调整接口。
2. 整体快照读取错误按基础设施故障传播，callback 异常保留 cause；可能停止曾被旧 legacy 吞掉异常的运行。绑定失效在副作用前重新排队、之后失败，遵守计划既定契约。
3. 导航预先租约当前明确 open/openable 的恢复候选，保留 Close/Move/Open 恢复且满足第一副作用前完整准入；成本是共享潜在阻挡物的导航可能保守串行化。GoTo 不因普通目的地而自动占用对象。
4. WorldState.tick 表示阶段内协调器确认的高层终态数，与真实提交 version 分离；worker callback 读取准入时进展，重试/延期/idle Pass 不增加。成本是不复制旧多机器人私有线程计数顺序，依赖该竞争顺序的 callback 需调整。

没有 parked 或未关闭审查问题。验证范围是明确具名回归和固定 12 样例双模式双策略，不宣称全仓 discover 或全部 Unity 场景通过。导航网格、间距、候选和重规划预算未变；没有 step 失败隐式传送。第三批接管全量动作注册表、大文件职责拆分及架构/性能工作，沿用本批参数检查、资源需求、不可变快照与结果版本契约。第一批报告和生成样本保持原样。
