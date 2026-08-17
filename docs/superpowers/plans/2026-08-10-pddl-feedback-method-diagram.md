# PDDL Feedback Method Diagram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `diagrams/02.drawio` 完全重构为准确表现 Planner feedback 与 VAL feedback 的紧凑 16:9 方法示意图。

**Architecture:** 使用未压缩的 draw.io XML 直接定义固定画布、模块节点、正交信息流和两条红色反馈环。第三方图标从 Font Awesome Free 7.3.1 官方 SVG 获取，调整颜色与尺寸后以内嵌 data URI 保存，使文件离线可编辑且无外部资源依赖。

**Tech Stack:** diagrams.net/draw.io XML、内嵌 SVG、Font Awesome Free 7.3.1、Python `xml.etree.ElementTree`、ImageMagick/可用的 SVG 查看工具。

## Global Constraints

- 画布为紧凑 16:9 横版，英文标签，整体视觉接近用户提供的参考图。
- Planner feedback 返回 Robot Allocation；VAL feedback 仅返回 PDDL Problem Generation，默认均标注 `≤ 2 retries`。
- 不出现 RAG、本地 deterministic PDDL repair、计算并行、执行验证或全局 Verified Executable Plan 的声称。
- 图标必须来自 Font Awesome Free 7.3.1 官方 SVG，内嵌并保留 CC BY 4.0 归属，不依赖外部 URL。
- 最终输出框标题固定为 `Per-Subtask Planning Results`，并显式表现 Planner 与 VAL 的部分成功状态。
- 只修改 `diagrams/02.drawio` 和本计划；不得覆盖或暂存工作区中的其他用户改动。

---

### Task 1: 重建 draw.io 信息架构与视觉布局

**Files:**
- Modify: `diagrams/02.drawio`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-08-09-pddl-feedback-method-diagram-design.md` 中已批准的信息架构、反馈语义和视觉约束。
- Produces: 单页、未压缩、离线可编辑的 draw.io XML，包含稳定节点 ID、嵌入图标和完整连线。

- [x] **Step 1: 保留原文件为 Git 可恢复基线并确认目标文件未被用户改动**

Run:

```bash
git status --short -- diagrams/02.drawio
git diff -- diagrams/02.drawio
```

Expected: `diagrams/02.drawio` 在实施前没有未提交修改。

- [x] **Step 2: 从官方 Font Awesome Free 7.3.1 资源选择并读取图标**

使用官方仓库/发布包中的 Free SVG，至少覆盖以下语义：输入任务、机器人、缓存对象、PDDL 文件、分解层级、LLM 芯片、Problem 文件、Planner 路径、VAL 检查和结果状态。不得使用 Pro 图标；每个嵌入 SVG 保留官方 attribution comment。

- [x] **Step 3: 用完整 XML 替换旧四区/泳道布局**

建立 `1600 × 900` 页面，并使用下列稳定节点：

```text
inputs_group, grounding, llm_core, decomposition, parsed_subtasks,
allocation, problem_generation, planning_tools, results,
edge_planner_feedback, edge_val_feedback
```

主流程按以下位置和方向组织：左侧 Inputs → Grounding → 中央 LLM；LLM 向上连接 Decomposition/Parsed Subtasks，向下分支到 Allocation 与 Problem Generation；两者汇入 Planning Tools；Planning Tools 向下输出 Results。

- [x] **Step 4: 嵌入并改造开源 SVG 图标**

每个图标使用：

```text
shape=image;html=1;imageAspect=0;aspect=fixed;image=data:image/svg+xml,...
```

蓝色模块图标使用 `#173F8F`，LLM 核心使用 `#D85B00`，工具使用 `#303744`，结果状态使用 `#18843A/#D62828`。图标保持原 viewBox 比例，不拉伸。

- [x] **Step 5: 添加准确的反馈环与许可说明**

Planner feedback 为左下红色虚线，终点 `allocation`；VAL feedback 为右下红色虚线，终点 `problem_generation`。许可说明固定为：

```text
Icons adapted from Font Awesome Free 7.3.1 · CC BY 4.0 · fontawesome.com
```

### Task 2: 结构与语义自动校验

**Files:**
- Test: `diagrams/02.drawio`

**Interfaces:**
- Consumes: Task 1 生成的未压缩 XML。
- Produces: XML 可解析性、关键节点、禁用文案、嵌入资源和画布边界的验证证据。

- [x] **Step 1: 使用仓库指定 pyenv Python 解析 XML**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -c "import xml.etree.ElementTree as ET; ET.parse('diagrams/02.drawio'); print('XML OK')"
```

Expected: 输出 `XML OK`。

- [x] **Step 2: 验证关键节点和反馈终点**

Run:

```bash
/home/dwb/.pyenv/bin/pyenv exec python -c "import xml.etree.ElementTree as ET; r=ET.parse('diagrams/02.drawio').getroot(); cells={c.get('id'):c for c in r.iter('mxCell')}; required={'inputs_group','grounding','llm_core','decomposition','parsed_subtasks','allocation','problem_generation','planning_tools','results','edge_planner_feedback','edge_val_feedback'}; assert required <= cells.keys(); assert cells['edge_planner_feedback'].get('target')=='allocation'; assert cells['edge_val_feedback'].get('target')=='problem_generation'; print('FLOW OK')"
```

Expected: 输出 `FLOW OK`。

- [x] **Step 3: 验证禁用文案和离线图标**

Run:

```bash
! rg -n "Optional RAG|Parallel Per-subtask|PDDL Repair|Verified Executable Plan|Execution Valid" diagrams/02.drawio
rg -n "Font Awesome Free 7\.3\.1|data:image/svg\+xml" diagrams/02.drawio
```

Expected: 第一条无匹配；第二条同时匹配许可和多个内嵌 SVG。

- [x] **Step 4: 检查 Git diff 与空白错误**

Run:

```bash
git diff --check -- diagrams/02.drawio
git diff --stat -- diagrams/02.drawio
```

Expected: 无 whitespace error；只有 `diagrams/02.drawio` 的完整重构变更。

### Task 3: 视觉预览与最终调整

**Files:**
- Modify: `diagrams/02.drawio`
- Temporary preview: `/tmp/02-drawio-preview.svg` or `/tmp/02-drawio-preview.png`

**Interfaces:**
- Consumes: 已通过结构校验的固定坐标和样式。
- Produces: 无遮挡、无越界、连线清晰且与参考图视觉层级一致的最终图。

- [x] **Step 1: 生成与 draw.io 坐标一致的临时 SVG/PNG 预览**

若本机无 diagrams.net CLI，则以相同节点坐标、文本、颜色和反馈路径生成临时 SVG，用图像查看工具检查实际布局；临时文件不得加入 Git。

- [x] **Step 2: 检查参考图关键视觉关系**

确认：LLM 为最大视觉锚点；Parsed Subtasks 与 Decomposition 垂直居中；Inputs/Grounding 在左；Allocation/Problem Generation 对称；Planning Tools 位于两者中间；Results 位于正下方；红色反馈线与蓝色主流程无不必要交叉。

- [x] **Step 3: 根据预览修正几何与文字**

只调整 `mxGeometry`、字体、间距、折点和颜色，不改变已批准的数据流语义。重复 XML 与 FLOW 校验直至通过。

- [x] **Step 4: 最终工作区检查**

Run:

```bash
git status --short
git diff --check -- diagrams/02.drawio
```

Expected: `diagrams/02.drawio` 是本次唯一新的实现改动；原有 `diagrams/01.drawio` 和其他未跟踪文件保持不变。
