# 可插拔机器人移动与单步避障实施计划

> **面向执行代理：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项实施本计划；使用复选框（`- [ ]`）跟踪步骤。

**目标：** 保持现有 `GoToObject` 传送行为兼容，同时增加可选择、真实逐格执行、支持多机器人联合避障与动态恢复的单步移动插件。

**架构：** `ThorRuntime` 只负责目标解析和 THOR 原语，并把统一 `NavigationRequest` 交给移动策略。传送策略封装当前逻辑；单步策略通过共享协调器把同一动作波次的请求交给 `multi_robot_avoidance.py`，串行提交真实 `Rotate`/`MoveAhead` 微步并根据权威状态重规划。共享 generated runtime 和 parallel runner 选择模式并输出双模式指标，因此已有生成脚本无需重写。

**技术栈：** Python 3.9、标准库 `dataclasses`/`enum`/`threading`/`unittest`、AI2-THOR、现有 `executor_system`、现有确定性时空 A* 原型。

**规格：** `docs/superpowers/specs/2026-08-24-pluggable-robot-movement-design.md`

## 全局约束

- `GoToObject` 动作名称、参数和生成任务计划结构不变；不批量修改已有 `executable_plan.py`。
- 移动模式只允许 `teleport` 和 `step`；默认 `teleport`，`step` 失败时不得隐式传送。
- 配置优先级为显式构造参数、命令行、`LAMMAP_MOVEMENT_MODE`、默认值。
- 网格尺寸固定 0.25 m，应用层硬间距固定 0.35 m。
- 权威位置映射采用最近网格点，容差为半格加浮点余量，即 0.125001 m；超出容差立即安全失败。
- 单步默认重规划预算和失败有向边上限均为 8，候选试验上限为 256，`max_ticks=max(32, 2 * len(walkable))`。
- 传送 runner 默认超时 30 秒，单步 runner 默认超时 120 秒；显式 `--timeout-seconds` 优先。
- 单步导航初始化完成后，`GoToObject` 路径不得发送 `Teleport`；初始化传送和显式计划传送单独计数。
- 每个行为变化严格执行 RED→GREEN→REFACTOR；生产实现前必须先看到对应测试因缺失行为而失败。
- Python 验证统一使用 `/home/dwb/.pyenv/bin/pyenv exec python`。
- 只暂存当前任务列出的文件；保留工作树中任何无关用户修改。

## 文件结构

- 新建 `scripts/executor_system/movement.py`：公共模式、配置、请求/结果、指标、动作波次、异常和策略协议。
- 新建 `scripts/executor_system/teleport_movement.py`：现有传送导航策略。
- 新建 `scripts/executor_system/step_movement.py`：单步策略与 THOR/网格转换适配。
- 新建 `scripts/executor_system/movement_coordinator.py`：联合批次、地图、规划、微步提交、恢复和停车。
- 修改 `scripts/executor_system/runtime.py`：构造策略、生成公共请求、委托导航、导航动作计数。
- 修改 `scripts/executor_system/executor.py`：单步动作波次和请求汇合。
- 修改 `scripts/executor_system/action_plan.py`：透传可选动作波次，不改变动作路由。
- 修改 `scripts/multi_robot_avoidance.py`：支持 1–4 个机器人和有向失败边。
- 修改 `scripts/executor_system/generated_plan_runtime.py`：生成脚本移动模式、模式默认超时和指标。
- 修改 `scripts/executor_system/parallel_runner.py`：向子进程传递模式并汇总指标。
- 新建 `scripts/benchmark_movement_modes.py`：固定样本选择、双模式运行、报告和阈值检查。
- 新建 `tests/movement_fakes.py`：可编程网格 THOR fake，仅供测试复用。
- 新建 `tests/test_movement_config.py`、`tests/test_movement_strategies.py`、`tests/test_step_movement.py`、`tests/test_movement_coordinator.py`、`tests/test_movement_benchmark.py`。
- 修改 `tests/test_multi_robot_avoidance.py`、`tests/test_parallel_runner.py`、`tests/test_executor_retry_policy.py`。
- 新建 `tests/fixtures/movement_benchmark_plans.json`：从现有 runner-compatible 生成脚本确定性选出的 12 个固定路径。
- 生成 `reports/movement_modes_benchmark.json` 和 `reports/movement_modes_benchmark.md`：最终真实双模式证据。

---

### 任务 1：公共移动契约、模式解析和线程安全指标

**文件：**
- 新建：`scripts/executor_system/movement.py`
- 新建：`tests/test_movement_config.py`

**接口：**
- 输入：显式模式字符串和可选环境映射。
- 输出：`MovementConfig.resolve(explicit_mode: Optional[str], environ: Optional[Mapping[str, str]]) -> MovementConfig`。
- 输出：`NavigationRequest`、`NavigationResult`、`ActionWave`、`NavigationMetrics`、`MovementStrategy`。
- 输出异常：`MovementConfigurationError`、`StepNavigationError`、`NavigationBatchAborted`。

- [ ] **步骤 1：编写模式、优先级和指标快照失败测试**

创建测试文件并加入以下用例；测试直接使用真实 dataclass，不 mock 配置逻辑：

```python
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from executor_system.movement import (
    MovementConfig,
    MovementConfigurationError,
    MovementMode,
    NavigationMetrics,
)


class MovementConfigTest(unittest.TestCase):
    def test_default_mode_is_teleport(self):
        config = MovementConfig.resolve(environ={})
        self.assertIs(config.mode, MovementMode.TELEPORT)
        self.assertEqual(config.max_replans, 8)
        self.assertEqual(config.max_failed_transitions, 8)
        self.assertEqual(config.max_assignment_trials, 256)

    def test_explicit_mode_overrides_environment(self):
        config = MovementConfig.resolve(
            explicit_mode="teleport",
            environ={"LAMMAP_MOVEMENT_MODE": "step"},
        )
        self.assertIs(config.mode, MovementMode.TELEPORT)

    def test_invalid_mode_lists_allowed_values(self):
        with self.assertRaisesRegex(
            MovementConfigurationError,
            "teleport.*step",
        ):
            MovementConfig.resolve(explicit_mode="warp", environ={})

    def test_navigation_metrics_snapshot_is_detached(self):
        metrics = NavigationMetrics(MovementMode.STEP)
        metrics.record_request_started()
        metrics.record_action("MoveAhead")
        metrics.increment("micro_steps")
        metrics.add_planning_time(0.125)
        snapshot = metrics.to_dict()
        snapshot["action_counts"]["MoveAhead"] = 99
        snapshot["planning_durations_seconds"].append(99.0)
        self.assertEqual(metrics.to_dict()["action_counts"]["MoveAhead"], 1)
        self.assertEqual(metrics.to_dict()["micro_steps"], 1)
        self.assertEqual(metrics.to_dict()["planning_durations_seconds"], [0.125])
```

