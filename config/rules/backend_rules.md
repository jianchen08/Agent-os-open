# 后端开发规范

---

## 概述

本规范定义了灵汐系统后端开发的标准规范和最佳实践，涵盖 API 设计、数据库规范、错误处理和日志规范。

### 来源标识说明

| 标识 | 含义 |
|------|------|
| [STD] | 来自国际标准、行业标准（RFC、PEP 等） |
| [DOC] | 来自官方文档的推荐做法 |
| [BEST] | 社区公认的最佳实践 |
| [TEAM] | 团队内部约定的规范 |

---

## 1. API 设计规范

### 1.1 RESTful 规范（RFC 7231）

来源：[STD] RFC 7231 - HTTP/1.1 Semantics and Content

RESTful API 设计遵循 RFC 7231 标准定义的核心原则：

| 原则 | 说明 | 来源 |
|------|------|------|
| 资源命名 | 使用名词复数表示资源 | [STD] RFC 7231 |
| 层级结构 | 使用嵌套表示资源关系 | [STD] RFC 7231 |
| HTTP 方法 | 正确使用 GET/POST/PUT/PATCH/DELETE | [STD] RFC 7231 |
| 统一接口 | 所有 API 遵循统一的接口约束 | [STD] RFC 7231 |
| 无状态 | 请求包含所有必要信息 | [STD] RFC 7231 |

### 1.2 HTTP 方法使用规范

来源：[STD] RFC 7231

| 方法 | 用途 | 幂等性 | 安全性 | 示例 |
|------|------|--------|--------|------|
| GET | 查询资源 | 幂等 | 安全 | `GET /users/123` |
| POST | 创建资源 | 非幂等 | 不安全 | `POST /users` |
| PUT | 更新资源（全量） | 幂等 | 不安全 | `PUT /users/123` |
| PATCH | 更新资源（部分） | 非幂等 | 不安全 | `PATCH /users/123` |
| DELETE | 删除资源 | 幂等 | 不安全 | `DELETE /users/123` |

### 1.3 URL 设计规范

来源：[BEST] RESTful API 最佳实践

| 规范 | 正确示例 | 错误示例 | 说明 |
|------|---------|---------|------|
| 使用名词复数 | `GET /users` | `GET /getUsers` | 资源导向 |
| 层级表示关系 | `GET /users/123/orders` | `GET /getUserOrders?userId=123` | 语义清晰 |
| 小写字母 | `GET /user-profiles` | `GET /UserProfiles` | 统一规范 |
| 省略文件扩展名 | `GET /users/123` | `GET /users/123.json` | RESTful 风格 |
| 查询参数用于过滤 | `GET /users?role=admin` | `GET /adminUsers` | 资源过滤 |

### 1.4 FastAPI 最佳实践

来源：[DOC] FastAPI 官方文档

```python
from fastapi import FastAPI, HTTPException, status, Query
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List

app = FastAPI(title="My API", version="1.0.0")

# 1. 请求模型定义
class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    role: str = Field(default="user")

# 2. 响应模型定义
class UserResponse(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: str
    
    class Config:
        from_attributes = True

# 3. 路径参数验证
@app.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int = Path(..., gt=0)) -> UserResponse:
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with id {user_id} not found"
        )
    return user

# 4. 查询参数
@app.get("/users", response_model=List[UserResponse])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    role: Optional[str] = None
) -> List[UserResponse]:
    users = await db.list_users(skip=skip, limit=limit, role=role)
    return users

# 5. 创建资源
@app.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate) -> UserResponse:
    existing = await db.get_user_by_email(user.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists"
        )
    return await db.create_user(user)
```

### 1.5 版本控制规范

来源：[BEST] API 版本控制最佳实践

| 方式 | 实现 | 示例 | 适用场景 |
|------|------|------|---------|
| URL 路径 | 在路径中包含版本 | `/v1/users`, `/v2/users` | 简单直观，推荐 |
| Header | 自定义 Header | `API-Version: v1` | 资源导向 |
| Query 参数 | 查询参数指定 | `/users?version=v1` | 不推荐 |

---

## 2. 数据库规范

### 2.1 设计原则

来源：[BEST] 数据库设计最佳实践

| 原则 | 说明 | 来源 |
|------|------|------|
| 第三范式（3NF） | 消除传递依赖 | [BEST] |
| 主键规范 | 必须有主键，推荐使用自增 ID 或 UUID | [BEST] |
| 索引规范 | 为高频查询字段添加索引 | [BEST] |
| 命名规范 | 表名和字段名使用 snake_case | [TEAM] |

### 2.2 SQLAlchemy 模型定义

来源：[DOC] SQLAlchemy 官方文档

```python
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 常规字段
    name = Column(String(100), nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    role = Column(String(50), default="user", nullable=False)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    
    # 索引
    __table_args__ = (
        Index("ix_users_name_email", "name", "email"),
    )

class Order(Base):
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    total_amount = Column(Integer, nullable=False)  # 分为单位
    status = Column(String(50), default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # 关系
    user = relationship("User", back_populates="orders")
```

### 2.3 Alembic 迁移规范

来源：[DOC] Alembic 官方文档

