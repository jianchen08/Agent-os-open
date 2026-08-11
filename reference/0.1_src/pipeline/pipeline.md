# Pipeline 模块文档

## 需求

管道核心框架，是 Agent OS 的执行引擎。核心思路是将 AI Agent 的处理流程抽象为**管道循环**：

```
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route
```

管道需要：
1. **驱动循环**：while 循环持续执行直到 `ended=True` 或挂起（`wait`）
2. **路由控制**：输入路由表决定插件选取和目标，输出路由表仲裁路由信号
3. **插件调度**：按优先级执行插件链，支持错误策略（ABORT/SKIP/RETRY/FALLBACK）
4. **暂停/恢复**：支持管道挂起与外部唤醒
5. **配置驱动**：从 YAML 文件加载管道配置并动态实例化插件

## 逻辑

### 管道循环流程

```
PipelineEngine.run()
  → 构建初始 state（user_input + agent_config + conversation_history）
  → while not ended:
      1. 递增迭代计数器（安全阀：max_iterations）
      2. InputRouteTable.resolve_plugins() → 解析插件列表
      3. 执行 Input 插件链 → 更新 state
      4. InputRouteTable.resolve_target() → 解析目标（core/end/wait）
      5. target == "end" → 写入拦截原因，结束
      6. target == "wait" → 保存 state 快照，挂起等待唤醒
      7. 获取 Core 插件 → 执行（含重试和错误恢复）
      8. 获取 Output 插件 → PluginChain 执行
      9. 收集 route_signals → OutputRouteTable.arbitrate() 仲裁
     10. apply_route() → 更新 core_type 或结束管道
  → 管道结束后执行一次终态 Output 插件链
  → 返回最终 state
```

### 插件接口层次

```
IPlugin (ABC)
  ├── IInputPlugin   → 返回 PluginResult（state_updates + skip_remaining）
  ├── ICorePlugin    → 返回 dict[str, Any]（直接合并到 state）
  └── IOutputPlugin  → 返回 OutputResult（state_updates + route_signal）
```

### 路由表设计

| 路由表 | 匹配策略 | 用途 |
|--------|---------|------|
| InputRouteTable | 可叠加（所有匹配条目合并插件列表） | 决定执行哪些 Input 插件及 target |
| OutputRouteTable | 互斥优先级（首匹配生效） | 仲裁 Output 插件产生的路由信号 |

### 路由信号

| route_type | 含义 | apply_route 行为 |
|-----------|------|-----------------|
| next_llm | 下一轮调用 LLM | state["core_type"] = "llm_call" |
| next_tool | 下一轮执行工具 | state["core_type"] = "tool_execute" |
| end | 管道结束 | state["ended"] = True |
| wait | 管道挂起 | 保存 state 快照，await 唤醒事件 |

### 插件执行链

```
PluginChain(plugins)
  → 按 priority 排序（数值小先执行）
  → 顺序执行 execute()
  → 每次执行后 state_updates 合并到 ctx.state
  → skip_remaining=True 时跳过后续插件
  → 错误策略：
      ABORT → skip_remaining=True + 记录错误
      SKIP  → 记录警告，继续
      FALLBACK → 使用 fallback_state 替代结果
      RETRY → 由调用方实现重试循环
```

### 配置加载

```
YAML 文件 → PipelineConfig
  → 环境变量替换（${ENV_VAR} → os.environ.get）
  → 动态导入插件类（importlib）
  → 实例化插件 → 注册到 PluginRegistry
  → 构建 InputRouteTable + OutputRouteTable
  → 组装 PipelineEngine
```

### 暂停/恢复机制

```
管道挂起：
  → _suspend_and_wait() → 保存 state 快照到 _suspended_state
  → 注册到 ServiceProvider 供外部查找
  → await _wake_event (超时 600s)

管道唤醒：
  → wake() → 设置 _wake_event
  → inject_and_wake(user_input) → 注入消息 + 唤醒
  → 管道从挂起点继续循环
```

### Agent 配置注入

```
run(user_input, agent_config)
  → agent_config.to_state() → 合并到 state
  → _apply_agent_plugin_configs() → 合并插件配置覆盖
  → _apply_agent_model_override() → 切换 LLM 模型
      Router 模式：切换路由别名
      直连模式：从 llm.yaml 重建插件
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `types.py` | TargetType, StateKeys, ErrorPolicy, RouteSignal, create_initial_state | 管道核心类型定义 |
| `plugin.py` | IPlugin, IInputPlugin, ICorePlugin, IOutputPlugin, PluginContext, PluginResult, OutputResult, find_plugin_config | 插件接口与上下文 |
| `chain.py` | PluginChain | 插件执行链（排序 + 错误策略） |
| `route.py` | InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable | 路由表（输入可叠加 + 输出互斥优先级） |
| `engine.py` | PipelineEngine | 管道引擎（核心循环 + 暂停/恢复 + Agent 注入） |
| `registry.py` | PluginRegistry, EngineRegistry, get_engine_registry | 插件与引擎注册表 |
| `config.py` | PipelineConfig, PipelineConfigBuilder | 管道配置加载与构建 |
| `config_store.py` | PipelineConfigStore | 管道配置存储（pipeline_id → PipelineConfig） |
| `condition_parser.py` | parse_condition | 安全条件表达式解析器（替代 eval） |
| `event_bus.py` | EventBus | 轻量事件总线（跨管道通信） |
| `hot_swap.py` | — | 配置热替换支持 |
| `rollback.py` | — | 状态回滚支持 |

### 依赖关系

```
types.py ← plugin.py ← chain.py ← engine.py
                       ← route.py ← engine.py
                       ← registry.py ← engine.py
config.py → registry.py
config_store.py → config.py
condition_parser.py → route.py
event_bus.py → registry.py
```

### 外部依赖

- `pyyaml` — YAML 配置解析
- Python 标准库：asyncio, importlib, logging, dataclasses, abc
