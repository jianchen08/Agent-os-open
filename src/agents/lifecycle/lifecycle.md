# 生命周期管理组件

## 需求
### 职责
提供 Agent 生命周期管理能力，包括检查点管理、经验沉淀、状态维护和转换。

### 对外接口
- 输入：用户/会话信息、数据库会话、嵌入服务
- 输出：状态查询结果、经验存储确认

### 依赖
- 依赖模块：`src.agents.interfaces`、`src.agents.types`、`src.db.models`
- 依赖库：asyncio

## 逻辑
### 流程设计
**LifecycleManager**：
1. 管理后台任务集合，防止内存泄漏
2. 存储成功经验到 EpisodesMemory 表
3. 支持异步调度经验存储
4. 清理已完成的任务

**StateManager**：
1. 维护 Agent 生命周期状态（IDLE/RUNNING/PAUSED/STOPPED）
2. 处理停止/暂停请求
3. 追踪工具调用记录

### 数据流向
```
Agent 执行 → StateManager 状态转换 → LifecycleManager 经验存储 → 数据库
```

### 数据模型
#### AgentLifecycleState 枚举
| 状态 | 说明 |
|------|------|
| IDLE | 空闲状态 |
| RUNNING | 运行中 |
| PAUSED | 已暂停 |
| STOPPED | 已停止 |

## 结构
### 文件清单（代码文件 - 具体接口）
#### lifecycle_manager.py
职责：生命周期管理器
暴露接口：
- `LifecycleManager(user_id: str | None, session_id: str | None, db_session: Any | None, embedding_service: IEmbeddingService | None, enable_learning: bool)`：管理器类
  - `store_experience(intent: str, result: str, iterations: int, tool_calls: list[ToolCallRecord], tags: list[str] | None) -> None`：存储成功经验
  - `schedule_experience_storage(intent: str, result: str, iterations: int, tool_calls: list[ToolCallRecord], tags: list[str] | None) -> None`：调度经验存储（异步）
  - `cleanup_tasks() -> None`：清理已完成任务
  - `cleanup() -> None`：清理所有后台任务

#### state_manager.py
职责：状态管理器
暴露接口：
- `StateManager(initial_state: AgentLifecycleState)`：管理器类
  - `state -> AgentLifecycleState`：当前状态属性
  - `stop_requested -> bool`：停止请求属性
  - `pause_requested -> bool`：暂停请求属性
  - `tool_calls -> list[ToolCallRecord]`：工具调用记录属性
  - `set_state(state: AgentLifecycleState) -> None`：设置状态
  - `request_stop() -> None`：请求停止
  - `request_pause() -> None`：请求暂停
  - `resume() -> None`：恢复执行
  - `reset() -> None`：重置状态
  - `prepare_for_execution() -> None`：准备执行
  - `add_tool_call(tool_call: ToolCallRecord) -> None`：添加工具调用记录
  - `set_tool_calls(tool_calls: list[ToolCallRecord]) -> None`：设置工具调用记录
  - `get_tool_calls_count() -> int`：获取工具调用次数
  - `should_stop() -> bool`：是否应该停止
  - `cleanup() -> None`：清理资源

#### __init__.py
职责：模块导出
暴露接口：
- `LifecycleManager`
- `StateManager`

### 测试策略
#### 组件测试
- 单元测试：状态转换、任务管理、经验存储
- 集成测试：与数据库的集成
- 并发测试：后台任务的并发处理

## 实现
→ 见代码文件