```python
# 迁移文件示例
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = '001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('role', sa.String(50), server_default='user', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email')
    )
    op.create_index('ix_users_name', 'users', ['name'])
    op.create_index('ix_users_email', 'users', ['email'])

def downgrade() -> None:
    op.drop_index('ix_users_email', table_name='users')
    op.drop_index('ix_users_name', table_name='users')
    op.drop_table('users')
```

### 2.4 数据库命名规范

来源：[TEAM] 团队约定

| 对象 | 命名规范 | 示例 |
|------|---------|------|
| 表名 | snake_case，复数名词 | `users`, `order_items` |
| 字段名 | snake_case | `user_name`, `created_at` |
| 主键 | `id` | `id` |
| 外键 | `{table_singular}_id` | `user_id`, `order_id` |
| 索引 | `ix_{table}_{column}` | `ix_users_email` |
| 唯一约束 | `uq_{table}_{column}` | `uq_users_email` |

---

## 3. 错误处理

### 3.1 Python 异常处理原则

来源：[STD] PEP 3134 - Exception Chaining and Embedded Tracebacks

| 原则 | 说明 | 来源 |
|------|------|------|
| 具体异常 | 捕获具体异常而非 Exception | [BEST] |
| 异常链 | 使用 `raise ... from` 保留原始异常 | [STD] PEP 3134 |
| 资源清理 | 使用 try/finally 或 context manager | [BEST] |
| 异常传播 | 不捕获应向上传播的异常 | [BEST] |

### 3.2 FastAPI 异常处理

来源：[DOC] FastAPI 官方文档

```python
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

app = FastAPI()

# 1. 自定义业务异常
class UserNotFoundError(Exception):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"User with id {user_id} not found")

class ValidationError(Exception):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")

# 2. 全局异常处理器
@app.exception_handler(UserNotFoundError)
async def user_not_found_handler(request: Request, exc: UserNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "success": False,
            "error": {
                "code": "USER_NOT_FOUND",
                "message": str(exc),
                "details": {"user_id": exc.user_id}
            }
        }
    )

# 3. 在业务逻辑中抛出异常
@app.get("/users/{user_id}")
async def get_user(user_id: int):
    user = await db.get_user(user_id)
    if not user:
        raise UserNotFoundError(user_id)  # 不返回 None
    return user
```

### 3.3 异常处理最佳实践

来源：[BEST] Python 异常处理最佳实践

```python
# 1. 具体异常捕获
try:
    result = await db.get_user(user_id)
except UserNotFoundError as e:
    raise UserNotFoundError(e.user_id) from e
except DatabaseConnectionError as e:
    logger.error(f"Database connection failed: {e}")
    raise InternalServerError("Database unavailable") from e

# 2. 使用 context manager
# 正确：自动资源管理
with get_db_session() as session:
    user = session.query(User).get(user_id)
    
# 3. 避免裸 except
# 错误
try:
    do_something()
except:
    pass

# 正确
try:
    do_something()
except SpecificException as e:
    handle_error(e)
```

### 3.4 错误响应格式

来源：[TEAM] 团队约定

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "用户友好的错误消息",
    "details": {
      "field": "具体字段信息",
      "reason": "详细原因说明"
    }
  },
  "request_id": "uuid-for-tracing"
}
```

| HTTP 状态码 | 错误码 | 适用场景 |
|-------------|--------|---------|
| 400 | `VALIDATION_ERROR` | 请求参数验证失败 |
| 401 | `UNAUTHORIZED` | 未认证 |
| 403 | `FORBIDDEN` | 无权限 |
| 404 | `NOT_FOUND` | 资源不存在 |
| 409 | `CONFLICT` | 资源冲突 |
| 422 | `UNPROCESSABLE_ENTITY` | 请求格式正确但无法处理 |
| 500 | `INTERNAL_ERROR` | 服务器内部错误 |
| 503 | `SERVICE_UNAVAILABLE` | 服务不可用 |

---

## 4. 日志规范

### 4.1 日志级别标准

来源：[STD] RFC 5424 - The Syslog Protocol

| 级别 | 数值 | 说明 | 使用场景 | 来源 |
|------|------|------|---------|------|
| DEBUG | 7 | 调试信息 | 开发时排查问题 | [STD] RFC 5424 |
| INFO | 6 | 一般信息 | 正常业务流程记录 | [STD] RFC 5424 |
| WARNING | 4 | 警告信息 | 潜在问题但不影响功能 | [STD] RFC 5424 |
| ERROR | 3 | 错误信息 | 功能失败但应用可继续 | [STD] RFC 5424 |
| CRITICAL | 2 | 严重错误 | 应用崩溃级错误 | [STD] RFC 5424 |

### 4.2 Python logging 配置

来源：[DOC] Python logging 官方文档

```python
import logging
import sys
from typing import Any
from pythonjsonlogger import jsonlogger

# 1. 日志格式化器配置
class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record['timestamp'] = self.formatTime(record)
        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        log_record['service'] = 'my-backend-service'

