# case-06 最终配对 GCR 差异：只读有界诊断

## 结论

已确认直接原因：**event repetition-05 将 Newspaper 放进 Drawer_2，而固定目标要求 Drawer_1**。它的 7 个动作和 3 次导航均报告成功，GCR 却从配对 full 的 2/3 降至 1/3。因此动作/导航成功率没有退化标志与 GCR 退化并不矛盾。

原始计划后续动作使用泛称 `Drawer`，目标为明确实例 `Drawer_1`。这使不同导航轨迹下的实例选择能够改变目标结果。此次实例差异有 stdout、动作租约和最终双向容纳关系共同证明；不是评估数值舍入或缺失目标造成的差异。

**尚未确定为什么只有 event 第 5 次走出了另一条导航轨迹。** 其余四对都相同；不能据此建立“确定性的 event 导航/缓存故障”，也不能将实际发生的配对 GCR 退化豁免或宣称正确性通过。没有修改代码、重跑 Unity、运行测试或委派工作；仅新增本报告。

## 来源与身份

- 原始证据根目录 `R = reports/executor_batch_3/939be0ba_step_comparison/runs/case-06/step/legacy`，读取 `R/{full,event}/repetition-{01..05}/result.json`；其中含 actions、stages/resources、goal_results、final_snapshot 和 stdout。
- 配对第 5 次 code SHA 都为 `939be0ba48b9b9d058cc64eb036f9639ddda3ff0`，`code_dirty=false`，seed=0，`fixed_goals_v2`，原始目标数均为 3。
- 两者 executable SHA256 相同：`663e80798d1fad7251a3353ca01a78e65fee2b304a2eb5c3074dc891b416ee03`；plan SHA256 相同：`7c261de12de3057dbd0e1a9549d59b7309c7b2bdcb2cf42535525752ab751d10`；manifest SHA256 也一致。
- 旧诊断：`workspace/navigation-baseline-diagnosis.md` 和 `old-navigation-replay/report.json`（均相对于本报告目录）。

## 五次配对结果

表内失败动作 A = `1:robot1:0 GoToObject(Chair_5)`，错误均为 `step navigation planning failed after full refresh with status NO_PLAN_FOUND; parking recovery found no safe joint plan`。所有 10 次评估均 valid，无超时；这里的状态是 execution_status，而非进程返回码。

| 重复 | full GCR / 状态 / 失败动作 | event GCR / 状态 / 失败动作 | full → event 导航成功 | Newspaper 最终容器 full → event |
|---|---|---|---|---|
| 01 | 2/3 / partial / A | 2/3 / partial / A | 2/3 → 2/3 | Drawer_1 → Drawer_1 |
| 02 | 2/3 / partial / A | 2/3 / partial / A | 2/3 → 2/3 | Drawer_1 → Drawer_1 |
| 03 | 2/3 / partial / A | 2/3 / partial / A | 2/3 → 2/3 | Drawer_1 → Drawer_1 |
| 04 | 2/3 / partial / A | 2/3 / partial / A | 2/3 → 2/3 | Drawer_1 → Drawer_1 |
| 05 | 2/3 / partial / A | 1/3 / completed / 无 | 2/3 → 3/3 | Drawer_1 → Drawer_2 |

前九个相同结果轨迹均 6/7 动作成功；event-05 为 7/7。case-06 的五次平均 GCR 为 full 2/3、event 3/5，下降 1/15；总体 legacy GCR 的该项贡献来自这一个已记录的目标损失。

## 第 5 对的动作和物体证据

原始 executable 中 Phase 1 为 `GoToObject(Vase), BreakObject(Vase)`；Phase 2 为 `GoToObject(Chair_5), PickupObject(Newspaper), GoToObject(Drawer), OpenObject(Drawer), PutObject(Newspaper, Drawer)`。没有取放 Pen 的动作，故 Chair_5/Pen 目标在所有 10 次均未满足。

