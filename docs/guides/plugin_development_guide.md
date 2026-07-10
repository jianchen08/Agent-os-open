# 插件开发完整指南

<!--
============================================================
【模板是什么】
Agent OS 管道插件的完整开发指南，从零开始手把手教你开发一个标准插件。

【模板的作用】
1. 指导新开发者快速上手 — 从概念到实现的完整路径
2. 提供端到端教程 — 包含真实可运行的示例
3. 集成所有工具链 — 脚手架、验证器、测试的联合使用
4. 建立最佳实践 — 通过示例传递正确的设计模式

【如何使用本模板】
1. 新手：按顺序从头到尾阅读并实践
2. 有经验者：跳到相关章节查阅特定主题
3. 作为参考文档：开发过程中随时查阅规范和模式

【适用场景】
- 新成员加入团队，需要学习插件开发
- 开发新的管道插件
- 理解 Agent OS 插件体系的设计哲学
- 排查插件开发中的常见问题

【与其他模板的关系】
- 上游：docs/standards/plugin_development_standard.md（规范定义）
- 上游：config/templates/plugin_test_template.md（测试模板）
- 下游：tools/plugin_scaffold.py（脚手架生成器）
- 下游：tools/plugin_validator.py（验证器）
-->

---

## 第一章：理解插件体系 [必填]

<!--
> **章节说明**：介绍 Agent OS 的插件体系设计哲学和核心概念。
> **填写时机**：首次阅读时。
-->

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

| 插件名 | 类型 | 优先级 | 职责 |
|--------|------|--------|------|
| message_inject | Input | 默认 | 注入用户消息 |
| context_window_guard | Input | 默认 | 上下文窗口管理 |
| param_inject | Input | 20 | 参数注入 |
| memory_read | Input | 默认 | 记忆检索 |
| knowledge_inject | Input | 默认 | 知识注入 |
| prompt_build | Input | 50 | 提示词构建 |
| security_check | Input | 70 | 安全检查 |
| stop_check | Output | 默认 | 停止条件检查 |
| error_check | Output | 默认 | 错误检测 |
| task_reminder | Output | 默认 | 任务提醒 |
| result_format | Output | 默认 | 结果格式化 |
| track | Output | 默认 | 统计追踪 |

---

## 第二章：快速开始 — 5 分钟创建一个插件 [必填]

<!--
> **章节说明**：使用脚手架工具快速创建插件骨架。
> **填写时机**：开发新插件时。
-->

### 2.1 使用脚手架生成器

```bash
# 进入项目根目录
cd /path/to/agent-os

# 生成 Input 插件骨架
python tools/plugin_scaffold.py input my_feature --desc "我的新功能插件" --priority 55

# 生成 Output 插件骨架
python tools/plugin_scaffold.py output result_handler --desc "结果处理器"

# 生成 Core 插件骨架
python tools/plugin_scaffold.py core custom_core --desc "自定义核心插件"
```

生成后的目录结构：

```
src/plugins/input/my_feature/
├── __init__.py              # 模块入口
├── my_feature.py            # 主插件逻辑
└── tests/
    ├── __init__.py
    └── test_my_feature.py   # 测试文件
```

### 2.2 注册到管道配置

编辑 `config/pipelines/default.yaml`：

```yaml
plugins:
  # 在适当位置添加（按优先级顺序）
  - name: my_feature
    config:
      enabled: true
      # 插件特有配置项
```

### 2.3 运行验证

```bash
# 验证插件合规性
python tools/plugin_validator.py input my_feature

# 运行测试
PYTHONPATH=src pytest src/plugins/input/my_feature/tests/ -v
```

---

## 第三章：从零开发 — 完整教程 [必填]

<!--
> **章节说明**：手把手开发一个完整的插件，涵盖从设计到测试的全流程。
> **填写时机**：首次开发插件时，按步骤实践。
-->

### 3.1 需求场景

我们要开发一个 **`duplicate_check`** 插件（Output 类型），用于检测 LLM 是否产生重复的工具调用和重复输出。