# 2. 配置 logging
def setup_logging(log_level: str = "INFO") -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(CustomJsonFormatter(
        '%(timestamp)s %(level)s %(name)s %(message)s'
    ))
    root_logger.addHandler(console_handler)
    
    # 结构化日志字段
    logging.getLogger('uvicorn.access').addFilter(
        RequestContextFilter()
    )

# 3. 使用结构化日志
logger = logging.getLogger(__name__)

def log_api_request(endpoint: str, duration_ms: float, status_code: int) -> None:
    logger.info(
        "API request completed",
        extra={
            "event": "api_request",
            "endpoint": endpoint,
            "duration_ms": duration_ms,
            "status_code": status_code
        }
    )

def log_error(error: Exception, context: dict[str, Any]) -> None:
    logger.error(
        f"Operation failed: {error}",
        extra={
            "event": "operation_error",
            "error_type": type(error).__name__,
            "error_message": str(error),
            **context
        },
        exc_info=True
    )
```

### 4.3 日志记录最佳实践

来源：[BEST] 日志最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 结构化日志 | 使用 JSON 格式便于检索 | [BEST] |
| 包含请求 ID | 每个请求关联唯一 ID | [BEST] |
| 敏感信息脱敏 | 不记录密码、Token 等 | [BEST] |
| 日志级别正确 | INFO 记录流程，DEBUG 记录细节 | [BEST] |
| 异常堆栈完整 | 使用 `exc_info=True` | [BEST] |
| 上下文信息 | 记录相关业务数据 | [BEST] |

### 4.4 日志内容规范

来源：[TEAM] 团队约定

```python
# 正确示例：包含足够上下文
logger.info(
    "Order created",
    extra={
        "order_id": order.id,
        "user_id": user.id,
        "amount": order.total_amount,
        "items_count": len(order.items)
    }
)

# 错误示例：缺少上下文
logger.info("Order created")
logger.debug("User fetched")
```

---

## 5. 禁止行为

### 5.1 API 设计禁止行为

来源：[BEST] API 设计反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止动词在 URL 中 | 违反 RESTful 原则 | 使用 HTTP 方法 | [STD] RFC 7231 |
| 禁止返回 HTML | 破坏 API 一致性 | 返回 JSON | [BEST] |
| 禁止缺少版本控制 | 破坏兼容性 | URL 路径版本控制 | [BEST] |
| 禁止错误使用状态码 | 误导客户端 | 使用正确的 HTTP 状态码 | [STD] RFC 7231 |
| 禁止暴露内部错误 | 安全风险 | 错误详情仅内部日志 | [BEST] |

### 5.2 数据库禁止行为

来源：[BEST] 数据库设计反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止 SELECT * | 不必要的字段传输 | 明确指定字段 | [BEST] |
| 禁止 N+1 查询 | 性能问题 | 使用 JOIN 或批量查询 | [BEST] |
| 禁止裸 SQL 拼接 | SQL 注入风险 | 使用参数化查询 | [BEST] |
| 禁止缺少索引 | 查询性能差 | 为高频查询添加索引 | [BEST] |
| 禁止外键约束缺失 | 数据一致性风险 | 适当使用外键 | [BEST] |
| 禁止使用中文表名/字段名 | 跨环境兼容问题 | 使用英文命名 | [TEAM] |

### 5.3 错误处理禁止行为

来源：[BEST] 异常处理反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止捕获 Exception | 隐藏真实问题 | 捕获具体异常 | [BEST] |
| 禁止 bare except | 捕获所有异常包括系统异常 | 明确异常类型 | [BEST] |
| 禁止吞掉异常 | 问题难以排查 | 重新抛出或记录日志 | [BEST] |
| 禁止返回 None 表示错误 | 调用方易忽略 | 抛出异常 | [BEST] |
| 禁止异常中暴露敏感信息 | 安全风险 | 内部记录，外部脱敏 | [BEST] |

### 5.4 日志禁止行为

来源：[BEST] 日志反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止记录密码/Token | 安全风险 | 脱敏或用占位符 | [BEST] |
| 禁止 print 调试 | 无法统一管理 | 使用 logging | [BEST] |
| 禁止日志过多 | 性能问题 | 适当降低日志级别 | [BEST] |
| 禁止日志过少 | 问题难以排查 | 关键节点记录 | [BEST] |
| 禁止日志无上下文 | 难以定位问题 | 添加业务上下文 | [BEST] |

---

## 6. 可扩展性

### 6.1 松耦合

来源：[BEST] 软件架构最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 接口定义边界 | 使用接口或抽象类定义模块边界 | [BEST] |
| 依赖注入解耦 | 通过依赖注入实现模块间解耦 | [BEST] |
| 事件驱动 | 事件驱动替代直接调用 | [BEST] |
| 插件机制 | 插件机制支持扩展 | [BEST] |

### 6.2 模块化

来源：[BEST] 模块化设计最佳实践

| 规范 | 说明 | 来源 |
|------|------|------|
| 按领域划分 | 按业务领域划分模块 | [BEST] |
| API 通信 | 模块间通过定义良好的 API 通信 | [BEST] |
| 独立部署 | 模块可独立部署和扩展 | [BEST] |
| 避免循环依赖 | 模块间禁止循环依赖 | [TEAM] |
