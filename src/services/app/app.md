# 应用服务组件

## 需求
### 职责
封装任务相关的业务逻辑，协调任务服务和看门狗服务，提供任务启动、审批、续执行等功能。

### 对外接口
- 输入：任务ID、用户ID、操作参数
- 输出：操作结果（dict）

### 依赖
- 依赖模块：src.services.task_service（任务服务）
- 依赖模块：src.tasks.services.approval_service（审批服务）
- 依赖模块：src.tasks.services.continuation_service（续执行服务）
- 依赖模块：src.services.watchdog_service（看门狗服务）

## 逻辑
### 流程设计
1. 启动任务：验证任务状态 → 获取看门狗 → 手动触发执行
2. 审批任务：获取阻塞任务 → 处理审批决策 → 更新任务状态
3. 续执行：验证任务状态 → 触发续执行

### 数据流向
```
用户请求 → TaskAppService → TaskService/ApprovalService/ContinuationService → 数据库
用户请求 → TaskAppService → WatchdogService → 任务执行
```

### 错误处理
- HTTPException：任务不存在、状态不允许、服务不可用
- 404 NOT_FOUND：任务不存在或无权访问
- 400 BAD_REQUEST：状态不允许操作
- 503 SERVICE_UNAVAILABLE：看门狗服务未启动

## 结构
### 子组件清单（文件夹 - 抽象说明）
无子组件，为原子服务组件。

### 文件清单（代码文件 - 具体接口）
#### task_app_service.py
职责：任务应用服务，封装任务相关业务逻辑
暴露接口：
- `TaskAppService`：任务应用服务类
  - `__init__(session: AsyncSession)`：初始化
  - `async start_task_execution(task_id: str, user_id: str) -> dict[str, Any]`：手动启动任务执行
  - `async get_blocked_tasks(user_id: str, limit: int = 50) -> list[dict[str, Any]]`：获取待审核任务列表
  - `async process_task_approval(task_id: str, action: str, reason: str | None, user_id: str) -> dict[str, Any]`：处理任务审批
  - `async trigger_task_continuation(task_id: str, user_id: str) -> dict[str, Any]`：手动触发任务续执行

### 测试策略
#### 组件测试
- 单元测试：各业务方法
- 集成测试：与数据库和看门狗服务的集成
- Mock策略：数据库会话 Mock、看门狗服务 Mock

## 实现
→ 见代码文件
