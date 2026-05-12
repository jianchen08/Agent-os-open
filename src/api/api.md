# API 模块

## 需求

### 职责

API 模块提供 RESTful API 和 WebSocket 接口，是系统与外部交互的入口。基于 FastAPI 框架实现，支持自动生成 OpenAPI 文档、请求验证、限流和错误处理。

### 对外接口

- 输入：HTTP 请求 / WebSocket 连接
- 输出：JSON 响应 / WebSocket 消息

### 依赖

- 依赖模块：auth（认证）、db（数据库）、agents（Agent 执行）、tools（工具）、workflows（工作流）
- 外部依赖：fastapi、uvicorn、websockets

## 逻辑

### 流程设计

```
HTTP 请求
    │
    ▼
中间件处理（CORS、日志、限流、错误处理）
    │
    ▼
路由分发
    │
    ├─► /api/v1/auth → 认证接口
    ├─► /api/v1/agents → Agent 管理
    ├─► /api/v1/tools → 工具管理
    ├─► /api/v1/workflows → 工作流管理
    ├─► /api/v1/threads → 会话管理
    ├─► /api/v1/execution → 执行控制
    └─► /ws/chat/{thread_id} → WebSocket 聊天
    │
    ▼
业务逻辑处理
    │
    ▼
响应返回
```

### 数据流向

```
客户端请求 → 中间件 → 路由处理器
    ↓
参数验证 → 业务服务调用
    ↓
数据库操作 → 响应构建
    ↓
错误处理 → 返回响应
```

### 数据模型

#### ErrorResponse（错误响应）

| 字段 | 类型 | 说明 |
|------|------|------|
| code | str | 错误码，格式: CATEGORY_NNN |
| message | str | 用户友好消息 |
| trace_id | str | 链路追踪 ID |
| timestamp | datetime | 时间戳 |
| path | str | 请求路径 |
| errors | dict | 字段级错误 |

#### PaginationParams（分页参数）

| 字段 | 类型 | 说明 |
|------|------|------|
| page | int | 页码，默认 1 |
| page_size | int | 每页数量，默认 20，最大 100 |

#### SortingParams（排序参数）

| 字段 | 类型 | 说明 |
|------|------|------|
| sort_by | str | 排序字段，默认 created_at |
| sort_order | str | 排序方向，asc/desc |

### API 设计

#### API 清单

| 路径 | 方法 | 职责 |
|------|------|------|
| /api/v1/auth/login | POST | 用户登录 |
| /api/v1/auth/logout | POST | 用户登出 |
| /api/v1/auth/refresh | POST | 刷新 Token |
| /api/v1/agents | GET | 获取 Agent 列表 |
| /api/v1/agents/{id} | GET | 获取 Agent 详情 |
| /api/v1/tools | GET | 获取工具列表 |
| /api/v1/workflows | GET | 获取工作流列表 |
| /api/v1/threads | GET | 获取会话列表 |
| /api/v1/threads | POST | 创建会话 |
| /api/v1/threads/{id}/messages | GET | 获取消息列表 |
| /ws/chat/{thread_id} | WebSocket | 实时聊天 |

#### 认证方式

- 方式：JWT Bearer Token
- Token 有效期：24 小时
- 刷新机制：支持 Refresh Token

### 配置设计

#### 模块配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| cors_origins | CORS 允许的源 | ["*"] |
| rate_limit.requests_per_minute | 每分钟请求数限制 | 60 |
| rate_limit.burst_size | 突发请求大小 | 10 |
| debug | 调试模式 | False |

### 错误处理

#### 错误码规范

| 前缀 | 类别 | 说明 |
|------|------|------|
| WS | WebSocket | WebSocket 连接和消息 |
| API | REST API | API 错误 |
| AUTH | Auth | 认证授权错误 |
| VAL | Validation | 数据验证错误 |
| SYS | System | 系统级错误 |

#### HTTP 状态码

| 状态码 | 说明 |
|--------|------|
| 200 | 请求成功 |
| 201 | 创建成功 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 500 | 服务器内部错误 |

### 安全设计

#### 模块安全

- 认证：JWT Token 认证
- 授权：RBAC 权限控制
- 限流：滑动窗口限流
- CORS：跨域请求控制

## 结构

### 组件清单（文件夹 - 抽象说明）

| 组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| routes/ | REST API 路由模块 | 输入：HTTP 请求 → 输出：JSON 响应 | - |
| schemas/ | 请求/响应数据模型 | 输入：原始数据 → 输出：验证后的模型 | - |
| websocket/ | WebSocket 处理 | 输入：WebSocket 连接 → 输出：实时消息 | - |
| services/ | 业务服务 | 输入：业务请求 → 输出：业务结果 | - |
| middleware_opt/ | 中间件优化 | 输入：请求 → 输出：处理后的请求 | - |
| views/ | 视图模型 | 输入：数据库模型 → 输出：API 响应 | - |

### 文件清单（代码文件 - 具体接口）

#### main.py
职责：FastAPI 应用入口
暴露接口：
- `create_app(title: str, cors_origins: list, ...) -> FastAPI`：创建应用实例
- `app: FastAPI`：默认应用实例

#### dependencies.py
职责：公共依赖注入
暴露接口：
- `PaginationParams`：分页参数类
- `SortingParams`：排序参数类
- `FilterParams`：过滤参数类
- `generate_trace_id() -> str`：生成追踪 ID

#### errors.py
职责：错误响应定义
暴露接口：
- `ErrorResponse`：错误响应模型
- `ErrorCode`：错误码枚举
- `create_error_response(code: str, message: str | None, trace_id: str, detail: str | None, details: dict | None, errors: dict | None) -> ErrorResponse`：创建错误响应
- `get_http_status(error_code: str) -> int`：获取 HTTP 状态码
- `is_valid_error_code(code: str) -> bool`：验证错误码是否有效

#### rate_limit.py
职责：限流实现
暴露接口：
- `RateLimitConfig`：限流配置类
- `SlidingWindowRateLimiter`：滑动窗口限流器
- `TokenBucketRateLimiter`：令牌桶限流器
- `is_whitelisted(key: str) -> bool`：检查是否在白名单

#### middleware.py
职责：中间件配置
暴露接口：
- `setup_middlewares(app: FastAPI, ...) -> None`：配置中间件

#### error_handler.py
职责：异常处理器
暴露接口：
- `setup_exception_handlers(app: FastAPI) -> None`：注册异常处理器

#### lifespan.py
职责：应用生命周期管理
暴露接口：
- `StartupManager`：启动管理器类

### 测试策略

#### 模块测试

- 单元测试：路由处理器、中间件
- 集成测试：API 端到端测试
- 测试覆盖：API ≥85%

## 实现

→ 见各组件代码文件
