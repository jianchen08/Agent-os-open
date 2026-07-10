---
name: 后端编码
description: 后端技术栈编码技能（规范+流程）。含 API 设计、数据库规范、错误处理、日志规范、API 端点测试要求。用于后端/全栈任务的编码阶段。
---

# 后端编码

## API 设计规范

### RESTful 规范

| 原则 | 说明 |
|------|------|
| 资源命名 | 使用名词复数表示资源 |
| 层级结构 | 使用嵌套表示资源关系 |
| HTTP 方法 | 正确使用 GET/POST/PUT/PATCH/DELETE |
| 统一接口 | 所有 API 遵循统一的接口约束 |
| 无状态 | 请求包含所有必要信息 |

### HTTP 方法

| 方法 | 用途 | 幂等性 | 示例 |
|------|------|--------|------|
| GET | 查询资源 | 幂等 | `GET /users/123` |
| POST | 创建资源 | 非幂等 | `POST /users` |
| PUT | 更新资源（全量） | 幂等 | `PUT /users/123` |
| PATCH | 更新资源（部分） | 非幂等 | `PATCH /users/123` |
| DELETE | 删除资源 | 幂等 | `DELETE /users/123` |

### URL 设计

| 规范 | 正确 | 错误 |
|------|------|------|
| 使用名词复数 | `GET /users` | `GET /getUsers` |
| 层级表示关系 | `GET /users/123/orders` | `GET /getUserOrders?userId=123` |
| 小写字母 | `GET /user-profiles` | `GET /UserProfiles` |
| 省略扩展名 | `GET /users/123` | `GET /users/123.json` |
| 查询参数过滤 | `GET /users?role=admin` | `GET /adminUsers` |

### 版本控制
URL 路径版本（推荐）：`/v1/users`, `/v2/users`

## 数据库规范

### 设计原则
- 第三范式（3NF），消除传递依赖
- 必须有主键，推荐自增 ID 或 UUID
- 为高频查询字段添加索引
- 表名和字段名使用 snake_case

### 命名规范

| 对象 | 命名 | 示例 |
|------|------|------|
| 表名 | snake_case，复数名词 | `users`, `order_items` |
| 字段名 | snake_case | `user_name`, `created_at` |
| 主键 | `id` | `id` |
| 外键 | `{table_singular}_id` | `user_id`, `order_id` |
| 索引 | `ix_{table}_{column}` | `ix_users_email` |
| 唯一约束 | `uq_{table}_{column}` | `uq_users_email` |

## 错误处理

### 错误响应格式

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "用户友好的错误消息",
    "details": {}
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

## 日志规范

### 日志级别

| 级别 | 使用场景 |
|------|---------|
| DEBUG | 开发时排查问题 |
| INFO | 正常业务流程记录 |
| WARNING | 潜在问题但不影响功能 |
| ERROR | 功能失败但应用可继续 |
| CRITICAL | 应用崩溃级错误 |

### 日志最佳实践
- 使用 JSON 结构化日志，便于检索
- 每个请求关联唯一 request_id
- 敏感信息脱敏（不记录密码、Token）
- 异常日志使用 `exc_info=True` 保留完整堆栈
- 日志包含业务上下文（如 order_id、user_id）

## API 端点测试要求

进入测试阶段时（测试通用规范已在常驻提示词）：

每个端点必须覆盖：
- 2xx 成功响应
- 4xx 客户端错误（缺失参数/无效输入/权限不足）
- 5xx 服务端错误（如可模拟）
- 校验响应 Schema（字段名、类型、必填项）
- 验证错误响应包含清晰的错误码和消息

## 禁止行为

### API 设计
- 动词在 URL 中 → 使用 HTTP 方法
- 返回 HTML → 返回 JSON
- 缺少版本控制 → URL 路径版本控制
- 错误使用状态码 → 使用正确的 HTTP 状态码
- 暴露内部错误 → 错误详情仅内部日志

### 数据库
- SELECT * → 明确指定字段
- N+1 查询 → 使用 JOIN 或批量查询
- 裸 SQL 拼接 → 使用参数化查询
- 缺少索引 → 为高频查询添加索引
- 外键约束缺失 → 适当使用外键
- 中文表名/字段名 → 使用英文命名

### 其他
- 记录密码/Token → 脱敏或用占位符
- 日志过多 → 适当降低日志级别
- 日志无上下文 → 添加业务上下文