- [ ] **步骤 2：运行测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_movement_config.py
```

预期：导入以 `ModuleNotFoundError: executor_system.movement` 失败。

- [ ] **步骤 3：实现精确公共类型**

在 `movement.py` 中实现以下签名；映射和位置在构造请求时复制，避免策略间共享可变输入：

```python
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

from .action_plan import PlannedAction
from .utils import RobotRef


class MovementConfigurationError(RuntimeError):
    pass


class StepNavigationError(RuntimeError):
    pass


class NavigationBatchAborted(StepNavigationError):
    pass


class MovementMode(str, Enum):
    TELEPORT = "teleport"
    STEP = "step"


@dataclass(frozen=True)
class MovementConfig:
    mode: MovementMode
    grid_size_m: float = 0.25
    hard_clearance_m: float = 0.35
    grid_snap_tolerance_m: float = 0.125001
    max_replans: int = 8
    max_failed_transitions: int = 8
    max_assignment_trials: int = 256

    @classmethod
    def resolve(
        cls,
        explicit_mode: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "MovementConfig":
        source = os.environ if environ is None else environ
        raw = (
            source.get("LAMMAP_MOVEMENT_MODE", "teleport")
            if explicit_mode is None
            else explicit_mode
        )
        try:
            mode = MovementMode(str(raw).strip().lower())
        except ValueError as exc:
            raise MovementConfigurationError(
                "movement mode must be one of: step, teleport"
            ) from exc
        return cls(mode=mode)


@dataclass(frozen=True)
class ActionWave:
    wave_id: int
    navigation_agent_ids: Tuple[int, ...]


@dataclass(frozen=True)
class NavigationRequest:
    robot: RobotRef
    agent_id: int
    dest_obj: Any
    destination: Dict[str, Any]
    center: Dict[str, float]
    candidate_positions: Tuple[Dict[str, float], ...]
    object_resource: Optional[str]
    next_action: Optional[PlannedAction]
    phase_coordinator: Optional[Any]
    action_wave: Optional[ActionWave] = None


@dataclass(frozen=True)
class NavigationResult:
    destination: Dict[str, Any]
    position: Dict[str, float]
    decision_trace: Tuple[Dict[str, Any], ...] = ()


@dataclass
class NavigationMetrics:
    mode: MovementMode
    requests: int = 0
    successes: int = 0
    failures: int = 0
    planning_batches: int = 0
    candidate_trials: int = 0
    priority_trials: int = 0
    replans: int = 0
    micro_steps: int = 0
    waits: int = 0
    parking_moves: int = 0
    failed_transitions: int = 0
    position_deviations: int = 0
    invisible_candidates: int = 0
    budget_exhaustions: int = 0
    planning_time_seconds: float = 0.0
    planning_durations_seconds: List[float] = field(default_factory=list)
    action_counts: Dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request_started(self) -> None:
        self.increment("requests")

    def record_request_succeeded(self) -> None:
        self.increment("successes")

    def record_request_failed(self) -> None:
        self.increment("failures")

    def record_action(self, action: str) -> None:
        with self._lock:
            self.action_counts[action] = self.action_counts.get(action, 0) + 1

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            value = getattr(self, field_name, None)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AttributeError(f"{field_name!r} is not an integer metric")
            setattr(self, field_name, value + int(amount))

    def add_planning_time(self, seconds: float) -> None:
        with self._lock:
            duration = float(seconds)
            self.planning_time_seconds += duration
            self.planning_durations_seconds.append(duration)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode.value,
                "requests": self.requests,
                "successes": self.successes,
                "failures": self.failures,
                "planning_batches": self.planning_batches,
                "candidate_trials": self.candidate_trials,
                "priority_trials": self.priority_trials,
                "replans": self.replans,
                "micro_steps": self.micro_steps,
                "waits": self.waits,
                "parking_moves": self.parking_moves,
                "failed_transitions": self.failed_transitions,
                "position_deviations": self.position_deviations,
                "invisible_candidates": self.invisible_candidates,
                "budget_exhaustions": self.budget_exhaustions,
                "planning_time_seconds": self.planning_time_seconds,
                "planning_durations_seconds": list(
                    self.planning_durations_seconds
                ),
                "action_counts": dict(self.action_counts),
            }


class MovementStrategy(Protocol):
    def navigate(self, request: NavigationRequest) -> NavigationResult:
        raise NotImplementedError
```

用锁包围每次指标读写。`increment()` 只允许已有整数计数字段，否则抛 `AttributeError`；`to_dict()` 返回普通字典和复制后的 `action_counts`。

- [ ] **步骤 4：运行测试并确认 GREEN**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_movement_config.py
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/executor_system/movement.py tests/test_movement_config.py
```

预期：全部通过，无 warning。

- [ ] **步骤 5：提交任务 1**

```bash
git add scripts/executor_system/movement.py tests/test_movement_config.py
git commit -m "feat: define robot movement strategy contract"
```

---

### 任务 2：提取传送策略并保持默认行为等价

**文件：**
- 新建：`scripts/executor_system/teleport_movement.py`
- 新建：`tests/test_movement_strategies.py`
- 修改：`scripts/executor_system/movement.py`
- 修改：`scripts/executor_system/runtime.py:134-197, 2747-2811`
- 修改：`tests/test_parallel_runner.py:184-310`
- 验证：`tests/test_executor_retry_policy.py:1202-1283`

**接口：**
- 输入：`NavigationRequest`。
- 输出：`TeleportMovementStrategy.navigate(request) -> NavigationResult`。
- 输出：`create_movement_strategy(runtime: Any, config: MovementConfig, metrics: NavigationMetrics) -> MovementStrategy`。
- 输出：`ThorRuntime.build_navigation_request(robot, dest_obj, *, next_action=None, phase_coordinator=None, action_wave=None) -> NavigationRequest`。
- 保持：`ThorRuntime.navigate_to_object(robot, dest_obj, *, allow_hand_preparation=True, next_action=None, phase_coordinator=None, action_wave=None) -> Dict[str, Any]`。

- [ ] **步骤 1：编写默认策略和传送顺序失败测试**

在新测试文件中复用 `object.__new__(ThorRuntime)` 构造不启动 Unity 的运行时，设置现有辅助方法，并断言：

