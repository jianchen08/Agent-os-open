# 插件测试流程模板

---

## 1. 测试目录结构 [必填]

```
# 方案 A：插件内嵌测试（推荐）
src/plugins/{plugin_type}/{plugin_name}/
├── __init__.py
├── {plugin_name}.py
└── tests/
    ├── __init__.py
    ├── test_{plugin_name}.py          # 单元测试
    └── test_{plugin_name}_integration.py  # [可选] 集成测试

# 方案 B：项目级测试目录
tests/
└── plugins/
    └── {plugin_type}/
        ├── test_{plugin_name}.py
        └── test_{plugin_name}_integration.py
```

---

## 2. 单元测试模板 [必填]

### 2.1 Input 插件测试模板

```python
"""{{plugin_name}} 插件单元测试。"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy
# TODO: 导入插件类
# from plugins.{plugin_type}.{plugin_name}.{plugin_name} import {PluginClass}

# ── 测试工具函数 ──────────────────────────────────────

def make_ctx(
    state: dict | None = None,
    config: dict | None = None,
    services: dict | None = None,
) -> PluginContext:
    """创建测试用 PluginContext。

    Args:
        state: 管道状态字典
        config: 插件配置字典
        services: 服务注册表

    Returns:
        测试用 PluginContext 实例
    """
    return PluginContext(
        state=state or {{}},
        config=config or {{}},
        _services=services or {{}},
    )

def make_plugin(config: dict | None = None):  # -> {PluginClass}
    """创建插件实例。

    Args:
        config: 插件配置字典

    Returns:
        插件实例
    """
    # TODO: 替换为实际导入
    # from plugins.{plugin_type}.{plugin_name}.{plugin_name} import {PluginClass}
    # return {PluginClass}(config=config)
    pass

# ── 基础属性测试 ──────────────────────────────────────

class Test{PluginClass}Properties:
    """插件基础属性测试。"""

    def test_name_property(self) -> None:
        """name 属性应返回正确的插件标识名。"""
        plugin = make_plugin()
        assert plugin.name == "{plugin_name}"

    def test_priority_property(self) -> None:
        """priority 属性应返回非负整数。"""
        plugin = make_plugin()
        assert isinstance(plugin.priority, int)
        assert plugin.priority >= 0

    def test_error_policy_declared(self) -> None:
        """应声明有效的 error_policy。"""
        plugin = make_plugin()
        assert hasattr(plugin.__class__, "error_policy")
        assert plugin.__class__.error_policy in (
            ErrorPolicy.ABORT,
            ErrorPolicy.SKIP,
            ErrorPolicy.RETRY,
            ErrorPolicy.FALLBACK,
        )

# ── 构造函数测试 ──────────────────────────────────────

class Test{PluginClass}Init:
    """插件初始化测试。"""

    def test_default_config(self) -> None:
        """无配置时应有合理默认值。"""
        plugin = make_plugin(config=None)
        assert plugin._enabled is True

    def test_empty_config(self) -> None:
        """空配置字典时应有合理默认值。"""
        plugin = make_plugin(config={{}})
        assert plugin._enabled is True

    def test_disabled_config(self) -> None:
        """enabled=False 时应正确禁用。"""
        plugin = make_plugin(config={{"enabled": False}})
        assert plugin._enabled is False

    # TODO: 添加插件特有配置项的测试

# ── 核心执行测试 ──────────────────────────────────────

class Test{PluginClass}Execute:
    """插件核心执行测试。"""

    @pytest.mark.asyncio
    async def test_execute_disabled_returns_empty(self) -> None:
        """禁用时 execute 应返回空结果。"""
        plugin = make_plugin(config={{"enabled": False}})
        ctx = make_ctx()
        result = await plugin.execute(ctx)
        assert result.state_updates == {{}}

    @pytest.mark.asyncio
    async def test_execute_returns_correct_type(self) -> None:
        """execute 应返回正确的结果类型。"""
        plugin = make_plugin()
        ctx = make_ctx(state={{"key": "value"}})
        result = await plugin.execute(ctx)
        assert isinstance(result, PluginResult)

    @pytest.mark.asyncio
    async def test_execute_does_not_modify_ctx_state(self) -> None:
        """execute 不应直接修改 ctx.state。"""
        plugin = make_plugin()
        original_state = {{"key": "value"}}
        ctx = make_ctx(state=dict(original_state))
        await plugin.execute(ctx)
        assert ctx.state == original_state

    @pytest.mark.asyncio
    async def test_execute_basic_functionality(self) -> None:
        """基本功能测试。"""
        plugin = make_plugin()
        ctx = make_ctx(state={{}})
        result = await plugin.execute(ctx)
        # TODO: 添加具体的功能断言
        assert result is not None

    @pytest.mark.asyncio
    async def test_execute_with_missing_state_keys(self) -> None:
        """缺少必需 state 键时应有防御。"""
        plugin = make_plugin()
        ctx = make_ctx(state={{}})
        # 不应抛出异常
        result = await plugin.execute(ctx)
        assert result is not None

    # TODO: 添加更多插件特有的执行路径测试

# ── 错误路径测试 ──────────────────────────────────────

class Test{PluginClass}Errors:
    """插件错误处理测试。"""

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self) -> None:
        """execute 中异常应被捕获并放入 result.error。"""
        plugin = make_plugin()
        ctx = make_ctx(state={{"trigger_error": True}})
        result = await plugin.execute(ctx)
        # 根据错误策略验证行为
        # ABORT: result.error 不为 None
        # SKIP: result 可能正常返回（跳过错误部分）
        # FALLBACK: result 使用 fallback 值
```