**功能需求**：
1. 检测连续重复的工具调用（相同工具+相同参数）
2. 检测重复的文本输出（高相似度）
3. 超过阈值时设置 `skip_remaining=True` 阻止后续插件处理

### 3.2 第一步：设计 State 交互

```python
# 读取的 state 键
raw_tool_calls: list[dict]      # 由 LLM Core 写入
raw_result: str                  # 由 LLM Core 写入

# 写入的 state 键
duplicate.is_duplicate: bool     # 是否检测到重复
duplicate.tool_count: int        # 重复工具调用计数
duplicate.output_count: int      # 重复输出计数
```

### 3.3 第二步：创建插件文件

```bash
python tools/plugin_scaffold.py output duplicate_check --desc "重复检测插件" --priority 30
```

### 3.4 第三步：实现插件逻辑

编辑 `src/plugins/output/duplicate_check/duplicate_check.py`：

```python
"""duplicate_check 插件 — 检测重复的工具调用和文本输出。

在管道输出阶段检查 LLM 产生的工具调用和文本是否重复，
防止陷入无限循环。

State 读写:
    读取: raw_tool_calls, raw_result
    写入: duplicate.is_duplicate, duplicate.tool_count, duplicate.output_count

配置项:
    enabled (bool): 是否启用，默认 True
    max_duplicate_calls (int): 最大允许重复工具调用次数，默认 10
    max_repetitive_output (int): 最大允许重复输出次数，默认 8
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, PluginContext, OutputResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class DuplicateCheckPlugin(IOutputPlugin):
    """检测重复的工具调用和文本输出。

    通过比较连续轮次的工具调用参数和文本输出哈希，
    检测 LLM 是否陷入重复循环。

    Attributes:
        error_policy: 错误处理策略，此处使用 SKIP（重复检测失败不影响主流程）
    """

    error_policy: ErrorPolicy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化重复检测插件。

        Args:
            config: 插件配置字典
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_duplicate_calls = self._config.get("max_duplicate_calls", 10)
        self._max_repetitive_output = self._config.get("max_repetitive_output", 8)
        self._call_history: list[str] = []
        self._output_history: list[str] = []

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "duplicate_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 30

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行重复检测。

        Args:
            ctx: 插件执行上下文

        Returns:
            检测结果，包含重复状态信息
        """
        if not self._enabled:
            return OutputResult()

        try:
            state_updates: dict[str, Any] = {}
            is_duplicate = False

            # 检测工具调用重复
            tool_calls = ctx.state.get("raw_tool_calls", [])
            if tool_calls:
                call_hash = self._hash_tool_calls(tool_calls)
                self._call_history.append(call_hash)
                duplicate_count = self._count_recent_duplicates(self._call_history, call_hash)
                state_updates["duplicate.tool_count"] = duplicate_count

                if duplicate_count >= self._max_duplicate_calls:
                    is_duplicate = True
                    logger.warning(
                        "Duplicate tool calls detected: %d consecutive calls",
                        duplicate_count,
                    )

            # 检测文本输出重复
            raw_result = ctx.state.get("raw_result", "")
            if raw_result:
                output_hash = self._hash_text(str(raw_result))
                self._output_history.append(output_hash)
                output_count = self._count_recent_duplicates(self._output_history, output_hash)
                state_updates["duplicate.output_count"] = output_count

                if output_count >= self._max_repetitive_output:
                    is_duplicate = True
                    logger.warning(
                        "Repetitive output detected: %d consecutive outputs",
                        output_count,
                    )

            state_updates["duplicate.is_duplicate"] = is_duplicate

            return OutputResult(
                state_updates=state_updates,
                skip_remaining=is_duplicate,
            )

        except Exception as e:
            logger.error("Plugin %s execution failed: %s", self.name, e)
            return OutputResult(error=e)

    @staticmethod
    def _hash_tool_calls(tool_calls: list[dict]) -> str:
        """计算工具调用的哈希值。"""
        content = str(sorted(
            (tc.get("function", {}).get("name", ""), tc.get("function", {}).get("arguments", ""))
            for tc in tool_calls
        ))
        return hashlib.md5(content.encode()).hexdigest()

    @staticmethod
    def _hash_text(text: str) -> str:
        """计算文本的哈希值。"""
        normalized = text.strip().lower()
        return hashlib.md5(normalized.encode()).hexdigest()

    @staticmethod
    def _count_recent_duplicates(history: list[str], current: str, window: int = 20) -> int:
        """计算最近窗口内与当前值相同的次数。"""
        recent = history[-window:]
        return sum(1 for h in recent if h == current)
```