```python
def test_runtime_defaults_to_teleport_strategy(self):
    runtime = runtime_without_controller()
    runtime.configure_movement(None, environ={})
    self.assertEqual(runtime.movement_config.mode.value, "teleport")
    self.assertIsInstance(runtime.movement_strategy, TeleportMovementStrategy)

def test_teleport_strategy_preserves_wait_recompute_move_notify_order(self):
    runtime, request, order = teleport_runtime_and_request(waited=True)
    result = runtime.movement_strategy.navigate(request)
    self.assertEqual(
        order,
        ["wait", "rebuild", "teleport_and_face", "notify"],
    )
    self.assertEqual(result.destination["objectId"], "Apple|second")
    self.assertEqual(result.position, {"x": 1.25, "y": 0.0, "z": 0.0})
```

测试 helper 必须使用真实 `NavigationRequest`，并让 `runtime.build_navigation_request()` 在重建时返回第二个对象/候选，而不是仅断言 mock 调用次数。

- [ ] **步骤 2：运行新测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_movement_strategies.py
```

预期：缺少 `TeleportMovementStrategy`、`configure_movement` 和工厂而失败。

- [ ] **步骤 3：实现传送策略和延迟导入工厂**

`teleport_movement.py` 的核心行为：

```python
class TeleportMovementStrategy:
    def __init__(self, runtime, metrics: NavigationMetrics) -> None:
        self.runtime = runtime
        self.metrics = metrics

    def navigate(self, request: NavigationRequest) -> NavigationResult:
        active = request
        coordinator = active.phase_coordinator
        if coordinator is not None:
            waited = coordinator.wait_until_goto_candidates_clear(
                active.agent_id,
                active.candidate_positions[:1],
            )
            if waited:
                active = self.runtime.build_navigation_request(
                    active.robot,
                    active.dest_obj,
                    next_action=active.next_action,
                    phase_coordinator=coordinator,
                    action_wave=active.action_wave,
                )
        selected = self.runtime.teleport_and_face_candidate_positions(
            active.agent_id,
            active.candidate_positions,
            face_target=active.center,
            object_resource=active.object_resource,
            search_center=active.center,
            restrict_to_candidate_positions=True,
        )
        if coordinator is not None:
            coordinator.notify_agent_position_changed(active.agent_id)
        return NavigationResult(
            destination=dict(active.destination),
            position=dict(selected),
        )
```

在 `movement.py` 增加延迟导入工厂，避免 `runtime` 循环导入：

```python
def create_movement_strategy(runtime, config, metrics):
    if config.mode is MovementMode.TELEPORT:
        from .teleport_movement import TeleportMovementStrategy
        return TeleportMovementStrategy(runtime, metrics)
    from .step_movement import StepMovementStrategy
    return StepMovementStrategy(runtime, config, metrics)
```

`StepMovementStrategy` 尚未创建时，只有显式选择 `step` 才允许导入失败；默认传送导入必须工作。

- [ ] **步骤 4：把运行时导航改为构造请求和委托**

给 `ThorRuntime.__init__` 增加 `movement_mode: Optional[str] = None`，并在创建 controller 前调用：

```python
def configure_movement(self, movement_mode=None, *, environ=None):
    self.movement_config = MovementConfig.resolve(movement_mode, environ)
    self.navigation_metrics = NavigationMetrics(self.movement_config.mode)
    self.movement_strategy = create_movement_strategy(
        self, self.movement_config, self.navigation_metrics
    )
```

提取 `build_navigation_request()`：刷新可达点、查找对象、验证中心、生成前 10 个候选并复制所有映射。`navigate_to_object()` 只保留持物准备、请求构造、指标 started/succeeded/failed、策略调用、目标记录和返回 `result.destination`；异常路径记录 failure 后原样抛出。

- [ ] **步骤 5：迁移两个现有导航测试并确认 GREEN**

调整 `PhaseCoordinatorTest` 中手工构造的 runtime，使其调用 `configure_movement("teleport", environ={})`，保留以下原断言：

- 等待候选后重新计算对象和候选。
- 顺序仍为 wait、teleport、notify、record。
- 返回第二次解析的目标对象。

运行：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_movement_strategies.py \
  tests.test_parallel_runner.PhaseCoordinatorTest \
  tests.test_executor_retry_policy.ExecutorRetryPolicyTest.test_teleport_and_face_retries_next_candidate_after_rotation_failure \
  tests.test_executor_retry_policy.ExecutorRetryPolicyTest.test_teleport_candidates_backfill_from_global_reachable_positions
```

预期：全部通过，传送候选重试轨迹不变。

- [ ] **步骤 6：提交任务 2**

```bash
git add \
  scripts/executor_system/movement.py \
  scripts/executor_system/teleport_movement.py \
  scripts/executor_system/runtime.py \
  tests/test_movement_strategies.py \
  tests/test_parallel_runner.py
git commit -m "refactor: extract teleport movement strategy"
```

---

### 任务 3：扩展纯规划器支持单机器人和失败有向边

**文件：**
- 修改：`scripts/multi_robot_avoidance.py:235-244, 996-1054, 1582-1718`
- 修改：`tests/test_multi_robot_avoidance.py`
- 修改：`scripts/README.md:268-417`

**接口：**
- 输入：`Scenario.blocked_transitions: FrozenSet[Tuple[GridPoint, GridPoint]]`。
- 输入 JSON：可选 `blocked_transitions: [[[x1,z1],[x2,z2]], [[x3,z3],[x4,z4]]]`，每一项为一条有向边。
- 保持输出：`plan_scenario(scenario, world_state) -> PlanningResult`。

- [ ] **步骤 1：编写单机器人和有向边失败测试**

在 `ScenarioModelTest` 与 `SpaceTimePlanningTest` 增加：

```python
def test_load_scenario_accepts_one_robot(self):
    scenario = load_scenario({
        "walkable": [[0, 0], [1, 0]],
        "robots": [{
            "id": "A",
            "start": [0, 0],
            "candidates": [{"id": "A1", "position": [1, 0], "cost": 0}],
        }],
    })
    self.assertEqual([robot.robot_id for robot in scenario.robots], ["A"])
    self.assertEqual(plan_scenario(scenario, WorldState.from_scenario(scenario)).status, "PLANNED")

def test_directed_blocked_transition_forces_detour(self):
    scenario = load_scenario({
        "walkable": [[0, 0], [1, 0], [2, 0], [0, 1], [1, 1], [2, 1]],
        "blocked_transitions": [[[0, 0], [1, 0]]],
        "robots": [{
            "id": "A",
            "start": [0, 0],
            "candidates": [{"id": "A1", "position": [2, 0], "cost": 0}],
        }],
    })
    result = plan_scenario(scenario, WorldState.from_scenario(scenario))
    self.assertEqual(result.status, "PLANNED")
    self.assertEqual(
        result.plan.paths["A"],
        (GridPoint(0, 0), GridPoint(0, 1), GridPoint(1, 1), GridPoint(2, 1), GridPoint(2, 0)),
    )
```

