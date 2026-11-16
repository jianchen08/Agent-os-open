# Speed-Dodge 调研问题清单（research_questions.md）

## 调研类型
technology（技术调研）：Godot 4 引擎 + Speed Dodge 类小游戏实现方案

## 调研目标
为 Godot 4 实现的躲障碍休闲小游戏产出工程级最佳实践调研报告，覆盖项目结构、玩家控制、障碍生成、碰撞、物理层、难度曲线、HUD、失败重试 8 个维度。

## 核心问题列表（8 个调研点 × 子问题）

### Q1. Godot 4 标准项目结构
- project.godot 哪些字段是必需的（config_version、application/run/main_scene、input map）
- main.tscn 作为入口场景的规范写法
- autoload 单例（Project Settings > Autoload）注册方式与典型用途（GameManager/ScoreManager）

### Q2. 玩家移动实现
- CharacterBody2D vs Area2D 适用边界（受物理引擎 vs 信号驱动）
- InputMap（ui_left/ui_right/ui_up/ui_down 还是自定义动作名）的项目实践
- 移动边界 clamp 的两种范式（clamp() 函数 vs position clamp 节点 / CanvasLayer 边距）
- 速度归一化（避免对角线加速）

### Q3. 障碍生成
- 模式 A：PackedScene + instantiate() 的代码实例化（适用场景）
- 模式 B：纯代码 Node2D + _draw() 自绘（适用场景）
- 性能、灵活度、调试成本对比；休闲小游戏推荐选哪种

### Q4. 碰撞检测
- CollisionShape2D + Area2D 信号（area_entered/exited）vs CharacterBody2D.move_and_slide()
- 物理引擎层掩码 vs 信号回调的语义差异
- 躲障碍游戏应选哪种（玩家主动移动 + 障碍碰撞判定）

### Q5. 物理层规划
- Godot 4 collision_layer / collision_mask 位掩码机制（32 层）
- 本游戏层规划：Player 层、Obstacle 层、它们的 mask 交互位
- 命名约定（layer 1 = player, layer 2 = obstacle）

### Q6. 难度递增曲线
- 线性 / 指数 / 阶梯三种典型曲线数学形式与体验感
- 上限封顶（cap）实现（min(value, MAX)）
- 休闲小游戏推荐曲线（阶梯 + 软上限）

### Q7. 计时器与 HUD
- Label + _process(delta) 累加 vs Timer 节点（timeout 信号）
- 适用场景：连续计数（存活秒数）vs 离散触发（生成间隔）
- 推荐：存活秒数走 _process，生成间隔走 Timer

### Q8. 失败流程与重试
- 方案 A：get_tree().reload_current_scene() 整场景重载
- 方案 B：信号触发游戏状态机重置（GameManager.state = GAME_OVER → MENU）
- 方案 C：UI 遮罩 + 局部清理
- 优缺点对比与休闲游戏推荐

## 搜索策略
- 优先级：Godot 官方文档（docs.godotengine.org）> GitHub 官方 demo > 权威博客
- 关键文档：tutorials / best_practices / step_by_step
- 关键词：英文为主（"Godot 4 CharacterBody2D move_and_slide"、"Godot 4 collision layer mask" 等）