### 2.2 Output 插件测试模板

```python
"""{{plugin_name}} Output 插件单元测试。"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext, OutputResult, RouteSignal
# TODO: 导入插件类

def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(state=state or {{}}, config=config or {{}}, _services={{}})

class Test{PluginClass}Output:
    """Output 插件测试。"""

    @pytest.mark.asyncio
    async def test_execute_returns_output_result(self) -> None:
        """应返回 OutputResult 类型。"""
        # TODO: 实现
        pass

    @pytest.mark.asyncio
    async def test_route_signal_generation(self) -> None:
        """测试路由信号生成。"""
        # TODO: 验证 route_signal 是否正确设置
        pass

    @pytest.mark.asyncio
    async def test_no_route_signal_by_default(self) -> None:
        """默认不应产生路由信号。"""
        # TODO: 实现
        pass
```

### 2.3 Core 插件测试模板

```python
"""{{plugin_name}} Core 插件单元测试。"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
# TODO: 导入插件类

def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(state=state or {{}}, config=config or {{}}, _services={{}})

class Test{PluginClass}Core:
    """Core 插件测试。"""

    @pytest.mark.asyncio
    async def test_execute_returns_dict(self) -> None:
        """应返回 dict[str, Any] 类型。"""
        # TODO: 实现
        pass

    @pytest.mark.asyncio
    async def test_execute_result_has_required_keys(self) -> None:
        """返回结果应包含必需的键。"""
        # TODO: 验证返回字典的键
        pass
```

---

## 3. 集成测试流程 [可选]

### 3.1 集成测试模板

```python
"""{{plugin_name}} 集成测试 — 验证插件间 state 交互。"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext, PluginResult, OutputResult
# TODO: 导入相关插件类

class Test{PluginClass}Integration:
    """插件集成测试。"""

    @pytest.mark.asyncio
    async def test_state_flow_with_upstream_plugin(self) -> None:
        """测试与上游插件的 state 流转。

        模拟上游插件写入 state 后，本插件能正确读取。
        """
        # 1. 模拟上游插件写入的 state
        upstream_state = {{
            "upstream.key": "value",
        }}

        # 2. 创建本插件并执行
        ctx = PluginContext(state=upstream_state, config={{}}, _services={{}})
        # plugin = {PluginClass}()
        # result = await plugin.execute(ctx)

        # 3. 验证本插件正确处理了上游 state
        # TODO: 添加断言

    @pytest.mark.asyncio
    async def test_state_flow_with_downstream_plugin(self) -> None:
        """测试与下游插件的 state 流转。

        模拟本插件写入 state 后，下游插件能正确读取。
        """
        # 1. 执行本插件
        ctx = PluginContext(state={{}}, config={{}}, _services={{}})
        # plugin = {PluginClass}()
        # result = await plugin.execute(ctx)

        # 2. 模拟下游插件读取 state
        downstream_state = {{**ctx.state, **result.state_updates}}
        # downstream_ctx = PluginContext(state=downstream_state, config={{}}, _services={{}})
        # downstream = DownstreamPlugin()
        # downstream_result = await downstream.execute(downstream_ctx)

        # 3. 验证下游插件能正确处理
        # TODO: 添加断言
```

