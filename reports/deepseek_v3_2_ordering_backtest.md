# DeepSeek-V3.2 任务分解语义顺序审查

- 输入：`data/grpo/deepseek_v3_2_decomposition_matches.jsonl`
- 样本：148 条任务，439 个子任务，468 个子任务对
- 原有匹配标签：全部样本均为 `fully_matched=true`，但匹配策略明确为 `order_independent`，因此它不能证明时序正确

## 结论

1. 当前数据中可作为“硬语义前后关系”的稳定规则是 `then` 阶段屏障：左侧阶段全部完成后，右侧阶段才可开始；同一阶段内由 `and` 或逗号连接的子任务不产生硬顺序。
2. 子任务数组顺序、分解输出中的编号顺序、以及任务文件里的 `assigned_robots` 都不能单独充当语义顺序真值。
3. 该规则抽取到 253 条直接屏障边、328 条传递闭包边；140 个任务对在纯语义层面无硬顺序。
4. 下游分配结果中有 49 / 148 条任务违反至少一条硬语义边：89 条被放在同一并行行，13 条被反向安排。若只看含硬语义边的 126 条任务，任务级违例率为 38.9%。
5. 分解文字的高置信并行声明中，至少有 48 条任务直接与 `then` 屏障冲突；其中 45 条传播到最终安排，3 条被分配阶段纠正，另有 4 条只在最终安排中出现。文字冲突数是保守下界，不代表所有文字错误。
6. 另有 32 条安排把同一机器人在同一行重复使用，涉及 50 个任务对；这是资源串行问题，应与语义强制关系分开处理。

## 模式

| `then` 数量 | 任务数 |
|---:|---:|
| 0 | 22 |
| 1 | 76 |
| 2 | 40 |
| 3 | 9 |
| 4 | 1 |

高频阶段形状（数字表示每个阶段内的子任务数）：

| 阶段形状 | 任务数 |
|---|---:|
| 1→1→1 | 38 |
| 1→1 | 25 |
| 1→2 | 21 |
| 2→1 | 18 |
| 3 | 16 |
| 1→1→1→1 | 9 |
| 1→3 | 6 |
| 3→1 | 5 |
| 1 | 4 |
| 4 | 2 |
| 2→1→1 | 2 |
| 1→1→1→1→1 | 1 |

高频违例技能对（含传递闭包边）：

| 技能对 | 违例边 | 语义边 | 违例率 |
|---|---:|---:|---:|
| Break->PutOn | 18 | 51 | 35.3% |
| Break->Open | 14 | 20 | 70.0% |
| Break->SwitchOn | 10 | 22 | 45.5% |
| Break->Break | 8 | 11 | 72.7% |
| PutOn->Open | 7 | 23 | 30.4% |
| PutOn->SwitchOn | 5 | 22 | 22.7% |
| Open->SwitchOn | 5 | 15 | 33.3% |
| SwitchOn->Break | 5 | 5 | 100.0% |
| Open->Open | 3 | 9 | 33.3% |
| PutIn->Open | 3 | 7 | 42.9% |
| PutOn->PutOn | 3 | 7 | 42.9% |
| Break->PutIn | 2 | 21 | 9.5% |

规则解释：

- `A and B, then C and D` 推出 `A→C`、`A→D`、`B→C`、`B→D`；不推出 `A→B` 或 `C→D`。
- 连续多个 `then` 形成阶段链，并具有传递性。
- `it` 只做对象共指；若它位于 `then` 右侧，顺序来自 `then`，不是来自共指本身。
- 共享对象、共享技能或共享机器人只能触发进一步审查，不能在“纯语义、无先决条件”设定下自动升级为硬边。

## 迭代回测

以 `then` 阶段边为语义真值，对三种弱启发式做微平均回测：

