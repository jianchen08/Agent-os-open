# Auth 模块

## 需求

### 职责

认证模块负责用户认证、JWT Token 管理和 RBAC 权限控制，提供完整的身份验证和授权功能。

### 对外接口

- 输入：用户凭证 / Token
- 输出：认证结果 / 权限判定

### 依赖

- 依赖模块：db（用户存储）、core（异常定义）
- 外部依赖：pyjwt、bcrypt

## 逻辑

### 流程设计

```
用户登录请求
    │
    ▼
AuthService.authenticate()
    │
    ├─► 获取用户（UserRepository）
    │
    ├─► 验证密码（bcrypt）
    │
    ├─► 检查用户状态
    │
    └─► 生成 Token 对（TokenManager）
    │
    ▼
返回 Token 信息
```

### 数据流向

```
用户凭证 → AuthService
    ↓
UserRepository → 数据库查询
    ↓
密码验证 → bcrypt
    ↓
Token 生成 → TokenManager
    ↓
返回 Token 对
```

### 数据模型

#### UserInDB（用户数据）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 用户 ID |
| username | str | 用户名 |
| email | str \| None | 邮箱 |
| password_hash | str | 密码哈希 |
| role | str | 角色，默认 user |
| is_active | bool | 是否激活，默认 True |
| preferences | dict | 用户偏好 |
| created_at | datetime | 创建时间 |
| updated_at | datetime \| None | 更新时间 |

#### TokenPair（Token 对）

| 字段 | 类型 | 说明 |
|------|------|------|
| access_token | str | 访问令牌 |
| refresh_token | str | 刷新令牌 |
| token_type | str | Token 类型，默认 Bearer |
| expires_in | int | 过期时间（秒） |

#### TokenPayload（Token 载荷）

| 字段 | 类型 | 说明 |
|------|------|------|
| sub | str | 用户 ID |
| role | str | 角色 |
| exp | int | 过期时间戳 |
| iat | int | 签发时间戳 |
| jti | str | Token ID |

### API 设计

#### 模块 API

| 方法 | 职责 |
|------|------|
| `AuthService.register(user_create: UserCreate) -> UserInDB` | 用户注册 |
| `AuthService.authenticate(username: str, password: str) -> dict` | 用户认证 |
| `AuthService.refresh_token(refresh_token: str) -> dict` | 刷新 Token |
| `AuthService.logout(user_id: UUID, ...) -> None` | 用户登出 |
| `TokenManager.create_token_pair(user_id: str, role: str) -> TokenPair` | 创建 Token 对 |
| `TokenManager.verify_token(token: str, token_type: str) -> TokenPayload` | 验证 Token |
| `TokenManager.revoke_token(token: str) -> None` | 撤销 Token |

### 配置设计

#### 模块配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| jwt_secret_key | JWT 密钥 | 从环境变量读取 |
| jwt_algorithm | JWT 算法 | HS256 |
| access_token_expire | 访问令牌过期时间（秒） | 3600 |
| refresh_token_expire | 刷新令牌过期时间（秒） | 604800 |

### 错误处理

#### 模块错误码

| 错误码 | 说明 |
|--------|------|
| TOKEN_ERROR | Token 错误 |
| TOKEN_EXPIRED | Token 过期 |
| TOKEN_INVALID | Token 无效 |
| TOKEN_REVOKED | Token 已撤销 |
| AUTH_FAILED | 认证失败 |
| INVALID_CREDENTIALS | 凭证无效 |
| USER_NOT_FOUND | 用户不存在 |
| USER_INACTIVE | 用户已禁用 |
| USER_EXISTS | 用户已存在 |
| PERMISSION_DENIED | 权限拒绝 |
| RATE_LIMIT_EXCEEDED | 限流超限 |

#### 异常类型

| 异常 | 说明 |
|------|------|
| AuthException | 认证基础异常 |
| TokenError | Token 错误 |
| TokenExpiredError | Token 过期 |
| TokenInvalidError | Token 无效 |
| TokenRevokedError | Token 已撤销 |
| AuthenticationError | 认证失败 |
| InvalidCredentialsError | 凭证无效 |
| UserNotFoundError | 用户不存在 |
| UserInactiveError | 用户已禁用 |
| UserExistsError | 用户已存在 |
| PermissionDeniedError | 权限拒绝 |
| RateLimitExceededError | 限流超限 |

