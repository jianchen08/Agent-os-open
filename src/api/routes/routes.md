# Routes 组件

## 需求

### 职责
提供 HTTP API 路由注册和端点定义，作为 API 层的入口，将请求分发到对应的服务层。

### 对外接口
- 输入：HTTP 请求（路径、方法、参数）
- 输出：HTTP 响应（JSON 数据）

### 依赖
- FastAPI 框架
- `src/api/schemas`：请求/响应数据模型
- `src/api/dependencies`：依赖注入（认证、数据库会话）
- `src/services`：业务服务层

## 逻辑

### 流程设计
```
HTTP 请求 → 路由匹配 → 参数验证 → 认证检查 → 调用服务层 → 返回响应
```

### 数据流向
1. 客户端发起 HTTP 请求
2. FastAPI 路由器匹配路径和方法
3. Pydantic 模型验证请求参数
4. 依赖注入获取当前用户和数据库会话
5. 调用对应服务层方法处理业务逻辑
6. 服务层返回结果，路由层封装为 HTTP 响应

### API设计

#### 路由分组

| 路由前缀 | 模块 | 职责 |
|----------|------|------|
| `/api/v1/auth` | auth.py | 用户认证（登录、注册、登出） |
| `/api/v1/health` | health.py | 健康检查 |
| `/api/v1/threads` | threads.py | 线程/会话管理 |
| `/api/v1/execution` | execution.py | 执行控制 |
| `/api/v1/agents` | agents.py | Agent 管理 |
| `/api/v1/tools` | tools.py | 工具管理 |
| `/api/v1/tasks` | tasks.py | 任务管理 |
| `/api/v1/task-phases` | task_phases.py | 任务阶段 |
| `/api/v1/memory` | memory.py | 记忆管理 |
| `/api/v1/config` | config.py | 配置管理 |
| `/api/v1/users` | users.py | 用户管理 |
| `/api/v1/monitoring` | monitoring.py | 监控接口 |
| `/api/v1/cost-control` | cost_control.py | 成本控制 |

### 错误处理
- 使用 HTTPException 返回标准错误响应
- 全局异常处理器捕获未处理异常
- 错误响应包含错误码和详细信息

### 安全设计
- JWT Token 认证（通过 `get_current_user` 依赖）
- 路由级别的权限控制
- 输入参数验证（Pydantic 模型）

## 结构

### 文件清单

#### `__init__.py`
职责：路由模块入口，注册所有子路由
暴露接口：
- `v1_router: APIRouter`：v1 版本路由器
- `auth_router: APIRouter`：认证路由
- `health_router: APIRouter`：健康检查路由
- `threads_router: APIRouter`：线程路由
- `agents_router: APIRouter`：Agent 路由
- `tasks_router: APIRouter`：任务路由

#### `auth.py`
职责：用户认证路由
暴露接口：
- `register(request: RegisterRequest) -> TokenResponse`：用户注册
- `login(request: LoginRequest) -> TokenResponse`：用户登录
- `refresh_token(request: RefreshTokenRequest) -> TokenResponse`：刷新 Token
- `logout(request: LogoutRequest) -> MessageResponse`：用户登出
- `get_me() -> UserResponse`：获取当前用户信息

#### `agents.py`
职责：Agent 管理路由
暴露接口：
- `check_agents_health() -> AgentHealthResponse`：Agent 健康检查
- `list_agents(page: int, page_size: int) -> AgentListResponse`：获取 Agent 列表
- `create_agent(request: AgentCreateRequest) -> AgentResponse`：创建 Agent
- `get_default_agent() -> AgentResponse`：获取默认 Agent
- `get_agent(agent_id: str) -> AgentResponse`：获取 Agent 详情
- `update_agent(agent_id: str, request: AgentUpdateRequest) -> AgentResponse`：更新 Agent
- `delete_agent(agent_id: str) -> None`：删除 Agent

#### `tasks.py`
职责：任务管理路由
暴露接口：
- `create_task(task_data: TaskCreateRequest) -> TaskResponse`：创建任务
- `list_tasks(skip: int, limit: int) -> list[TaskResponse]`：获取任务列表
- `get_task(task_id: str) -> TaskDetailResponse`：获取任务详情
- `update_task(task_id: str, task_data: TaskUpdateRequest) -> TaskResponse`：更新任务
- `delete_task(task_id: str) -> None`：删除任务
- `get_evaluation_status(task_id: str) -> EvaluationStatusResponse`：获取评估状态
- `start_task_execution(task_id: str) -> TaskStartResponse`：启动任务执行

#### `threads.py`
职责：线程/会话管理路由
暴露接口：
- `list_threads(page: int, page_size: int) -> dict`：获取线程列表
- `create_thread(request: ThreadCreateRequest) -> dict`：创建线程
- `get_thread(thread_id: str) -> dict`：获取线程详情
- `get_thread_state(thread_id: str) -> dict`：获取线程状态
- `get_thread_history(thread_id: str) -> dict`：获取线程历史
- `update_thread(thread_id: str, request: ThreadUpdateRequest) -> dict`：更新线程
- `delete_thread(thread_id: str) -> dict`：删除线程
- `delete_message(thread_id: str, message_id: str) -> dict`：删除消息
- `get_thread_messages(thread_id: str) -> dict`：获取线程消息列表
- `retry_message(thread_id: str, message_id: str) -> dict`：重试消息
- `edit_message(thread_id: str, message_id: str, request: dict) -> dict`：编辑消息

#### `health.py`
职责：健康检查路由
暴露接口：
- `health_check() -> dict[str, Any]`：健康检查端点
- `ping() -> dict[str, str]`：简单 ping 端点

### 测试策略
- 单元测试：路由参数验证、响应格式
- 集成测试：完整请求-响应流程
- 认证测试：Token 验证、权限控制

## 实现
→ 见代码文件 `src/api/routes/*.py`
