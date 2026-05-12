# Schemas 组件

## 需求

### 职责
定义 API 请求和响应的数据模型，提供数据验证和序列化功能。

### 对外接口
- 输入：原始请求数据（JSON 字典）
- 输出：验证后的 Pydantic 模型实例

### 依赖
- Pydantic 库
- Python 标准库（datetime、typing、uuid）

## 逻辑

### 流程设计
```
原始 JSON 数据 → Pydantic 模型验证 → 类型转换 → 业务层使用
```

### 数据流向
1. 客户端发送 JSON 请求数据
2. FastAPI 自动调用 Pydantic 模型进行验证
3. 验证通过后转换为模型实例
4. 业务层使用模型实例访问数据
5. 响应数据通过模型序列化返回

### 数据模型

#### 认证模型（auth.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `RegisterRequest` | 注册请求 | username, email, password |
| `LoginRequest` | 登录请求 | username, password |
| `RefreshTokenRequest` | 刷新 Token | refresh_token |
| `LogoutRequest` | 登出请求 | refresh_token, logout_all_devices |
| `TokenResponse` | Token 响应 | access_token, refresh_token, token_type, expires_in |
| `UserResponse` | 用户信息响应 | id, username, email, role, created_at |

#### Agent 模型（agents.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `AgentCreateRequest` | 创建 Agent | name, model, system_prompt, tool_names, max_iterations, timeout |
| `AgentUpdateRequest` | 更新 Agent | name, model, system_prompt, tool_names, status |
| `AgentResponse` | Agent 响应 | id, name, model, system_prompt, tool_names, status, created_at |
| `AgentListResponse` | Agent 列表响应 | items, total, page, page_size |

#### 任务模型（tasks.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `PhaseStatusInfo` | 阶段状态 | status, start_time, end_time, output, error |
| `TaskPhaseStatusResponse` | 任务阶段状态响应 | task_id, current_phase, task_status, phases |
| `AcceptanceCriterionStatus` | 验收标准状态 | id, description, type, is_red_line, weight, status |
| `TaskACListResponse` | 任务 AC 列表响应 | task_id, total, passed, failed, pending, acceptance_criteria |
| `ACEvaluateRequest` | AC 评估请求 | evidence |
| `ACEvaluationResult` | AC 评估结果 | task_id, ac_id, passed, score, feedback, details |
| `TaskACResultResponse` | 任务 AC 结果响应 | task_id, ac_id, status, evaluation_result |

#### 线程模型（thread.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `ThreadCreateRequest` | 创建线程请求 | intent, agent_id, metadata |
| `ThreadUpdateRequest` | 更新线程请求 | intent, agent_id, metadata |
| `ThreadResponse` | 线程响应 | thread_id, current_state, intent, created_at, updated_at |
| `ThreadDetailResponse` | 线程详情响应 | 继承 ThreadResponse + messages |
| `MessageResponse` | 消息响应 | id, thread_id, role, content, agent_id, timestamp |

#### 通用模型（common.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `MessageResponse` | 消息响应 | message, success |
| `PaginatedResponse[T]` | 分页响应基类 | items, total, page, page_size |

#### 消息模型（message.py）
| 模型 | 用途 | 核心字段 |
|------|------|----------|
| `MessageResponse` | 消息响应 | id, session_id, parent_id, sequence, role, content, tool_calls |
| `MessageEditRequest` | 消息编辑请求 | content |
| `MessageRetryRequest` | 消息重试请求 | new_content, regenerate_all |
| `MessageListResponse` | 消息列表响应 | messages, total, session_id |

### 错误处理
- 字段验证失败自动返回 422 错误
- 自定义验证器提供详细错误信息
- 使用 `field_validator` 装饰器实现自定义验证逻辑

## 结构

### 文件清单

#### `__init__.py`
职责：Schema 模块入口，导出所有模型
暴露接口：
- 认证模型：`LoginRequest`, `RefreshTokenRequest`, `LogoutRequest`, `TokenResponse`, `UserResponse`
- Agent 模型：`AgentCreateRequest`, `AgentUpdateRequest`, `AgentResponse`, `AgentListResponse`
- 工具模型：`ToolResponse`, `ToolListResponse`
- 通用模型：`MessageResponse`, `PaginatedResponse`
- 任务模型：`TaskPhaseStatusResponse`, `TaskACListResponse`, `ACEvaluationResult`

#### `auth.py`
职责：认证相关数据模型
暴露接口：
- `RegisterRequest(BaseModel)`：注册请求模型
- `LoginRequest(BaseModel)`：登录请求模型
- `RefreshTokenRequest(BaseModel)`：刷新 Token 请求模型
- `LogoutRequest(BaseModel)`：登出请求模型
- `TokenResponse(BaseModel)`：Token 响应模型
- `UserResponse(BaseModel)`：用户信息响应模型

#### `agents.py`
职责：Agent 相关数据模型
暴露接口：
- `AgentCreateRequest(BaseModel)`：创建 Agent 请求模型
- `AgentUpdateRequest(BaseModel)`：更新 Agent 请求模型
- `AgentResponse(BaseModel)`：Agent 响应模型
- `AgentListResponse(BaseModel)`：Agent 列表响应模型

#### `tasks.py`
职责：任务阶段和评估相关 Schema
暴露接口：
- `PhaseStatusInfo(BaseModel)`：阶段状态信息
- `TaskPhaseStatusResponse(BaseModel)`：任务阶段状态响应
- `AcceptanceCriterionStatus(BaseModel)`：验收标准状态
- `TaskACListResponse(BaseModel)`：任务 AC 列表响应
- `ACEvaluateRequest(BaseModel)`：评估 AC 请求
- `ACEvaluationResult(BaseModel)`：AC 评估结果
- `TaskACResultResponse(BaseModel)`：任务 AC 结果响应

#### `thread.py`
职责：线程/会话相关数据模型
暴露接口：
- `ThreadCreateRequest(BaseModel)`：创建线程请求
- `ThreadUpdateRequest(BaseModel)`：更新线程请求
- `ThreadResponse(BaseModel)`：线程响应模型
- `ThreadDetailResponse(ThreadResponse)`：线程详情响应
- `MessageResponse(BaseModel)`：消息响应模型

#### `common.py`
职责：通用数据模型
暴露接口：
- `MessageResponse(BaseModel)`：消息响应
- `PaginatedResponse(BaseModel, Generic[T])`：分页响应基类

#### `message.py`
职责：消息相关数据模型
暴露接口：
- `MessageResponse(BaseModel)`：消息响应模型
- `MessageEditRequest(BaseModel)`：消息编辑请求
- `MessageRetryRequest(BaseModel)`：消息重试请求
- `MessageListResponse(BaseModel)`：消息列表响应

### 测试策略
- 单元测试：字段验证、类型转换、默认值
- 边界测试：必填字段缺失、类型错误、值范围
- 序列化测试：JSON 序列化/反序列化

## 实现
→ 见代码文件 `src/api/schemas/*.py`
