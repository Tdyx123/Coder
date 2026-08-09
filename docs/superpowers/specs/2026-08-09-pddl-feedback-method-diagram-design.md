# PDDL Planner/VAL Feedback 方法示意图重构设计

## 目标

完全重构 `diagrams/02.drawio`，以用户提供的参考图为视觉基准，准确表达 `scripts/pddlrun_llmseparate.py` 在启用 Planner feedback 与 VAL feedback、其他配置保持默认时的核心流程。成品应适合论文、报告或演示文稿中的方法总览，并保持 draw.io 原生可编辑。

## 信息架构

- 中央主体为橙色 `LLM Reasoning & Planning Core`，用于组织分解、分配和 Problem 生成三个 LLM 驱动阶段，但不暗示 Fast Downward 或 VAL 属于 LLM。
- 顶部依次展示 `Task Decomposition` 与虚线边框的 `Parsed Subtasks`；Parsed Subtasks 仅表现文本子任务列表，不绘制不存在的依赖 DAG。
- 左侧输入框包含 `Task`、`Robot List`、`Cached Objects`、`PDDL Domains`，并连接到 `Object & Static-State Grounding`。
- 下部左右两侧分别展示 `Robot Allocation` 与 `PDDL Problem Generation`，中部展示独立的 `Planning / Validation Tools`，内部顺序固定为 `Fast Downward → VAL`。
- 底部输出为 `Per-Subtask Planning Results`，展示 Planner 与 VAL 可以部分成功；不使用 `Verified Executable Plan` 或真实执行已验证的表述。

## 反馈与语义约束

- Planner feedback 使用红色虚线，从 Planning / Validation Tools 返回 Robot Allocation，标签为 `Planner feedback · ≤ 2 retries`。
- VAL feedback 使用另一条红色虚线，从 Planning / Validation Tools 返回 PDDL Problem Generation，标签为 `VAL-guided regeneration · failed subtask only · ≤ 2 retries`。
- 两条反馈线不得返回 Task Decomposition 或 Grounding。
- 图中不包含默认关闭的 RAG、本地 deterministic PDDL repair、全局计划合并或环境执行验证。
- PDDL Problem Generation 不标记为计算并行；子任务之间的全局资源冲突和状态传递不声称已验证。

## 视觉设计

- 采用紧凑的 16:9 横向画布，主体布局接近参考图的中心辐射结构。
- 颜色语义固定：深蓝表示 LLM 驱动流程，橙色表示中央 LLM 核心，深灰表示外部规划/验证工具，红色虚线表示反馈重试，绿色表示结果与有效状态。
- 使用一致的圆角、1.5–2 px 描边、正交连线和清晰留白；避免旧图的超宽四区布局和多层泳道。
- 标题采用 20–24 px，主体标签 15–18 px，辅助说明不低于 12 px；确保缩放到演示文稿宽度后仍可阅读。
- 使用 Font Awesome Free 7.3.1 官方 SVG 图标作为主要图标来源，按模块语义选择并调整颜色、尺寸与局部构图后以内嵌 data URI 保存。保留 SVG 内归属注释，并在画布边缘保留简洁许可说明；文件不得依赖外部 URL。

## 验收标准

- `diagrams/02.drawio` 是有效、未压缩的 draw.io XML，可被 diagrams.net 打开和继续编辑。
- 画布无明显交叉主流程线、文字遮挡、图标拉伸或元素越界。
- 两条反馈环的目标、范围和默认重试次数正确。
- 成品中不存在 `RAG`、`Parallel Per-subtask`、默认本地 `PDDL Repair`、`Execution Valid` 或 `Verified Executable Plan` 等误导性内容。
- 所有第三方图标均以内嵌 SVG 保存，具有明确开源来源和许可归属。
- XML 校验和本地渲染检查通过；如环境具备 draw.io 导出工具，同时生成预览并进行视觉检查。
