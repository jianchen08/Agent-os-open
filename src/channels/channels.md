# channels 模块文档

> 数据准确性说明：本文档基于 `src/channels/` 实际代码核对。

## 需求

提供管道与外部系统之间的输入/输出适配层，将外部请求转换为管道 state，将管道结果转换为外部响应格式。

支持以下通道（所有 IM 通道共用 `gateway/` 网关层）：

1. **CLI 通道**（`cli/`）：交互式命令行界面
2. **HTTP API 通道**（`api/`）：RESTful HTTP API（FastAPI）
3. **WebSocket 通道**（`websocket/`）：实时双向通信（Web UI 后端）
4. **网关层**（`gateway/`）：钉钉 / 飞书 / QQ / 企微 共用的鉴权、消息标准化、会话路由
5. **IM 适配器**：`dingtalk/`（钉钉）/ `feishu/`（飞书）/ `qq/`（QQ）/ `wecom/`（企微）

> 新增一个 IM 平台，只需在 `src/channels/<platform>/` 下加一个 `adapter.py`（实现 `BaseComboAdapter`），业务逻辑零改动。

## 逻辑

### 适配器架构

```
外部系统 → IInputAdapter.receive() → initial_state → PipelineEngine.run() → final_state → IOutputAdapter.send()
```

- `IInputAdapter`（`input_adapter.py`）：接收外部请求，返回初始 state 字典
- `IOutputAdapter`（`output_adapter.py`）：输出管道结果，支持一次性输出（`send`）和流式输出（`send_stream`）
- `BaseComboAdapter`（`base_combo_adapter.py`）：输入/输出合一的组合适配器基类，IM 通道多基于它实现

### CLI 通道（`cli/`）

交互式命令行界面，支持多轮对话、斜杠命令、rich Console 流式输出、自动确认模式。

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `cli/cli_main.py` | `CLIApplication`, `main` | CLI 入口（应用初始化 + 主循环），对应 `pyproject.toml` 的 `agent-os` 命令 |
| `cli/cli_interaction.py` | — | CLI 交互逻辑（输入处理 / 输出格式化） |
| `cli/cli_commands.py` | — | CLI 命令处理（斜杠命令） |
| `cli/input_adapter.py` | `CLIInputAdapter` | CLI 输入适配器（stdin 读取） |
| `cli/output_adapter.py` | `CLIOutputAdapter` | CLI 输出适配器（rich Console 输出） |

### HTTP API 通道（`api/`）

RESTful HTTP API 通道（FastAPI），含 21 个 `routes_*.py` 路由模块、JWT 认证、限流。

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `api/app.py` | `create_app`, `app` | FastAPI 应用实例与工厂 |
| `api/auth.py` | — | 认证中间件 |
| `api/deps.py` | — | FastAPI 依赖注入 |
| `api/models.py` | — | API 数据模型 |
| `api/rate_limiter.py` | — | 限流器 |
| `api/routes_*.py` | — | 21 个路由模块（agents / artifacts / auth / tasks / themes / threads / tools / workspaces ...） |

### WebSocket 通道（`websocket/`）

实时双向通信通道（Web UI 后端），负责流式消息推送、管道状态事件、多会话并发。

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `websocket/app_factory.py` | `create_app` 等 | **后端主入口**（`python -m channels.websocket.app_factory`），FastAPI + WebSocket 应用工厂 |
| `websocket/ws_handler.py` | — | WebSocket 连接与消息处理 |
| `websocket/stream_handler.py` | — | 流式响应处理 |
| `websocket/static_files.py` | — | 静态文件挂载 |

> 注意：早期文档提到的 `websocket/server.py` / `websocket/protocol.py` / `websocket/session_manager.py` 已不存在，实际文件见上表。

### 网关层（`gateway/`）

钉钉 / 飞书 / QQ / 企微 共用的网关层（鉴权、消息标准化、会话路由）。

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `gateway/channel_gateway.py` | `ChannelGateway` | 网关核心（协议解析 / 鉴权 / 限流 / 路由） |
| `gateway/message_normalizer.py` | — | 消息格式标准化 |
| `gateway/session_bridge.py` | — | 会话桥接 |
| `gateway/unified_types.py` | — | 统一消息类型定义 |

### IM 适配器

各 IM 平台适配器（均含 `adapter.py`，基于 `BaseComboAdapter`）：

| 目录 | 平台 | 关键文件 |
|------|------|----------|
| `dingtalk/` | 钉钉 | `adapter.py` / `stream_client.py` |
| `feishu/` | 飞书 | `adapter.py` / `card_builder.py` / `stream_client.py` |
| `qq/` | QQ（OneBot） | `adapter.py` / `onebot_client.py` / `helpers.py` |
| `wecom/` | 企微 | `adapter.py` / `crypto.py`（加解密）/ `stream_client.py` |

### 类继承关系

```
IInputAdapter (ABC)              # input_adapter.py
└── CLIInputAdapter              # cli/input_adapter.py

IOutputAdapter (ABC)             # output_adapter.py
└── CLIOutputAdapter             # cli/output_adapter.py

BaseComboAdapter                 # base_combo_adapter.py（输入/输出合一，IM 通道基类）
├── DingTalkAdapter              # dingtalk/adapter.py
├── FeishuAdapter                # feishu/adapter.py
├── QQAdapter                    # qq/adapter.py
└── WeComAdapter                 # wecom/adapter.py
```

### 运行方式

```bash
# Web UI / WebSocket 后端（主入口，docker-compose 也指向它）
PYTHONPATH=src python -m channels.websocket.app_factory

# CLI 通道
PYTHONPATH=src python -m channels.cli.cli_main
```
