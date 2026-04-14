# Agent Pipeline Configuration System

配置系统负责加载 YAML 配置、处理继承关系、实例化插件。

## 目录结构

```
config/
└── pipelines/
    ├── default.yaml       # 默认管道配置
    ├── l1-main.yaml       # L1 主 Agent 配置
    └── l2-subtask.yaml    # L2 子任务 Agent 配置
```

## 配置文件格式

### 基本结构

```yaml
pipeline:
  name: "agent_name"
  
  # Pre 插件配置 - 按 core_type 分组
  pre:
    llm_call:              # LLM 调用前的插件
      - plugin: plugin_name
        priority: 10
        config:
          key: value
    tool_execute:          # 工具执行前的插件
      - plugin: plugin_name
        priority: 20
  
  # Post 插件配置 - 按 core_type 分组
  post:
    llm_call:              # LLM 调用后的插件
      - plugin: plugin_name
        priority: 10
    tool_execute:          # 工具执行后的插件
      - plugin: plugin_name
        priority: 20
  
  # Router 插件配置 - 不分 core_type
  router:
    - plugin: router_name
      priority: 10
      config:
        key: value
```

### 配置继承

使用 `inherit` 字段实现配置继承：

```yaml
inherit: default            # 继承 default.yaml

pipeline:
  name: "l1_main_agent"
  
  # 扩展或覆盖父配置
  pre:
    llm_call:
      - plugin: trigger_inject
        priority: 5         # 覆盖优先级
```

**继承规则**：
- 子配置覆盖父配置的同名字段
- 字典类型：深度合并
- 列表类型：扩展策略（子配置的列表追加到父配置后）
- 其他类型：直接覆盖

### 环境变量替换

支持 `${VAR_NAME}` 语法：

```yaml
config:
  api_key: "${API_KEY}"
  database_url: "${DB_URL:-localhost:5432}"  # 带默认值
```

## 使用方式

### 1. 初始化配置加载器

```python
from agent_os.core import ConfigLoader, PluginRegistry

# 创建插件注册表
registry = PluginRegistry()

# 注册插件
registry.register("memory_read", MemoryReadPlugin())
registry.register("prompt_build", PromptBuildPlugin())

# 创建配置加载器
loader = ConfigLoader(
    config_dir="config/pipelines",
    registry=registry
)
```

### 2. 加载管道配置

```python
# 加载默认配置
config = loader.load_pipeline_config("default.yaml")

# 加载 L1 主 Agent 配置
config = loader.load_pipeline_config("l1-main.yaml")

# 加载 L2 子任务配置
config = loader.load_pipeline_config("l2-subtask.yaml")
```

### 3. 获取插件链

```python
# 获取 LLM 调用的 Pre 插件链
pre_chain = config.get_pre_chain("llm_call")

# 获取工具执行的 Post 插件链
post_chain = config.get_post_chain("tool_execute")

# 获取 Router 插件链
router_chain = config.get_router_chain()
```

### 4. 在管道中使用

```python
from agent_os.core import run_agent_loop, PluginContext

# 创建初始状态
initial_state = {
    "messages": [user_message],
    "session_id": "session-123",
    "iteration": 0,
}

# 运行管道
final_state = await run_agent_loop(
    pipeline_config=config,
    initial_state=initial_state,
    core_registry=core_registry,
)
```

## 插件配置规范

### 插件配置字段

每个插件配置包含三个字段：

```yaml
- plugin: plugin_name      # 必填：插件名称（注册表中的 key）
  priority: 10             # 可选：优先级（覆盖插件默认优先级）
  config:                  # 可选：插件参数
    key1: value1
    key2: value2
```

### 优先级约定

| 范围 | 含义 | 典型用途 |
|------|------|----------|
| 1-9 | 系统级 | 停止请求检查、触发器注入 |
| 10-29 | 准备级 | 上下文构建、参数注入 |
| 30-49 | 数据级 | 记忆检索、知识注入 |
| 50-69 | 构建级 | 提示词构建、工具描述 |
| 70-89 | 校验级 | 安全检查、推理拦截 |
| 90-99 | 兜底级 | 默认结束策略 |
| 100+ | 自定义 | 用户自定义插件 |

**规则**：同一范围内多个插件，小的先执行。

## 配置示例

### 示例 1：最小配置

```yaml
pipeline:
  name: "minimal_agent"
  
  pre:
    llm_call:
      - plugin: prompt_build
        priority: 50
  
  post:
    llm_call:
      - plugin: persist
        priority: 10
  
  router:
    - plugin: default_end
      priority: 100
```

### 示例 2：带环境变量

```yaml
pipeline:
  name: "production_agent"
  
  pre:
    llm_call:
      - plugin: memory_read
        priority: 40
        config:
          db_url: "${DATABASE_URL}"
          cache_ttl: "${CACHE_TTL:-300}"
```

### 示例 3：继承与覆盖

```yaml
# L2 子任务配置
inherit: default

pipeline:
  name: "l2_subtask"
  
  pre:
    tool_execute:
      - plugin: security_check
        priority: 70
        config:
          default_isolation: container  # 覆盖父配置
          require_approval: true         # 子任务需要审批
  
  router:
    - plugin: task_evaluation
      priority: 40
      config:
        evaluation_threshold: 0.8
```

## 错误处理

### 配置加载错误

```python
try:
    config = loader.load_pipeline_config("nonexistent.yaml")
except FileNotFoundError as e:
    print(f"配置文件不存在: {e}")
except ValueError as e:
    print(f"配置格式错误: {e}")
```

### 环境变量缺失

```python
# 如果环境变量不存在且无默认值，会抛出 ValueError
config:
  api_key: "${MISSING_API_KEY}"  # ValueError: Environment variable 'MISSING_API_KEY' not found
```

### 插件未注册

```python
# 如果配置中引用的插件未注册，会抛出 ValueError
pre:
  llm_call:
    - plugin: unknown_plugin  # ValueError: Plugin 'unknown_plugin' not found in registry
```

## 最佳实践

1. **配置分层**：使用继承机制，将通用配置放在 `default.yaml`，各层级 Agent 继承并扩展
2. **环境隔离**：使用环境变量管理敏感信息（API Key、数据库密码等）
3. **优先级规划**：遵循优先级约定，确保插件执行顺序正确
4. **配置验证**：启动时检查配置完整性，避免运行时错误
5. **文档同步**：配置变更时同步更新文档

## 调试技巧

### 查看合并后的配置

```python
# 加载配置
config = loader.load_pipeline_config("l1-main.yaml")

# 查看原始配置字典
import json
print(json.dumps(config.config, indent=2, ensure_ascii=False))
```

### 查看插件链

```python
# 查看 Pre 插件链
pre_chain = config.get_pre_chain("llm_call")
for plugin in pre_chain._plugins:
    print(f"{plugin.name} (priority={plugin.priority})")

# 查看 Router 插件链
router_chain = config.get_router_chain()
for plugin in router_chain._plugins:
    print(f"{plugin.name} (priority={plugin.priority})")
```

## 参考文档

- [Agent 插件化架构设计](../../docs/future/agent-plugin-architecture.md) - 完整架构设计
- [管道契约](../../docs/future/agent-plugin-architecture.md#5.3-管道契约) - State 字段约定
- [优先级约定](../../docs/future/agent-plugin-architecture.md#5.3.4-优先级约定) - 优先级规范
