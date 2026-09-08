# 批量运行持久化与实时进度

## 行为与兼容性

每次 attempt 的 `result.json` 和完整日志仍即时落盘。不同 attempt 可并行保存；同一 attempt 的重复提交被拒绝。运行期间完整汇总保持启动占位，工作线程及子进程清理结束后才全量生成一次；需要观察运行进度时使用独立进度文件。历史结果、最终汇总格式和 `--rebuild-summary RUN_DIR` 恢复入口保持兼容。

执行器新增 `--progress-file PATH`，每秒原子发布轻量 JSON，阶段变化立即发布。协议版本为 1：

- 身份与状态：`run_id`、`summary_path`、`phase`、`updated_at`。阶段为 `running`、`finalizing`、`completed`、`interrupted`、`failed`。
- 任务计数：`planned_tasks`、`completed_tasks`、`running_tasks`、`pending_retry_tasks`。排队任务不算运行；还有重试机会的超时任务不算最终完成。
- 尝试计数：`attempts_started`、`attempts_persisted`。仅成功持久化后计入已落盘。
- 最终任务统计：`success_count` 表示进程正常完成，另有 `failure_count`、`timeout_count`、`valid_evaluation_count`、`task_success_count`。
- 参数与错误：`movement_mode`、`effective_timeout_seconds`、`storage_error`，发布失败时可包含 `progress_error`。

进度发布失败不影响已保存结果。任务结果保存失败或最终汇总失败会使运行失败。SIGTERM 进入中断路径，先回收执行器拥有的子进程组，再尽力汇总。强制 SIGKILL 无法执行退出处理，已提交的单任务文件仍可用于恢复。

Manager 为每次操作指定独立进度文件，以无缓冲模式启动 Python，持续读取 stdout/stderr 并保留有上限的日志尾部。进度与日志每秒写入现有 `OperationLog.details`，页面沿用两秒轮询，无需数据库迁移。

Manager 校验进度协议、计数、路径和运行身份。短暂读取失败保留上次状态及时间；仅旧执行器使用按文件变化发现汇总的兼容逻辑。新协议没有可验证的身份时不会猜测汇总归属。进程退出码与最终汇总状态共同决定批次状态，`completed` 不代表所有任务目标达成。

`timeout_seconds` 缺省或 `null` 表示自动，Manager 不传超时 CLI 参数，由执行器决定 step 120 秒、teleport 30 秒；显式数值继续优先。页面清空超时输入框即选择自动，运行后显示实际模式及超时。现有最多两次超时重试、启动与收尾预算不变。

## 验证记录（2026-09-08）

- Coder 存储、进度、执行器、结果契约、进程监督：131 项测试通过。
- Manager 后端全量：121 项测试通过。
- 前端 TypeScript 检查及 Vite 生产构建通过。构建仍提示主 chunk 较大。
- 回归覆盖并发与重复保存、写盘/汇总失败、恢复、队列与重试统计、双管道大输出、日志缓冲、运行中 API 进度、旧执行器、无有效身份的新执行器、异常回调清理及真实 SIGTERM 子进程回收。
- 从原运行中选取 `FloorPlan1_task_25`（fill the cup with water, switch on the toaster, and open the drawer）重跑：step 自动 120 秒，约 37.97 秒完成、1 次尝试、无超时；评估有效但 `task_success=false`。首次沙箱尝试被 AI2-THOR 缓存锁写权限阻断，获得该小样本执行权限后的结果才用于上述结论。未重跑整批，未修改原结果。

## 合成负载对照

手工构造 340 个任务、361 次结果保存，4 个保存线程，完整汇总 341,926,779 字节（约 342 MB）。保存时延从调用 `record_attempt` 到返回，包含旧实现随后执行的汇总重写，不能直接与排查记录中的“子进程结束到结果文件落盘”时延等同。

| 指标 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 保存与最终汇总总耗时 | 693.93 秒 | 7.88 秒 |
| 前 20 次平均保存耗时 | 0.453 秒 | 0.044 秒 |
| 后 20 次平均保存耗时 | 12.534 秒 | 0.038 秒 |
| 最终单次汇总耗时 | 4.11 秒 | 4.10 秒 |
| 全量重建次数 | 362 | 1 |
| 峰值 RSS | 3004 MiB | 1060 MiB |

这是开发机上的一次合成验证，受缓存和同时进行的验证任务影响；用于确认重复全量汇总已消除，不用于预测真实导航运行总耗时。完整汇总仍占用显著内存，本轮保留了所有证据字段。

可重现脚本 `tests/benchmark_run_result_storage.py` 使用临时目录，完成后自动清理数据。修改前版本固定为提交 `a35a60b097b3e945f6e99c1ff9f2b9617f7f6ed7`，也可用 `--baseline-revision` 指定其他基线。不要并行启动两组基准测试。

```bash
/home/dwb/.pyenv/bin/pyenv exec python tests/benchmark_run_result_storage.py before --report /tmp/storage-before.json
/home/dwb/.pyenv/bin/pyenv exec python tests/benchmark_run_result_storage.py after --report /tmp/storage-after.json
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests.test_run_progress tests.test_run_result_storage tests.test_parallel_runner tests.test_run_result_contract tests.test_process_supervisor
```

代码变更已写入两个工作区；尚未重启或部署 Manager 服务。导航内部等待与取消传播的根因不在本轮修复范围。
