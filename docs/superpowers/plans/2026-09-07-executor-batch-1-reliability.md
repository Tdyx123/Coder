# 执行系统第一批：评估、异常与运行可靠性修改计划

> **面向执行代理：** 使用 `superpowers:executing-plans` 按任务实施，使用复选框跟踪进度。本文件是修改计划，不代表其中的代码已经实现。

**目标：** 固定评分依据，完整传播异常，为超时建立停止边界，并保证批量任务相互隔离、结果可以恢复。

**架构：** 在现有运行链路上增加任务级评估上下文、取消控制和结果存储。保留当前机器人执行顺序与失败后继续行为；不在本批重写调度器。子进程是阻塞仿真调用的最终隔离边界。

**技术栈：** Python 3.9、标准库 `dataclasses/threading/subprocess/os/signal/json/unittest`、AI2-THOR、pyenv；不新增第三方依赖。

**规格：** 本文件“范围与固定决策”及“公共契约”是自包含规格，依据 2026-09-07 全面评审，代码基线为 `070b808b`。

**依赖：** 无前置批次。完成后交付[第二批](2026-09-07-executor-batch-2-execution-semantics.md)；[第三批](2026-09-07-executor-batch-3-architecture-performance.md)最后实施。

## 范围与固定决策

- 本批解决目标列表被追加、验证缓存跨任务残留、缺失元数据被判成功、包含关系误匹配、异常遗漏、超时后继续提交动作、全机 GPU 清理、超时输出无法序列化、结果只在末尾落盘的问题。
- 保留现有动作名、`TaskPlan` 输入格式、预任务阶段、对象别名与导航恢复。移动默认仍为 `step`；网格 0.25 m、硬间距 0.35 m。
- 历史“动作失败后继续”行为保持，标记 `execution_policy="legacy"`。严格执行策略由第二批增加。
- 修正后的评分默认标记 `evaluation_version="fixed_goals_v2"`，不提供重新启用“执行中追加评分目标”的开关；旧文件原样保留并标记为旧版本。
- 保留 HOT/COLD 的“本任务曾经观察到满足即可”规则，以及切片、破蛋的历史事件证据。其他可逆状态按终态判断；同一任务的历史证据不能用于其他任务。
- 不修改 RU 的现有公式；在结果中保存公式输入。空目标只有经过既有 `noop_subtasks` 验证的无操作任务可以记为完成，普通空目标为无效评估。
- 取消只能阻止新的仿真调用，已经进入 `controller.step` 的调用可能完成。停止成功之前不生成有效终态评估；无法停止时返回不完整结果并依赖外层进程回收。
- `--timeout-seconds` 继续表示计划执行预算：step 默认 120 秒，teleport 默认 30 秒。新增启动余量 60 秒、收尾余量 10 秒；父进程硬上限为三者之和，终止进程组默认最多再使用 5 秒。
- 计划实施及验证从仓库根目录运行，统一使用 `/home/dwb/.pyenv/bin/pyenv exec python`。不覆盖历史报告，不混入无关修改。

## 公共契约

新增类型放在 `scripts/executor_system/evaluation.py`、`execution_control.py`、`run_results.py`，不得从批量 CLI 模块反向导入这些基础类型。

```text
GoalSpec(name: str, states: Tuple[str, ...], contains: Tuple[str, ...])
    frozen dataclass；原始字段规范化为字符串和元组，不保存调用方可变字典。
EvaluationContext.from_goals(goals, *, allow_empty: bool = False) -> EvaluationContext
EvaluationContext.goals -> Tuple[GoalSpec, ...]
EvaluationContext.record_observation(name: str, state: str, object_id: Optional[str]) -> None
EvaluationContext.has_observation(name: str, state: str) -> bool
EvaluationContext.evaluate(runtime) -> Dict[str, Any]

ExecutionControl(deadline: Optional[float] = None)
ExecutionControl.cancel(reason: str) -> None
ExecutionControl.check() -> None
ExecutionControl.wait(timeout: float) -> bool
ExecutionControl.cancelled -> bool
ExecutionControl.reason -> Optional[str]
ExecutionCancelled(RuntimeError)
ExecutionShutdownTimeout(RuntimeError)
PlanExecutionTimeout(TimeoutError)  # 从 parallel_runner 移至 execution_control，原路径重导出

ActionLedger(planned_keys: Sequence[str] = ())
ActionLedger.record_started(key: str) -> None
ActionLedger.record_terminal(key: str, status: str, *, ignored_for_legacy: bool = False) -> None
ActionLedger.record_attempt() -> None
ActionLedger.freeze() -> Dict[str, Any]

normalize_output(value: Union[str, bytes, None]) -> str
atomic_write_json(path: Path, value: Mapping[str, Any]) -> None
validate_result(value: Any, *, returncode: int) -> Dict[str, Any]
RunResultStore(output_dir: Path, run_id: str)
RunResultStore.record_attempt(task_key: str, attempt: int, result: Mapping[str, Any]) -> Path
RunResultStore.rebuild_summary() -> Dict[str, Any]
```

