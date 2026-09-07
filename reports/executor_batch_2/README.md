# 第二批验证证据

- [执行语义与接口](../../docs/executor_batch_2.md)
- [任务6实施与RED/GREEN证据](task6-implementation.md)；最终完整门禁 **575 tests / 17.797s / OK**，见 [verification.log](verification.log)。
- `verify.sh`：第一批全套与第二批新增/受影响具名测试；不用 discover。
- `semantic_examples.py` / `semantic_examples.json`：三个已执行的确定性仿真替身案例，真实执行器/资源租约/step算法；不代表 Unity 验收。
- `run_real_validation.py`：固定 48 次真实 Unity 验收驱动，最终结果 **pending**。
- `reviews/`：任务1–5的独立审查、修复后裁定历史；Task5 至 1d243042 规格/质量均 approved，任务6待独立审查。

第一批验收生产 SHA 为 `3c2661b267b767386114ed8cad5c6330871306a9`；第二批起点 `8f95c7ca092d7025a5ca67def5015daa56f6c568`。具体任务 SHA 见批次计划和任务实施记录。完整验收应绑定最后审查后的 production SHA，不能用预检代替。
