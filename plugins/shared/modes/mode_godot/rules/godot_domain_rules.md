# Godot 领域规则

> 与 `coding_domain_rules.md` 配套使用：通用工程原则（需求第一/最小输出/错误处理/
> Bug 修复安全）见该文件，本文件只写 Godot 域专属契约。

---

## 一、场景与脚本即文本契约

### 1.1 .tscn 段式格式 [error]

- 场景文件是文本格式：`[node name="X" type="Y" parent="..."]` 段落定义节点，属性为 `key = value` 行（position 等是 `Vector2(...)` / `Vector3(...)`）
- 新增节点必须补齐段头与 parent，缺 parent 即挂错树；改数值属性只改对应行
- 禁止顺手重排、重格式化或"优化"未涉及的段落与脚本

### 1.2 先读原文再改 [error]

- 修改前必须 file_read 场景与脚本原文，确认节点结构与变量命名——禁止凭空猜节点路径或属性名
- 脚本遵循 GDScript 4 语法（`@export` / `@onready`、`signal.connect`、类型标注）；禁止混用 3.x 旧 API

### 1.3 消息引用是权威定位 [error]

- 消息中 `<reference source="godot">` 是用户在编辑器中选中的节点（含名称/类型/路径），为修改目标的权威定位；不得改写其他节点

---

## 二、godot_run 执行面纪律

- `method` 为 `'<group>.<command>'`（如 `node.add` / `scene.save`）；不确定命令名或参数时先 `engine.search` / `engine.class_info` / `engine.commands` 查证，禁止盲猜
- 编辑器改动走 UndoRedo 可撤销；写脚本后必须重读核对再进入验证
- 结果带 `editor_unreachable` 时 godot_run 会**自动拉起**工程编辑器（status 确认 closed 才拉，绝不启动第二个实例）并重试；自动拉起失败按返回指引处理（设置 GODOT_EDITOR_BIN 或请用户打开）
- 3D 放置必须锚定实测 bounds（`node.get global_position` / `get_aabb`）回读校验，不得只信截图

---

## 三、验证优先于目测 [error]

- 修改完成后必须用 godot_run 做运行验证（`scene.play` → 观察报错/输出 → 停止）；"看起来对"不是验证
- GDScript 纯逻辑函数：项目已有测试设施（GUT/gdUnit）则随栈补测试；没有则 godot_run 运行验证兜底，禁止为验证临时引入测试框架

---

## 四、项目接入契约

- Godot 项目**固定安装两个宿主插件**：`addons/agentos`（宿主桥：选中引用推送 + :9600 连接器面）与 `addons/godot_mcp`（godot-mcp-go 执行面通道，对应 godot_run 工具）。安装配方唯一真值 = `config/tools/project_create.yaml`
- 项目缺插件（外部导入/历史项目）：重跑 `project_create(path=<项目路径>)` 幂等补装，禁止手工从别处拷贝拼装
- 机器接线只剩 `GODOT_MCP_BIN`（godot-mcp-go 二进制位置，.env 操作员级）；**执行面目标工程由当前任务 workspace 自动路由**（编辑器打开工程即接线），agent 禁止改写 .env
