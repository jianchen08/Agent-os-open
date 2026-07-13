# 插件开发完整指南

Agent OS 管道插件的完整开发指南，从概念到实现，配合真实代码示例讲解。

> 配套规范见同目录 [插件开发标准规范](plugin_development_standard.md)，接口定义见 `src/pipeline/plugin.py`。

---

## 第一章：理解插件体系

### 1.1 什么是管道插件

Agent OS 将 AI Agent 的处理流程抽象为**管道循环**：

```
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route
```

插件就是管道循环中的**可插拔处理单元**。每个插件负责一个独立的关注点，按优先级排序执行。

### 1.2 三种插件类型

| 类型 | 职责 | 执行位置 | 返回类型 |
|------|------|----------|----------|
| **Input 插件** | 预处理（校验、注入、权限检查） | Core 之前 | `PluginResult` |
| **Core 插件** | 核心逻辑（LLM 调用、工具执行） | 管道中心 | `dict[str, Any]` |
| **Output 插件** | 后处理（格式化、路由信号、统计） | Core 之后 | `OutputResult` |

### 1.3 插件间通信

插件之间通过 `state` 字典通信，**不直接调用**：

```
Input 插件 A → 写入 state["security.decision"] → 
Input 插件 B → 读取 state["security.decision"] → 执行拦截
```

**核心原则**：检测与执行可合并也可解耦，取决于复杂度。简单的几行代码就合并，复杂的有独立状态管理的就解耦。

### 1.4 现有插件一览

项目 `src/plugins/` 下已有 47 个插件（input 22 / output 21 / core 4），均位于 `src/plugins/{type}/{name}/` 目录下。以下是常用插件及其职责，完整列表直接查看 `src/plugins/{input,output,core}/` 目录：

**Input 插件（部分）：**

| 插件名 | 职责 |
|--------|------|
| `context_window_guard` | 上下文窗口管理与压缩 |
| `param_inject` | 参数注入（task_id / session_id / user_id / timestamp） |
| `memory_read` | 记忆检索（语义） |
| `knowledge_inject` | 知识注入 |
| `prompt_build` | 提示词构建 |
| `security_check` | 安全检查 |
| `isolation_guard` | 隔离守卫（工具级拦截） |
| `level_guard` | 层级守卫 |
| `tool_schema` | 工具 Schema 注入 |
| `pause_guard` | 任务暂停检测 |

**Output 插件（部分）：**

| 插件名 | 职责 |
|--------|------|
| `stop_check` | 停止条件检查 |
| `error_check` | 错误检测 |
| `duplicate_check` | 重复工具调用/输出检测（三级渐进策略） |
| `task_reminder` | 任务提醒 |
| `result_format` | 结果格式化 |
| `track` | 统计追踪（token/耗时） |
| `output_repetition_guard` | 输出重复守卫 |
| `stuck_detector` | 卡死检测 |
| `child_task_guard` | 子任务守卫 |
| `llm_error_recovery` | LLM 错误恢复 |

**Core 插件：**

| 插件名 | 职责 |
|--------|------|
| `llm_core` | LLM 调用核心 |
| `tool_core` | 工具执行核心 |
| `stream_repeat_monitor` | 流式重复监控 |

---

## 第二章：快速开始 — 创建一个插件

### 2.1 创建目录结构

> **注意**：项目当前未提供脚手架生成器（`tools/plugin_scaffold.py`）和合规性验证器（`tools/plugin_validator.py`）。插件骨架需手动创建，标准结构如下。

每个插件是一个独立目录，位于 `src/plugins/{input|output|core}/{plugin_name}/`：

```
src/plugins/output/my_feature/
├── __init__.py          # 模块入口，导出插件类
└── plugin.py            # 主插件逻辑（文件名固定为 plugin.py）
```

`__init__.py` 使用相对导入导出插件类：

```python
"""my_feature 插件 — 一句话描述。"""

from .plugin import MyFeaturePlugin

__all__ = ["MyFeaturePlugin"]
```

> **命名约定**：主文件统一命名为 `plugin.py`（而非 `{plugin_name}.py`），这是项目现有 47 个插件的一致做法。`__init__.py` 通过 `from .plugin import XxxPlugin` 导出。

