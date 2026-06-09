## 电器与固定装置

* `microwave` / `Microwave` 微波炉。可开关门、可开关电源、可作为 receptacle，也会改变温度。
  三种效果：

  * **加热**：微波炉打开电源时，里面的物体会变为 `Hot`。具体方式是 `OpenObject(Microwave)` → `PutObject` 把物体放入微波炉 → `CloseObject(Microwave)` → `ToggleObjectOn(Microwave)` → 等待。
  * **烹饪**：文档明确说部分物体会在开着的微波炉中自动 cooked；`Potato`/`PotatoSliced` 可以被微波炉烹饪，`EggCracked` 放入微波炉后再打开微波炉也会被烹饪。
  * **约束**：微波炉门开着时不能打开电源；微波炉开着时不能打开门，所以要先 `ToggleObjectOff` 再 `OpenObject`。

* `toaster` / `Toaster` 烤面包机。可开关、可作为 receptacle，但 receptacle 限制很强。
  一种主要效果：

  * **烘烤/烹饪面包片**：只能把 `BreadSliced` 放入 toaster；toaster 为 on 时，里面的 `BreadSliced` 会自动变为 cooked。具体方式是先对 `Bread` 执行 `SliceObject` 生成 `BreadSliced`，再 `PutObject(BreadSliced, Toaster)`，然后 `ToggleObjectOn(Toaster)` 并等待。整块 `Bread` 不是 toaster 的有效对象。

* `coffee_machine` / `CoffeeMachine` 咖啡机。可开关、可作为 receptacle、可移动。
  一种主要效果：

  * **冲咖啡并加热杯子**：咖啡机只接受 `Mug`。如果 coffee machine 处于 on，且放入的是空 `Mug`，`Mug` 会自动被填充为 `coffee`，并且温度变为 `Hot`。具体方式是 `PutObject(empty Mug, CoffeeMachine)` → `ToggleObjectOn(CoffeeMachine)` → 等待；也可以先打开咖啡机再放入空 `Mug`。
  * **约束**：`Cup`、`Bowl` 等不是文档中的 coffee machine 兼容对象；`Mug` 必须为空。

* `fridge` / `Fridge` 冰箱。可开门、可作为 receptacle、会改变温度。
  两种效果：

  * **冷却**：放入 fridge 的物体会自动变为 `Cold`。具体方式是 `OpenObject(Fridge)` → `PutObject(item, Fridge)` → 可选 `CloseObject(Fridge)` → 等待。
  * **存放**：作为常规容器使用，能容纳多种食物和厨具。
  * **约束**：不是 toggleable，不通过 `ToggleObjectOn` 制冷；也不触发 cooked。

* `faucet` / `Faucet` 水龙头。可开关，本身不是容器，但会产生 running water source。
  三种效果：

  * **流水**：`ToggleObjectOn(Faucet)` 后，从 faucet spout 流出水；如果 faucet 位于 `Sink` 或 `Bathtub` 上方，对应内部区域会蓄水。
  * **注水**：空的 fillable 物体移到流水下方会自动装水，例如 `Mug`, `Cup`, `Bowl`, `Pot`, `Kettle`, `Bottle`。实际操作通常是把物体放到 `SinkBasin` 中、位于 faucet 下方，然后 `ToggleObjectOn(Faucet)` 并等待。
  * **清洗**：dirtyable 物体在流水下会自动变 clean，例如 `Mug`, `Cup`, `Bowl`, `Plate`, `Pot`, `Pan`。也可以直接用 `CleanObject`。
  * **约束**：不要把 faucet 当 receptacle；放置目标通常是 `SinkBasin` 或 `Sink`。

* `stove_burner` / `StoveBurner` 炉灶火眼。可作为热源和受限 receptacle。
  三种效果：

  * **加热**：active 的 burner 上方/上面的物体会变为 `Hot`。但用 `PutObject` 直接放到 `StoveBurner` 上时，文档限制只接受 `Pot`, `Pan`, `Kettle`。
  * **烹饪**：部分 cookable 食物在 active burner 上方会自动 cooked。按你的例子，常用对象是 `BreadSliced`, `EggCracked`, `Potato`, `PotatoSliced`；通常用 `PlaceObjectAtPoint` 放到 active burner 的上方坐标，再等待。
  * **点燃**：`Candle` 的烛芯移到 active burner 上方会被点燃。
  * **约束**：虽然 `StoveBurner` 在表里是 toggleable，但不能直接对 burner 执行 `ToggleObjectOn/Off`；必须通过关联的 `StoveKnob` 控制。

* `stove_knob` / `StoveKnob` 炉灶旋钮。
  一种主要效果：

  * **控制火眼开关**：对 `StoveKnob` 执行 `ToggleObjectOn` / `ToggleObjectOff`，会同步打开/关闭它关联的 `StoveBurner`。
  * **约束**：`StoveKnob` 不是 receptacle，也不是 pickupable；它的作用是控制 burner 状态。若场景中有多个 knob，需要通过观察 metadata 中 burner 的 `isToggled` 或视觉火焰变化来确认哪个 knob 关联哪个 burner。

