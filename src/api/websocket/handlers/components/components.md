# Components 组件

## 需求

### 职责
提供 WebSocket 处理器的子组件，实现消息验证、持久化、流式处理等核心功能。

### 对外接口
- 输入：消息数据、数据库会话、Agent 循环
- 输出：处理结果

### 依赖
- `src/db`：数据库访问层
- `src/agents`：Agent 执行引擎
- `src/api/websocket`：WebSocket 消息总线

## 逻辑

### 流程设计

#### 消息验证流程
```
原始数据 → 格式检查 → 字段验证 → 返回验证结果
```

#### 消息持久化流程
```
消息数据 → 生成 ID → 构建记录 → 保存数据库 → 返回结果
```

#### 流式处理流程
```
用户输入 → Agent 执行 → 流式事件 → 累积结果 → 返回最终结果
```

### 数据流向
1. UserInputHandler 调用 MessageValidator 验证消息
2. 验证通过后调用 MessagePersistence 保存消息
3. 调用 StreamProcessor 执行 Agent 流式处理
4. 流式结果通过 UnifiedHub 推送到前端

### API设计

#### MessageValidator 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `validate_user_input` | 验证用户输入 | data: dict | ValidationResult |
| `validate_regenerate_request` | 验证重新生成请求 | data: dict | ValidationResult |

#### MessagePersistence 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `save_user_message` | 保存用户消息 | db, thread_id, message_id, content, user_id | bool |
| `save_ai_message` | 保存 AI 消息 | db, thread_id, message_id, content, agent_config, ... | bool |
| `update_message_content` | 更新消息内容 | db, message_id, content | bool |

#### StreamProcessor 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `process` | 处理流式输出 | thread_id, agent_loop, content, enable_thinking, message_id | StreamResult |

### 数据模型

#### ValidationResult
| 字段 | 类型 | 说明 |
|------|------|------|
| `is_valid` | bool | 是否验证通过 |
| `content` | str \| None | 消息内容 |
| `enable_thinking` | bool | 是否启用思考模式 |
| `error_code` | str \| None | 错误码 |
| `error_message` | str \| None | 错误消息 |

#### StreamResult
| 字段 | 类型 | 说明 |
|------|------|------|
| `message_id` | str | 消息 ID |
| `final_content` | str | 最终内容 |
| `thinking_content` | str | 思考内容 |
| `has_error` | bool | 是否有错误 |
| `error_detail` | str \| None | 错误详情 |
| `tool_calls` | list | 工具调用列表 |
| `second_ai_message_id` | str \| None | 第二条 AI 消息 ID |
| `has_tool_calls` | bool | 是否有工具调用 |
| `first_ai_message_content` | str | 第一条 AI 消息内容 |

### 错误处理
- 验证失败返回详细错误信息
- 数据库操作失败自动回滚
- 流式处理异常记录日志并返回错误结果

## 结构

### 文件清单

#### `__init__.py`
职责：组件模块入口，导出所有组件
暴露接口：
- `MessageValidator`：消息验证器
- `MessagePersistence`：消息持久化
- `StreamProcessor`：流式处理器

#### `message_validator.py`
职责：验证 WebSocket 消息格式
暴露接口：
- `ValidationResult(dataclass)`：验证结果
  - `is_valid: bool`：是否有效
  - `content: str | None`：消息内容
  - `enable_thinking: bool`：是否启用思考
  - `error_code: str | None`：错误码
  - `error_message: str | None`：错误消息
- `MessageValidator`：消息验证器类
  - `validate_user_input(data) -> ValidationResult`：验证用户输入
  - `validate_regenerate_request(data) -> ValidationResult`：验证重新生成请求

#### `message_persistence.py`
职责：消息数据库持久化
暴露接口：
- `MessagePersistence`：消息持久化类
  - `save_user_message(db, thread_id, message_id, content, user_id, parent_record_id) -> bool`：保存用户消息
  - `save_ai_message(db, thread_id, message_id, content, agent_config, ...) -> bool`：保存 AI 消息
  - `update_message_content(db, message_id, content) -> bool`：更新消息内容
  - `_get_next_sequence(db, thread_id) -> int`：获取下一个序列号

#### `stream_processor.py`
职责：处理 Agent 流式输出
暴露接口：
- `StreamResult(dataclass)`：流式处理结果
  - `message_id: str`：消息 ID
  - `final_content: str`：最终内容
  - `thinking_content: str`：思考内容
  - `has_error: bool`：是否有错误
  - `error_detail: str | None`：错误详情
  - `tool_calls: list`：工具调用列表
  - `second_ai_message_id: str | None`：第二条消息 ID
  - `has_tool_calls: bool`：是否有工具调用
  - `first_ai_message_content: str`：第一条消息内容
- `StreamProcessor`：流式处理器类
  - `process(thread_id, agent_loop, content, enable_thinking, message_id) -> StreamResult`：处理流式输出
  - `_extract_reasoning_content(msg) -> str | None`：提取思考内容
  - `_get_pure_tool_result(thread_id, tool_call_id, fallback_content) -> str`：获取纯净工具结果

### 测试策略
- 单元测试：各组件的核心方法
- 集成测试：与数据库、Agent 的交互
- Mock 策略：Mock 数据库会话、Agent 循环

## 实现
→ 见代码文件 `src/api/websocket/handlers/components/*.py`