另加反向仍可用测试，证明只禁止 `(source, target)` 而非无向边。

- [ ] **步骤 2：运行测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_multi_robot_avoidance.ScenarioModelTest.test_load_scenario_accepts_one_robot \
  tests.test_multi_robot_avoidance.SpaceTimePlanningTest.test_directed_blocked_transition_forces_detour
```

预期：单机器人被 2–4 限制拒绝，blocked transition 未生效。

- [ ] **步骤 3：实现模型、解析和搜索过滤**

给 `Scenario` 增加默认字段：

```python
blocked_transitions: FrozenSet[Tuple[GridPoint, GridPoint]] = frozenset()
```

在 `load_scenario()` 中验证每个有向边包含两个不同的 walkable point，保留输入顺序，不调用 `sorted()`。机器人数量检查改为 `1 <= len(raw_robots) <= 4`。

在 `_space_time_a_star()` 的 neighbor 循环、预约检查前增加：

```python
if (point, neighbor) in scenario.blocked_transitions:
    continue
```

等待边 `(point, point)` 不允许出现在输入，因此不会意外禁止等待。

- [ ] **步骤 4：运行完整原型测试和 CLI 演示**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_multi_robot_avoidance.py
/home/dwb/.pyenv/bin/pyenv exec python scripts/multi_robot_avoidance.py --demo crossing --plan-only
```

预期：原有 34 个测试加新增测试全部通过；CLI 返回 0 和 `PLANNED`。

- [ ] **步骤 5：更新 README 输入契约并提交**

在原型最小 JSON 和限制列表中写明 1–4 个机器人及 `blocked_transitions` 的有向、批次级语义，然后：

```bash
git add scripts/multi_robot_avoidance.py tests/test_multi_robot_avoidance.py scripts/README.md
git commit -m "feat: support failed edges in avoidance planner"
```

---

### 任务 4：实现单请求单步策略的真实微步 happy path

**文件：**
- 新建：`scripts/executor_system/step_movement.py`
- 新建：`scripts/executor_system/movement_coordinator.py`
- 新建：`tests/movement_fakes.py`
- 新建：`tests/test_step_movement.py`
- 修改：`scripts/executor_system/runtime.py`

**接口：**
- 输出：`StepMovementStrategy.navigate(request) -> NavigationResult`。
- 输出：`StepMovementCoordinator.execute_batch(requests: Sequence[NavigationRequest], completed_agent_ids: FrozenSet[int] = frozenset()) -> Dict[int, NavigationResult]`。
- 内部：`StepMovementCoordinator.refresh_world() -> RuntimeWorldSnapshot`。
- 内部类型：

```python
@dataclass(frozen=True)
class RuntimeWorldSnapshot:
    version: int
    positions: Dict[int, GridPoint]
    thor_positions: Dict[GridPoint, Dict[str, float]]
    walkable_map: GlobalWalkableMap
```

`positions` 是每个物理 agent 的权威网格位置；`thor_positions` 保存规划网格到真实 THOR 坐标的稳定副本，所有微步只能从该映射取目标。

- [ ] **步骤 1：创建可编程 GridThorRuntime 测试工具**

`tests/movement_fakes.py` 提供真实状态变化而非调用计数 mock：

```python
class GridThorRuntime:
    def __init__(self, positions, walkable_by_agent, objects):
        self.physical_agent_count = len(positions)
        self.positions = {int(key): dict(value) for key, value in positions.items()}
        self.walkable_by_agent = {
            int(key): [dict(item) for item in value]
            for key, value in walkable_by_agent.items()
        }
        self.objects = {item["objectId"]: dict(item) for item in objects}
        self.actions = []
        self.failed_edges = set()

    def current_agent_position(self, agent_id):
        return dict(self.positions[agent_id])

    def refresh_reachable_positions(self, agent_id):
        return [dict(item) for item in self.walkable_by_agent[agent_id]]

    def move_to_adjacent_position_direct(self, agent_id, target):
        source_key = position_to_grid_key(self.positions[agent_id])
        target_key = position_to_grid_key(target)
        self.actions.append(("MoveAhead", agent_id, source_key, target_key))
        if (source_key, target_key) in self.failed_edges:
            return False
        self.positions[agent_id] = dict(target)
        return True

    def face_position_direct(self, agent_id, target):
        self.actions.append(("Face", agent_id, dict(target)))

    def find_object(self, object_id, *, agent_id=None, require_center=False):
        return dict(self.objects[object_id])
```

同时实现 `agent_position_items()`、`scene_object_bounds()` 返回空列表和测试所需 `physical_agent_id()`。

- [ ] **步骤 2：编写单机器人逐格到达测试**

使用 0.25 m 直线路径，从 `[0,0]` 到 `[2,0]`：

```python
def test_step_strategy_moves_each_grid_edge_and_never_teleports(self):
    runtime, request = make_single_navigation_request(
        start=(0, 0), candidate=(2, 0), object_id="Apple|1"
    )
    config = MovementConfig.resolve("step", environ={})
    metrics = NavigationMetrics(config.mode)
    result = StepMovementStrategy(runtime, config, metrics).navigate(request)

    self.assertEqual(position_to_grid_key(result.position), (2, 0))
    self.assertEqual(
        [action[0] for action in runtime.actions],
        ["MoveAhead", "MoveAhead", "Face"],
    )
    self.assertNotIn("Teleport", [action[0] for action in runtime.actions])
    self.assertEqual(metrics.to_dict()["micro_steps"], 2)
```

- [ ] **步骤 3：运行测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_step_movement.StepMovementHappyPathTest.test_step_strategy_moves_each_grid_edge_and_never_teleports
```

预期：单步策略和协调器模块缺失。

- [ ] **步骤 4：实现坐标适配、场景构造和串行提交**

`step_movement.py` 只做策略委托：

```python
class StepMovementStrategy:
    def __init__(self, runtime, config, metrics):
        self.coordinator = StepMovementCoordinator(runtime, config, metrics)

    def navigate(self, request):
        results = self.coordinator.execute_batch((request,))
        return results[request.agent_id]