### 3.5 第四步：编写测试

编辑 `src/plugins/output/duplicate_check/tests/test_duplicate_check.py`：

```python
"""duplicate_check 插件单元测试。"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext, OutputResult


def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(state=state or {}, config=config or {}, _services={})


def make_plugin(config: dict | None = None):
    """创建插件实例。"""
    from plugins.output.duplicate_check.duplicate_check import DuplicateCheckPlugin
    return DuplicateCheckPlugin(config=config)


class TestDuplicateCheckProperties:
    """基础属性测试。"""

    def test_name(self) -> None:
        assert make_plugin().name == "duplicate_check"

    def test_priority(self) -> None:
        assert make_plugin().priority == 30

    def test_error_policy_is_skip(self) -> None:
        from pipeline.types import ErrorPolicy
        assert make_plugin().__class__.error_policy == ErrorPolicy.SKIP


class TestDuplicateCheckInit:
    """初始化测试。"""

    def test_default_config(self) -> None:
        plugin = make_plugin(config=None)
        assert plugin._enabled is True
        assert plugin._max_duplicate_calls == 10
        assert plugin._max_repetitive_output == 8

    def test_custom_config(self) -> None:
        plugin = make_plugin(config={
            "max_duplicate_calls": 5,
            "max_repetitive_output": 3,
        })
        assert plugin._max_duplicate_calls == 5
        assert plugin._max_repetitive_output == 3

    def test_disabled(self) -> None:
        plugin = make_plugin(config={"enabled": False})
        assert plugin._enabled is False


class TestDuplicateCheckExecute:
    """核心执行测试。"""

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self) -> None:
        plugin = make_plugin(config={"enabled": False})
        result = await plugin.execute(make_ctx())
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_no_duplicates(self) -> None:
        plugin = make_plugin()
        ctx = make_ctx(state={"raw_tool_calls": [{"function": {"name": "test"}}]})
        result = await plugin.execute(ctx)
        assert result.state_updates.get("duplicate.is_duplicate") is False

    @pytest.mark.asyncio
    async def test_detects_duplicate_tool_calls(self) -> None:
        plugin = make_plugin(config={"max_duplicate_calls": 3})
        calls = [{"function": {"name": "same_tool", "arguments": "{}"}}]

        # 连续执行 3 次相同调用
        for _ in range(3):
            ctx = make_ctx(state={"raw_tool_calls": calls})
            result = await plugin.execute(ctx)

        assert result.state_updates.get("duplicate.is_duplicate") is True
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_detects_duplicate_output(self) -> None:
        plugin = make_plugin(config={"max_repetitive_output": 2})

        # 连续执行 2 次相同输出
        for _ in range(2):
            ctx = make_ctx(state={"raw_result": "Same output text"})
            result = await plugin.execute(ctx)

        assert result.state_updates.get("duplicate.is_duplicate") is True

    @pytest.mark.asyncio
    async def test_empty_state_no_error(self) -> None:
        plugin = make_plugin()
        ctx = make_ctx(state={})
        result = await plugin.execute(ctx)
        assert result is not None
        assert result.state_updates.get("duplicate.is_duplicate") is False
```

### 3.6 第五步：注册到管道配置

编辑 `config/pipelines/default.yaml`，在 Output 插件区域添加：

```yaml
  - name: duplicate_check
    config:
      max_duplicate_calls: 10
      max_repetitive_output: 8
```

### 3.7 第六步：验证和测试

