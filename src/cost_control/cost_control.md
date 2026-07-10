# 成本控制模块

## 需求

### 职责
提供 Token 预算管理、成本监控和超限保护功能，确保系统在预算范围内运行，防止意外超支。

### 对外接口
- 输入：预估 Token 数、用户 ID、任务 ID、会话 ID
- 输出：预算检查结果、使用量统计、告警信息

### 依赖
- 依赖模块：`src.core.exceptions`（异常）、`src.core.constants`（常量）、`src.core.tokenizer`（Token 计数）
- 外部依赖：PyYAML、Pydantic

## 逻辑

### 流程设计
```
LLM 调用前 → 预算检查 → 通过/拒绝
                ↓ 通过
            执行 LLM 调用
                ↓
            记录实际使用量
                ↓
            检查告警阈值 → 触发告警/保护策略
```

### 数据流向
1. 预算检查：预估 Token → 检查全局/用户/任务/会话限制 → 返回结果
2. 使用量记录：实际 Token → 更新统计 → 检查告警
3. 告警触发：使用率检查 → 确定告警级别 → 执行保护策略

### 数据模型
#### 预算状态
| 字段 | 类型 | 说明 |
|------|------|------|
| scope | str | 范围（global/user/task/session） |
| limit | int | 限制值 |
| used | int | 已使用 |
| remaining | int | 剩余 |
| usage_percent | float | 使用率 |
| alert_level | BudgetAlertLevel | 告警级别 |

#### 使用记录
| 字段 | 类型 | 说明 |
|------|------|------|
| tokens | int | Token 数 |
| model | str | 模型名称 |
| scope | str | 范围 |
| cost | float | 成本 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `get_budget_manager() -> BudgetManager` | 获取预算管理器单例 |
| `BudgetManager.check_budget(estimated_tokens, user_id, task_id, session_id) -> bool` | 检查预算 |
| `BudgetManager.record_usage(tokens, model, user_id, task_id, session_id) -> BudgetAlert | None` | 记录使用量 |
| `BudgetManager.get_budget_status(user_id, task_id, session_id) -> BudgetStatus` | 获取预算状态 |
| `BudgetManager.get_usage_statistics() -> dict` | 获取使用统计 |

### 配置设计
#### 模块配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| daily_token_limit | 每日 Token 限制 | 1,000,000 |
| monthly_token_limit | 每月 Token 限制 | 30,000,000 |
| per_task_token_limit | 单任务限制 | 50,000 |
| per_session_token_limit | 单会话限制 | 100,000 |
| warning_threshold | 警告阈值 | 70% |
| critical_threshold | 严重阈值 | 90% |
| exhausted_threshold | 耗尽阈值 | 100% |

#### 保护策略配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| auto_save_at_warning | 警告时自动保存 | True |
| auto_pause_at_critical | 严重时自动暂停 | True |
| auto_stop_at_exhausted | 耗尽时自动停止 | True |

### 错误处理
#### 模块错误码
| 错误码 | 说明 |
|--------|------|
| BUDGET_EXCEEDED | 预算超限（任务/会话级别） |
| QUOTA_EXHAUSTED | 配额耗尽（全局级别） |

### 安全设计
- 预算检查在 LLM 调用前执行，防止超支
- 支持多级限制：全局 → 用户 → 任务 → 会话
- 自动保护策略防止意外超支

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件，为扁平结构。

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `BudgetManager`：预算管理器类
- `get_budget_manager() -> BudgetManager`：获取预算管理器单例
- `CostControlConfig`：成本控制配置类
- `load_cost_control_config(config_path: str | None) -> CostControlConfig`：加载配置
- `BudgetExceededException`：预算超限异常
- `QuotaExhaustedException`：配额耗尽异常

#### budget_manager.py
职责：预算管理器
暴露接口：
- `BudgetAlertLevel`：预算告警级别枚举
- `BudgetAlertAction`：预算告警动作枚举
- `BudgetAlert`：预算告警数据类
- `UsageRecord`：使用记录数据类
- `BudgetStatus`：预算状态数据类
- `BudgetManager.__init__(config: CostControlConfig | None, alert_callback: Callable[[BudgetAlert], None] | None)`：初始化管理器
- `BudgetManager.check_budget(estimated_tokens: int, user_id: str | None, task_id: str | None, session_id: str | None) -> bool`：检查预算
- `BudgetManager.record_usage(tokens: int, model: str, user_id: str | None, task_id: str | None, session_id: str | None) -> BudgetAlert | None`：记录使用量
- `BudgetManager.get_budget_status(user_id: str | None, task_id: str | None, session_id: str | None) -> BudgetStatus`：获取预算状态
- `BudgetManager.get_usage_statistics() -> dict[str, Any]`：获取使用统计
- `BudgetManager.reset_task_budget(task_id: str) -> None`：重置任务预算
- `BudgetManager.reset_session_budget(session_id: str) -> None`：重置会话预算
- `get_budget_manager() -> BudgetManager`：获取预算管理器单例
- `reset_budget_manager() -> None`：重置预算管理器（用于测试）

#### config.py
职责：成本控制配置
暴露接口：
- `AlertThresholds`：告警阈值配置 Pydantic 模型
- `ProtectionConfig`：保护策略配置 Pydantic 模型
- `GlobalBudget`：全局预算配置 Pydantic 模型
- `CostRates`：成本费率配置 Pydantic 模型
- `UserBudget`：用户预算配置 Pydantic 模型
- `CostControlConfig`：成本控制完整配置 Pydantic 模型
- `CostControlConfig.get_model_cost_rate(model_name: str) -> float`：获取模型成本率
- `CostControlConfig.get_user_budget(user_level: str) -> UserBudget`：获取用户预算配置
- `load_cost_control_config(config_path: str | None) -> CostControlConfig`：加载配置
- `get_cost_control_config() -> CostControlConfig`：获取配置单例
- `reset_cost_control_config() -> None`：重置配置（用于测试）

#### decorators.py
职责：成本控制装饰器
暴露接口：
- `budget_check(estimated_tokens: int | None, user_id_param: str | None, task_id_param: str | None, session_id_param: str | None, model_param: str | None) -> Callable`：预算检查装饰器
- `BudgetContext`：预算上下文管理器类
- `BudgetContext.__init__(user_id: str | None, task_id: str | None, session_id: str | None)`：初始化上下文管理器
- `BudgetContext.check(estimated_tokens: int) -> bool`：检查预算
- `BudgetContext.record(tokens: int, model: str) -> None`：记录使用量
- `BudgetContext.get_status() -> BudgetStatus`：获取预算状态

#### exceptions.py
职责：成本控制模块异常定义
暴露接口：
- `CostControlException`：成本控制异常基类
- `BudgetExceededException`：预算超限异常
- `QuotaExhaustedException`：配额耗尽异常

### 测试策略
#### 模块测试
- 单元测试：预算检查、使用量记录、告警触发
- 集成测试：装饰器集成、多级限制
- Mock 策略：Mock Token 计数器

## 实现
→ 见代码文件
