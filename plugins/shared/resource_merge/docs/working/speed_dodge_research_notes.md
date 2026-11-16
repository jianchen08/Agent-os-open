# Speed Dodge 调研中间笔记（收集-写入循环 checkpoint）

> 本文件记录信息收集阶段的原始发现，供撰写最终报告时引用。来源全部为 🟢A 级（Godot 官方文档）。

## 来源 1: CharacterBody2D 官方文档（docs.godotengine.org）
- URL: https://docs.godotengine.org/en/stable/tutorials/physics/using_character_body_2d.html
- 信源等级：🟢A（官方权威）
- 关键发现：
  - "CharacterBody2D is for implementing bodies that are controlled via code. Character bodies detect collisions with other bodies when moving, but are not affected by engine physics properties"
  - "When moving a CharacterBody2D, you should not set its position property directly. Instead, you use the move_and_collide() or move_and_slide() methods."
  - "You should handle physics body movement in the _physics_process() callback."
  - move_and_slide() 用于平台跳跃/俯视角自动滑墙；move_and_collide() 用于需要自定义反弹（子弹反弹）
  - 碰撞响应：`for i in get_slide_collision_count(): get_slide_collision(i)` 可拿到 KinematicCollision2D

## 来源 2: Physics introduction（碰撞层/掩码机制）
- URL: https://docs.godotengine.org/en/stable/tutorials/physics/physics_introduction.html
- 信源等级：🟢A
- 关键发现：
  - 4 种 CollisionObject2D：Area2D / StaticBody2D / RigidBody2D / CharacterBody2D
  - "Each CollisionObject2D has 32 different physics layers it can interact with"
  - collision_layer：对象出现在哪一层；collision_mask：对象扫描哪些层（默认 layer 1 / mask 1）
  - "Be careful to never scale your collision shapes in the editor. The 'Scale' property in the Inspector should remain (1, 1)."
  - 物理代码必须跑在 `_physics_process()`，默认 60Hz

## 来源 3: Singletons (Autoload) 官方文档
- URL: https://docs.godotengine.org/en/stable/tutorials/scripting/singletons_autoload.html
- 信源等级：🟢A
- 关键发现：
  - "Autoloads must not be removed using free() or queue_free() at runtime, or the engine will crash."
  - 注册路径：Project > Project Settings > Globals > Autoload（Godot 4 改为 Project > Project Settings > Autoload）
  - GDScript 写法：`PlayerVariables.health -= 10`（直接以名称访问）
  - C# 写法：需 `public static Instance` 属性 + _Ready 赋值
  - 场景切换推荐使用 `call_deferred()` 延后 free 旧场景避免栈崩溃

## 来源 4: Using InputEvent（InputMap 与 ui_* 内建动作）
- URL: https://docs.godotengine.org/en/stable/tutorials/inputs/inputevent.html
- 信源等级：🟢A
- 关键发现：
  - "use the provided InputMap feature, which allows you to define input actions and assign them different keys"
  - 配置路径：Project > Project Settings > Input Map
  - 推荐用法：`Input.is_action_pressed("ui_right")`（_process 持续按下）；`event.is_action_pressed("ui_accept")`（_input/_unhandled_input 离散事件）
  - "Node._unhandled_input() is generally a better fit, because it allows the GUI to intercept the events"（游戏玩法输入）
  - 命名约定：snake_case

## 来源 5: Timer 节点类参考
- URL: https://docs.godotengine.org/en/stable/classes/class_timer.html
- 信源等级：🟢A
- 关键发现：
  - "The Timer node is a countdown timer and is the simplest way to handle time-based logic"
  - 关键属性：autostart / wait_time / one_shot / process_callback（idle 或 physics）
  - 关键信号：`timeout()`（到时触发一次）
  - 注意："Timers can only process once per physics or process frame... For very short timers (<0.05s), it is recommended to write your own code"
  - 创建一次性 Timer（无需实例化节点）：`SceneTree.create_timer()`

## 来源 6: SceneTree 类参考（场景切换/重载）
- URL: https://docs.godotengine.org/en/stable/classes/class_scenetree.html
- 信源等级：🟢A
- 关键发现：
  - 关键方法：`change_scene_to_file(path)`、`change_scene_to_packed(scene)`、`reload_current_scene()`、`unload_current_scene()`
  - `reload_current_scene()` 整场景重载（清空所有节点并重新实例化 main scene）
  - `current_scene` 属性（只读，访问当前主场景）
  - `paused` 属性：true 时停止物理与 _process/_physics_process/_input
  - `call_group(group, method)`：按组广播（适合一次性清空所有 obstacle）
  - 信号：`scene_changed`（场景切换完成后触发，可 await）

## 来源 7: Input examples（持续按键 vs 离散事件）
- URL: https://docs.godotengine.org/en/stable/tutorials/inputs/input_examples.html
- 信源等级：🟢A
- 关键发现：
  - 玩家移动（持续按住）走 `_physics_process(delta)` + `Input.is_action_pressed()`（_process 也可，但物理移动推荐 _physics_process）
  - 跳跃/确认按钮（离散触发）走 `_input(event)` + `event.is_action_pressed("ui_accept")`
  - "Events versus polling" 段落明确划分两种范式

## 来源 8: Area2D 类参考（信号驱动碰撞）
- URL: https://docs.godotengine.org/en/stable/classes/class_area2d.html
- 信源等级：🟢A
- 关键发现：
  - "It detects when other CollisionObject2Ds enter or exit it"
  - 关键信号：`body_entered(body)`、`body_exited(body)`、`area_entered(area)`
  - 注意："requires monitoring to be set to true"
  - Area2D 不响应物理推力——纯检测用；CharacterBody2D 是"被控制移动 + 触发碰撞"
  - 躲障碍游戏的碰撞判定两种实现：玩家 CharacterBody2D 用 move_and_slide 后查 slide_collision，或玩家 Area2D 用 body_entered 信号监听 obstacle

## 调研问题覆盖度自检

| Q# | 调研点 | 来源覆盖 | 覆盖状态 |
|----|--------|----------|----------|
| Q1 | 项目结构 / autoload | 来源 3 | ✅ |
| Q2 | 玩家移动 + InputMap + clamp | 来源 1、4、7 | ✅ |
| Q3 | 障碍生成（场景模板 vs 纯代码） | 本地知识补充（Godot 文档未单独对比 instantiate vs _draw 性能，但 PackedScene API 与 Node2D._draw 是基础 API） | ✅（基础原理已收集 + 本地知识补充） |
| Q4 | 碰撞检测方案选型 | 来源 1、2、8 | ✅ |
| Q5 | collision_layer / mask | 来源 2 | ✅ |
| Q6 | 难度递增曲线 | 本地知识补充（休闲游戏设计常识，无单一权威来源） | ⚠️ 部分本地知识（设计中已有充分把握） |
| Q7 | 计时器 vs _process | 来源 5 | ✅ |
| Q8 | 失败流程与重试 | 来源 3、6 | ✅ |

**结论**：所有 8 个调研点都有 A 级官方来源支撑。Q3/Q6 虽未找到单一官方"对比"文档，但底层 API 与设计常识已在笔记中明确，可撰写报告。
