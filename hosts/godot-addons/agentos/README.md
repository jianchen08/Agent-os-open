# AgentOS Host - Godot 4 插件

灵汐 AgentOS（v0.2）的 Godot 4 编辑器宿主接入 Demo。

本插件作为 **对端**（peer），在 Godot 4 编辑器内启动一个极简 TCP/HTTP 服务
（监听 `127.0.0.1:9600`），供仓内 `plugins/shared/system/connectors/creative/game_engine.py`
连接器轮询调用：探活、取场景信息、取选中节点等。

> 连接器侧（Python）无需改动，已通过 HTTP 连接 `http://127.0.0.1:9600`。

## 设计说明

- Godot 4 没有内置 HTTP server 类，本插件基于 `TCPServer` 手写了一个最简
  HTTP/1.0 解析（仅解析 method + path + body，无 keep-alive），足以支撑连接器探活与取上下文。
- 仅在编辑器内运行（`@tool extends EditorPlugin`），不会进入打包后的游戏。
- `/execute` 出于安全考虑 **不真正执行任意命令**，仅回显收到的命令与参数（最小 Demo）。

## 端点

| 方法 | 路径            | 说明                                                  |
| ---- | --------------- | ----------------------------------------------------- |
| GET  | `/health`       | 探活，返回 `{ "status":"ok", "version":"0.2.0" }`      |
| GET  | `/status`       | 连接器探活主入口，返回 `{ version, engine, engine_version, project }` |
| GET  | `/context`      | 字段对齐 `game_engine.get_context()` 期望；含 `selection_detail`（name/type/path/preview_kind） |
| GET  | `/scene`        | 当前编辑场景信息（名称/路径/根节点类/节点数）            |
| GET  | `/selection`    | 当前选中节点名称列表                                    |
| GET  | `/selection/preview?index=N` | 第 N 个选中节点的预览 PNG（≤512px）：贴图节点用贴图缩略图，其余截编辑器 2D/3D 视口；无预览 404 |
| GET  | `/capabilities` | 支持的能力列表                                          |
| POST | `/execute`      | 最小实现：回显 `{ command, args }`，不真正执行           |
| GET  | `/screenshot` `/assets` `/play` `/stop` | 占位端点，返回 stub，保持接口形状兼容 |

## 选中推送（→ AgentOS pipeline_godot_context 插件）

除被动 HTTP 服务外，插件以**事件驱动**向 AgentOS 推送选中状态（无轮询）：

- `EditorSelection.selection_changed` 信号（防抖 300ms）→ POST
  `http://127.0.0.1:9100/ext/pipeline_godot_context/selection`（内核默认端口 9100）；
- payload：`{ type, engine, engine_version, project, scene, items:[{name,type,path,preview_kind}], signature, ts }`，
  `type` 为 `selection`（选中变化，含清空）/ `heartbeat`（5s 心跳）/ `offline`（插件退出）；
- 推送失败静默（AgentOS 未启动不报错）；目标端点可在 Project Settings `agentos/push_endpoint` 覆盖。

AgentOS 侧由 `plugins/shared/pipeline/input/godot_context/` 插件接收：
转发前端（聊天框引用卡片实时镜像）+ 在用户消息后注入 `<reference>` 引用消息。

### `/context` 返回字段（对齐 game_engine.py）

```jsonc
{
  "active_scene": "res://main.tscn",
  "selected_object": "Player",
  "scene_name": "Main",
  "engine_version": "4.2.1",
  "selected_objects": ["Player", "Camera2D"]
}
```

## 安装与运行

1. 将 `hosts/godot-addons/agentos/` 整个目录复制（或软链）到 Godot 项目的
   `addons/agentos/` 下：

   ```
   <你的 Godot 项目>/
     addons/
       agentos/
         plugin.cfg
         agentos_connector.gd
   ```

2. 在 Godot 4 编辑器中：**项目 (Project) → 项目设置 (Project Settings) → 插件 (Plugins)**
   勾选启用 **AgentOSConnector**。

3. 启用后编辑器输出区会打印：
   ```
   [AgentOS] 宿主服务已启动: http://127.0.0.1:9600
   ```

## 可选项目设置

在 `project.godot` 或 Project Settings 中可覆盖默认端口与地址：

```ini
[agentos]
[agentos/host]
port=9600
address="127.0.0.1"
```

## 验证

```bash
curl http://127.0.0.1:9600/health
# -> {"status":"ok","version":"0.2.0"}

curl http://127.0.0.1:9600/status
# -> {"status":"ok","version":"0.2.0","engine":"godot","engine_version":"4.x","project":"..."}

curl http://127.0.0.1:9600/scene
# -> {"name":"Main","path":"res://main.tscn","root":"Node2D","node_count":7}
```

## 文件

- `plugin.cfg` — Godot EditorPlugin 声明
- `agentos_connector.gd` — `EditorPlugin` 实现（TCP/HTTP 服务）

## 与灵汐连接器的关系

```
┌──────────────┐  HTTP (9600)        ┌─────────────────────────┐
│ 灵汐 Python  │  <───────────────── │ 本插件 (Godot 4 内)      │
│ game_engine  │  /health /status    │ EditorPlugin + TCPServer │
│ 连接器        │  /context /scene    │                          │
└──────────────┘                     └─────────────────────────┘
```

- 连接器（Python）：`plugins/shared/system/connectors/creative/game_engine.py`
- 本插件（对端）：`hosts/godot-addons/agentos/agentos_connector.gd`
