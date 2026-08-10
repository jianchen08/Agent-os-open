"""会话级隔离守卫插件测试。

核心契约（与任务级 isolation_guard 解耦）：
- 主会话（无 task_id）+ 绑定工作空间 + 会话隔离=isolated：
  bash_execute 注入 _container_id + 默认 working_dir=/workspace
- non_isolated / 无工作空间 / 有任务上下文（task_id）：完全不干预
- 非 bash_execute 工具：不干预
- 会话容器不可用：降级宿主执行（不注入）
"""
import json
from unittest.mock import AsyncMock, patch

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.input.session_isolation.plugin import SessionIsolationPlugin


def _make_plugin() -> SessionIsolationPlugin:
    """构造会话级隔离守卫插件。"""
    return SessionIsolationPlugin(config={})


def _make_ctx(state: dict | None = None) -> PluginContext:
    """构造 tool_execute 上下文。"""
    default_state: dict = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {"command": "ls"}}],
        "workspace": r"D:\myproject\demo-app",
        "isolation_level": "isolated",
    }
    if state:
        default_state.update(state)
    return PluginContext(state=default_state, config={}, _services={})


def _patch_container(env_id: str = "cua-demo-app") -> AsyncMock:
    """Mock SessionWorkspaceService 的容器获取。"""
    return patch(
        "infrastructure.session.session_workspace.SessionWorkspaceService.get_or_create_session_container",
        AsyncMock(return_value=env_id),
    )


def _tool_calls(result) -> list[dict]:
    """从插件结果提取注入后的工具调用。"""
    return result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])


# ============================================================================
# 1. 生效条件：主会话 + workspace + isolated
# ============================================================================


class TestSessionIsolationActivation:
    """插件生效/不生效的条件矩阵。"""

    async def test_main_session_isolated_injects_container(self):
        """主会话 + isolated：bash_execute 注入 _container_id 与 /workspace。"""
        plugin = _make_plugin()
        ctx = _make_ctx()

        with _patch_container("cua-demo-app"):
            result = await plugin.execute(ctx)

        calls = _tool_calls(result)
        assert len(calls) == 1
        args = calls[0]["args"]
        assert args["_container_id"] == "cua-demo-app"
        assert args["working_dir"] == "/workspace"

    async def test_non_isolated_no_injection(self):
        """non_isolated：完全不干预。"""
        plugin = _make_plugin()
        ctx = _make_ctx({"isolation_level": "non_isolated"})

        with _patch_container():
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_no_workspace_no_injection(self):
        """无工作空间：不干预（兼容旧会话/CLI）。"""
        plugin = _make_plugin()
        ctx = _make_ctx({"workspace": ""})

        with _patch_container():
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_task_context_no_injection(self):
        """有 task_id（任务管道）：不干预，由 isolation_guard 管理。"""
        plugin = _make_plugin()
        ctx = _make_ctx({StateKeys.TASK_ID: "task_123"})

        with _patch_container():
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_non_bash_tool_no_injection(self):
        """非 bash_execute 工具：不干预。"""
        plugin = _make_plugin()
        ctx = _make_ctx(
            {
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "file_read", "args": {"path": "/a.txt"}},
                ],
            }
        )

        with _patch_container():
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_mixed_tools_only_bash_injected(self):
        """混合工具调用：仅 bash_execute 被注入，其余原样保留。"""
        plugin = _make_plugin()
        ctx = _make_ctx(
            {
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash_execute", "args": {"command": "ls"}},
                    {"name": "file_read", "args": {"path": "/a.txt"}},
                ],
            }
        )

        with _patch_container("cua-demo-app"):
            result = await plugin.execute(ctx)

        calls = _tool_calls(result)
        assert calls[0]["args"]["_container_id"] == "cua-demo-app"
        assert "_container_id" not in calls[1]["args"]

    async def test_explicit_working_dir_preserved(self):
        """显式指定 working_dir 时保留原值，不覆盖。"""
        plugin = _make_plugin()
        ctx = _make_ctx(
            {
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash_execute", "args": {"command": "ls", "working_dir": "/workspace/src"}},
                ],
            }
        )

        with _patch_container():
            result = await plugin.execute(ctx)

        args = _tool_calls(result)[0]["args"]
        assert args["working_dir"] == "/workspace/src"

    async def test_string_args_parsed(self):
        """args 为 JSON 字符串时先解析再注入。"""
        plugin = _make_plugin()
        ctx = _make_ctx(
            {
                StateKeys.RAW_TOOL_CALLS: [
                    {"name": "bash_execute", "args": json.dumps({"command": "ls"})},
                ],
            }
        )

        with _patch_container("cua-demo-app"):
            result = await plugin.execute(ctx)

        args = _tool_calls(result)[0]["args"]
        assert args["_container_id"] == "cua-demo-app"
        assert args["command"] == "ls"

    async def test_container_unavailable_degrades_to_host(self):
        """容器获取失败：降级宿主执行（不注入、不报错）。"""
        plugin = _make_plugin()
        ctx = _make_ctx()

        with _patch_container(None):
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_disabled_by_config(self):
        """enabled=false 时完全不执行。"""
        plugin = SessionIsolationPlugin(config={"enabled": False})
        ctx = _make_ctx()

        with _patch_container("cua-demo-app"):
            result = await plugin.execute(ctx)

        assert not result.state_updates

    async def test_non_tool_execute_skipped(self):
        """非 tool_execute 核心类型跳过。"""
        plugin = _make_plugin()
        ctx = _make_ctx({StateKeys.CORE_TYPE: "llm_call"})

        with _patch_container():
            result = await plugin.execute(ctx)

        assert not result.state_updates
