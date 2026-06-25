# WebSocket 通道（M9）

## 需求

为 Agent OS 提供基于 WebSocket 的实时双向通信通道，支持前端与后端的流式交互。

### 核心能力

1. **WebSocket 服务器**：基于 aiohttp 的轻量级 WebSocket 服务器
2. **会话管理**：连接注册/注销/查找/广播/断线重连恢复
3. **通信协议**：定义前后端统一的事件类型和消息信封格式
4. **输入适配**：从 WebSocket 接收前端消息，转换为管道初始 state
5. **输出适配**：将管道结果和流式 chunk 通过 WebSocket 推送回前端
6. **控制信号**：支持停止生成（stop_generation）和审批（resume_action）

## 逻辑

### 通信协议

```
前端 ←→ WebSocket ←→ WebSocketServer ←→ SessionManager
                                              ↕
                              WebSocketInputAdapter → 管道 engine
                              WebSocketOutputAdapter ← 管道 engine
```

### 事件分类

| 分类 | 事件类型 | 方向 |
|------|---------|------|
| 连接 | connection_confirmation | 后端→前端 |
| 流式输出 | stream_start / stream_chunk / stream_end | 后端→前端 |
| 思考过程 | thinking_start / thinking_chunk / thinking_end | 后端→前端 |
| 工具执行 | execution_start / execution_progress / execution_done | 后端→前端 |
| 管道状态 | pipeline_start / pipeline_end / iteration_start / iteration_end | 后端→前端 |
| 错误 | plugin_error / pipeline_error | 后端→前端 |
| 控制 | user_input / stop_generation / resume_action | 前端→后端 |

### 消息信封格式

```json
{
  "type": "stream_chunk",
  "data": { ... },
  "timestamp": "2026-04-11T12:00:00+00:00",
  "request_id": "uuid"
}
```

### 流式输出时序

```
stream_start → stream_chunk(1) → stream_chunk(2) → ... → stream_end
```

### 会话恢复

前端通过 `thread_id` 标识会话，重连时传入相同 `thread_id`：
1. 服务器注销旧会话
2. 注册新 WebSocket 连接
3. `thread_id` 映射到新 session_id

## 结构

### 文件清单

| 文件 | 职责 |
|------|------|
| `protocol.py` | EventType 枚举、ControlCommand 枚举、EventEnvelope 信封、数据类（StreamStartData 等）、create_event 工厂函数 |
| `session_manager.py` | SessionManager — 会话注册/注销/查找/广播/超时清理 + SessionInfo 数据类 + WebSocketConnection 协议 |
| `server.py` | WebSocketServer — 基于 aiohttp 的 WebSocket 服务器，连接处理/消息分发/健康检查 |
| `adapter.py` | WebSocketInputAdapter / WebSocketOutputAdapter / WebSocketAdapter — 管道输入/输出适配 |
| `__init__.py` | 公共 API 导出 |
| `README.md` | 本文档 |

### 依赖

| 依赖 | 用途 |
|------|------|
| `aiohttp` | WebSocket 服务器和 HTTP 处理 |
| `channels/input_adapter.py` | IInputAdapter 抽象基类 |
| `channels/output_adapter.py` | IOutputAdapter 抽象基类 |
| `pipeline/types.py` | StateKeys 常量 |

### 测试文件

| 文件 | 测试数 | 覆盖范围 |
|------|--------|---------|
| `tests/test_websocket.py` | 65 | Protocol(25) + SessionManager(16) + Server(7) + InputAdapter(6) + OutputAdapter(7) + Adapter(4) |

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/ws` | GET | WebSocket 连接（新会话） |
| `/ws/{thread_id}` | GET | WebSocket 连接（重连恢复） |
| `/health` | GET | 健康检查（返回活跃会话数） |

### 当前限制（M9 阶段）

1. 前端 UI 尚未实现，当前仅提供后端 WebSocket 服务
2. 思考过程事件（thinking_*）已定义但尚未在 LLMCore 中集成
3. 无消息持久化，断线期间的消息会丢失
4. 无身份认证机制
