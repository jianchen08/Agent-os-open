# Godot 项目宿主插件（canonical 安装源）

Godot 项目接入灵汐 AgentOS **固定安装两个编辑器插件**，本目录是两者的唯一安装源。
正常情况下无需手工安装：`project_create` 工具按项目类型配置
（`config/tools/project_create.yaml`）在项目出生时自动复制并启用；已存在的
项目对同路径重跑 `project_create` 即幂等补装。

| 目录 | 装到 `<项目>/addons/` 下 | 职责 |
|------|------------------------|------|
| `agentos/` | `addons/agentos/` | AgentOS 宿主桥：HTTP 服务（127.0.0.1:9600，供 game_engine 连接器探活/取上下文）+ 选中状态事件推送（→ `pipeline/input/godot_context/`，驱动聊天引用）。详见 [agentos/README.md](agentos/README.md) |
| `godot_mcp/` | `addons/godot_mcp/` | godot-mcp-go 的编辑器执行面通道：场景/节点/脚本/运行/调试等命令经 WebSocket 由 MCP 服务驱动，AgentOS 侧对应 `godot_run` 工具（`plugins/shared/tools/external_mcp/godot_mcp/`） |

## 机器接线与执行面路由

- `GODOT_MCP_BIN`：godot-mcp-go v0.9.0 可执行文件路径（`.env`，机器必填项）
- `GODOT_EDITOR_BIN`（可选）：Godot 编辑器可执行文件——工程编辑器未开时 `godot_run` 自动拉起（status 确认 closed 才拉，绝不启动第二实例）；未设置则回退 PATH 的 godot/godot4，都没有时返回设置指引
- **执行面目标工程按任务 workspace 自动路由**（2026-09-03 裁定）：`godot_run` 调用时
  解析当前 workspace 指向的工程（`project` 参数 > workspace/祖先的 project.godot >
  worktree 还原主工程），按需启动对应的 godot-mcp-go serve 进程——把工程在编辑器里
  打开即完成接线，无需改任何 env（GODOT_PROJECT_DIR 已退役）。

变量含义见 `.env.example`；GODOT_MCP_BIN 缺失时 godot_run 调用即失败并指名变量。