| 动作 | full-05 | event-05 |
|---|---|---|
| Phase 1 两动作 | 均成功，结束 world 172 | 均成功，结束 world 84 |
| Phase 2 GoToObject(Chair_5) | NO_PLAN_FOUND，legacy 决策 skip | 成功，world 152 |
| PickupObject(Newspaper) | 成功，world 194 | 成功，world 153 |
| GoToObject(Drawer) | 成功，world 200；stdout Drawer_c561ea90 | 成功，world 159；stdout Drawer_8801d035 |
| OpenObject(Drawer) | 成功，world 201；租约 Drawer_1 | 成功，world 160；租约 Drawer_2 |
| PutObject(Newspaper, Drawer) | 成功，world 202；租约 Newspaper + Drawer_1 | 成功，world 161；租约 Newspaper + Drawer_2 |

world version 包含控制器提交，不能将 full/event 的数值差直接解释成导航动作差。

- Drawer_1 = `Drawer|-00.31|+00.63|+03.37`；Drawer_2 = `Drawer|-02.11|+00.65|-00.07`；Newspaper = `Newspaper|-02.76|+00.68|+01.18`。
- full-05：Drawer_1 `isOpen=true`，`receptacleObjectIds=[Newspaper]`；Newspaper `parentReceptacles=[Drawer_1]`，位置约 `(-0.433747, 0.619129, 3.522045)`。Drawer_2 关闭且为空。
- event-05：Drawer_1 关闭且为空；Drawer_2 `isOpen=true`，`receptacleObjectIds=[Newspaper]`；Newspaper `parentReceptacles=[Drawer_2]`，位置约 `(-1.731913, 0.647447, 0.188953)`。
- 两者 goal_results 都只以 Drawer_1 为该目标候选；full `contains_satisfied=true`，event 为 false。Vase 的 BROKEN 均满足，Chair_5/Pen 均未满足。机器人手中都已无物体。
- 最终 robot1 full 在 `(-0.75, 0.902657, 3.5)`，event 在约 `(-2.249999, 0.902657, 0.500000)`；其余三机器人位置一致且没有动作队列。
- full-05 导航 failed_transitions=3、micro_steps=29；event-05 为 2、47。event-05 errors/robot_failures 为空。

## 可以解释的机制与仍缺失的证据

原始 bundle 的 `object_mappings` 含 `drawer: Drawer`，但 object_id_bindings 分别定义 Drawer_1/Drawer_2。`object_resolver.py:42` 仅对非 multiple 绑定增加泛类型别名，所以泛称 Drawer 不等于固定 Drawer_1。`find_objects` 在无已准入绑定时按已操作历史、可见性、距离和 objectId 排序（约 564–621 行）；选中后，Open/Put 的动作资源记录表明所操作实例各自保持一致。这与“成功走近 Newspaper 后选择另一抽屉”的轨迹一致，**具体首次选择时的完整排序候选未保存，不能把距离单独断言为唯一决定因素**。

所有 10 次 Phase 1 结束 robot1 坐标都为 `(-0.75, 0.9026566743850708, 3.5)`；第 5 对 yaw 也完全一致（99.8022232055664）。该对 Phase 1 快照中 Chair_5、Newspaper 和已破碎 Vase 的 position/parent 状态一致。后续不同导航结果不是从这些已保存字段中的明显起点差异就能解释。原始记录没有逐步候选可达图、blocked-edge 变化、控制器碰撞决策轨迹，无法定位导航首次分歧，更不能仅凭最终成功轨迹排除缓存、模拟器或时序因素。

旧代码 `b1a679ec` 同环境五次 replay 全部 partial、GCR2/3、同一动作 A 的同一 NO_PLAN_FOUND，且实际加载 runtime 路径已记录为旧 worktree。这证明反复出现的导航失败不需要第三批代码即可发生；**它没有证明新出现的 event-05 Drawer_2 目标损失只是旧问题，也不能抵消本次验收退化**。本次 event 的 1/5 结果分化尚未建立确定性 event 故障；触发机制继续标记 unresolved，保留所有结果和原验收分母。
