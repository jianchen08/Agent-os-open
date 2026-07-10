# Config 模块 — 配置热重载与 Schema 校验

## 需求

M8 配置热重载系统需要解决两个核心问题：

1. **配置变更即时生效** — 运行中的 Agent 服务需要在不重启的情况下加载最新配置
2. **配置变更安全可靠** — 错误的配置不应破坏运行中的服务，需要提前校验

## 逻辑

### 热重载流程

```
文件变更 → watchdog 事件 → 防抖过滤 → 判断配置类型 → 调用对应重载器 → 通知回调
```

- **ConfigReloadHandler**：继承 `FileSystemEventHandler`，仅处理 `.yaml/.yml` 文件，忽略 `.`/`~` 开头的临时文件，内置防抖（`debounce_seconds`）
- **ConfigReloader**：管理 watchdog Observer 生命周期，通过 `register_reloader` 注入各配置类型的重载函数（与具体 Registry 解耦），通过 `add_callback` 注册变更通知回调
- 配置类型判断基于路径关键词（`pipelines`/`agents`/`templates`/`triggers`）

### Schema 校验策略

| 配置类型 | 必填字段 | 额外校验 |
|---------|---------|---------|
| Pipeline | `name`, `input_routes`, `output_routes` | 类型检查（name=str, routes=list） |
| Agent | `config_id`, `name` | `level` 合法值 L1/L2/L3，`agent_type` 合法值 main/specialized/system |

- 不引入 jsonschema，自实现简化校验（与 M7 SchemaValidator 一致）
- `auto` 模式根据路径关键词和内容特征自动判断配置类型

### 集成方式

```python
# 与 AgentRegistry 集成示例
from config import ConfigReloader

reloader = ConfigReloader(config_dir="config")
reloader.register_reloader("agent", agent_registry.load_from_file)
reloader.register_reloader("pipeline", pipeline_registry.reload)
reloader.add_callback(on_config_changed)
reloader.start()
```

## 结构

### 文件清单

| 文件 | 职责 |
|------|------|
| `reload.py` | 配置热重载（ConfigReloadHandler + ConfigReloader） |
| `schema.py` | 配置 Schema 校验（ConfigSchemaValidator） |
| `__init__.py` | 公共 API 导出 |
| `README.md` | 模块文档 |
