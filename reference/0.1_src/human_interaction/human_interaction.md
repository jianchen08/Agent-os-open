# 人类交互组件

## 需求
### 职责
提供统一的人类交互服务——只要是需要请求人类交互的场景，就必须使用此工具，禁止通过其他方式绕过人类交互。支持三种模式：选择模式（choice）、对话模式（conversation）、通知模式（notification），实现 Agent 与人类的协作交互。

### 对外接口
- 输入：交互请求（审批/对话）
- 输出：交互响应（批准/拒绝/对话结果）

### 依赖
- 外部依赖：无
- 内部依赖：src.api.websocket（通知推送）

## 逻辑
### 流程设计
1. **请求创建**：创建交互请求，判断是否自动审批
2. **通知推送**：通过 WebSocket 推送请求到前端
3. **等待响应**：阻塞等待用户响应或超时
4. **响应处理**：处理用户提交的响应

### 数据流向
```
Agent -> request_interaction() -> 自动审批判断 -> WebSocket 推送 -> 用户响应 -> submit_response() -> 返回结果
```

### 数据模型
#### InteractionType（交互类型）
| 值 | 说明 |
|---|---|
| APPROVAL | 审批模式 |
| CONVERSATION | 对话模式 |

#### InteractionMode（交互模式）
| 值 | 说明 |
|---|---|
| APPROVAL_SIMPLE | 简单审批 |
| APPROVAL_WITH_OPTIONS | 带选项审批 |
| APPROVAL_WITH_EDIT | 可编辑审批 |
| CONVERSATION_FREE | 自由对话 |
| CONVERSATION_GUIDED | 引导对话 |

#### InteractionStatus（交互状态）
| 值 | 说明 |
|---|---|
| PENDING | 待处理 |
| PROCESSING | 处理中 |
| COMPLETED | 已完成 |
| TIMEOUT | 超时 |
| CANCELLED | 已取消 |
| AUTO_APPROVED | 自动批准 |

#### InteractionRequest（交互请求）
| 字段 | 类型 | 说明 |
|---|---|---|
| request_id | str | 请求 ID |
| thread_id | str | 线程 ID |
| interaction_type | InteractionType | 交互类型 |
| mode | InteractionMode | 交互模式 |
| priority | Priority | 优先级 |
| timeout | float | 超时时间（秒） |
| title | str | 标题 |
| description | str | 描述 |
| context | InteractionContext | 交互上下文 |
| approval_options | list[ApprovalOption] | 审批选项 |
| status | InteractionStatus | 状态 |

#### InteractionResponse（交互响应）
| 字段 | 类型 | 说明 |
|---|---|---|
| request_id | str | 请求 ID |
| response_type | ResponseType | 响应类型 |
| selected_option | str | 用户选择的选项文本（label），所见即所得 |
| modified_data | dict | 修改后的数据 |
| reason | str | 原因 |

### 配置设计
#### AutoApprovalConfig（自动审批配置）
| 配置项 | 说明 | 默认值 |
|---|---|---|
| whitelist | 白名单操作 | read_file, list_directory 等 |
| blacklist | 黑名单操作 | delete_file, execute_shell_dangerous 等 |
| auto_approve_threshold | 自动审批风险阈值 | 3 |
| default_timeout | 默认超时时间 | 300 秒 |

### 错误处理
| 异常类型 | 触发场景 |
|---|---|
| InteractionTimeoutError | 交互超时 |
| InteractionCancelledError | 交互取消 |
| InteractionDeniedError | 交互拒绝 |

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### interfaces.py
职责：交互服务接口定义
暴露接口：
- `IInteractionNotifier`：交互通知器接口
  - `notify_request(request: InteractionRequest) -> bool`：通知新请求
  - `notify_cancel(request_id: str, reason: str) -> bool`：通知取消
  - `notify_timeout(request_id: str) -> bool`：通知超时
- `IHumanInteractionService`：人类交互服务接口
  - `request_interaction(request: InteractionRequest) -> str`：发起交互
  - `wait_for_response(request_id: str, timeout: float) -> InteractionResponse`：等待响应
  - `submit_response(request_id: str, response: InteractionResponse) -> bool`：提交响应
  - `cancel_request(request_id: str, reason: str) -> bool`：取消请求
  - `get_request(request_id: str) -> InteractionRequest`：获取请求
  - `get_pending_requests(...) -> list[InteractionRequest]`：获取待处理
  - `approve(request_id: str, option_id: str, reason: str) -> InteractionResponse`：批准
  - `deny(request_id: str, reason: str) -> InteractionResponse`：拒绝
  - `modify_and_approve(request_id: str, modified_data: dict) -> InteractionResponse`：修改后批准
  - `end_conversation(request_id: str, result: str) -> InteractionResponse`：结束对话
  - `should_auto_approve(operation: str, risk_level: int) -> bool`：判断自动审批

#### models.py
职责：交互数据模型
暴露接口：
- `InteractionType`：交互类型枚举
- `InteractionMode`：交互模式枚举
- `InteractionSource`：交互来源枚举
- `InteractionStatus`：交互状态枚举
- `ResponseType`：响应类型枚举
- `Priority`：优先级枚举
- `TimeoutAction`：超时处理策略枚举
- `ApprovalOption`：审批选项
- `InteractionContext`：交互上下文
- `ConversationContext`：对话上下文
- `InteractionRequest`：交互请求
  - `create_approval_request(...) -> InteractionRequest`：创建审批请求
  - `create_conversation_request(...) -> InteractionRequest`：创建对话请求
  - `to_dict() -> dict`：转换为字典
- `InteractionResponse`：交互响应
  - `create_approval_response(...) -> InteractionResponse`：创建审批响应
  - `create_conversation_end_response(...) -> InteractionResponse`：创建对话结束响应
  - `is_approved -> bool`：是否批准
  - `is_denied -> bool`：是否拒绝

#### service.py
职责：交互服务实现
暴露接口：
- `InteractionTimeoutError`：交互超时异常
- `InteractionCancelledError`：交互取消异常
- `InteractionDeniedError`：交互拒绝异常
- `AutoApprovalConfig`：自动审批配置
- `HumanInteractionService`：人类交互服务实现
  - 实现 IHumanInteractionService 所有接口
  - `get_history(limit: int) -> list[InteractionResponse]`：获取历史
  - `cleanup_expired() -> int`：清理过期请求
- `get_human_interaction_service() -> HumanInteractionService`：获取服务单例
- `set_human_interaction_service(service: HumanInteractionService) -> None`：设置服务

#### websocket_notifier.py
职责：WebSocket 通知器实现
暴露接口：
- `WebSocketInteractionNotifier`：WebSocket 交互通知器
  - 实现 IInteractionNotifier 所有接口

### 测试策略
#### 组件测试
- 单元测试：请求/响应模型、自动审批判断
- 集成测试：完整交互流程、超时处理
- 覆盖率要求：核心逻辑 >= 85%

## 实现
-> 见代码文件：src/human_interaction/