```

`movement_coordinator.py` 按接口定义不可变 `RuntimeWorldSnapshot`。`refresh_world()` 对每个 agent 调用 `refresh_reachable_positions()`，通过 `GlobalWalkableMap.replace_all()` 原子构造并集；同一网格点出现多个浮点坐标时，用 `(x, z, y)` 排序后的第一项填入 `thor_positions`，确保重跑确定性。

`_build_scenario()` 为请求 agent 构造多个候选，为其他 agent 构造 `start == candidate` 的静态意图；候选 cost 使用 `candidate_index + euclidean_distance`。调用 `plan_scenario()` 后按 `micro_steps` 顺序执行 `move_to_adjacent_position_direct()`，每步核对真实 grid key、刷新 moved agent snapshot 并递增 metrics。最后调用 `face_position_direct()`，重新读取对象并返回真实位置。

- [ ] **步骤 5：接通显式 step 工厂并运行 GREEN**

确认任务 2 的 `create_movement_strategy()` 能导入 `StepMovementStrategy`，然后：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_step_movement.py tests/test_movement_strategies.py
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/executor_system/step_movement.py \
  scripts/executor_system/movement_coordinator.py \
  tests/movement_fakes.py tests/test_step_movement.py
```

预期：全部通过；测试动作记录没有 `Teleport`。

- [ ] **步骤 6：提交任务 4**

```bash
git add \
  scripts/executor_system/step_movement.py \
  scripts/executor_system/movement_coordinator.py \
  scripts/executor_system/runtime.py \
  tests/movement_fakes.py tests/test_step_movement.py
git commit -m "feat: execute GoToObject with grid steps"
```

---

### 任务 5：动态地图、失败边、位置偏差与不可见候选恢复

**文件：**
- 修改：`scripts/executor_system/movement_coordinator.py`
- 修改：`scripts/executor_system/step_movement.py`
- 修改：`tests/movement_fakes.py`
- 修改：`tests/test_step_movement.py`

**接口：**
- 内部输出：`StepMovementCoordinator._plan_and_execute(active_states: Dict[int, ActiveNavigationState]) -> Dict[int, NavigationResult]`。
- 内部状态：请求级排除候选、本批次 `blocked_transitions`、`replan_count` 和未执行预约。
- 异常：预算耗尽时 `StepNavigationError` 包含 agent、目标、重规划次数和最后规划状态。

```python
@dataclass
class ActiveNavigationState:
    request: NavigationRequest
    excluded_candidate_keys: Set[GridPoint] = field(default_factory=set)
    decision_trace: List[Dict[str, Any]] = field(default_factory=list)
```

该状态只在一次 `execute_batch()` 内存活；重规划沿用同一实例，因此不可见候选不会再次被选择，批次结束或异常时整体丢弃。

- [ ] **步骤 1：编写失败边绕行 RED 测试**

构造 2×3 网格，将直线首边 `(0,0)->(1,0)` 设置为 fake runtime 失败边。断言最终走上方绕路、失败边只尝试一次、`replans == 1`：

```python
self.assertEqual(position_to_grid_key(result.position), (2, 0))
self.assertEqual(runtime.edge_attempts[((0, 0), (1, 0))], 1)
self.assertIn(((0, 0), (0, 1)), runtime.successful_edges)
self.assertEqual(metrics.to_dict()["replans"], 1)
```

- [ ] **步骤 2：编写动态地图和位置偏差 RED 测试**

- 在第一次成功移动后从 moved agent snapshot 删除原剩余路径点，断言从真实位置绕行。
- 让第一次 MoveAhead 实际落在另一个合法 reachable grid，断言预期点未提交，随后从观察点重规划。
- 让实际位置无法映射到任何 reachable point，断言 `StepNavigationError` 且没有后续动作。

- [ ] **步骤 3：编写不可见候选和预算 RED 测试**

目标提供两个候选。fake 在第一个终点返回 `visible=False`，第二个返回 `visible=True`，断言第一个被排除且第二个完成。另设所有边失败与 `max_replans=1`，断言异常包含 `replan budget 1 exhausted`，预约表 `released_from_tick` 已设置。

- [ ] **步骤 4：运行聚焦测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_step_movement.StepMovementRecoveryTest
```

预期：当前 happy path 在首个失败、地图变化或不可见终点处终止，无法满足恢复断言。

- [ ] **步骤 5：实现统一重规划循环**

使用一个循环处理所有恢复来源：

```python
while unfinished_requests:
    planning = self._plan(active_states, blocked_transitions)
    if planning.status != "PLANNED":
        planning = self._refresh_and_retry_once(active_states, blocked_transitions)
    if planning.status != "PLANNED":
        raise self._planning_error(planning, active_states)
    outcome = self._execute_until_boundary(planning.plan, active_states)
    if outcome.kind == "complete":
        return self._finish_results(active_states)
    replans += 1
    self.metrics.increment("replans")
    if replans > self.config.max_replans:
        raise self._budget_error(active_states, replans)
    blocked_transitions.update(outcome.failed_transitions)
    if len(blocked_transitions) > self.config.max_failed_transitions:
        raise self._failed_transition_budget_error(
            active_states,
            blocked_transitions,
        )
    self._release_future_reservations(planning.plan, outcome.release_tick)
    snapshot = self.refresh_world()
```

`_execute_until_boundary()` 只在 THOR 真实 grid key 等于目标时提交。MoveAhead/Rotate 失败都添加有向边；动态地图移除路径或终点返回 replan outcome；不可见终点加入对应请求的候选排除集合。所有异常路径通过 `finally` 释放未来预约。

- [ ] **步骤 6：运行恢复测试和原型回归并确认 GREEN**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_step_movement.py tests/test_multi_robot_avoidance.py
```

预期：全部通过，恢复测试的动作序列中仍无 `Teleport`。

- [ ] **步骤 7：提交任务 5**

```bash
git add \
  scripts/executor_system/movement_coordinator.py \
  scripts/executor_system/step_movement.py \
  tests/movement_fakes.py tests/test_step_movement.py
git commit -m "feat: replan failed robot movement steps"
```

---

### 任务 6：确定性动作波次和联合多机器人请求

**文件：**
- 修改：`scripts/executor_system/movement.py`
- 修改：`scripts/executor_system/executor.py:20-315`
- 修改：`scripts/executor_system/action_plan.py:906-970, 1056-1135`
- 修改：`scripts/executor_system/runtime.py:2747-2811`
- 修改：`scripts/executor_system/step_movement.py`
- 修改：`scripts/executor_system/movement_coordinator.py`
- 新建：`tests/test_movement_coordinator.py`
- 修改：`tests/test_parallel_runner.py`

**接口：**
- 输出：`PhaseCoordinator.before_action(agent_id: int, action_type: str, action_cursor: int) -> Optional[ActionWave]`。
- 输出：`PhaseCoordinator.submit_step_navigation(wave: ActionWave, request: NavigationRequest, execute_batch: Callable[[Sequence[NavigationRequest], FrozenSet[int]], Dict[int, NavigationResult]]) -> NavigationResult`。
- `AI2ThorAdapter.execute(robot_id, action, *, next_action=None, world_state=None, phase_coordinator=None, action_wave=None)` 透传到 `navigate_to_object()`。