| 启发式 | Precision | Recall | F1 | TP | FP | FN |
|---|---:|---:|---:|---:|---:|---:|
| 任务文件数组全序 | 0.541 | 0.771 | 0.636 | 253 | 215 | 75 |
| 分解编号全序 | 0.701 | 1.000 | 0.824 | 328 | 140 | 0 |
| 参考同机器人 + 分解编号 | 0.685 | 0.689 | 0.687 | 226 | 104 | 102 |

参考机器人安排的弱证据：

- 硬语义边中同机器人占 226 / 328 （68.9%）。
- 无硬语义关系的任务对中，同机器人仍占 104 / 140 （74.3%）。
- 实际分配与任务文件参考机器人完全一致 330 / 439 （75.2%）。
- 这与生成代码一致：参考机器人是按 `robot list` 顺序选择第一个满足技能/质量约束的机器人。它适合做能力可行性参考，不适合做时序标签。

## 下游安排中的语义违例

| ID | 任务 | 并行违例 | 反向违例 | 当前安排 | 建议安排 |
|---|---|---:|---:|---|---|
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan211:9` | break the window, then break the laptop, then switch on the laptop, then put the pillow on the sofa. | 1 | 2 | `Subtask 1: Robot 3;Subtask 4: Robot 1;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 3;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 3;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan203:28` | put the vase on the coffee table, then open the drawer, then switch on the laptop, then switch on the lightswitch. | 6 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 1;Subtask 3: Robot 3;Subtask 4: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 3;<br>Subtask 4: Robot 2;` |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan209:3` | break the laptop, then put the remote control in the drawer. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan417:22` | open the cabinet, then put the toilet paper in the garbage can, then open the toilet, then switch on the faucet. | 1 | 1 | `Subtask 1: Robot 3;Subtask 3: Robot 3;<br>Subtask 2: Robot 3;<br>Subtask 4: Robot 3;` | `Subtask 1: Robot 3;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 3;<br>Subtask 4: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan406:24` | break the mirror, then switch on the faucet, then put the toiletpaper on the toiletpaperhanger. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 1;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan204:10` | break the window, then open the box, then put the vase on the shelf. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan417:19` | break the mirror, then open the toilet, then put the toilet paper in the cabinet. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan313:11` | break the mirror, then put the pen on the desk, then open the drawer. | 3 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;Subtask 3: Robot 2;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan216:1` | break the vase, then put it on the shelf, then switch on the desklamp. | 1 | 1 | `Subtask 1: Robot 1;Subtask 3: Robot 2;<br>Subtask 2: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan228:26` | break the window, then switch on the lightswitch, then break the vase, then break the plate. | 6 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 1;Subtask 3: Robot 2;Subtask 4: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan221:22` | open the box, then put the pen on the dining table, then switch on the television. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan204:21` | switch on the desklamp, then put the creditcard in the drawer, then put the cellphone on the drawer. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan310:7` | break the cellphone, then open the cabinet, then put the pen in the box. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan324:8` | put the mug on the shelf, then open the book, then put the CD in the drawer. | 3 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan205:2` | put the watch on the chair, then put the credit card on the side table. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan419:17` | open the drawer, then switch on the showerhead, then put the toiletpaper in the garbagecan. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 3;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan208:26` | switch on the floorlamp, then break the vase, then put the keychain in the box. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan7:21` | open the book, then put the fork in the pan, then cool the lettuce in the fridge. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:18` | break the cellphone, then put the dumbbell and the cellphone on the desk. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan314:4` | put the box on the floor, then open the book and the drawer. | 2 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 3;Subtask 3: Robot 3;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan11:10` | break the bowl, then put the dishsponge in the bowl, then open the drawer, then fill the kettle with water. | 3 | 2 | `Subtask 1: Robot 1;Subtask 3: Robot 1;Subtask 4: Robot 2;<br>Subtask 2: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;<br>Subtask 4: Robot 2;` |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan19:29` | cool the tomato in the fridge, then open the cabinet. | 1 | 0 | `Subtask 1: Robot 3;Subtask 2: Robot 2;` | `Subtask 1: Robot 3;<br>Subtask 2: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan302:18` | break the bowl, then put the teddybear on the bed, then open the safe. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 3;<br>Subtask 3: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan204:22` | break the cellphone, then switch on the cellphone, then break the statue, then put the creditcard in the drawer, then break the window. | 3 | 3 | `Subtask 1: Robot 1;Subtask 3: Robot 2;Subtask 5: Robot 3;<br>Subtask 2: Robot 1;<br>Subtask 4: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 2;<br>Subtask 5: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:22` | open the drawer, then break the window, then put the tissuebox on the garbagecan. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan5:5` | put the mug on the countertop, open the cabinet, wash the bowl, then switch on the stoveknob. | 3 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 1;Subtask 3: Robot 2;Subtask 4: Robot 1;` | `Subtask 1: Robot 1;Subtask 3: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan202:16` | open the box, then open the laptop, switch it on, and open the book. | 1 | 0 | `Subtask 1: Robot 2;Subtask 4: Robot 2;<br>Subtask 2: Robot 4;<br>Subtask 3: Robot 4;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 4;Subtask 4: Robot 2;<br>Subtask 3: Robot 4;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan420:27` | put towel on towelholder, then wash the cloth, then put cloth on sink, then put toiletpaper on garbagecan. | 1 | 2 | `Subtask 1: Robot 1;Subtask 4: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;<br>Subtask 4: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan308:29` | break the window, then put the credit card on the drawer, put the pencil in the drawer, and break the mug. | 2 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 4: Robot 3;<br>Subtask 3: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;Subtask 4: Robot 3;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan218:19` | break the window and the plate, then put the cellphone in the drawer, then switch on the floorlamp. | 2 | 1 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 4: Robot 3;<br>Subtask 3: Robot 2;` | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan308:11` | put the alarm clock on the desk, then open the drawer, put the cellphone in the drawer, and switch on the desk lamp. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;<br>Subtask 3: Robot 3;Subtask 4: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;Subtask 3: Robot 3;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:28` | break the plate, then put the watch on the sidetable, then open the book. | 3 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;Subtask 3: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan328:5` | put pen on garbagecan, break the cellphone, open the drawer, then switch on the laptop. | 3 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;Subtask 3: Robot 2;Subtask 4: Robot 1;` | `Subtask 1: Robot 2;Subtask 2: Robot 1;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan318:4` | break the mug, then put the book on the armchair, switch on the lightswitch, and put the pencil in the box. | 3 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;Subtask 3: Robot 1;Subtask 4: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 1;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan204:26` | break the window, then switch on the desklamp, open the laptop, switch on the cellphone, and open the box. | 4 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 2;Subtask 3: Robot 2;Subtask 4: Robot 2;Subtask 5: Robot 2;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 2;<br>Subtask 5: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan16:26` | open the drawer, then put the ladle on the cabinet, then put the potato in the microwave. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan306:26` | break the cellphone, then put the alarmclock on the shelf and put the keychain in the drawer. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 2;Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:12` | break the cellphone, then put the dumbbell on the desk, then put the pen in the garbage can. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan328:16` | break the cellphone and the window, then open the drawer, then put the pencil in the garbage can. | 2 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 3: Robot 1;<br>Subtask 4: Robot 1;` | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:4` | break the bowl, then put the creditcard on the drawer, then switch on the lightswitch. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 3;<br>Subtask 3: Robot 2;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 3;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan428:13` | break the mirror, then put the soapbottle on the countertop and switch on the faucet. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan227:20` | break the statue, then put the newspaper in the drawer, then open the cabinet, then break the vase. | 1 | 1 | `Subtask 1: Robot 1;Subtask 3: Robot 2;<br>Subtask 2: Robot 2;<br>Subtask 4: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan403:17` | break the showerglass, then put the soapbar on the garbagecan, then switch on the faucet. | 1 | 0 | `Subtask 1: Robot 2;Subtask 2: Robot 1;<br>Subtask 3: Robot 2;` | `Subtask 1: Robot 2;<br>Subtask 2: Robot 1;<br>Subtask 3: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan225:3` | break the television, then put the credit card on the shelf, then break the window. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan413:9` | break the mirror, then open the drawer, then put the toilet paper on the drawer. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 1;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan313:26` | break the mirror, then switch on the lightswitch, then open the box, then open the book. | 6 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 3: Robot 2;Subtask 4: Robot 4;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;<br>Subtask 3: Robot 2;<br>Subtask 4: Robot 4;` |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan202:8` | break the television, then put the book on the floor. | 1 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 2;` |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:13` | break the mirror, then put the cd on the shelf, put the tennis racket on the desk, and break the laptop. | 2 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 3;Subtask 4: Robot 2;<br>Subtask 3: Robot 1;` | `Subtask 1: Robot 1;<br>Subtask 2: Robot 3;Subtask 3: Robot 1;Subtask 4: Robot 2;` |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan221:21` | break the statue and the vase, then put the keychain on the chair. | 2 | 0 | `Subtask 1: Robot 1;Subtask 2: Robot 2;Subtask 3: Robot 3;` | `Subtask 1: Robot 1;Subtask 2: Robot 2;<br>Subtask 3: Robot 3;` |