`ExecutionControl` 是任务共享对象，机器人线程显式接收；现有 `action_deadline_scope` 委托到它，不能产生相互独立的取消状态。清理代码拥有单独的收尾预算，不用已取消的任务控制器执行新的业务动作。

结果新增以下字段，后续批次沿用名称：

| 字段 | 定义 |
|---|---|
| `metrics_schema_version` | 固定为整数 `2` |
| `evaluation_version` | `fixed_goals_v2`；缺少版本的旧文件读取为 `legacy_v1` |
| `execution_policy` | 本批固定为 `legacy` |
| `process_status` | `completed / failed / timeout / cancelled`；由父进程确认 |
| `execution_status` | `completed / partial / failed / timeout / cancelled` |
| `evaluation_status` | `valid / incomplete / invalid` |
| `task_success` | 有效评估时等于 `bool(sr)`；其他情况为 `null` |
| `original_goal_count`、`satisfied_goal_count` | 固定目标数、满足目标数；无有效终态时后者为 `null` |
| `ru_inputs` | 计算 RU 时保存实际输入 `{no_trans, no_trans_gt, max_trans}`：分别来自 bundle 的 `no_trans`、任务 `trans`（默认 0）和优先 `min_trans`、否则 `max_trans`（默认 0）；保存传入公式之前的值，不包括公式内部的 `+1`。尚未进入计算阶段可缺省；持久化尝试与重建结果原样保留 |
| `raw_action_sr` | `succeeded / (succeeded + failed)`；分母为零时为 `null` |
| `action_counts` | `planned / started / succeeded / failed / skipped / cancelled / unexecuted / attempts` |
| `ignored_failure_count` | 被历史指标排除的失败数，仍计入新指标的失败数 |
| `worker_errors`、`cleanup_errors` | 含阶段、机器人、异常类型、信息和 traceback 的独立列表 |
| `phase_durations_seconds` | `startup / execution / evaluation / cleanup`；未进入的阶段为零 |
| `run_id`、`task_key`、`attempt` | 父进程分配并验证的运行身份 |

`status`、`success_count`、`action_sr`、`executed_actions`、`failed_actions` 作为兼容字段保留，有效旧统计继续按旧规则计算；新汇总不把它们标注为任务成功率。缺失或无效结果不得补造 `action_sr=1`。`gcr/tc/sr/ru` 在终态不可靠时为 `null`。记录有返回码 0 的动作失败任务为 `process_status=completed, execution_status=partial`；非零退出码不得被子进程中的成功字段覆盖。

动作键使用 `stage_index:robot_id:cursor`，不只依赖用户提供的 `action_id`。生产入口构造账本时传入完整计划键；无预登记键的单元测试可由 record_started 自动登记。`attempts` 指逻辑动作尝试，不包含 helper 内部每个仿真微步；第三批另行统计 controller 调用。重复尝试不增加逻辑动作数，延期导航不产生终态，每个动作至多一个终态。`freeze()` 后拒绝修改，避免超时返回后迟到线程改变报告。

父进程通过子进程环境 `LAMMAP_RUN_ID/LAMMAP_TASK_KEY/LAMMAP_ATTEMPT` 传递身份，不给旧脚本增加必须支持的 CLI 参数。共享 generated runtime 读取并回写，独立运行时自行生成身份；父进程校验 v2 身份。无身份的有效 v1 结果可由父进程补上自己的运行身份，但必须保留 `legacy_v1` 版本，不能伪装成 v2。

## 文件分工

