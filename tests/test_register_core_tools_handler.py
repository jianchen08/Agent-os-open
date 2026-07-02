"""回归测试：register_core_tools 对"有定义无 handler"工具的补注册行为。

根因（已修复）：
  动态加载器（tools.loader.DynamicToolLoader）在遇到需要依赖注入的工具时，
  会"只注册工具定义、不注册 handler"（registry.register(tool_def)，无 handler）。
  此时 registry.has(name) 为 True，但 registry.get_handler(name) 为 None。

  旧版 register_core_tools 的跳过检查仅判断 registry.has(name)，导致 task_evaluate
  等工具被错误跳过、handler 永不注册；子任务管道里 Agent 调用时报：
      Tool 'task_evaluate' not found

修复要点：
  1. 跳过检查改为双重判断：has(name) 且 get_handler(name) is not None 才跳过；
  2. register_with_handler 加 overwrite=True，使"定义已存在"时仍能覆盖补上 handler。

本测试用真实 ToolRegistry + 真实 TaskEvaluateTool 复现该状态并验证修复。
"""

from __future__ import annotations

import pytest

from tools.builtin import register_core_tools
from tools.builtin.task_evaluate.tool import TaskEvaluateTool
from tools.registry import ToolRegistry
from tools.types import Tool, ToolCategory, ToolLevel, ToolSource


def _make_tool_def(name: str) -> Tool:
    """构造一个最小可用 Tool 定义。"""
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        input_schema={"type": "object", "properties": {}},
        source=ToolSource.CODE,
        category=ToolCategory.TASK,
        level=ToolLevel.SYSTEM,
    )


@pytest.fixture()
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """构造一个仅含 task_evaluate 的隔离注册表环境。

    通过 monkeypatch 限定 register_core_tools 只处理 task_evaluate，
    避免触发其他真实工具（网络/文件系统）的副作用。
    """
    # CORE_SYSTEM_TOOLS 在 register_core_tools 内部通过
    # `from tools.loader import CORE_SYSTEM_TOOLS` 引用，patch 其源头模块。
    monkeypatch.setattr(
        "tools.loader.CORE_SYSTEM_TOOLS",
        ["task_evaluate"],
    )
    return ToolRegistry()


class TestRegisterCoreToolsHandlerGap:
    """验证 register_core_tools 在工具处于"有定义无 handler"状态时的行为。"""

    def test_definition_without_handler_gets_handler_registered(
        self, isolated_registry: ToolRegistry
    ) -> None:
        """复现 bug 场景：先注册无 handler 的定义，再调 register_core_tools。

        修复前：register_core_tools 因 has()=True 跳过 → handler 始终为 None。
        修复后：双重检查发现 handler 缺失 → 补注册 handler。
        """
        registry = isolated_registry

        # 模拟动态加载器：只注册定义，不注册 handler
        registry.register(_make_tool_def("task_evaluate"))

        # 断言初始状态：定义在，handler 缺失（bug 触发条件）
        assert registry.has("task_evaluate") is True
        assert registry.get_handler("task_evaluate") is None

        # 触发 register_core_tools
        registered = register_core_tools(registry=registry, session=None, skip_existing=True)

        # 修复后：task_evaluate 应出现在已注册列表中
        assert "task_evaluate" in registered, (
            f"task_evaluate 应被注册，实际 registered={registered}"
        )

        # 核心断言：handler 必须存在（这才是 Agent 能否调用的关键）
        handler = registry.get_handler("task_evaluate")
        assert handler is not None, "task_evaluate 的 handler 未被注册（根因未修复）"

    def test_fully_registered_tool_is_skipped(
        self, isolated_registry: ToolRegistry
    ) -> None:
        """对照组：定义和 handler 都已存在时，应正确跳过（不重复注册）。"""
        registry = isolated_registry

        # 预先完整注册（含 handler）
        tool_instance = TaskEvaluateTool()
        registry.register_with_handler(
            tool=tool_instance.get_tool_definition(),
            handler=tool_instance.execute,
            overwrite=True,
        )
        original_handler = registry.get_handler("task_evaluate")

        registered = register_core_tools(registry=registry, session=None, skip_existing=True)

        # 已完整注册 → 应被跳过，不在本次 registered 列表中
        assert "task_evaluate" not in registered, "完整注册的工具不应被重复注册"
        # handler 引用不变（未被覆盖）
        assert registry.get_handler("task_evaluate") is original_handler

    def test_handler_is_real_task_evaluate_execute(
        self, isolated_registry: ToolRegistry
    ) -> None:
        """验证补注册的 handler 确实是 TaskEvaluateTool.execute（对象身份验证）。

        Bug 方法论要求：验证对象身份，不仅是值。
        """
        registry = isolated_registry
        registry.register(_make_tool_def("task_evaluate"))

        register_core_tools(registry=registry, session=None, skip_existing=True)

        handler = registry.get_handler("task_evaluate")
        # bound method 的 __self__ 应是 TaskEvaluateTool 实例
        assert handler is not None
        assert isinstance(handler.__self__, TaskEvaluateTool), (
            f"handler 应绑定到 TaskEvaluateTool 实例，实际 {type(handler.__self__)}"
        )