### 3.2 集成测试场景

| 场景 | 测试内容 | 优先级 |
|------|----------|--------|
| Input→Core state 传递 | Input 插件写入的 state 能被 Core 读取 | 高 |
| Core→Output state 传递 | Core 产出的结果能被 Output 正确处理 | 高 |
| 安全链路 | security_check 写入的决策能被后续插件读取 | 高 |
| 错误传播 | 某插件失败后，下游插件的防御行为 | 中 |
| 路由信号 | Output 插件产生的信号能被路由表正确仲裁 | 中 |

---

## 4. 测试执行流程 [必填]

### 4.1 本地开发测试流程

```bash
# 步骤 1：运行单个插件的单元测试
cd {project_root}
PYTHONPATH=src pytest src/plugins/{plugin_type}/{plugin_name}/tests/ -v

# 步骤 2：运行所有插件测试
PYTHONPATH=src pytest src/plugins/ -v --tb=short

# 步骤 3：运行插件验证器
python tools/plugin_validator.py {plugin_type} {plugin_name}

# 步骤 4：运行验证器（所有插件）
python tools/plugin_validator.py --all
```

### 4.2 CI 集成测试流程

```yaml
# .github/workflows/plugin_tests.yml 示例
name: Plugin Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.14'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run plugin unit tests
        run: PYTHONPATH=src pytest src/plugins/ -v --tb=short
      - name: Run plugin validator
        run: PYTHONPATH=src python tools/plugin_validator.py --all --format json
```

### 4.3 测试验收标准

| 检查项 | 必要性 | 通过标准 |
|--------|--------|----------|
| 所有单元测试通过 | 必选 | 0 个失败 |
| 插件验证器通过 | 必选 | 0 个 ERROR |
| 核心路径覆盖 | 必选 | execute 方法正常/禁用/异常三路径通过 |
| 构造函数默认值 | 必选 | config=None 时正常工作 |
| 集成测试 | 按需 | 有 state 交互的插件必须有 |
| 文档字符串存在 | 推荐 | 模块和类有 docstring |

---

## 5. Mock 规范 [必填]

### 5.1 PluginContext Mock

```python
def make_ctx(
    state: dict | None = None,
    config: dict | None = None,
    services: dict | None = None,
) -> PluginContext:
    """标准 PluginContext Mock 工厂。"""
    return PluginContext(
        state=state or {},
        config=config or {},
        _services=services or {},
    )
```

### 5.2 服务 Mock

```python
from unittest.mock import MagicMock, AsyncMock

# Mock LLM 服务
mock_llm = MagicMock()
mock_llm.call = AsyncMock(return_value={"content": "test response"})

# Mock 工具注册表
mock_tool_registry = MagicMock()
mock_tool_registry.get_tool.return_value = MagicMock()

# 使用
ctx = make_ctx(services={"llm": mock_llm, "tool_registry": mock_tool_registry})
```

### 5.3 Mock 规则

| 规则 | 说明 |
|------|------|
| 不依赖真实 LLM | 所有 LLM 调用使用 Mock |
| 不依赖外部服务 | 网络、数据库、文件系统均 Mock |
| 不依赖执行顺序 | 每个测试可独立运行 |
| Mock 最小化 | 只 Mock 外部依赖，不过度 Mock 内部逻辑 |

---