### 2.2 注册到管道配置

编辑 `config/pipelines/default.yaml`，在对应插件链（`plugins:` 列表）中添加：

```yaml
plugins:
  # 按 priority 排序插入，参考已有插件的优先级分组
  - name: my_feature
    config:
      enabled: true
      priority: 20            # 数值越小越先执行
      # 插件特有配置项（带默认值，见第三章）
```

Input 插件还需在对应 `input_routes[].plugins` 列表中按需引用，Output 插件则由 `output_routes` 的路由仲裁自动触发。

### 2.3 运行测试

```bash
# 项目无独立验证器，通过单元测试验证插件正确性
PYTHONPATH=src pytest tests/ -k "my_feature" -v
```

---

## 第三章：从零开发 — 完整教程

本章以一个 **Output 插件**为例，完整演示从设计到测试的全流程。

> 项目里已有真实的复杂 Output 插件 `duplicate_check`（三级渐进重复检测策略），完整源码见 `src/plugins/output/duplicate_check/plugin.py`，可作为高级参考。本章为教学目的展示一个**精简但接口完全正确**的示例。

### 3.1 需求场景

开发一个 **`result_length_guard`** 插件（Output 类型），在管道输出阶段检查 LLM 输出长度，超过阈值时写入警告标记。

**功能需求**：
1. 读取 `raw_result`（由 `llm_core` 写入）
2. 超过阈值时写入 `length_guard.warning` 状态
3. 不阻断管道流程（错误策略 `SKIP`）

### 3.2 第一步：设计 State 交互

State 是插件间通信的唯一通道，使用 `{namespace}.{key}` 命名空间格式：

```python
# 读取的 state 键（由 Core 插件写入）
raw_result: str | None      # StateKeys.RAW_RESULT，LLM 的原始输出

# 写入的 state 键
length_guard.warning: str | None   # 超长警告，None 表示正常
```

> **关键**：计数与跨轮状态应存入 `state`（由引擎在迭代间保持），而非插件实例属性。插件实例在管道内可能被复用，用实例属性做跨迭代计数会出错。真实 `duplicate_check` 的计数全部走 `router.duplicate_count` 等 state 键。

### 3.3 第二步：创建插件文件

手动创建目录（项目未提供脚手架）：

```
src/plugins/output/result_length_guard/
├── __init__.py      # 导出 ResultLengthGuardPlugin
└── plugin.py        # 主逻辑
```

`__init__.py`：

```python
"""result_length_guard 插件 — 输出长度守卫。"""

from .plugin import ResultLengthGuardPlugin

__all__ = ["ResultLengthGuardPlugin"]
```

### 3.4 第三步：实现插件逻辑

编辑 `src/plugins/output/result_length_guard/plugin.py`：

```python
"""result_length_guard 插件 — 检查 LLM 输出长度。

在管道输出阶段读取 raw_result，超过阈值时写入警告状态。
不阻断管道流程（错误策略 SKIP）。

State 读写:
    读取: raw_result
    写入: length_guard.warning

配置项:
    max_length (int): 输出最大字符数，默认 2000
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ResultLengthGuardPlugin(IOutputPlugin):
    """输出长度守卫插件。

    Attributes:
        error_policy: SKIP，本插件失败不影响主流程
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化长度守卫插件。

        Args:
            config: 插件配置字典
        """
        self._config = config or {}
        self._max_length = self._config.get("max_length", 2000)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "result_length_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级（可被 config.priority 覆盖）。"""
        return self._config.get("priority", 25)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行长度检查。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含长度警告状态的输出结果
        """
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if raw_result is None:
            return OutputResult()

        text = str(raw_result)
        if len(text) > self._max_length:
            logger.warning(
                "[%s] Output length %d exceeds max %d",
                self.name,
                len(text),
                self._max_length,
            )
            return OutputResult(
                state_updates={
                    "length_guard.warning": f"输出长度 {len(text)} 超过上限 {self._max_length}",
                },
            )

        return OutputResult(state_updates={"length_guard.warning": None})
```