- [ ] **步骤 1：编写波次与线程顺序无关 RED 测试**

使用两个线程，故意让 agent 1 先报告、agent 0 后报告。两者当前动作都是 `GoToObject`，断言：

```python
self.assertEqual(wave0, wave1)
self.assertEqual(wave0.navigation_agent_ids, (0, 1))
self.assertEqual(executed_batches, [((0, 1), frozenset())])
self.assertEqual(results[0].position, positions[0])
self.assertEqual(results[1].position, positions[1])
```

`execute_batch` 必须只调用一次，输入按 agent id 排序。另加三 agent 用例：agent 2 已先调用 `mark_agent_done()`，随后 agent 0/1 形成导航波次，断言 batch executor 收到 `completed_agent_ids=frozenset({2})`。

- [ ] **步骤 2：编写非导航屏障、完成和失败 RED 测试**

- agent 0 报告 `GoToObject`，agent 1 报告 `OpenObject`；断言 agent 1 在导航批次完成前不能离开 `before_action()`。
- agent 1 在等待时 `mark_agent_done()`；断言屏障缩小参与集合且不死锁。
- batch executor 对 agent 0 抛根因；断言 agent 0 获根因，其他导航请求获 `NavigationBatchAborted`，非导航线程被唤醒。
- deadline 已过时断言沿用 `PlanExecutionTimeout`。

- [ ] **步骤 3：运行协调器测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_movement_coordinator.py
```

预期：`PhaseCoordinator` 没有动作波次 API。

- [ ] **步骤 4：实现两阶段波次状态机**

在 `executor.py` 增加私有 `_ActionWaveState`，由同一 `Condition` 保护：

```python
@dataclass
class _ActionWaveState:
    wave_id: int
    announced: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    navigation_agent_ids: Tuple[int, ...] = ()
    requests: Dict[int, NavigationRequest] = field(default_factory=dict)
    results: Dict[int, NavigationResult] = field(default_factory=dict)
    root_exception: Optional[BaseException] = None
    navigation_complete: bool = False
```

`before_action()` 仅在 `runtime.movement_config.mode is MovementMode.STEP` 时参与屏障；传送返回 `None`。当全部未完成 active agent 已报告时，以排序后的 `GoToObject` agent 构造 `ActionWave`。非导航 agent 等待 `navigation_complete`。

`submit_step_navigation()` 收集本波次导航请求；最后一个到达者之外，只有 `min(navigation_agent_ids)` 被允许执行排序 batch。执行者在锁内复制此刻已经 `mark_agent_done()` 的物理 agent id，然后在锁外调用 `execute_batch(sorted_requests, completed_agent_ids)`。执行结束后原子写入每 agent 结果或异常、标记完成并 `notify_all()`。所有 deadline、done 和 failed 变化都重新计算等待条件；禁止持有 `Condition` 锁执行 THOR 动作。

- [ ] **步骤 5：显式透传 ActionWave**

`Executor._execute_queue()` 在 `execute_action()` 前调用 `before_action()`；`execute_action(action, action_wave)` 传给 adapter。给 `AI2ThorAdapter.execute()`、`ThorRuntime.navigate_to_object()` 和 `build_navigation_request()` 添加可选 `action_wave`，最终存入 `NavigationRequest`。

`StepMovementStrategy.navigate()` 使用：

```python
if request.phase_coordinator is not None and request.action_wave is not None:
    return request.phase_coordinator.submit_step_navigation(
        request.action_wave,
        request,
        lambda requests, completed_agent_ids: self.coordinator.execute_batch(
            requests,
            completed_agent_ids=frozenset(completed_agent_ids),
        ),
    )
return self.coordinator.execute_batch((request,))[request.agent_id]
```

直接调用 legacy `actions.GoToObject` 没有波次时继续使用单请求路径，其他机器人静态预约。

- [ ] **步骤 6：增加真实联合 crossing 集成测试**

使用十字 walkable map 和两个同时 `GoToObject` 请求，通过真实 `plan_scenario()` 执行。断言每个提交边界的两个真实位置都通过 `GeometryConflictModel.conflicts()` 检查、无边互换、最终均到目标，批次 metrics 为 1。

- [ ] **步骤 7：运行 GREEN 和执行器回归**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_movement_coordinator.py \
  tests/test_step_movement.py \
  tests.test_parallel_runner.PhaseCoordinatorTest \
  tests.test_parallel_runner.TolerantExecutorTest \
  tests.test_parallel_runner.OrdinaryExecutorFailureContinuationTest
```

预期：全部通过，无线程在测试结束后存活。

- [ ] **步骤 8：提交任务 6**

```bash
git add \
  scripts/executor_system/movement.py \
  scripts/executor_system/executor.py \
  scripts/executor_system/action_plan.py \
  scripts/executor_system/runtime.py \
  scripts/executor_system/step_movement.py \
  scripts/executor_system/movement_coordinator.py \
  tests/test_movement_coordinator.py \
  tests/test_parallel_runner.py
git commit -m "feat: coordinate joint robot movement waves"
```

---

### 任务 7：为已完成堵路机器人增加物理停车恢复

**文件：**
- 修改：`scripts/executor_system/movement_coordinator.py`
- 修改：`tests/test_movement_coordinator.py`
- 修改：`tests/movement_fakes.py`

**接口：**
- 内部输出：`parking_candidates(agent_id: int, active_requests: Sequence[NavigationRequest], snapshot: RuntimeWorldSnapshot) -> Tuple[Dict[str, float], ...]`。
- `execute_batch(requests, completed_agent_ids=frozenset())` 只允许 `completed_agent_ids` 集合中的 agent 获取停车意图。该集合来自任务 6 中 `PhaseCoordinator` 的锁内完成状态快照，使用物理 agent id，提交后不受后续线程状态变化影响。

- [ ] **步骤 1：编写已完成机器人堵路 RED 测试**

构造一条有侧袋停车点的窄通道：agent 0 要通过，agent 1 已完成并停在通道中。传入 `completed_agent_ids=frozenset({1})`，断言 agent 1 先逐格进入侧袋、agent 0 到达目标、没有 `Teleport`，metrics `parking_moves > 0`。

- [ ] **步骤 2：编写未完成机器人不可停车 RED 测试**

使用同一地图但传空 completed 集合，断言 `StepNavigationError`，agent 1 位置和动作列表不变。再增加候选与物体 AABB 冲突测试，断言该停车点被过滤。