## 工具、餐具与容器

* `knife` / `Knife` 刀。pickupable 工具物体。
  主要用途：

  * **移动/放置**：可 `PickupObject`，也可放到 `Pot`, `Pan`, `Bowl`, `Mug`, `Plate`, `Sink`, `SinkBasin`, `CounterTop`, `Drawer` 等兼容 receptacle。
  * **切割语义**：基础 iTHOR 文档把切割建模为对 sliceable 物体执行 `SliceObject`，如 `Apple`, `Bread`, `Potato`, `Tomato`, `Lettuce`, `Egg`。`Knife` 本身没有“UseKnife”动作；一些任务封装会额外要求 agent 先拿着 `Knife` 再允许 `SliceObject`，但这是任务层约束，不是物体表中给 `Knife` 的直接效果。

* `mug` / `Mug` 马克杯。pickupable、receptacle、fillable、dirtyable、breakable。
  四种效果：

  * **盛放小物体**：作为小 receptacle，可放置一些餐具或小物体。
  * **装液体**：可用 `FillObjectWithLiquid` 填入 `"water"`, `"coffee"`, `"wine"`；空 `Mug` 放到 running water 下会自动装水。
  * **咖啡机交互**：空 `Mug` 放入 on 的 `CoffeeMachine` 会自动变为装有 coffee 且 `Hot`。
  * **清洗/打碎**：dirty 状态下放到 running water 下会自动 clean；也可 `CleanObject`。部分材质/实例可被高冲击打碎，需看 metadata 的 `breakable`。

* `cup` / `Cup` 杯子。pickupable、receptacle、fillable、dirtyable、部分 breakable。
  三种效果：

  * **盛放小物体**：作为 receptacle。
  * **装液体**：可直接 `FillObjectWithLiquid`，也可放到 faucet running water 下自动装水。
  * **清洗/打碎**：dirty 时可被 running water 自动清洗；部分 `Cup` 可因足够冲击而破碎。
  * **区别**：`Cup` 没有 coffee machine 的自动冲咖啡效果；coffee machine 文档限制为 `Mug`。

* `bowl` / `Bowl` 碗。pickupable、receptacle、fillable、dirtyable、部分 breakable。
  三种效果：

  * **盛放食物/小物体**：可放 `Apple`, `Potato`, `Tomato`, `Egg` 等多种物体。
  * **装水**：空 `Bowl` 放到 running water 下会自动装水；也可用 `FillObjectWithLiquid`。
  * **清洗/破碎**：dirty 时可被 running water 自动 clean；玻璃/陶瓷等实例可被打碎，塑料实例通常不可碎，需以 metadata 为准。

* `plate` / `Plate` 盘子。pickupable、receptacle、dirtyable、部分 breakable。
  三种效果：

  * **承载食物**：可作为 `Apple`, `BreadSliced`, `Potato`, `EggCracked` 等物体的放置面。
  * **搬运组合**：因为 `Plate` 是 pickupable receptacle，把物体放在 `Plate` 上后再拿起 `Plate`，可以一起移动。
  * **清洗/破碎**：dirty 时放到 running water 下会 clean；部分盘子可破碎。
  * **约束**：`Plate` 不是 fillable，不能像 `Cup`/`Bowl` 那样装液体。

* `pot` / `Pot` 锅。pickupable、receptacle、fillable、dirtyable。
  三种效果：

  * **炉灶加热容器**：`Pot` 是 `StoveBurner` 直接接受的三类对象之一。具体方式是 `PutObject(Pot, active StoveBurner)`，然后等待，锅会受热变 `Hot`。
  * **盛放/装水**：可作为 receptacle，也可在 running water 下自动装水。
  * **清洗**：dirty 时放到 running water 下自动 clean。
  * **约束**：文档没有建模“锅里煮汤/煮食材”的复杂过程；cookable 食物是否 cooked 取决于食物本身是否接触到热源判定或被 `CookObject` 设置。

* `pan` / `Pan` 平底锅。pickupable、receptacle、dirtyable。
  两种效果：

  * **炉灶加热容器**：`Pan` 可直接 `PutObject` 到 `StoveBurner` 上，是 burner 允许的三类直接放置对象之一。
  * **盛放/清洗**：可作为 receptacle 盛放食物；dirty 时可被 running water 自动清洗。
  * **约束**：`Pan` 不是 fillable，不能像 `Pot` 那样装水。

* `kettle` / `Kettle` 水壶。openable、pickupable、fillable。
  三种效果：

  * **装水**：空 `Kettle` 放到 running water 下会自动装水；也可用 fill action。
  * **炉灶加热**：`Kettle` 是 `StoveBurner` 直接接受的三类对象之一，可 `PutObject(Kettle, active StoveBurner)` 后等待加热。
  * **开合**：可 `OpenObject` / `CloseObject`。
  * **约束**：不是 dirtyable，不触发 cooked。

* `bottle` / `Bottle` 瓶子。pickupable、fillable、breakable。
  三种效果：

  * **装液体**：可 `FillObjectWithLiquid`，空瓶放到 running water 下会自动装水。
  * **倒空**：`EmptyLiquidFromObject` 可清空；如果旋转到足够向下，液体也可能自动消失为 empty。
  * **破碎**：受到足够冲击会 break。
  * **约束**：不是 receptacle，也不是 dirtyable。