**要点对照真实代码**：
- 导入路径是 `pipeline.plugin`（`src/pipeline/plugin.py`）和 `pipeline.types`（`src/pipeline/types.py`）
- `ErrorPolicy` 有四个成员：`ABORT` / `SKIP` / `RETRY` / `FALLBACK`
- `StateKeys.RAW_RESULT` 等预定义键在 `pipeline.types` 中，避免硬编码字符串
- `priority` 通常从 `config.get("priority", 默认值)` 读取，便于配置覆盖
- 跨迭代状态写 state，不存实例属性

### 3.5 第四步：编写测试

```python
"""result_length_guard 插件单元测试。"""

from __future__ import annotations

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.result_length_guard.plugin import ResultLengthGuardPlugin


def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(state=state or {}, config=config or {}, _services={})


def make_plugin(config: dict | None = None) -> ResultLengthGuardPlugin:
    """创建插件实例。"""
    return ResultLengthGuardPlugin(config=config)


class TestResultLengthGuardProperties:
    """基础属性测试。"""

    def test_name(self) -> None:
        assert make_plugin().name == "result_length_guard"

    def test_priority_default(self) -> None:
        assert make_plugin().priority == 25

    def test_priority_config_override(self) -> None:
        assert make_plugin(config={"priority": 99}).priority == 99

    def test_error_policy_is_skip(self) -> None:
        from pipeline.types import ErrorPolicy
        assert make_plugin().__class__.error_policy == ErrorPolicy.SKIP


class TestResultLengthGuardInit:
    """初始化测试。"""

    def test_default_config(self) -> None:
        plugin = make_plugin(config=None)
        assert plugin._max_length == 2000

    def test_custom_config(self) -> None:
        plugin = make_plugin(config={"max_length": 500})
        assert plugin._max_length == 500


class TestResultLengthGuardExecute:
    """核心执行测试。"""

    async def test_no_result_returns_empty(self) -> None:
        plugin = make_plugin()
        result = await plugin.execute(make_ctx())
        assert result.state_updates == {}

    async def test_normal_length(self) -> None:
        plugin = make_plugin()
        ctx = make_ctx(state={StateKeys.RAW_RESULT: "短文本"})
        result = await plugin.execute(ctx)
        assert result.state_updates.get("length_guard.warning") is None

    async def test_exceeds_max_length(self) -> None:
        plugin = make_plugin(config={"max_length": 5})
        ctx = make_ctx(state={StateKeys.RAW_RESULT: "这是一段超过五个字符的文本"})
        result = await plugin.execute(ctx)
        assert result.state_updates["length_guard.warning"] is not None
        assert "5" in result.state_updates["length_guard.warning"]
```

> **异步测试说明**：项目 `pyproject.toml` 配置了 `asyncio_mode = "auto"`，`async def test_*()` 函数会被 pytest-asyncio 自动识别，**无需** `@pytest.mark.asyncio` 装饰器。

### 3.6 第五步：注册到管道配置

编辑 `config/pipelines/default.yaml`，在 `plugins:` 的 Output 区域添加：

```yaml
  - name: result_length_guard
    config:
      max_length: 2000
      priority: 25
```

> Output 插件由 `output_routes` 路由仲裁统一触发，无需像 Input 插件那样在 `input_routes[].plugins` 中显式引用。

### 3.7 第六步：运行测试

```bash
# 项目无独立验证器，通过单元测试验证插件正确性
PYTHONPATH=src pytest tests/ -k "result_length_guard" -v
```

---

## 第四章：高级主题

### 4.1 类型插槽使用

当插件需要自定义类型时，使用 `register_types` 注册：

```python
from pipeline.plugin_types import PluginTypeSlot

class MyPlugin(IOutputPlugin):
    @classmethod
    def register_types(cls, slots: PluginTypeSlot) -> None:
        """注册自定义类型。"""
        slots.register_enum("my_plugin", "status", ["idle", "processing", "done"])
        slots.register_constant("my_plugin", "max_items", 100)
        slots.register_state_key("my_plugin", "item_count", default=0)
        slots.register_handler("my_plugin", "on_complete", my_handler_func)
```