| 文件 | 修改责任 |
|---|---|
| 新建 `evaluation.py`、修改 `goals.py/demo_state.py` | 固定目标、任务内历史证据、兼容函数 |
| 新建 `execution_control.py` | 任务截止时间、取消状态、停止异常 |
| 新建 `run_results.py` | 新结果契约、逻辑动作账本、输出规范化、原子存储 |
| 新建 `process_supervisor.py` | 子进程组启动、预算、终止与回收 |
| 修改 `runtime.py/actions.py/generated_plan_runtime.py` | 接入评估和停止边界，隔离输出，稳健收尾 |
| 修改 `executor.py/action_plan.py/parallel_runner.py` | 异常汇集、取消传播、持续保存、保留兼容入口 |
| 修改 `summarize_run_metrics.py/benchmark_movement_modes.py` | 新旧指标分组、无效评估展示 |

表中无目录的生产文件均位于 `scripts/executor_system/`，最后一行位于 `scripts/`。下面列出的测试文件均位于 `tests/`。

## 任务 1：固定评分目标并隔离历史证据

**文件：** 新建 `evaluation.py`、`tests/test_evaluation_contract.py`；修改 `goals.py`、`demo_state.py`、`actions.py`、`runtime.py`、`generated_plan_runtime.py`。

**输入/输出：** 从 `bundle.gcr` 构造 `EvaluationContext`，保存到 `runtime.evaluation_context`；`runtime.evaluate(goals)` 保留签名，委托同一任务的上下文并核对传入目标一致性。

- [ ] 添加下述测试到 `unittest.TestCase`；新增文件沿用现有测试的 `scripts` 路径引导。导入 `EvaluationContext` 和标准库 `copy`。

```python
def test_observation_does_not_add_a_goal(self):
    goals = [{"name": "Cabinet", "states": ["OPENED"], "contains": []}]
    original = copy.deepcopy(goals)
    context = EvaluationContext.from_goals(goals)
    context.record_observation("Mug", "HOT", "Mug|1")
    self.assertEqual(goals, original)
    self.assertEqual(len(context.goals), 1)
    self.assertEqual(context.goals[0].name, "Cabinet")

def test_observations_are_not_shared_between_tasks(self):
    goals = [{"name": "Mug", "states": ["HOT"], "contains": []}]
    first = EvaluationContext.from_goals(goals)
    first.record_observation("Mug", "HOT", "Mug|1")
    second = EvaluationContext.from_goals(goals)
    self.assertFalse(second.has_observation("Mug", "HOT"))
```

- [ ] 补齐确定场景：柜子未打开、记录杯子变热后 GCR 仍为 0；同一任务 HOT 冷却后保持历史满足；OFF/CLOSED/CLEANED 缺失字段返回未知；Cup 与 EggCup 不匹配；同类型多个实例不能合并出一个满足所有状态的对象；原始目标被调用方后续修改不影响评分。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_evaluation_contract.py`，确认新契约测试失败。
- [ ] 用冻结 `GoalSpec` 保存目标；`record_groundtruth_state` 改为只记录观察，不追加目标。运行入口显式创建上下文；辅助动作从所属 runtime 读取上下文。`set_ground_truth` 的兼容路径复制输入并清空兼容缓存，生产运行不再依赖全局目标缓存。
- [ ] 对状态缺失使用三值判定：存在有效匹配且满足为真，存在完整证据且不满足为假，所需字段缺失为未知。任一目标最终未知则 `evaluation_status=invalid`，总指标为 `null`，保留逐目标解释。
- [ ] 包含关系先解析已绑定实例 ID并精确比较；未绑定类型名称与元数据中的 `objectType` 精确匹配；已登记切片/破蛋别名沿用对象解析器，不使用任意字符串子串。同一目标的全部状态和包含关系必须由同一实例满足。
- [ ] 运行新测试及 `test_runtime_object_aliases.py`、`test_executor_retry_policy.py`、`test_parallel_runner.py`。只有明确检查旧错误评分的断言可以改写，并标注 `fixed_goals_v2`。

**交付：** 目标不变性、同进程顺序任务隔离、历史温度口径都有回归证据；不要求第二批调度器。

## 任务 2：将动作结果和实验指标分开计数

**文件：** 新建 `run_results.py`、`tests/test_run_result_contract.py`；修改 `executor.py`、`parallel_runner.py`、`generated_plan_runtime.py`、`scripts/summarize_run_metrics.py`、`scripts/benchmark_movement_modes.py` 及其现有测试。

**输入/输出：** 消费动作开始、尝试、终态事件；产出本计划定义的 v2 结果。账本与旧 `TolerantRunStats` 暂时并行维护，后者不负责新指标。

- [ ] 写入逻辑动作与尝试分离的失败测试，导入 `ActionLedger`：

```python
def test_retries_do_not_inflate_logical_success(self):
    ledger = ActionLedger()
    ledger.record_started("0:robot1:0")
    for _ in range(3):
        ledger.record_attempt()
    ledger.record_terminal("0:robot1:0", "failed", ignored_for_legacy=True)
    result = ledger.freeze()
    self.assertEqual(result["action_counts"]["failed"], 1)
    self.assertEqual(result["action_counts"]["attempts"], 3)
    self.assertEqual(result["raw_action_sr"], 0.0)
    self.assertEqual(result["ignored_failure_count"], 1)
