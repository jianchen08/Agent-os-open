# 协调器组件

## 需求
### 职责
提供多种协调器，解耦 AgentLoop 的职责，包括 LLM 客户端管理、记忆服务集成、监控管理、工具管理和隔离系统协调。

### 对外接口
- 输入：Agent 配置、会话上下文、依赖组件
- 输出：初始化后的服务实例、执行结果

### 依赖
- 依赖模块：`src.agents.types`、`src.llm`、`src.memory`、`src.tools`、`src.isolation`、`src.monitoring`
- 依赖库：LangChain、Pydantic

## 逻辑
### 流程设计
各协调器独立运作，通过依赖注入接收配置和外部组件：

1. **LLMCoordinator**：创建和管理 LLM 客户端，提供 LangChain 兼容接口
2. **MemoryCoordinator**：初始化记忆检索、嵌入服务、分层上下文存储
3. **MonitoringCoordinator**：管理用量监控和任务进度
4. **ToolCoordinator**：加载工具、转换为 LangChain 格式、处理 subagent 调用
5. **IsolationCoordinator**：决策工具隔离级别、管理隔离环境生命周期

### 数据流向
```
AgentConfig → 各协调器初始化 → 服务实例 → AgentLoop 使用
```

### 配置设计
#### IsolationConfig 隔离配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| enabled | 全局开关 | True |
| enable_fallback | 降级开关 | True |
| default_level | 默认隔离级别 | host |
| whitelist | 工具白名单 | read, write, grep 等 |
| blacklist | 工具黑名单 | bash, shell_execute 等 |
| max_environments | 最大环境数 | 10 |
| environment_ttl | 环境生存时间(秒) | 3600 |

## 结构
### 文件清单（代码文件 - 具体接口）
#### llm_coordinator.py
职责：LLM 客户端协调器
暴露接口：
- `LLMCoordinator(config: AgentConfig, llm_factory: LLMFactory | None)`：协调器类
  - `get_langchain_llm() -> BaseChatModel`：获取 LangChain 兼容客户端
  - `get_native_llm() -> LLMClient`：获取原生 LLM 客户端
  - `cleanup() -> None`：清理资源

#### memory_coordinator.py
职责：记忆协调器
暴露接口：
- `MemoryCoordinator(config: AgentConfig, user_id: str | None, session_id: str | None, ...)`：协调器类
  - `initialize() -> None`：初始化所有记忆组件
  - `retriever -> IRetriever | None`：记忆检索器属性
  - `embedding_service -> IEmbeddingService | None`：嵌入服务属性
  - `layered_context_store -> LayeredContextStore | None`：分层上下文存储属性
  - `context_builder -> ContextBuilder | None`：上下文构建器属性
  - `knowledge_injection -> KnowledgeInjectionService | None`：知识注入服务属性
  - `register_dynamic_tools(tool_registry, evaluator_callback) -> dict[str, Any]`：注册动态工具
  - `cleanup() -> None`：清理资源
  - `get_memory_stats() -> dict[str, Any]`：获取记忆统计
  - `search_memories(query, memory_types, top_k) -> dict[str, Any]`：搜索记忆

#### monitoring_coordinator.py
职责：监控协调器
暴露接口：
- `MonitoringCoordinator(session_id: str, user_id: str | None, ...)`：协调器类
  - `initialize() -> None`：初始化监控组件
  - `usage_monitor -> IUsageMonitor | None`：用量监控器属性
  - `task_progress_manager -> ITaskProgressManager | None`：任务进度管理器属性
  - `get_usage_statistics() -> dict[str, Any] | None`：获取用量统计
  - `cleanup() -> None`：清理资源

#### tool_coordinator.py
职责：工具协调器
暴露接口：
- `ToolCoordinator(tool_ids: list[str], tool_registry: ToolRegistry, tool_executor: ToolExecutor, ...)`：协调器类
  - `get_tools_for_graph() -> list[Any]`：获取 LangGraph 可用工具列表
  - `execute_tool(tool_name: str, arguments: dict, context: ExecutionContext | None) -> ToolResult`：执行工具
  - `cleanup() -> None`：清理资源

#### isolation_config.py
职责：隔离配置定义
暴露接口：
- `IsolationConfig`：隔离配置数据类
  - `from_file(path: str) -> IsolationConfig`：从文件加载
  - `to_file(path: str) -> None`：保存到文件
  - `get_tool_policy(tool_name: str) -> IsolationLevel | None`：获取工具隔离策略
  - `is_tool_whitelisted(tool_name: str) -> bool`：检查白名单
  - `is_tool_blacklisted(tool_name: str) -> bool`：检查黑名单
  - `should_isolate_category(category: str) -> bool`：判断分类是否隔离
  - `validate() -> bool`：验证配置
- `get_default_config() -> IsolationConfig`：获取默认配置
- `load_config(path: str | None) -> IsolationConfig`：加载配置

#### isolation_coordinator.py
职责：隔离协调器
暴露接口：
- `IsolationCoordinator(config: IsolationConfig | None, isolation_manager: IsolationManager | None)`：协调器类
  - `initialize() -> None`：初始化协调器
  - `should_isolate(tool_name: str, context: ExecutionContext) -> bool`：判断是否需要隔离
  - `pre_execute(tool_name: str, inputs: dict, context: ExecutionContext) -> IsolationContext`：执行前处理
  - `execute(tool_name: str, inputs: dict, context: ExecutionContext, isolation_ctx: IsolationContext, original_executor: Callable) -> ToolResult`：执行工具
  - `post_execute(tool_name: str, context: ExecutionContext, result: ToolResult) -> None`：执行后处理
  - `cleanup() -> None`：清理资源
- `create_isolation_coordinator(config: IsolationConfig | None, config_path: str | None) -> IsolationCoordinator`：创建协调器
- `get_isolation_coordinator(config_path: str | None) -> IsolationCoordinator`：获取单例协调器

#### isolation_tool_wrapper.py
职责：隔离工具执行包装器
暴露接口：
- `IsolationToolWrapper(original_executor: ToolExecutor, isolation_coordinator: IsolationCoordinator)`：包装器类
  - `execute(tool_name: str, inputs: dict, context: ExecutionContext, **kwargs) -> ToolResult`：执行工具（带隔离）
  - `get_stats() -> dict[str, int]`：获取执行统计
  - `reset_stats() -> None`：重置统计
  - `initialize() -> None`：初始化包装器
  - `cleanup() -> None`：清理资源
- `wrap_executor_with_isolation(executor: ToolExecutor, coordinator: IsolationCoordinator) -> IsolationToolWrapper`：包装执行器

#### __init__.py
职责：模块导出
暴露接口：
- `LLMCoordinator`、`MemoryCoordinator`、`MonitoringCoordinator`、`ToolCoordinator`
- `IsolationConfig`、`load_config`、`IsolationCoordinator`、`IsolationToolWrapper` 等

### 测试策略
#### 组件测试
- 单元测试：各协调器的初始化、资源清理
- 集成测试：协调器与实际服务的集成
- Mock 策略：外部依赖（LLM、数据库）Mock

## 实现
→ 见代码文件
