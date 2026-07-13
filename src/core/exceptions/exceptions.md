# 异常组件

## 需求
### 职责
提供统一的异常层次结构，支持业务异常和系统异常的分类处理，包含详细的错误信息和上下文。

### 对外接口
- 输入：错误消息、错误码、详情、原因
- 输出：结构化异常对象、字典格式输出

### 依赖
- 外部依赖：无
- 内部依赖：无

## 逻辑
### 流程设计
1. **异常创建**：根据错误类型选择合适的异常类
2. **日志记录**：自动记录异常日志（域异常为 WARNING，系统异常为 ERROR）
3. **异常处理**：通过 to_dict() 转换为 API 响应格式

### 数据流向
```
业务逻辑 -> 抛出异常 -> 日志记录 -> 异常处理器 -> API 响应
```

### 数据模型
#### 异常层次结构
```
BaseAppException
├── DomainException（业务逻辑错误）
│   ├── ValidationException（验证错误）
│   ├── NotFoundException（资源未找到）
│   ├── ConflictException（冲突错误）
│   ├── PermissionException（权限错误）
│   └── BusinessRuleException（业务规则错误）
└── SystemException（系统级错误）
    ├── DatabaseException（数据库错误）
    ├── CacheException（缓存错误）
    ├── ExternalServiceException（外部服务错误）
    ├── ConfigurationException（配置错误）
    └── TimeoutException（超时错误）
```

#### BaseAppException（异常基类）
| 字段 | 类型 | 说明 |
|---|---|---|
| message | str | 错误消息 |
| code | str | 错误码 |
| details | dict | 错误详情 |
| cause | Exception | 原始异常 |

### 错误处理
| 异常类型 | 默认错误码 | 日志级别 |
|---|---|---|
| ValidationException | VAL_001 | WARNING |
| NotFoundException | NOT_FOUND | WARNING |
| ConflictException | CONFLICT | WARNING |
| PermissionException | PERMISSION_DENIED | WARNING |
| DatabaseException | DB_ERROR | ERROR |
| ExternalServiceException | EXTERNAL_SERVICE_ERROR | ERROR |
| TimeoutException | TIMEOUT | ERROR |

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：核心异常基类定义
暴露接口：
- `BaseAppException`：应用异常基类
  - `to_dict() -> dict`：转换为字典
- `DomainException`：域异常基类
- `ValidationException`：验证异常
  - `__init__(message: str, field: str | None, details: dict)`：初始化
- `NotFoundException`：未找到异常
  - `__init__(message: str, resource_type: str | None, resource_id: str | None)`：初始化
- `ConflictException`：冲突异常
- `PermissionException`：权限异常
- `BusinessRuleException`：业务规则异常
- `SystemException`：系统异常基类
- `DatabaseException`：数据库异常
- `CacheException`：缓存异常
- `ExternalServiceException`：外部服务异常
- `ConfigurationException`：配置异常
- `TimeoutException`：超时异常

#### agent.py
职责：Agent 相关异常
暴露接口：
- `AgentException`：Agent 异常基类
- `AgentNotFoundError`：Agent 未找到
- `AgentAlreadyExistsError`：Agent 已存在
- `AgentExecutionError`：Agent 执行错误
- `SubAgentNestingError`：子 Agent 嵌套错误

#### auth.py
职责：认证相关异常
暴露接口：
- `AuthException`：认证异常基类
- `TokenError`：Token 错误基类
- `TokenExpiredError`：Token 过期
- `TokenInvalidError`：Token 无效
- `TokenRevokedError`：Token 已撤销
- `AuthenticationFailedError`：认证失败
- `InvalidCredentialsError`：凭证无效
- `UserNotFoundError`：用户未找到
- `UserInactiveError`：用户已禁用
- `UserExistsError`：用户已存在
- `PermissionDeniedError`：权限拒绝
- `RateLimitExceededError`：速率限制

#### llm.py
职责：LLM 相关异常
暴露接口：
- `LLMException`：LLM 异常基类
- `RateLimitError`：速率限制
- `AuthenticationError`：认证错误
- `InvalidRequestError`：无效请求
- `ModelNotAvailableError`：模型不可用
- `LLMTimeoutError`：LLM 超时
- `ContentFilterError`：内容过滤
- `BudgetExhaustedError`：预算耗尽

#### tool.py
职责：工具相关异常
暴露接口：
- `ToolException`：工具异常基类
- `ToolNotFoundError`：工具未找到
- `ToolAlreadyExistsError`：工具已存在
- `ToolValidationError`：工具验证错误
- `ToolExecutionError`：工具执行错误
- `ApprovalRequiredError`：需要审批
- `MCPException`：MCP 异常基类
- `MCPConnectionError`：MCP 连接错误
- `MCPConfigError`：MCP 配置错误

#### workflow.py
职责：工作流相关异常
暴露接口：
- `WorkflowException`：工作流异常基类
- `WorkflowNotFoundError`：工作流未找到
- `WorkflowValidationError`：工作流验证错误
- `WorkflowExecutionError`：工作流执行错误
- `NodeExecutionError`：节点执行错误
- `CycleDetectedError`：循环检测
- `AdapterError`：适配器错误
- `MaxIterationsExceededError`：超过最大迭代

#### config.py
职责：配置相关异常
暴露接口：
- `ConfigException`：配置异常基类
- `ConfigNotFoundError`：配置未找到
- `ConfigValidationError`：配置验证错误
- `ModelNotFoundError`：模型未找到
- `ProviderNotFoundError`：提供商未找到
- `EndpointNotFoundError`：端点未找到
- `EnvVarNotFoundError`：环境变量未找到

#### orchestration.py
职责：编排相关异常
暴露接口：
- `OrchestrationException`：编排异常基类
- `TaskNotFoundError`：任务未找到
- `ResourceExhaustedError`：资源耗尽
- `TaskExecutionError`：任务执行错误
- `SchedulerError`：调度器错误

#### cost_control.py
职责：成本控制相关异常
暴露接口：
- `CostControlException`：成本控制异常基类
- `BudgetExceededException`：预算超限
- `QuotaExhaustedException`：配额耗尽

#### di.py
职责：DI 相关异常
暴露接口：
- `DIException`：DI 异常基类
- `ServiceNotFoundError`：服务未找到
- `ServiceAlreadyRegisteredError`：服务已注册
- `CircularDependencyError`：循环依赖
- `InvalidServiceFactoryError`：无效工厂
- `ServiceValidationError`：服务验证错误

#### __init__.py
职责：异常模块导出
暴露接口：
- 导出所有异常类

### 测试策略
#### 组件测试
- 单元测试：异常创建、to_dict 转换
- 集成测试：异常捕获和处理链
- 覆盖率要求：核心逻辑 >= 90%

## 实现
-> 见代码文件：src/core/exceptions/
