# 节点组件

## 需求
### 职责
提供 LangGraph StateGraph 中的节点函数，包括 LLM 调用、工具执行、人工审批、评估提醒和路由决策。

### 对外接口
- 输入：AgentState 状态对象、RunnableConfig 配置
- 输出：状态更新字典

### 依赖
- 依赖模块：`src.agents.state`、`src.agents.formatters`、`src.agents.utils`、`src.tools`、`src.core`、`src.memory`
- 依赖库：LangGraph、LangChain

## 逻辑
### 流程设计
节点执行流程：

1. **call_model_node**：
   - 从 LayeredContextStore 构建消息列表
   - 处理工具描述和绑定
   - 调用 LLM（支持思考模式、结构化输出）
   - 处理响应并创建执行记录

2. **execute_tools_node**：
   - 获取待执行的工具调用
   - 检测重复调用
   - 创建执行记录
   - 执行工具并更新记录
   - 格式化结果消息

3. **human_approval_node**：
   - 构建审批请求
   - 等待用户响应
   - 处理审批结果

4. **evaluate_reminder_node**：
   - 注入提醒消息
   - 提示 Agent 检查任务完成情况

5. **should_continue**：
   - 使用路由引擎评估状态
   - 决定下一步节点

### 数据流向
```
AgentState → call_model_node → LLM 响应
         → execute_tools_node → 工具执行结果
         → should_continue → 路由决策
```

## 结构
### 文件清单（代码文件 - 具体接口）
#### call_model.py
职责：调用 LLM 模型节点
暴露接口：
- `call_model_node(state: AgentState, config: RunnableConfig | None) -> dict[str, Any]`：调用 LLM 节点函数
- `_build_tools_description(tools: list[Any]) -> str`：构建工具描述文本

#### execute_tools.py
职责：执行工具节点
暴露接口：
- `execute_tools_node(state: AgentState, config: RunnableConfig | None) -> dict[str, Any]`：执行工具节点函数
- `_get_injected_params(tool_executor: Any, tool_name: str) -> list[str]`：获取注入参数列表
- `_get_injected_value(param: str, context: dict[str, Any], record_id: str | None) -> Any`：获取注入参数值

#### helpers.py
职责：节点辅助函数
暴露接口：
- `_create_execution_record(session_id: str, tool_name: str, tool_args: dict, tool_id: str, task_id: str | None, ...) -> str | None`：创建工具执行记录
- `_update_execution_record(record_id: str, session_id: str, tool_name: str, success: bool, output: Any, error: str | None, duration_ms: int) -> None`：更新执行记录
- `_add_message_to_context_store(layered_context_store: Any, content: str, tool_name: str, tool_id: str) -> None`：添加消息到上下文存储
- `_build_result(state: dict[str, Any], tool_messages: list[Any], new_tool_calls: list[dict[str, Any]], layered_context_store: Any | None) -> dict[str, Any]`：构建执行结果

#### human_approval.py
职责：人工审批节点
暴露接口：
- `human_approval_node(state: AgentState) -> dict[str, Any]`：人工审批节点函数
- `request_human_approval(thread_id: str, title: str, description: str, operation: str, ...) -> tuple[bool, dict[str, Any] | None]`：请求人工审批
- `request_conversation(thread_id: str, title: str, topic: str, ...) -> tuple[bool, str, list[dict[str, Any]]]`：请求与用户对话

#### reminders.py
职责：评估提醒节点
暴露接口：
- `evaluate_reminder_node(state: AgentState) -> dict[str, Any]`：评估提醒节点函数

#### routing.py
职责：路由函数
暴露接口：
- `should_continue(state: AgentState) -> Literal["tools", "evaluate_reminder", "end"]`：条件路由函数

#### __init__.py
职责：模块导出
暴露接口：
- `call_model_node`、`execute_tools_node`、`human_approval_node`、`evaluate_reminder_node`、`should_continue`

### 测试策略
#### 组件测试
- 单元测试：各节点函数的核心逻辑
- 集成测试：节点间的协作流程
- Mock 策略：LLM 客户端、工具执行器 Mock

## 实现
→ 见代码文件