```bash
# 验证合规性
python tools/plugin_validator.py output duplicate_check

# 运行测试
PYTHONPATH=src pytest src/plugins/output/duplicate_check/tests/ -v
```

---

## 第四章：高级主题 [按需]

<!--
> **章节说明**：涵盖插件开发中的高级主题。
> **填写时机**：遇到特定场景时查阅。
-->

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

在其他插件中读取：

```python
async def execute(self, ctx: PluginContext) -> PluginResult:
    StatusEnum = ctx.plugin_types.get_enum_class("my_plugin", "status")
    max_items = ctx.plugin_types.get_constant("my_plugin", "max_items")
    state_key = ctx.plugin_types.get_state_key("my_plugin", "item_count")
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

可用路由信号类型：

| route_type | 含义 | 效果 |
|------------|------|------|
| `next_llm` | 下一轮调用 LLM | `state["core_type"] = "llm_call"` |
| `next_tool` | 下一轮执行工具 | `state["core_type"] = "tool_execute"` |
| `end` | 管道结束 | `state["ended"] = True` |
| `wait` | 管道挂起 | 保存 state，等待唤醒 |
| `delegate` | 委派到子管道 | `pipeline_registry.route()` |

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

## 第五章：常见问题 [按需]

<!--
> **章节说明**：解答插件开发中的常见问题。
-->

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

使用 `pytest-asyncio`：

```python
@pytest.mark.asyncio
async def test_my_plugin() -> None:
    plugin = MyPlugin()
    ctx = make_ctx()
    result = await plugin.execute(ctx)
    assert result is not None
```

---

## 附录 A：插件开发检查清单

开发完成后，逐项检查：

- [ ] 使用脚手架生成标准目录结构
- [ ] 正确继承 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin`
- [ ] 实现 `name`、`priority`、`execute` 三个必需成员
- [ ] 声明了正确的 `error_policy`
- [ ] 构造函数接受 `config: dict | None = None`
- [ ] State 键使用命名空间格式
- [ ] 不直接修改 `ctx.state`
- [ ] 不使用 `eval()` / `exec()` 等不安全操作
- [ ] 有单元测试覆盖核心路径
- [ ] 模块和类有完整文档字符串
- [ ] 在 `config/pipelines/default.yaml` 中注册
- [ ] 通过 `plugin_validator` 验证
- [ ] 所有测试通过

---

## 附录 B：参考资源

| 资源 | 路径 | 说明 |
|------|------|------|
| 插件开发标准规范 | `docs/standards/plugin_development_standard.md` | 完整规范文档 |
| 插件测试流程模板 | `config/templates/plugin_test_template.md` | 测试编写指南 |
| 脚手架生成器 | `tools/plugin_scaffold.py` | 一键生成插件骨架 |
| 插件验证器 | `tools/plugin_validator.py` | 自动检查插件合规性 |
| 管道配置 | `config/pipelines/default.yaml` | 默认管道配置 |
| 插件接口定义 | `src/pipeline/plugin.py` | IPlugin 等基类定义 |
| 类型插槽 | `src/pipeline/plugin_types.py` | 类型注册机制 |
| 现有插件示例 | `src/plugins/` | 22+ 已有插件可参考 |

---

<!--

## 评估指南

> **检查维度**：
>
> | 维度 | 检查内容 | 必填 | 通过标准 |
> |------|---------|------|---------|
> | 完整性 | 覆盖从概念到实现的完整路径 | 是 | 5 章 + 2 附录 |
> | 准确性 | 代码示例可直接运行 | 是 | 示例与现有代码一致 |
> | 可操作性 | 按教程可实际开发出插件 | 是 | 包含完整的端到端示例 |
> | 格式规范 | Markdown 格式正确 | 是 | 代码块有语言标注 |
>
> **评估结论标准**：
>
> | 结论 | 判定条件 | 后续动作 |
> |------|---------|---------|
> | 通过 | 所有必填维度通过 | 指南可用 |
> | 有条件通过 | 必填维度通过，存在改进建议 | 标注改进项 |
> | 不通过 | 存在必填维度未通过 | 退回修改 |

-->