```

- [ ] 覆盖零动作率为 `null`、延期不重复计数、已冻结账本拒绝写入、进程正常但目标失败、返回码非零但 JSON 写成功、指标文件不存在/损坏/身份不一致。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_run_result_contract.py`，观察 RED。
- [ ] 将终态计数和身份验证集中到 `run_results.py`；父进程优先决定进程状态，子进程报告动作和评估状态。有效空 no-op 的任务分数可以为 1，但其 `raw_action_sr` 仍为 `null`。
- [ ] 汇总按 `(metrics_schema_version, evaluation_version, execution_policy, movement_mode)` 分组。旧输入仍可读取；v2 输出同时报告有效评估数和全部任务数，不把未知值静默当成成功，也不悄悄从总任务完成率分母删除。
- [ ] 保留旧报表列的显式 legacy 模式；新增 v2 模式默认用于 v2 文件。旧文件保留其历史超时记零规则，不能无标识套用到 v2 的有效样本均值。
- [ ] 运行新测试及 `test_parallel_runner.py`、`test_summarize_run_metrics.py`、`test_movement_benchmark.py`，检查 `gcr=1` 与 `sr=0` 仍能分别展示。

**交付：** 新旧口径可以区分，失败排除可追踪，无数据不会补造成满分。

## 任务 3：完整传播线程异常，建立取消与静止状态

**文件：** 新建 `execution_control.py`、`tests/test_execution_shutdown.py`；修改 `executor.py`、`action_plan.py`、`parallel_runner.py`、`runtime.py`、`generated_plan_runtime.py`。

**输入/输出：** 每个任务一个 `ExecutionControl`，机器人和导航作用域共享它；阶段返回前确认所有工作线程结束，或抛出 `ExecutionShutdownTimeout` 并封存不完整结果。

- [ ] 添加控制器边界测试，导入 `ExecutionControl`、`ExecutionCancelled`：

```python
def test_cancellation_is_monotonic(self):
    control = ExecutionControl()
    control.cancel("worker failed")
    control.cancel("later shutdown")
    self.assertTrue(control.cancelled)
    self.assertEqual(control.reason, "worker failed")
    with self.assertRaises(ExecutionCancelled):
        control.check()
```