- [ ] **步骤 3：运行测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_movement_coordinator.CompletedRobotParkingTest
```

预期：当前规划重试后仍 `NO_PLAN_FOUND`，不会移动 blocker。

- [ ] **步骤 4：实现确定性停车候选和一次恢复**

候选必须：属于全局 walkable、避开当前 agent、避开所有活跃终点与已有路径 corridor、通过场景物体占地过滤。排序键固定为：

```python
(
    -minimum_distance_to_active_targets,
    -minimum_distance_to_other_agents,
    position_to_grid_key(position),
)
```

每个已完成 agent 最多保留 3 个停车候选，防止候选笛卡尔积爆炸。初次规划和一次全刷新均失败后，才把符合条件的 completed agent 变成可移动 `RobotIntent`；其他静态 agent 保持唯一当前候选。停车恢复最多尝试一次，仍失败则返回原规划错误加 parking trace。

- [ ] **步骤 5：运行停车、恢复和 crossing 测试并确认 GREEN**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_movement_coordinator.CompletedRobotParkingTest \
  tests.test_movement_coordinator.JointMovementWaveTest \
  tests.test_step_movement.StepMovementRecoveryTest
```

- [ ] **步骤 6：提交任务 7**

```bash
git add \
  scripts/executor_system/movement_coordinator.py \
  tests/test_movement_coordinator.py tests/movement_fakes.py
git commit -m "feat: park completed robots during navigation"
```

---

### 任务 8：接入生成运行时、子进程参数、指标和固定双模式基准

**文件：**
- 修改：`scripts/executor_system/generated_plan_runtime.py:90-260`
- 修改：`scripts/executor_system/parallel_runner.py:840-1195`
- 修改：`scripts/executor_system/runtime.py`
- 修改：`scripts/README.md`
- 新建：`scripts/benchmark_movement_modes.py`
- 新建：`tests/test_movement_benchmark.py`
- 修改：`tests/test_parallel_runner.py`
- 新建：`tests/fixtures/movement_benchmark_plans.json`

**接口：**
- 生成脚本参数：`--movement-mode teleport|step`。
- parallel runner 参数：`--movement-mode teleport|step`。
- 输出：`effective_timeout_seconds(movement_mode, explicit_timeout) -> float`。
- 输出：runner JSON 中的 `movement_mode` 和 `navigation_metrics`。
- 基准 CLI：`--select-manifest`、`--manifest`、`--output-json`、`--output-md`、`--check`。

- [ ] **步骤 1：编写模式参数、超时和子进程命令 RED 测试**

在 `tests/test_parallel_runner.py` 增加：

```python
def test_movement_mode_defaults_choose_mode_specific_timeout(self):
    self.assertEqual(effective_timeout_seconds("teleport", None), 30.0)
    self.assertEqual(effective_timeout_seconds("step", None), 120.0)
    self.assertEqual(effective_timeout_seconds("step", 45.0), 45.0)

def test_run_generated_executable_passes_movement_mode(self):
    with patch("executor_system.parallel_runner.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        run_generated_executable(
            executable_path,
            metrics_output=metrics_path,
            timeout_seconds=120,
            movement_mode="step",
        )
    self.assertIn("--movement-mode", run.call_args.args[0])
    self.assertIn("step", run.call_args.args[0])
```

给 generated runtime parser 增加直接测试，断言非法模式由 argparse 拒绝。

- [ ] **步骤 2：运行参数测试并确认 RED**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_parallel_runner.ParallelRunnerCliTest.test_movement_mode_defaults_choose_mode_specific_timeout \
  tests.test_parallel_runner.ParallelRunnerCliTest.test_run_generated_executable_passes_movement_mode
```

- [ ] **步骤 3：实现 generated runtime 和 parallel runner 透传**

两个 parser 都为 movement mode 增加 choices，并将其默认值设为 `None`，使未提供 CLI 参数时仍可读取环境变量；benchmark 显式传入模式。把 timeout parser 默认值也改为 `None`，在执行前调用：

```python
def effective_timeout_seconds(movement_mode, explicit_timeout):
    if explicit_timeout is not None:
        return float(explicit_timeout)
    return 120.0 if movement_mode == "step" else 30.0
```

generated runtime 在 standalone 和 runner 两条路径构造 `ThorRuntime(robot_defs, floor, cloud_rendering, render_image, movement_mode=args.movement_mode)`；runner 结束前写入：

```python
result["movement_mode"] = runtime.movement_config.mode.value
result["navigation_metrics"] = runtime.navigation_metrics.to_dict()
```

parallel runner 的 `run_generated_executable()`、round、retry 和 summary 函数逐层接收 `movement_mode`，子进程命令显式加入该参数。summary 顶层记录模式和有效 timeout。

- [ ] **步骤 4：为导航动作类型增加作用域计数**

在 `ThorRuntime` 增加线程局部 `navigation_action_scope`; 两种策略在 `navigate()` 外层进入该 context。`_step_direct()` 和传送直接 helper 在作用域内调用 `navigation_metrics.record_action(payload["action"])`。初始化场景不在作用域内，因此不会污染 `GoToObject` 传送计数。

新增测试分别执行初始化样例 payload、显式 `Teleport` 和策略内 `MoveAhead`，断言只有策略作用域动作进入 `navigation_metrics.action_counts`。

- [ ] **步骤 5：编写基准选择与阈值 RED 测试**

`tests/test_movement_benchmark.py` 使用临时目录创建合成 candidate metadata，验证每个 FloorPlan 类别恰选三个 strata：`single_navigation`、`concurrent_goto`、`multiple_waves`。再用合成双模式结果验证 `--check` 对以下条件失败：step 导航成功率低于 0.90、12 个样本中导航成功少于 10、导航 Teleport 非零、GCR 差超过 0.05、规划 P95 超过 0.250 秒。

- [ ] **步骤 6：实现确定性 selector、runner 和报告**

`benchmark_movement_modes.py` 使用 `ast.literal_eval` 读取每个生成文件的 `BUNDLE_DATA`、`TASK_FILE` 和 `TASK_INDEX`，再读取 task record 的 `robot list`。候选文件必须直接导入 `executor_system.generated_plan_runtime`，且物理机器人数量为 2–4；否则在分类前排除并记录原因。按 FloorPlan 范围和 action queues 分类，排序键固定为 `(floor_plan, task_id, resolved_path)`；每类每 stratum 选择第一个，缺任一桶立即报错，不降级替换规则。

manifest 格式：

```json
{
  "version": 1,
  "cases": [
    {
      "category": "kitchen",
      "stratum": "single_navigation",
      "path": "logs/intermediate_runs/final_test_new_0609_1___3/toast_the_bread_in_the_toaster,_then_put_the_bread_on_the_countertop,_then_switc/20260628_001/plan_to_code/executable_plan.py",
      "task_id": "FloorPlan3_task_13",
      "robot_count": 3
    }
  ]
}
```

运行模式使用现有 `run_generated_executable()`，每个 path 顺序执行 teleport 和 step，输出逐样本及聚合 JSON。Markdown 表格列出 mode、status、navigation success、navigation Teleport、replans、GCR、TC、SR、runtime 和失败类别。`--check` 根据规格阈值返回 0 或 1。

同时更新 `scripts/README.md`：说明 `teleport` 默认兼容、`step` 的显式启用方式、环境变量优先级、两种默认超时、无传送回退保证，以及生成/校验固定基准的完整命令。

- [ ] **步骤 7：生成并审查固定 12 样本 manifest**

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_movement_modes.py \
  --select-manifest \
  --root logs/intermediate_runs \
  --manifest tests/fixtures/movement_benchmark_plans.json
```

