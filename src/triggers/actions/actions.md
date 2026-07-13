# 触发器动作组件

## 需求
### 职责
执行触发器的各种动作，包括通知发送、API 调用、任务重试、任务完成和自定义动作。

### 对外接口
- 输入：动作配置（ActionConfig）、执行上下文
- 输出：执行结果（ExecutionResult）

### 依赖
- 依赖模块：src.triggers.models（触发器模型）
- 依赖模块：src.core.di（依赖注入容器）
- 依赖模块：httpx（HTTP 客户端）
- 依赖模块：jinja2（模板渲染）

## 逻辑
### 流程设计
1. 接收动作配置和上下文
2. 根据动作类型选择处理器
3. 执行动作
4. 返回执行结果

### 数据流向
```
ActionConfig + Context → ActionExecutor → Handler → ExecutionResult
```

### 错误处理
- 返回 ExecutionResult 包含 success、message、error 字段
- 记录执行日志便于排查

## 结构
### 子组件清单（文件夹 - 抽象说明）
无子组件，为原子服务组件。

### 文件清单（代码文件 - 具体接口）
#### executor.py
职责：动作执行器，执行各类触发器动作
暴露接口：
- `ActionExecutor`：动作执行器类
  - `__init__()`：初始化
  - `async execute(action_config: ActionConfig, context: dict[str, Any]) -> ExecutionResult`：执行动作
  - `register_custom_handler(name: str, handler)`：注册自定义处理器
  - `unregister_custom_handler(name: str) -> bool`：注销自定义处理器
  - `list_custom_handlers() -> list[str]`：列出所有自定义处理器
  - `async process_retry_queue()`：处理重试队列
  - `get_retry_queue_status() -> dict[str, Any]`：获取重试队列状态

  支持的动作类型：
  - `ActionType.NOTIFICATION`：通知发送（WebSocket、数据库、Webhook）
  - `ActionType.API_CALL`：API 调用（GET、POST、PUT、DELETE）
  - `ActionType.TASK_RETRY`：任务重试（支持指数退避）
  - `ActionType.TASK_COMPLETE`：任务完成
  - `ActionType.CUSTOM`：自定义动作

### 测试策略
#### 组件测试
- 单元测试：各动作处理器
- 集成测试：与外部服务的集成（WebSocket、HTTP）
- Mock策略：HTTP 客户端 Mock、依赖注入容器 Mock

## 实现
→ 见代码文件
