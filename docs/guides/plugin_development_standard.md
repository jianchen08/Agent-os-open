# 插件开发标准规范

<!--
============================================================
【模板是什么】
Agent OS 管道插件的开发标准规范，定义命名、目录结构、配置格式、接口约束等标准化要求。

【模板的作用】
1. 规范插件开发流程 — 确保所有插件遵循统一标准
2. 提供可检查的规范条目 — 支持自动化验证
3. 作为插件评审的依据 — 保证代码质量一致性
4. 降低新插件开发门槛 — 明确约定优于配置

【如何使用本模板】
1. 开发新插件前，通读本规范了解约束
2. 开发过程中，对照各章节检查合规性
3. 开发完成后，使用 plugin_validator 工具自动验证
4. 评审时，以本规范作为评审标准

【适用场景】
- 开发新的 Input / Output / Core 插件
- 修改现有插件时确认是否仍符合规范
- 评审插件代码质量
- 自动化 CI 检查插件合规性

【占位符说明】
- `{plugin_name}`：插件名称，如 `memory_read`、`stop_check`
- `{PluginClass}`：插件类名，如 `MemoryReadPlugin`、`StopCheckPlugin`
============================================================
-->

---

## 1. 命名规范 [必填]

<!--
> **章节说明**：定义插件的命名规则，包括文件名、类名、配置键名等。
> **填写要求**：所有插件必须严格遵循命名规范，否则验证器不通过。
-->

### 1.1 插件名称命名

| 命名对象 | 格式 | 示例 | 说明 |
|----------|------|------|------|
| 插件标识名 | `snake_case` | `memory_read`、`stop_check` | 用于配置引用、目录名、日志 |
| 目录名 | 与插件标识名一致 | `src/plugins/input/memory_read/` | 每个插件一个独立目录 |
| 主文件名 | 与插件标识名一致 | `memory_read.py` | 目录内主模块文件 |
| 配置键名 | 与插件标识名一致 | `plugins:` 下的 `name: memory_read` | YAML 配置中引用 |

### 1.2 类名命名

| 插件类型 | 类名格式 | 示例 |
|----------|----------|------|
| Input 插件 | `{CamelCase}Plugin` | `MemoryReadPlugin`、`SecurityCheckPlugin` |
| Output 插件 | `{CamelCase}Plugin` | `StopCheckPlugin`、`DuplicateCheckPlugin` |
| Core 插件 | `{CamelCase}Core` | `LLMCore`、`ToolCore` |

### 1.3 命名禁区

| 禁止项 | 原因 | 正确做法 |
|--------|------|----------|
| 驼峰目录名 | 不符合 Python 惯例 | 使用 `snake_case` |
| 缩写类名 | 可读性差 | 使用完整单词 |
| 与内置插件同名 | 注册冲突 | 添加有意义的修饰前缀 |
| 数字开头 | Python 标识符限制 | 以字母开头 |

---

## 2. 目录结构规范 [必填]

<!--
> **章节说明**：定义插件的标准化目录结构和文件组织方式。
> **填写要求**：所有插件必须遵循此目录结构。
-->

### 2.1 标准目录结构

```
src/plugins/
├── {plugin_type}/                    # Input | Core | Output
│   ├── __init__.py                   # 模块入口（重导出插件类）
│   └── {plugin_name}/                # 插件目录
│       ├── __init__.py               # 导出插件类
│       ├── {plugin_name}.py          # 主插件逻辑
│       └── tests/                    # [可选] 插件专属测试
│           ├── __init__.py
│           └── test_{plugin_name}.py
```

### 2.2 目录层级规则

| 规则 | 说明 |
|------|------|
| 插件类型一级目录 | 必须是 `input`、`core`、`output` 之一 |
| 插件二级目录 | 与插件标识名一致，每个插件一个独立目录 |
| 文件深度 | 主逻辑文件不超过 2 层（`{type}/{name}/{name}.py`） |
| 测试位置 | 推荐 `tests/` 子目录，也可放在项目级 `tests/` 中 |

### 2.3 `__init__.py` 规范

插件目录的 `__init__.py` 必须导出插件主类：