## 分解文字的高置信冲突

| ID | 任务 | 冲突声明 |
|---|---|---|
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan211:9` | break the window, then break the laptop, then switch on the laptop, then put the pillow on the sofa. | # We can parallelize SubTask 1 and SubTask 4 because they don't depend on each other initially. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan203:28` | put the vase on the coffee table, then open the drawer, then switch on the laptop, then switch on the lightswitch. | # We can parallelize all subtasks because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan209:3` | break the laptop, then put the remote control in the drawer. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan417:22` | open the cabinet, then put the toilet paper in the garbage can, then open the toilet, then switch on the faucet. | # We can parallelize SubTask 1 and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan406:24` | break the mirror, then switch on the faucet, then put the toiletpaper on the toiletpaperhanger. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan204:10` | break the window, then open the box, then put the vase on the shelf. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan417:19` | break the mirror, then open the toilet, then put the toilet paper in the cabinet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan313:11` | break the mirror, then put the pen on the desk, then open the drawer. | # We can parallelize SubTask 1, SubTask 2, and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan216:1` | break the vase, then put it on the shelf, then switch on the desklamp. | # We can parallelize SubTask 1 and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan228:26` | break the window, then switch on the lightswitch, then break the vase, then break the plate. | # We can parallelize SubTask 1, SubTask 2, SubTask 3, and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan221:22` | open the box, then put the pen on the dining table, then switch on the television. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan204:21` | switch on the desklamp, then put the creditcard in the drawer, then put the cellphone on the drawer. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan310:7` | break the cellphone, then open the cabinet, then put the pen in the box. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan324:8` | put the mug on the shelf, then open the book, then put the CD in the drawer. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan205:2` | put the watch on the chair, then put the credit card on the side table. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan419:17` | open the drawer, then switch on the showerhead, then put the toiletpaper in the garbagecan. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan208:26` | switch on the floorlamp, then break the vase, then put the keychain in the box. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan7:21` | open the book, then put the fork in the pan, then cool the lettuce in the fridge. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:18` | break the cellphone, then put the dumbbell and the cellphone on the desk. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan11:10` | break the bowl, then put the dishsponge in the bowl, then open the drawer, then fill the kettle with water. | # We can parallelize SubTask 1 and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan19:29` | cool the tomato in the fridge, then open the cabinet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan312:11` | break the window, then put the keychain in the drawer, then put the pillow on the bed. | # We can parallelize SubTask 2 and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan302:18` | break the bowl, then put the teddybear on the bed, then open the safe. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan204:22` | break the cellphone, then switch on the cellphone, then break the statue, then put the creditcard in the drawer, then break the window. | # We can parallelize SubTask 1, SubTask 3, and SubTask 5 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:22` | open the drawer, then break the window, then put the tissuebox on the garbagecan. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan202:16` | open the box, then open the laptop, switch it on, and open the book. | # We can parallelize SubTask 1 and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan420:27` | put towel on towelholder, then wash the cloth, then put cloth on sink, then put toiletpaper on garbagecan. | # We can parallelize SubTask 1 and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan308:29` | break the window, then put the credit card on the drawer, put the pencil in the drawer, and break the mug. | We can parallelize SubTask 1, SubTask 2, and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan308:11` | put the alarm clock on the desk, then open the drawer, put the cellphone in the drawer, and switch on the desk lamp. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:28` | break the plate, then put the watch on the sidetable, then open the book. | # We can parallelize SubTask 1, SubTask 2, and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan328:5` | put pen on garbagecan, break the cellphone, open the drawer, then switch on the laptop. | # We can parallelize SubTask 1, SubTask 2, SubTask 3, and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan423:21` | put the soap bar in the garbage can, then put the hand towel on the hand towel holder, then switch on the faucet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan318:4` | break the mug, then put the book on the armchair, switch on the lightswitch, and put the pencil in the box. | # We can parallelize SubTask 1, SubTask 2, SubTask 3, and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan204:26` | break the window, then switch on the desklamp, open the laptop, switch on the cellphone, and open the box. | # We can parallelize all subtasks because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan16:26` | open the drawer, then put the ladle on the cabinet, then put the potato in the microwave. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan306:26` | break the cellphone, then put the alarmclock on the shelf and put the keychain in the drawer. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:12` | break the cellphone, then put the dumbbell on the desk, then put the pen in the garbage can. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan201:4` | break the bowl, then put the creditcard on the drawer, then switch on the lightswitch. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan428:13` | break the mirror, then put the soapbottle on the countertop and switch on the faucet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan428:5` | open the cabinet, then wash the cloth, then put the candle on the cabinet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan227:20` | break the statue, then put the newspaper in the drawer, then open the cabinet, then break the vase. | # We can parallelize SubTask 1 and SubTask 3 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan403:17` | break the showerglass, then put the soapbar on the garbagecan, then switch on the faucet. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan225:3` | break the television, then put the credit card on the shelf, then break the window. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other, but SubTask 3 must come after SubTask 1 and SubTask 2 since the task description specifies "then" relationships. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan413:9` | break the mirror, then open the drawer, then put the toilet paper on the drawer. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan313:26` | break the mirror, then switch on the lightswitch, then open the box, then open the book. | # We can parallelize SubTask 1, SubTask 2, SubTask 3, and SubTask 4 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_003107:final_test_new_0630_0:FloorPlan202:8` | break the television, then put the book on the floor. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260713_194633:final_test_new_0712_1:FloorPlan320:13` | break the mirror, then put the cd on the shelf, put the tennis racket on the desk, and break the laptop. | # We can parallelize SubTask 1 and SubTask 2 because they don't depend on each other. |
| `pddlrun_llmseparate_20260709_154853:final_test_new_0709_1:FloorPlan221:21` | break the statue and the vase, then put the keychain on the chair. | SubTask 3 can be done after or in parallel with the breaking tasks. |

## 建议落地规则

1. 在任务分解输出中增加机器可读的 `semantic_stages`，不要从自然语言并行说明二次猜测。
2. 分配阶段把相邻语义阶段之间的笛卡尔积作为 barrier；机器人能力和资源冲突只在阶段内部继续拆 wave。
3. 评测拆成三项：子任务集合双射、语义边保持率、机器人能力可行率。现有 `fully_matched` 只覆盖第一项。
4. 若未来任务文本引入 `before`、`after`、`while`，应先扩展语义语法并人工标注小型验证集；不要直接用 PDDL 先决条件替代语义标签。

## 审查边界

- 文字并行冲突抽取采用保守规则，因此其数量是下界。
- `then` 阶段规则适用于本批生成式任务语言；它不是通用自然语言时序解析器。
- 建议安排保留模型选择的机器人，只插入语义 barrier，并在同一机器人重复出现时拆分 wave；它没有优化路径成本。
