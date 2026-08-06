# 合法 0 步计划分配实施计划

> **面向执行代理：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项实施本计划；使用复选框（`- [ ]`）跟踪步骤。

**目标：** 接受 Fast Downward 成功生成的 0 步计划，并让对应子任务完全绕过机器人能力过滤和 CP-SAT 分配。

**架构：** 只有 planner record 成功且计划文本带有 Fast Downward 零成本标记时，才把无动作计划认定为合法 no-op。所有子任务都保留 requirement 和输出记录，但在分配前拆分 requirements，只让需执行记录进入候选过滤和 CP-SAT，最后把带有显式未分配字段的 no-op 记录合并回原顺序。

**技术栈：** Python 3、`unittest`、OR-Tools CP-SAT、Fast Downward 计划产物。

## 全局约束

- 保留用户在 `scripts/llm_client.py` 中已有的未提交修改。
- 不把任意空输出或失败的 planner 输出当成合法 no-op。
- 保持子任务顺序和完成数对齐方式不变。
- Python 验证统一使用 `/home/dwb/.pyenv/bin/pyenv exec python`。

---

### 任务 1：识别合法 0 步规划输出

**文件：**
- 修改：`scripts/pddlrun_llmseparate_v2.py:2750-2821`
- 测试：`tests/test_pddlrun_config.py`

**接口：**
- 输入：包含 `compatibility_output` 以及可选 `return_code`、`status`、`has_planner_error` 的 planner record。
- 输出：`TaskManager._is_valid_zero_action_plan(planner_record: Dict[str, Any], plan_text: str) -> bool`，以及包含 `requires_execution: bool` 的 requirement。

- [ ] **步骤 1：为合法和非法无动作计划编写失败测试**

在 `PDDLRunConfigTests` 中增加测试，创建真实计划文件并调用 `_build_subtask_requirements`：

```python
def test_v2_build_requirements_accepts_successful_zero_step_plan(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        manager = pddlrun_llmseparate_v2.TaskManager(
            str(root), "test-model", config=SharedRunConfig(root)
        )
        plan_path = root / "subtask_01_plan.txt"
        plan_path.write_text("; cost = 0 (unit cost)\n", encoding="utf-8")

        requirements = manager._build_subtask_requirements(
            subtasks=["#SubTask 1: Already complete"],
            decomposed_plan="#SubTask 1: Already complete",
            problem_pddl=[""],
            planner_records=[{
                "problem_file": "subtask_01_problem_validated.pddl",
                "compatibility_output": str(plan_path),
                "return_code": 0,
                "status": "completed",
                "has_planner_error": False,
            }],
            objects_ai="objects=[]",
            preferred_predecessors={1: []},
        )

        self.assertFalse(requirements[0]["requires_execution"])
        self.assertEqual(requirements[0]["required_skills"], [])
        self.assertEqual(requirements[0]["duration"], 0)
        self.assertEqual(requirements[0]["required_locations"], [])

def test_v2_build_requirements_rejects_unmarked_empty_plan(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        manager = pddlrun_llmseparate_v2.TaskManager(
            str(root), "test-model", config=SharedRunConfig(root)
        )
        plan_path = root / "subtask_01_plan.txt"
        plan_path.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(
            pddlrun_llmseparate_v2.PDDLError,
            "No plan actions found",
        ):
            manager._build_subtask_requirements(
                subtasks=["#SubTask 1: Missing plan"],
                decomposed_plan="#SubTask 1: Missing plan",
                problem_pddl=[""],
                planner_records=[{
                    "problem_file": "subtask_01_problem_validated.pddl",
                    "compatibility_output": str(plan_path),
                    "return_code": 0,
                    "status": "completed",
                    "has_planner_error": False,
                }],
                objects_ai="objects=[]",
                preferred_predecessors={1: []},
            )
```