```python
"""{plugin_name} 插件 — {一句话描述}"""

from plugins.{plugin_type}.{plugin_name}.{plugin_name} import {PluginClass}

__all__ = ["{PluginClass}"]
```

---

## 3. 配置格式规范 [必填]

<!--
> **章节说明**：定义插件在管道配置文件（YAML）中的标准格式。
> **填写要求**：所有插件配置必须遵循此格式。
-->

### 3.1 管道配置中的插件声明

```yaml
# config/pipelines/default.yaml

plugins:
  - name: {plugin_name}          # 必填，与目录名一致
    config:
      enabled: true              # 必填，是否启用
      # ... 插件特有配置项

core_plugins:
  {core_type}:                   # 必填，如 llm_call / tool_execute
    class: plugins.{type}.{name}.{name}.{PluginClass}
    config:
      # ... 核心插件配置项
```

### 3.2 配置项规范

| 配置项 | 必填 | 类型 | 说明 |
|--------|------|------|------|
| `name` | 是 | string | 插件标识名 |
| `config.enabled` | 是 | boolean | 是否启用 |
| `config` 内其他项 | 按需 | any | 插件特有配置参数 |

### 3.3 配置值约束

- **布尔值**：使用 `true` / `false`，不用 `yes` / `no`
- **数值**：带单位注释（如 `timeout_seconds: 300`）
- **字符串**：不使用引号包裹，除非包含特殊字符
- **默认值**：每个配置项必须有合理默认值，插件不应因缺少配置而崩溃

---

## 4. 接口约束规范 [必填]

<!--
> **章节说明**：定义插件实现时必须遵守的接口约束。
> **填写要求**：所有插件代码必须满足这些约束。
-->

### 4.1 基类继承

| 插件类型 | 必须继承 | execute 返回类型 |
|----------|----------|------------------|
| Input 插件 | `IInputPlugin` | `PluginResult` |
| Core 插件 | `ICorePlugin` | `dict[str, Any]` |
| Output 插件 | `IOutputPlugin` | `OutputResult` |

### 4.2 必须实现的属性和方法

```python
from pipeline.plugin import IInputPlugin, ICorePlugin, IOutputPlugin
from pipeline.plugin import PluginContext, PluginResult, OutputResult

class {PluginClass}(IInputPlugin):
    """插件简述。"""

    error_policy: ErrorPolicy = ErrorPolicy.ABORT  # 必填：声明错误策略

    @property
    def name(self) -> str:
        """插件唯一标识名称，必须与目录名和配置名一致。"""
        return "{plugin_name}"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return {priority_value}

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行插件逻辑。"""
        # 实现逻辑
        return PluginResult(state_updates={...})
```

### 4.3 构造函数约束

```python
class {PluginClass}(IInputPlugin):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置字典，从 YAML 加载
        """
        self._config = config or {}
        # 从 config 读取参数，带默认值
        self._enabled = self._config.get("enabled", True)
```

**关键约束**：

1. 构造函数必须接受 `config: dict[str, Any] | None = None` 参数
2. 所有配置值必须有默认值，`config` 为 `None` 时插件仍可正常构造
3. 不在构造函数中执行 I/O 操作或外部依赖初始化

### 4.4 execute 方法约束

| 约束 | 说明 |
|------|------|
| 必须是 `async` | 管道引擎异步调用 |
| 参数只能是 `ctx: PluginContext` | 不接受其他参数 |
| 必须返回对应类型 | Input→`PluginResult`，Core→`dict`，Output→`OutputResult` |
| 不抛出未捕获异常 | 异常由插件链按 `error_policy` 处理 |
| 不直接修改 `ctx.state` | 通过返回 `state_updates` 让引擎合并 |

### 4.5 State 命名空间约定

| 插件类型 | 写入命名空间 | 读取命名空间 |
|----------|-------------|-------------|
| Input | `context.*`、`knowledge.*`、`prompt.*`、`tool.*`、`security.*` | `session.*`、`task.*` |
| Core | `raw_result`、`raw_tool_calls`、`raw_thinking` | 所有 Input 写入的命名空间 |
| Output | `router.*`、`track.*`、`memory.*`、`evaluation.*` | `raw_result`、`execution_status` |