在其他插件中读取（返回类型取决于插件类型，Input→`PluginResult`、Output→`OutputResult`、Core→`dict`）：

```python
async def execute(self, ctx: PluginContext) -> OutputResult:
    StatusEnum = ctx.plugin_types.get_enum_class("my_plugin", "status")
    max_items = ctx.plugin_types.get_constant("my_plugin", "max_items")
    state_key = ctx.plugin_types.get_state_key("my_plugin", "item_count")  # 返回 "my_plugin.item_count"
    handler = ctx.plugin_types.get_handler("my_plugin", "on_complete")
```

### 4.2 插件间协作模式

**模式 1：直接检测+执行**（简单拦截）

```python
class SimpleGuardPlugin(IInputPlugin):
    async def execute(self, ctx: PluginContext) -> PluginResult:
        if self._should_block(ctx.state):
            return PluginResult(
                state_updates={"blocked.reason": "安全原因"},
                skip_remaining=True,
            )
        return PluginResult()
```

**模式 2：检测与执行解耦**（复杂流程）

```python
# 插件 A：检测
class SecurityDetectorPlugin(IInputPlugin):
    async def execute(self, ctx: PluginContext) -> PluginResult:
        decision = self._analyze(ctx.state)
        return PluginResult(state_updates={"security.decision": decision})

# 插件 B：执行（优先级更低，读取 A 写入的 state）
class ApprovalGuardPlugin(IInputPlugin):
    async def execute(self, ctx: PluginContext) -> PluginResult:
        decision = ctx.state.get("security.decision", {})
        if decision.get("needs_approval"):
            # 进入审批流程...
            pass
        return PluginResult()
```

### 4.3 路由信号（仅 Output 插件）

Output 插件可以产生路由信号，控制管道下一步走向：

```python
class MyOutputPlugin(IOutputPlugin):
    @property
    def route_signals(self) -> list[str]:
        """声明可能产生的路由信号类型。"""
        return ["next_llm", "end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        if self._should_end(ctx.state):
            return OutputResult(
                route_signal=RouteSignal(
                    route_type="end",
                    reason="任务完成",
                ),
            )
        return OutputResult(
            route_signal=RouteSignal(
                route_type="next_llm",
                reason="需要继续对话",
            ),
        )
```

可用路由信号类型（定义见 `src/pipeline/types.py` 的 `RouteSignal` 与 `output_routes` 路由表）：

| route_type | 含义 | 效果 |
|------------|------|------|
| `next_llm` | 下一轮调用 LLM | `state["core_type"] = "llm_call"` |
| `next_tool` | 下一轮执行工具 | `state["core_type"] = "tool_execute"` |
| `end` | 管道结束 | `state["ended"] = True` |
| `wait` | 管道挂起 | 保存 state，等待唤醒 |

> **`delegate` / `decision` 信号说明**：0.1 代码中 `RouteType` 枚举保留了 `delegate` 和 `decision`，但引擎实际不消费这两个信号——跨管道路由在 0.1 已通过 task 工具链实现。新插件开发**不应**使用这两个信号。0.2 已正式将其从路由信号中移除（收敛为 4 种），跨管道路由统一走工具触发专门服务，而非由引擎直接派生子管道。

`RouteSignal` 的完整字段：`route_type`（必填）、`target`（路由目标，可为字符串或列表）、`reason`（原因描述）、`payload`（附加数据）。

### 4.4 配置覆盖（Agent 级别）

Agent 配置可以覆盖插件的默认配置：

```yaml
# config/agents/executor/my_agent.yaml
plugins:
  disabled:
    - "memory_read"           # 禁用记忆读取
  enabled:
    task_reminder:
      max_reminders: 5        # 覆盖默认的 3 次
      cooldown_seconds: 120   # 覆盖默认的 300 秒
```

---

## 第五章：常见问题

### Q1：插件应该使用什么错误策略？

**决策树**：

```
插件失败后：
├── 管道不能继续？ → ABORT（如安全检查、参数注入）
├── 可以降级运行？ → FALLBACK（如上下文构建、Schema 生成）
├── 不影响当轮？ → SKIP（如统计、格式化、日志）
└── 瞬态错误？ → RETRY（如外部 API 调用）
```

