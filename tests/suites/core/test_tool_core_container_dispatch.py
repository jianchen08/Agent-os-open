"""tool_core 容器分发路由测试 — TDD 红灯。

覆盖目标：`use_docker=True` 的 bash_execute 不再绕过 BashTool 直接调
IsolationManager，而是先把 container_id 注入 tool_args，然后走
`_execute_single_tool`（让 BashTool 带着容器 backend 跑完整轮询流程）。

本测试不依赖真实 docker：
- mock IsolationManager.get_or_create_environment 返回带 env_id 的假 environment
- mock BashTool（注册为 bash_execute handler）记录收到的 tool_args
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.core.tool_core import ToolCore


def _make_ctx(**overrides: Any) -> PluginContext:
    """创建带 execution_contexts/workspace 等字段的 PluginContext。"""
    from pipeline.types import create_initial_state

    state = create_initial_state(**overrides)
    return PluginContext(state=state)


def _docker_execution_context() -> list[dict[str, Any]]:
    """构造一个 provider=docker 的 execution_contexts 条目。"""
    return [
        {
            "tool_name": "bash_execute",
            "provider": "docker",
            "level": "isolated",
            "reason": "task_metadata",
        }
    ]


# ============================================================
# Step 4: use_docker 分发改路由
# ============================================================


@pytest.mark.asyncio
async def test_use_docker_goes_through_bashtool_with_container_id(monkeypatch):
    """use_docker=True → 走 _execute_single_tool（BashTool），tool_args 注入 _container_id。

    关键断言：
    1. BashTool handler 被调用（不是 _execute_in_isolated_container）；
    2. tool_args 里有 _container_id（值是 env_id）；
    3. _execute_in_isolated_container 不被调用。
    """
    core = ToolCore()

    # 记录 BashTool handler 收到的 args
    captured_args: dict[str, Any] = {}

    async def fake_bashtool(args: dict) -> dict:
        captured_args.update(args)
        return {"status": "completed", "output": "ok", "exit_code": 0}

    core.register_tool("bash_execute", fake_bashtool)

    # mock IsolationManager.get_or_create_environment 返回带 env_id 的 environment
    fake_env = MagicMock()
    fake_env.env_id = "container_abc123"
    fake_env.status = "ready"

    fake_manager = AsyncMock()
    fake_manager.get_or_create_environment = AsyncMock(return_value=fake_env)

    async def fake_get_manager():
        return fake_manager

    with patch(
        "isolation.manager.get_isolation_manager", new=fake_get_manager
    ), patch.object(
        core, "_execute_in_isolated_container", new=AsyncMock(
            side_effect=AssertionError("不应调用 _execute_in_isolated_container")
        )
    ):
        ctx = _make_ctx(
            raw_tool_calls=[{"name": "bash_execute", "args": {"command": "echo hi"}}],
            execution_contexts=_docker_execution_context(),
            task_id="task-1",
            workspace="/tmp/ws",
        )
        result = await core.execute(ctx)

    # BashTool 被调用，且收到 _container_id
    assert "_container_id" in captured_args, (
        f"BashTool 应收到 _container_id，实际 args keys: {list(captured_args.keys())}"
    )
    assert captured_args["_container_id"] == "container_abc123"


@pytest.mark.asyncio
async def test_use_docker_false_unchanged():
    """use_docker=False → 走原 _execute_single_tool，tool_args 无 _container_id（回归）。"""
    core = ToolCore()

    captured_args: dict[str, Any] = {}

    async def fake_bashtool(args: dict) -> dict:
        captured_args.update(args)
        return {"status": "completed", "output": "ok"}

    core.register_tool("bash_execute", fake_bashtool)

    ctx = _make_ctx(
        raw_tool_calls=[{"name": "bash_execute", "args": {"command": "echo hi"}}],
        execution_contexts=[
            {
                "tool_name": "bash_execute",
                "provider": "host",  # 非 docker
                "level": "non_isolated",
                "reason": "test",
            }
        ],
        task_id="task-1",
        workspace="/tmp/ws",
    )
    await core.execute(ctx)

    assert "_container_id" not in captured_args, "非 docker 路径不应注入 _container_id"
    assert captured_args.get("command") == "echo hi"


@pytest.mark.asyncio
async def test_use_docker_blocked_falls_back_to_single_tool():
    """execution_context 标记 blocked=True → 即使 provider=docker 也不走容器，降级普通路径。"""
    core = ToolCore()

    captured_args: dict[str, Any] = {}

    async def fake_bashtool(args: dict) -> dict:
        captured_args.update(args)
        return {"status": "completed", "output": "ok"}

    core.register_tool("bash_execute", fake_bashtool)

    ctx = _make_ctx(
        raw_tool_calls=[{"name": "bash_execute", "args": {"command": "x"}}],
        execution_contexts=[
            {
                "tool_name": "bash_execute",
                "provider": "docker",
                "level": "denied",
                "reason": "blocked",
                "blocked": True,
            }
        ],
        task_id="task-1",
        workspace="/tmp/ws",
    )
    await core.execute(ctx)

    # blocked 时不注入 container_id（降级为普通 host 执行）
    assert "_container_id" not in captured_args


@pytest.mark.asyncio
async def test_use_docker_without_workspace_returns_error():
    """use_docker=True 但 workspace 缺失 → 返回明确错误（不静默创建无挂载容器）。"""
    core = ToolCore()

    async def fake_bashtool(args: dict) -> dict:
        raise AssertionError("workspace 缺失时不应调用 BashTool")

    core.register_tool("bash_execute", fake_bashtool)

    ctx = _make_ctx(
        raw_tool_calls=[{"name": "bash_execute", "args": {"command": "x"}}],
        execution_contexts=_docker_execution_context(),
        task_id="task-1",
        # 故意不设 workspace
    )
    result = await core.execute(ctx)

    tool_results = result[StateKeys.TOOL_RESULTS]
    assert len(tool_results) == 1
    assert tool_results[0]["success"] is False
    assert "workspace" in tool_results[0]["error"].lower() or "工作空间" in tool_results[0]["error"]