**规则**：
- 写入 state 时使用 `{namespace}.{key}` 格式，如 `security.decision`、`memory.context`
- 不写入其他插件的命名空间
- 读取其他插件写入的 state 时，做好键不存在的防御（使用 `.get()` 并提供默认值）

### 4.6 类型插槽使用规范

插件如需注册自定义类型（枚举、常量、状态键、处理函数），通过 `register_types` 类方法：

```python
@classmethod
def register_types(cls, slots: PluginTypeSlot) -> None:
    """注册插件自定义类型。"""
    slots.register_enum("my_plugin", "status", ["idle", "running", "done"])
    slots.register_constant("my_plugin", "max_retries", 3)
    slots.register_state_key("my_plugin", "attempt_count", default=0)
```

**规则**：
- 命名空间使用插件标识名（`plugin_name`）
- 不使用其他插件的命名空间
- 注册操作只在 `register_types` 中进行，不在 `execute` 中动态注册

---

## 5. 错误策略规范 [必填]

<!--
> **章节说明**：定义插件错误策略的选择标准。
> **填写要求**：每个插件必须声明正确的错误策略。
-->

### 5.1 策略选择标准

| 策略 | 适用场景 | 行为 | 典型插件 |
|------|----------|------|----------|
| `ABORT` | 不确定就不能继续 | 跳过后续插件 + 记录错误 | 安全检查、停止判断、参数注入 |
| `FALLBACK` | 降级也能跑 | 使用 `fallback_state` 替代结果 | 上下文构建、工具 Schema |
| `SKIP` | 失败不影响当轮结果 | 记录警告，继续执行 | 记忆写入、追踪统计、格式化 |
| `RETRY` | 瞬态错误可重试 | 由调用方实现重试循环 | 外部 API 调用 |

### 5.2 错误处理最佳实践

```python
async def execute(self, ctx: PluginContext) -> PluginResult:
    try:
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)
    except SpecificException as e:
        logger.warning("Plugin %s encountered expected error: %s", self.name, e)
        return PluginResult(state_updates={}, error=e)
    except Exception as e:
        logger.error("Plugin %s unexpected error: %s", self.name, e)
        return PluginResult(state_updates={}, error=e)
```

---

## 6. 日志规范 [可选]

<!--
> **章节说明**：定义插件的日志记录标准。
-->

### 6.1 Logger 命名

```python
import logging
logger = logging.getLogger(__name__)
```

使用 `__name__` 自动生成模块路径 logger，不硬编码字符串。

### 6.2 日志级别使用

| 级别 | 使用场景 |
|------|----------|
| `DEBUG` | 插件内部状态、配置参数、执行细节 |
| `INFO` | 插件启停、关键决策、配置变更 |
| `WARNING` | 非致命错误、降级操作、兼容性问题 |
| `ERROR` | 执行失败、异常捕获 |

### 6.3 日志格式

```python
logger.info("Plugin %s started with config: %s", self.name, self._config)
logger.warning("Plugin %s fallback triggered: %s", self.name, reason)
logger.error("Plugin %s execution failed: %s", self.name, exc)
```

---

## 7. 测试规范 [必填]

<!--
> **章节说明**：定义插件的测试要求。
-->

### 7.1 测试覆盖要求

| 测试类型 | 必要性 | 说明 |
|----------|--------|------|
| 单元测试 | 必选 | 覆盖 `execute` 方法的核心路径 |
| 错误路径测试 | 必选 | 验证 error_policy 行为 |
| 配置默认值测试 | 推荐 | 验证 config=None 时正常工作 |
| 集成测试 | 推荐 | 与其他插件的 state 交互 |

### 7.2 测试 Mock 规范

```python
from unittest.mock import MagicMock
from pipeline.plugin import PluginContext

def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(
        state=state or {},
        config=config or {},
        _services={},
    )
```

**规则**：
- 所有测试使用 Mock 的 `PluginContext`，不依赖真实 LLM 调用
- 不依赖外部服务（网络、数据库、文件系统）
- 测试可独立运行，不需要特定环境