### Q2：插件之间如何共享数据？

**只能通过 state**。插件 A 写入 `state["namespace.key"]`，插件 B 读取。

命名空间约定：
- 使用 `{plugin_name}.{key}` 格式
- 不写入其他插件的命名空间
- 读取时使用 `.get()` 提供默认值

### Q3：插件如何访问外部服务？

通过 `ctx.get_service()` 获取注册的服务：

```python
async def execute(self, ctx: PluginContext) -> PluginResult:
    try:
        llm = ctx.get_service("llm")
        result = await llm.call(prompt="...")
    except KeyError:
        logger.warning("LLM service not available")
        return PluginResult()
```

### Q4：如何调试插件？

```python
import logging
logger = logging.getLogger(__name__)

async def execute(self, ctx: PluginContext) -> PluginResult:
    logger.debug("Plugin %s input state: %s", self.name, list(ctx.state.keys()))
    # ... 插件逻辑 ...
    logger.debug("Plugin %s output: %s", self.name, result.state_updates)
    return result
```

启用 DEBUG 日志：设置环境变量 `LOG_LEVEL=DEBUG` 或在 logging 配置中调整。

### Q5：插件测试如何处理异步？

项目 `pyproject.toml` 配置了 `asyncio_mode = "auto"`，`async def test_*()` 函数会被 pytest-asyncio 自动识别，**无需** `@pytest.mark.asyncio` 装饰器：

```python
async def test_my_plugin() -> None:
    plugin = MyPlugin()
    ctx = make_ctx()
    result = await plugin.execute(ctx)
    assert result is not None
```

---

## 附录 A：插件开发检查清单

开发完成后，逐项检查：

- [ ] 目录结构符合标准（`{type}/{name}/` 下 `__init__.py` + `plugin.py`）
- [ ] 主文件命名为 `plugin.py`，`__init__.py` 用 `from .plugin import XxxPlugin` 导出
- [ ] 正确继承 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin`
- [ ] 实现 `name`、`priority`、`execute` 三个必需成员
- [ ] 声明了正确的 `error_policy`（`ABORT` / `SKIP` / `RETRY` / `FALLBACK`）
- [ ] 构造函数接受 `config: dict[str, Any] | None = None`
- [ ] State 键使用命名空间格式（`{namespace}.{key}`），跨迭代状态存 state 不存实例属性
- [ ] 优先从 `config.get("priority", 默认值)` 读取优先级
- [ ] 不直接修改 `ctx.state`，通过返回 `state_updates` 让引擎合并
- [ ] 不使用 `eval()` / `exec()` 等不安全操作
- [ ] 有单元测试覆盖核心路径和错误路径
- [ ] 模块和类有完整文档字符串
- [ ] 在 `config/pipelines/default.yaml` 中注册
- [ ] 所有测试通过（`PYTHONPATH=src pytest tests/ -k "<插件名>" -v`）

---

## 附录 B：参考资源

| 资源 | 路径 | 说明 |
|------|------|------|
| 插件开发标准规范 | `docs/guides/plugin_development_standard.md` | 完整规范文档（同目录） |
| 插件接口定义 | `src/pipeline/plugin.py` | `IPlugin`/`IInputPlugin`/`ICorePlugin`/`IOutputPlugin`/`PluginContext`/`PluginResult`/`OutputResult` |
| 类型与枚举 | `src/pipeline/types.py` | `ErrorPolicy`/`RouteSignal`/`StateKeys` 定义 |
| 类型插槽 | `src/pipeline/plugin_types.py` | `PluginTypeSlot` 类型注册机制 |
| 管道配置 | `config/pipelines/default.yaml` | 默认管道配置（插件注册位置） |
| 真实插件示例 | `src/plugins/output/duplicate_check/plugin.py` | 三级渐进策略的完整 Output 插件实现 |
| 现有插件目录 | `src/plugins/{input,output,core}/` | 47 个已有插件可参考（input 22 / output 21 / core 4） |