- [ ] **步骤 2：运行两个测试并确认 RED**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_build_requirements_accepts_successful_zero_step_plan \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_build_requirements_rejects_unmarked_empty_plan
```

预期：合法 0 步测试以 `No plan actions found` 失败；非法空文件测试用于锁定保留的错误行为。

- [ ] **步骤 3：实现 0 步验证和 requirement 字段**

在 `_build_subtask_requirements` 附近增加辅助方法：

```python
@staticmethod
def _is_valid_zero_action_plan(
    planner_record: Dict[str, Any],
    plan_text: str,
) -> bool:
    if planner_record.get("return_code") not in (None, 0):
        return False
    if planner_record.get("has_planner_error") is True:
        return False
    if planner_record.get("status") not in (None, "completed", "ok"):
        return False
    return bool(
        re.search(
            r"^\s*;\s*cost\s*=\s*0(?:\s|\(|$)",
            str(plan_text),
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
```

在 `_build_subtask_requirements` 中用分类逻辑替换无条件报错。合法 no-op 输出空技能/位置、零载重和零时长，并设置 `requires_execution=False`；有动作记录保持现有提取逻辑，并设置 `requires_execution=True`；非法无动作文件仍抛出原错误。

- [ ] **步骤 4：运行聚焦测试并确认 GREEN**

运行步骤 2 的命令。预期：两个测试均通过。

- [ ] **步骤 5：提交任务 1**

```bash
git add scripts/pddlrun_llmseparate_v2.py tests/test_pddlrun_config.py
git commit -m "fix: accept valid zero-step planner output"
```

---

### 任务 2：从机器人分配中排除 no-op requirements

**文件：**
- 修改：`scripts/pddlrun_llmseparate_v2.py:2677-2740`
- 测试：`tests/test_pddlrun_config.py`

**接口：**
- 输入：包含 `requires_execution`、`subtask_id`、调度字段和能力字段的 requirement。
- 输出：每个子任务对应一条 allocation 记录；no-op 没有候选或已分配机器人，起止时间均为零。

- [ ] **步骤 1：编写混合分配失败测试**

创建一个零成本计划和一个 `GoToObject` 计划，使用真实可执行机器人调用 `_allocate_subtasks_with_cpsat`，并断言：

```python
self.assertEqual([item["subtask_id"] for item in allocated], [1, 2])
self.assertFalse(allocated[0]["requires_execution"])
self.assertEqual(allocated[0]["candidate_robots"], [])
self.assertIsNone(allocated[0]["assigned_robot"])
self.assertIsNone(allocated[0]["assigned_robot_domain"])
self.assertEqual((allocated[0]["start"], allocated[0]["end"]), (0, 0))
self.assertTrue(allocated[1]["requires_execution"])
self.assertEqual(allocated[1]["assigned_robot"], "robot1")
```

同时断言保存的 `requirements.json`、`candidate_robots.json` 和 `subtasks.json` 保留两个子任务 ID，并把 no-op 候选编码为 `[]`。

- [ ] **步骤 2：编写全 no-op 且无机器人的失败测试**

将 `manager._solve_subtask_assignment` patch 为一旦调用就抛错；使用单个零成本计划和 `available_robots=[]` 调用 `_allocate_subtasks_with_cpsat`，再断言成功返回未分配输出，以证明全部 requirement 为 no-op 时不会调用 CP-SAT。

- [ ] **步骤 3：运行分配测试并确认 RED**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_allocation_excludes_zero_step_requirements \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_all_noop_allocation_skips_cpsat
```

预期：测试失败，因为当前 `_build_subtask_requirements` 拒绝该计划，或 `_allocate_subtasks_with_cpsat` 要求每个子任务都有 assignment。

- [ ] **步骤 4：实现 actionable/no-op 拆分与输出合并**

在 `_allocate_subtasks_with_cpsat` 中：

```python
actionable_requirements = [
    requirement
    for requirement in requirements
    if requirement.get("requires_execution", True)
]
if actionable_requirements:
    actionable_candidates = self._filter_candidate_robots(
        actionable_requirements, available_robots
    )
    assignments = self._solve_subtask_assignment(
        actionable_requirements, available_robots
    )
else:
    actionable_candidates = {}
    assignments = {}

candidates = {
    requirement["subtask_id"]: actionable_candidates.get(
        requirement["subtask_id"], []
    )
    for requirement in requirements
}
```

构造输出时按 `requires_execution` 分支。no-op 使用空机器人字段和 `start=end=0`；actionable 继续读取求解器 assignment。两类输出都包含 `requires_execution`，并继续遍历原始 `requirements`，保证顺序稳定。

- [ ] **步骤 5：运行分配测试并确认 GREEN**

运行步骤 3 的命令。预期：两个测试均通过。

- [ ] **步骤 6：运行聚焦分配回归测试**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_build_requirements_reads_validated_problem_locations \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_location_capacity_serializes_only_shared_locations \
  tests.test_pddlrun_config.PDDLRunConfigTests.test_v2_build_requirements_prefers_pairwise_predecessors
```

预期：全部既有行为保持通过。

- [ ] **步骤 7：提交任务 2**

```bash
git add scripts/pddlrun_llmseparate_v2.py tests/test_pddlrun_config.py
git commit -m "fix: skip robot allocation for no-op subtasks"
```

---

### 任务 3：完整验证

**文件：**
- 验证：`scripts/pddlrun_llmseparate_v2.py`
- 验证：`tests/test_pddlrun_config.py`

**接口：**
- 输入：任务 1-2 完成后的实现。
- 输出：通过语法检查的代码和无回归的完整测试结果。

- [ ] **步骤 1：编译检查变更的 Python 文件**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m py_compile \
  scripts/pddlrun_llmseparate_v2.py tests/test_pddlrun_config.py
```

- [ ] **步骤 2：运行完整配置测试模块**

```bash
/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_pddlrun_config.py
```

- [ ] **步骤 3：检查最终 diff**

```bash
git diff --check
git status --short
git diff -- scripts/pddlrun_llmseparate_v2.py tests/test_pddlrun_config.py
```

确认无关的 `scripts/llm_client.py` 修改未被触碰，并排除在功能提交之外。