### 安全设计

#### 模块安全

- 密码加密：bcrypt 哈希存储
- Token 管理：JWT + Redis 撤销机制
- 登录限流：5 次/分钟
- 权限控制：RBAC 角色权限

## 结构

### 组件清单（文件夹 - 抽象说明）

本模块为单层结构，无子组件。

### 文件清单（代码文件 - 具体接口）

#### service.py
职责：认证服务
暴露接口：
- `AuthService`：认证服务类
  - `async register(user_create: UserCreate) -> UserInDB`
  - `async authenticate(username: str, password: str) -> dict[str, Any]`
  - `async refresh_token(refresh_token: str) -> dict[str, Any]`
  - `async logout(user_id: UUID, refresh_token: str | None, logout_all_devices: bool) -> None`
  - `async get_user_by_id(user_id: UUID) -> UserInDB | None`

#### token.py
职责：Token 管理（撤销存储优先使用 Redis，Redis 不可用时降级到内存）
暴露接口：
- `TokenManager`：Token 管理器类
  - `__init__(secret_key, algorithm, access_token_expire_minutes, refresh_token_expire_days, redis_url)`
  - `create_token_pair(user_id: str, role: str) -> TokenPair`
  - `verify_token(token: str, token_type: str) -> TokenPayload`
  - `refresh_token_pair(refresh_token: str, role: str) -> TokenPair`
  - `revoke_token(token: str) -> None`
  - `revoke_all_user_tokens(user_id: str) -> None`

#### password.py
职责：密码处理
暴露接口：
- `hash_password(password: str) -> str`：密码哈希
- `verify_password(plain_password: str, hashed_password: str) -> bool`：密码验证

#### rbac.py
职责：RBAC 权限控制（含资源级细粒度权限）
暴露接口：
- `Permission`：权限枚举
- `Role`：角色枚举
- `RBACManager`：RBAC 管理器类
  - `get_role_permissions(role: Role | str) -> set[Permission]`
  - `has_permission(role: Role | str, permission: Permission) -> bool`
  - `check_permission(role: Role | str, permission: Permission) -> None`（无权限时抛出 PermissionDeniedError）
  - `add_resource_permission(resource: str, role: Role, permissions: set[Permission])`
  - `has_resource_action_permission(role, resource, action) -> bool`（资源×操作权限矩阵检查）
  - `check_resource_action_permission(role, resource, action) -> None`（无权限时抛出 PermissionDeniedError）

#### permission_matrix.py
职责：资源×操作权限矩阵（E-2 细化，从 3 个粗粒度角色扩展到资源级权限控制）
暴露接口：
- `Resource`：受控资源枚举（threads/tasks/agents/tools/config/memory/triggers/evaluation/users/workspaces/plugins/reviews/maintenance/artifacts）
- `Action`：操作枚举（read/create/update/delete/manage/execute）
- `RESOURCE_PERMISSION_MATRIX`：资源×角色→操作集合 权限矩阵
- `has_resource_action_permission(role, resource, action) -> bool`

#### models.py
职责：数据模型定义
暴露接口：
- `UserCreate`：用户创建请求模型
- `UserInDB`：用户数据库模型
- `TokenPair`：Token 对模型
- `TokenPayload`：Token 载荷模型

#### dependencies.py
职责：FastAPI 依赖注入
暴露接口：
- `init_auth_dependencies(...) -> None`：初始化依赖
- `get_current_user(token: str) -> UserInDB`：获取当前用户
- `get_current_active_user(user: UserInDB) -> UserInDB`：获取活跃用户
- `require_role(role: str) -> Callable`：角色要求装饰器
- `require_permission(resource: str, action: str) -> Callable`：权限要求装饰器
- `require_resource_action(resource: Resource, action: Action) -> Callable`：资源×操作权限检查依赖（E-2 细化）
- `require_admin() -> Callable`：管理员要求装饰器

#### exceptions.py
职责：异常定义
暴露接口：
- `InvalidCredentialsError`：凭证无效异常
- `UserExistsError`：用户已存在异常
- `UserInactiveError`：用户已禁用异常
- `UserNotFoundError`：用户不存在异常

### 测试策略

#### 模块测试

- 单元测试：密码哈希、Token 生成验证
- 集成测试：认证流程、权限检查
- 测试覆盖：核心逻辑 ≥90%

## 实现

→ 见代码文件