---

## 8. 文档规范 [可选]

<!--
> **章节说明**：定义插件的文档要求。
-->

### 8.1 模块文档字符串

```python
"""{plugin_name} 插件 — {一句话描述}。

{详细描述插件的功能、职责和在管道中的位置。}

State 读写:
    读取: {读取的 state 键列表}
    写入: {写入的 state 键列表}

配置项:
    enabled (bool): 是否启用，默认 True
    {其他配置项说明}
"""
```

### 8.2 类文档字符串

```python
class {PluginClass}(IInputPlugin):
    """{简短描述}。

    {详细说明职责、行为和注意事项。}

    Attributes:
        error_policy: 错误处理策略
    """
```

---

## 9. 版本与兼容性规范 [可选]

<!--
> **章节说明**：定义插件的版本管理和兼容性要求。
-->

### 9.1 向后兼容规则

| 变更类型 | 兼容要求 |
|----------|----------|
| 新增配置项 | 必须有默认值，不影响已有配置 |
| 修改配置项 | 旧值必须仍被接受（deprecation 警告） |
| 修改 state 键 | 旧键至少保留一个版本周期的兼容读取 |
| 删除功能 | 提前一个版本标记 deprecated |
| 修改接口 | 不允许，创建新接口 |

### 9.2 弃用流程

1. 标记 `@deprecated` 并在日志中输出 WARNING
2. 保留一个完整版本周期的兼容
3. 在下一个主版本中移除

---

## 10. 安全规范 [必填]

<!--
> **章节说明**：定义插件的安全要求。
-->

| 规则 | 说明 |
|------|------|
| 不执行不信任代码 | 不使用 `eval()`、`exec()` 执行动态代码 |
| 不暴露敏感信息 | 不在日志中输出 API Key、密码等敏感数据 |
| 不越权访问 | 不直接访问其他插件的内部状态 |
| 资源限制 | 异步操作设置超时，防止无限等待 |
| 输入验证 | 对从 state 读取的数据做类型检查和防御 |

---

## 验证清单

插件开发完成后，对照以下清单逐项检查：

- [ ] 命名符合 snake_case 规范，类名符合 CamelCase 规范
- [ ] 目录结构符合标准（`{type}/{name}/{name}.py`）
- [ ] 配置格式符合 YAML 规范，所有配置项有默认值
- [ ] 正确继承 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin`
- [ ] 实现了 `name`、`priority`、`execute` 三个必需成员
- [ ] 构造函数接受 `config` 参数，默认 `None`
- [ ] execute 方法是 async，返回正确类型
- [ ] 声明了正确的 `error_policy`
- [ ] State 键使用命名空间格式（`namespace.key`）
- [ ] 不直接修改 `ctx.state`，通过返回值传递更新
- [ ] 有单元测试覆盖核心路径和错误路径
- [ ] 使用 `logging.getLogger(__name__)` 记录日志
- [ ] 无 `eval()` / `exec()` 等不安全操作
- [ ] 模块和类有完整的文档字符串

---

<!--

## 评估指南

> **检查维度**：
>
> | 维度 | 检查内容 | 必填 | 通过标准 |
> |------|---------|------|---------|
> | 完整性 | 所有 [必填] 章节已填写 | 是 | 10 个章节完整，覆盖命名、目录、配置、接口、错误策略、测试、安全 |
> | 准确性 | 规范与现有插件代码一致 | 是 | 示例代码可直接运行，命名空间与实际插件匹配 |
> | 可操作性 | 规范可直接指导开发 | 是 | 包含代码模板、示例、验证清单 |
> | 格式规范 | Markdown 格式正确，表格对齐 | 是 | 所有表格列对齐，代码块有语言标注 |
>
> **评估结论标准**：
>
> | 结论 | 判定条件 | 后续动作 |
> |------|---------|---------|
> | 通过 | 所有必填维度通过，无阻塞性问题 | 规范可用 |
> | 有条件通过 | 必填维度通过，但存在改进建议 | 标注改进项，规范可发布 |
> | 不通过 | 存在必填维度未通过 | 退回修改 |

-->
