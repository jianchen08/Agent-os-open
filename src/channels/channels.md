# channels 模块文档

## 需求

提供管道与外部系统之间的输入/输出适配层，将外部请求转换为管道 state，将管道结果转换为外部响应格式。

支持三种通道：
1. **CLI 通道**：交互式命令行界面（M1 交付）
2. **API 通道**：RESTful HTTP API（M9 WebSocket 交付后扩展）
3. **WebSocket 通道**：实时双向通信（M9 交付）

## 逻辑

### 适配器架构

```
外部系统 → IInputAdapter.receive() → initial_state → PipelineEngine.run() → final_state → IOutputAdapter.send()
```

- `IInputAdapter`：接收外部请求，返回初始 state 字典
- `IOutputAdapter`：输出管道结果，支持一次性输出（`send`）和流式输出（`send_stream`）

### CLI 通道

CLI 通道提供交互式命令行界面，支持：
- 多轮对话（用户输入 → 管道执行 → 输出 → 等待下次输入）
- 命令模式（斜杠命令）
- 流式输出（rich Console 彩色实时显示）
- 自动确认模式（auto_confirm_runner）

### API 通道

RESTful HTTP API 通道，支持：
- 线程管理（创建/查询/列表）
- 认证鉴权（JWT Token）
- 异步执行与结果轮询

### WebSocket 通道

实时双向通信通道，支持：
- 流式消息推送（LLM 输出实时传输）
- 管道状态事件通知
- 多会话并发管理

## 结构

### 通道层文件

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `__init__.py` | — | 模块入口 |

### CLI 通道（`cli/`）

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `cli/__init__.py` | — | CLI 子模块入口 |
| `cli/cli_main.py` | `CLIApplication`, `main` | CLI 入口（应用初始化 + 主循环） |
| `cli/cli_interaction.py` | — | CLI 交互逻辑（输入处理/输出格式化） |
| `cli/cli_commands.py` | — | CLI 命令处理（斜杠命令） |
| `cli/input_adapter.py` | `CLIInputAdapter` | CLI 输入适配器（stdin 读取） |
| `cli/output_adapter.py` | `CLIOutputAdapter` | CLI 输出适配器（rich Console 输出） |

### API 通道（`api/`）

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `api/__init__.py` | — | API 子模块入口 |
| `api/app.py` | `app` | FastAPI 应用实例 |
| `api/auth.py` | — | 认证中间件 |
| `api/models.py` | — | API 数据模型 |
| `api/routes_auth.py` | — | 认证路由 |
| `api/routes_threads.py` | — | 线程管理路由 |

### WebSocket 通道（`websocket/`）

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `websocket/__init__.py` | — | WebSocket 子模块入口 |
| `websocket/adapter.py` | — | WebSocket 适配器 |
| `websocket/protocol.py` | — | 通信协议定义 |
| `websocket/server.py` | — | WebSocket 服务器 |
| `websocket/session_manager.py` | `SessionManager` | 会话管理器 |

### 类继承关系

```
IInputAdapter (ABC)
├── CLIInputAdapter

IOutputAdapter (ABC)
├── CLIOutputAdapter
```

### 运行方式

```bash
# CLI 通道
$env:PYTHONPATH="src"
python -m channels.cli.cli_main

# API 通道（FastAPI）
$env:PYTHONPATH="src"
python -m channels.api.app
```