## 食物

* `bread` / `Bread` 面包。pickupable、sliceable。
  两种效果：

  * **切片**：`SliceObject(Bread)` 会生成多个 `BreadSliced`，以及一个较大的 bread endpiece。
  * **烘烤/烹饪派生物**：`Bread` 本体不是 toaster 的目标；`BreadSliced` 可放入 on 的 `Toaster` 自动 cooked，也可放到 active `StoveBurner` 上方自动 cooked。

* `potato` / `Potato` 土豆。pickupable、sliceable、cookable。
  三种效果：

  * **切片**：`SliceObject(Potato)` 生成多个 `PotatoSliced`。
  * **烹饪**：`Potato` 和 `PotatoSliced` 都可以在 active `StoveBurner` 上方或 on 的 `Microwave` 中自动 cooked；也可直接 `CookObject`。
  * **先熟后切**：如果先把 `Potato` cooked，再 `SliceObject`，生成的是 cooked `PotatoSliced`。

* `egg` / `Egg` 鸡蛋。pickupable、sliceable、breakable。
  三种效果：

  * **打碎/切开**：`SliceObject(Egg)` 或 `BreakObject(Egg)` 会把 `Egg` 变成 `EggCracked`；从高处掉落或被足够力量 throw/drop 也可能自动 break 成 `EggCracked`。
  * **烹饪派生物**：`EggCracked` 是 cookable；放到 active `StoveBurner` 上方会自动 cooked，放入 `Microwave` 后打开 microwave 也会自动 cooked。
  * **约束**：为稳定触发 cooked，通常先把 `Egg` 变成 `EggCracked`，再用 stove/microwave 烹饪。

## 水槽

* `sink` / `Sink` 水槽。部分 `Sink` 是 receptacle，通常与 `Faucet` 和 `SinkBasin` 配套。
  两种效果：

  * **放置区域**：可以作为一些物体的放置目标，但是否是 receptacle 要看具体实例 metadata。
  * **接水/清洗场景的一部分**：水不是由 `Sink` 本身产生，而是由 `Faucet` 产生；当 faucet 位于 sink 上方并打开时，sink 内部区域会蓄水，位于流水下的 fillable/dirtyable 物体会注水或清洗。

* `sink_basin` / `SinkBasin` 水槽内盆。receptacle，继承 sink 语义。
  三种效果：

  * **精确放置**：比 `Sink` 更适合作为“把物体放到水龙头下”的目标。具体方式是 `PutObject(item, SinkBasin)` 或用 `PlaceObjectAtPoint` 放到 basin 中合适坐标。
  * **注水辅助**：把 `Mug`, `Cup`, `Bowl`, `Pot`, `Kettle`, `Bottle` 放在 `SinkBasin` 中并打开 `Faucet`，空容器会在流水下自动装水。
  * **清洗辅助**：把 dirty 的 `Mug`, `Cup`, `Bowl`, `Plate`, `Pot`, `Pan` 放在 `SinkBasin` 中并打开 `Faucet`，会自动 clean。
  * **约束**：`SinkBasin` 本身不 toggle、不 pickup、不产生水；水源仍是 `Faucet`。

## 速查关系

| 目标       | 推荐交互链                                                                                                                                               |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| 烤面包      | `SliceObject(Bread)` → `PutObject(BreadSliced, Toaster)` → `ToggleObjectOn(Toaster)` → 等待                                                           |
| 微波土豆     | `PutObject(Potato/PotatoSliced, Microwave)` → `CloseObject(Microwave)` → `ToggleObjectOn(Microwave)` → 等待                                           |
| 煎/加热炉上食物 | `ToggleObjectOn(StoveKnob)` → 确认对应 `StoveBurner` active → `PlaceObjectAtPoint(BreadSliced/EggCracked/Potato/PotatoSliced, burner_above_point)` → 等待 |
| 炉上加热容器   | `ToggleObjectOn(StoveKnob)` → `PutObject(Pot/Pan/Kettle, StoveBurner)` → 等待                                                                         |
| 冲咖啡      | `PutObject(empty Mug, CoffeeMachine)` → `ToggleObjectOn(CoffeeMachine)` → 等待                                                                        |
| 装水       | `PutObject(empty Mug/Cup/Bowl/Pot/Kettle/Bottle, SinkBasin)` → `ToggleObjectOn(Faucet)` → 等待                                                        |
| 洗餐具      | `PutObject(dirty Mug/Cup/Bowl/Plate/Pot/Pan, SinkBasin)` → `ToggleObjectOn(Faucet)` → 等待                                                            |
| 冷却物体     | `OpenObject(Fridge)` → `PutObject(item, Fridge)` → 等待或 `CloseObject(Fridge)`                                                                        |
| 鸡蛋变熟     | `SliceObject(Egg)` 或 `BreakObject(Egg)` → 得到 `EggCracked` → microwave 或 active stove burner 烹饪                                                      |