- [ ] 用 `threading.Event` 构造真实线程测试：注入未捕获 RuntimeError；动作在 controller 内阻塞；取消后试图提交第二动作；关闭 controller 抛异常。每个测试用 `finally` 释放事件并限时 join，禁止留下测试线程。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execution_shutdown.py`，确认失败来自缺失取消或异常传播。
- [ ] 工作线程最外层捕获并上报所有退出异常；`KeyboardInterrupt/SystemExit` 记录后在主线程保留中断语义，不作为普通可重试动作。复用原 traceback，不把基础设施异常转换成跳过动作。
- [ ] 在 `_step_direct` 获取 controller 锁之前和锁内提交之前检查取消；计划等待、动作恢复、重试、导航请求提交和波次唤醒后检查同一控制器。controller 已接收的调用允许收敛，但收敛后不得继续下一动作。
- [ ] 主线程发现超时/工作线程异常后，先取消并通知所有条件变量，再在统一 5 秒停止窗口内 join。存活线程存在时不执行 `Done`、目标评估或可能等待 controller 锁的 `stop()`；封存结果，标记 runtime 不可再用，抛出 `ExecutionShutdownTimeout`。
- [ ] 生成脚本捕获停止失败，先保存不完整结果再退出；外层进程监督器负责最终回收。正常静止后才评估和关闭。关闭异常写入 `cleanup_errors`，不覆盖原错误，也不跳过结果写入。
- [ ] 为普通运行入口也安装有界执行控制，默认执行预算与移动模式相同。保留底层 `timeout_seconds=None` 显式禁用执行截止时间的库调用方式，但异常发生后的停止窗口仍有界。
- [ ] 运行新测试、`test_navigation_execution_scope.py`、`test_navigation_batch_failures.py`、`test_parallel_runner.py`。

**交付：** 不承诺强行中断 Python 线程；承诺取消后不再提交新动作、无法静止时不输出有效终态评分。

## 任务 4：用本任务进程组替换全机 GPU 清理

**文件：** 新建 `process_supervisor.py`、`tests/test_process_supervisor.py`；修改 `parallel_runner.py`、`generated_plan_runtime.py`、`test_parallel_runner.py`。

**接口：** `run_owned_process(command, *, timeout_seconds, termination_grace_seconds, stdout_path, stderr_path, env) -> ProcessOutcome`；`ProcessOutcome` 包含返回码、是否超时、PID、进程组 ID、终止记录、墙钟耗时。

- [ ] 用标准库子进程创建 A、B 两个独立进程组，A 再生成后代；超时回收 A，断言 B 仍可响应，A 的后代已退出。用临时文件输出，避免遗留子进程持有管道使 `communicate()` 永久等待。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_process_supervisor.py`，确认 RED；测试仅清理自身创建的组。
- [ ] POSIX 启动参数固定为 `Popen(..., start_new_session=True)`，组 ID 从创建的进程对象登记。超时时仅向登记组发送 SIGTERM，使用终止窗口的 40%，再 SIGKILL，使用剩余 60%；默认窗口 5 秒即 2 秒加 3 秒。禁止扫描进程名推断归属。
- [ ] 删除执行路径对 `cleanup_gpu_processes` 的调用。旧函数保留一个版本作为发出弃用提示的无操作入口；旧 `gpu_cleanup_events` 字段保留空数组，新增 `process_cleanup_events` 记录归属与结果。
- [ ] 新增 `--startup-grace-seconds`（60）、`--finalization-grace-seconds`（10）、`--termination-grace-seconds`（5），均验证为有限正数。外层总限额等于启动余量加执行预算加收尾余量；内部执行预算保持原值。
- [ ] 父进程自身中断时进入 `finally`，回收所有登记组并保存已完成结果。对无法验证归属的残留只记录错误，不扩大清理范围。
- [ ] 运行子进程测试和现有超时重试测试。保留最多两轮超时重试；确定的输入、协议或动作错误不增加重跑次数。

**交付：** 并行实验互不误杀，仿真后代进程不会仅因 Python 父进程退出而遗留。

## 任务 5：持续、原子地保存结果与诊断输出

**文件：** 修改 `run_results.py`、`parallel_runner.py`、`generated_plan_runtime.py`；新建 `tests/test_run_result_storage.py`。

**输入/输出：** 每次尝试独立落盘，汇总可由已保存尝试重建；不依赖临时目录内的唯一结果副本。

- [ ] 添加字节输出回归测试，导入 `normalize_output`：

```python
def test_timeout_bytes_are_json_serializable(self):
    result = {"stderr": normalize_output(b"timeout\xff")}
    encoded = json.dumps(result, ensure_ascii=False)
    self.assertIsInstance(encoded, str)
    self.assertIn("timeout", result["stderr"])
```

