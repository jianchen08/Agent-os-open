---
name: Godot 编码
description: Godot 4 技术栈编码技能（规范+流程）。含接入自愈、场景/脚本修改流程、godot_run 运行验证、常用命令组。用于 Godot 游戏项目的编码、调试与验证阶段。
---

# Godot 编码

> 领域契约（.tscn 格式/引用语义/验证铁律）见常驻的 godot_domain_rules，本技能写执行流程。

## 第 0 步：接入自愈（编码前必做）

1. 确认项目根存在 `project.godot`；没有则先 `project_create(goal=<标题>, path=<项目路径>, project_type="godot")` 脚手架
2. 确认两个宿主插件在位：`addons/agentos/plugin.cfg` 与 `addons/godot_mcp/plugin.cfg`
3. 缺任一 → `project_create(goal=<标题>, path=<项目路径>)` 幂等补装（auto 识别，禁止手工拼装）
4. godot_run 探活：调 `engine.commands`；编辑器未开时 godot_run **自动拉起**工程编辑器（GODOT_EDITOR_BIN）并重试，无需手动干预；拉起失败按返回指引处理（禁止第二个编辑器实例）；插件刚装好但编辑器未重开时提示用户重启编辑器使插件生效

## 第 1 步：定位（先读原文，编码前必做）

1. 消息含 `<reference source="godot">` → 以其中的节点（名称/类型/路径）为修改目标权威定位
2. file_read 目标场景（.tscn）与脚本（.gd）原文，确认节点结构、属性名、变量命名与信号连接
3. 需要引擎 API 事实时用 godot_run 查证：`engine.search {query}` 模糊搜、`engine.class_info {class}` 类详情——禁止凭记忆猜 API 形参

## 第 2 步：修改

- .tscn：按段式格式改——节点段落 `[node name="X" type="Y" parent="..."]`，属性 `key = value` 行；新增节点补齐段头与 parent；只动需求要求的部分
- .gd：GDScript 4 语法（`@export`/`@onready`、`signal.connect`、类型标注）；改逻辑先读全文再改，不做无关重排
- project.godot：改配置（输入映射/自启动/插件开关）直接改对应段；启用插件写 `[editor_plugins]` 的 `enabled=PackedStringArray(...)`

## 第 3 步：运行验证（必做，"看起来对"不是验证）

1. `scene.save`（若有改动）→ `scene.play` → 观察报错/输出 → `scene.stop`（或按结果指引）
2. 编辑器内改动验证：重读改动文件 + `engine.class_info` 抽查结构；3D 放置回读 `global_position`/`get_aabb` 校验，不只信截图
3. 独立调试构建验证用 `game:true` 路由（需 `godot_mcp/runtime/direct_server`）
4. GDScript 纯逻辑函数：项目已有 GUT/gdUnit 则随栈补测试；没有则以上述运行验证兜底，禁止临时引入测试框架

## 第 4 步：自检收尾

1. 重读全部改动文件核对（.tscn 段式/parent 正确、.gd 语法、project.godot 段完整）
2. 汇总：改了哪些文件与节点、验证方式与结果、已知问题——写入执行报告后 task_evaluate

## 红线

- 禁止凭空猜节点路径/属性名/API 形参（先读原文、先查证）
- 禁止改写 `<reference>` 之外的节点
- 禁止跳过运行验证直接评估
- 禁止手工拷贝宿主插件（用 project_create 幂等补装）、禁止改写 .env 机器接线