验证：

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m json.tool \
  tests/fixtures/movement_benchmark_plans.json
```

确认恰好 12 个不同路径、四类各 3 个、三种 stratum 各出现 4 次。路径必须相对仓库且文件存在。

- [ ] **步骤 8：运行生成/runner/benchmark 测试并确认 GREEN**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_movement_benchmark.py \
  tests/test_parallel_runner.py \
  tests/test_generate_single_subtask_code.py
```

- [ ] **步骤 9：提交任务 8**

```bash
git add \
  scripts/executor_system/generated_plan_runtime.py \
  scripts/executor_system/parallel_runner.py \
  scripts/executor_system/runtime.py \
  scripts/README.md \
  scripts/benchmark_movement_modes.py \
  tests/test_movement_benchmark.py \
  tests/test_parallel_runner.py \
  tests/fixtures/movement_benchmark_plans.json
git commit -m "feat: benchmark generated plans by movement mode"
```

---

### 任务 9：完整验证、真实双模式迭代和验收报告

**文件：**
- 验证：本计划所有生产和测试文件。
- 生成：`reports/movement_modes_benchmark.json`
- 生成：`reports/movement_modes_benchmark.md`
- 修改：失败根因直接涉及的生产文件和对应测试文件；每个修复保持单一根因和独立提交。

**接口：**
- 输入：固定 `tests/fixtures/movement_benchmark_plans.json`。
- 输出：通过 `scripts/benchmark_movement_modes.py --check` 的 JSON/Markdown 报告。
- 完成门槛：规格“验收标准”每一项均有当前运行证据。

- [ ] **步骤 1：编译全部变更 Python 文件**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/multi_robot_avoidance.py \
  scripts/benchmark_movement_modes.py \
  scripts/executor_system/movement.py \
  scripts/executor_system/teleport_movement.py \
  scripts/executor_system/step_movement.py \
  scripts/executor_system/movement_coordinator.py \
  scripts/executor_system/runtime.py \
  scripts/executor_system/executor.py \
  scripts/executor_system/action_plan.py \
  scripts/executor_system/generated_plan_runtime.py \
  scripts/executor_system/parallel_runner.py \
  tests/test_movement_config.py \
  tests/test_movement_strategies.py \
  tests/test_step_movement.py \
  tests/test_movement_coordinator.py \
  tests/test_movement_benchmark.py
```

预期：退出码 0，无语法错误。

- [ ] **步骤 2：运行聚焦与现有完整回归**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_movement_config.py \
  tests/test_movement_strategies.py \
  tests/test_step_movement.py \
  tests/test_movement_coordinator.py \
  tests/test_movement_benchmark.py \
  tests/test_multi_robot_avoidance.py \
  tests/test_parallel_runner.py \
  tests/test_executor_retry_policy.py \
  tests/test_generate_single_subtask_code.py
```

预期：全部通过，测试进程退出后没有活动机器人线程。

- [ ] **步骤 3：运行固定 12 样本真实双模式基准**

```bash
/home/dwb/.pyenv/bin/pyenv exec python scripts/benchmark_movement_modes.py \
  --manifest tests/fixtures/movement_benchmark_plans.json \
  --output-json reports/movement_modes_benchmark.json \
  --output-md reports/movement_modes_benchmark.md \
  --check
```

预期：真实 AI2-THOR 可启动；24 次模式运行都产生结果；检查退出码 0。

- [ ] **步骤 4：对未通过阈值执行严格根因迭代**

如果步骤 3 返回 1，不能修改 manifest、降低阈值或启用传送回退。按报告中的第一个失败样本和第一个失败类别执行：

1. 用相同 path、mode 和 timeout 单独重跑，保存其 decision trace。
2. 从最早错误边界反向定位到配置、候选、规划、微步、地图、可见性、波次或停车中的唯一根因。
3. 在对应现有测试模块增加最小 deterministic RED 用例并确认失败原因正确。
4. 实施一个只修复该根因的最小变更，运行聚焦 GREEN 和任务 2 的完整回归命令。
5. 单独提交，消息使用 `fix: <具体根因>`。
6. 重新执行步骤 3；每次只处理当前最早失败，直到 `--check` 返回 0。

若同一架构方向连续三次不同修复仍不能消除失败，停止增加补丁，回到设计文档核查联合规划或动作波次假设，再与用户确认架构调整。

- [ ] **步骤 5：逐项审计最终报告**

从 JSON 直接验证：

```text
step.navigation_success_rate >= 0.90
step.cases_without_navigation_failure >= 10
step.goto_navigation_teleports == 0
teleport_success_gcr - step_success_gcr <= 0.05
planner_fixture_p95_seconds <= 0.250
all step subprocesses terminated within effective timeout
```

同时检查 deterministic tests 证明无顶点冲突、边互换和 `<= 0.35 m` 预约冲突；传送 parity tests 证明默认兼容。任一证据缺失时目标仍未完成。

- [ ] **步骤 6：检查 diff、报告和工作树**

```bash
git diff --check
git status --short
git diff --stat HEAD~1..HEAD
/home/dwb/.pyenv/bin/pyenv exec python -m json.tool \
  reports/movement_modes_benchmark.json
```

确认没有生成脚本被改写，没有无关文件进入提交，Markdown 报告与 JSON 聚合值一致。

- [ ] **步骤 7：提交最终证据**

```bash
git add reports/movement_modes_benchmark.json reports/movement_modes_benchmark.md
git commit -m "test: validate pluggable robot movement modes"
```

提交后重新运行步骤 2 的完整测试命令和步骤 3 的 `--check`，以新鲜输出作为最终完成证据。