- [ ] 补齐：写入中断仍能读取上次完整 JSON；两个运行器分配不同汇总文件；同名脚本不同路径不会覆盖；第二轮失败保留第一轮证据；主进程中断后可离线重建汇总；磁盘写失败使该运行明确失败。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_run_result_storage.py`，确认 RED。
- [ ] 规范化实现使用 `value.decode("utf-8", errors="replace")` 处理 bytes，`None` 转为空串；JSON 写入使用 `allow_nan=False`，禁止输出非标准 NaN/Infinity。
- [ ] 使用同目录临时文件、flush、文件 fsync、`os.replace` 写入 JSON；一次尝试先落结果，再更新汇总。临时文件失败时删除，原文件保持可读。
- [ ] 运行身份使用 UUID；任务键使用规范化绝对可执行路径的 SHA-256。布局固定为 `output_dir/runs/<run_id>/<task_key>/attempt_<N>/`，包含 `result.json/stdout.log/stderr.log`。完整失败/超时输出始终保存；成功 stdout 默认删除，`--save-all-stdout` 时保留。
- [ ] 原有日期加序号的汇总命名保持；分配时用 `O_CREAT|O_EXCL` 保留路径并立即写入有效的 `in_progress` JSON。运行期间持续更新，最终标记 `completed`。不要仅靠先 glob 再写文件分配编号。
- [ ] 新增 `--rebuild-summary RUN_DIR`：只读已完成的尝试文件，验证身份和版本，以每任务最高已完成尝试重建汇总，不重新执行任务。未完成尝试明确标记 `interrupted`，不覆盖它的日志。
- [ ] 运行存储测试和 `test_parallel_runner.py`，确认既有 CLI 仍只在输出目录顶层产生本次汇总文件。

**交付：** 单个任务超时或主运行器中断不导致整批已完成证据丢失。

## 任务 6：隔离运行输出并完成版本化回归

**文件：** 修改 `runtime.py`、`generated_plan_runtime.py`、`README.md`、`scripts/README.md`；新建 `tests/test_runtime_output_isolation.py`，修改 `test_runtime_metadata.py`、`test_runtime_third_party_views.py`。

**接口：** `ThorRuntime(..., *, output_root: Optional[Path] = None)` 新增关键字参数；未传入时创建独立临时运行目录，生成脚本传入自己运行 ID 对应目录。

- [ ] 创建两个 runtime 输出目录，在一个目录执行初始化清理，断言另一个目录的帧、视频和元数据不变。关闭两次应幂等；关闭异常不阻止结果写出。
- [ ] 运行 `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_runtime_output_isolation.py`，确认 RED。
- [ ] 将帧、视频、元数据输出统一绑定到运行目录；禁止清理模块源码目录或其他运行目录。原输出 API 的返回值继续包含实际绝对路径。
- [ ] 文档列出新字段、新超时预算、历史与 v2 指标差异、离线重建命令和只清理所属进程组的约束。
- [ ] 每个任务完成后单独审查并提交对应代码与测试；本批最后集中验证，保存提交 SHA、命令、通过数与真实环境限制。

## 批次验收与交接

- [ ] 运行原评审的 15 个测试文件，加本批新增测试；不要把“预计通过”写成实际结果。

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_action_plan_pre_task.py tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py tests/test_movement_config.py \
  tests/test_movement_strategies.py tests/test_movement_coordinator.py \
  tests/test_step_movement.py tests/test_navigation_batch_failures.py \
  tests/test_navigation_execution_scope.py tests/test_runtime_object_aliases.py \
  tests/test_runtime_metadata.py tests/test_runtime_third_party_views.py \
  tests/test_pddlrun_executor_adapter.py tests/test_multi_robot_avoidance.py \
  tests/test_movement_benchmark.py tests/test_evaluation_contract.py \
  tests/test_run_result_contract.py tests/test_execution_shutdown.py \
  tests/test_process_supervisor.py tests/test_run_result_storage.py \
  tests/test_runtime_output_isolation.py tests/test_summarize_run_metrics.py
```

- [ ] 使用现有固定 12 样例，分别保存原提交和本批提交的真实运行报告，输出目录区分提交与评估版本；缺失可执行样例或仿真依赖时明确记为尚未完成真实验证。
- [ ] 验收硬条件：固定目标分母；未知不判成功；工作线程异常不遗漏；取消后无新动作提交；未静止不评估；无跨运行清理；每个结束尝试均有可解析结果。
- [ ] 交给第二批：本批提交 SHA、v2 结果实例、取消测试、旧行为回归结果。第二批不得重命名本批公共字段或重新引入全局目标缓存。

**回退：** 分任务提交便于定位回退；需要复现实验旧指标时使用旧提交和独立输出目录。回退代码不删除新结果、不改写旧结果，也不重新启用全机 GPU 清理作为修复措施。
