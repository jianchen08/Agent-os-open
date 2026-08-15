"""SessionIsolationPlugin 单元测试——会话级隔离守卫的早退条件与容器注入。

注意：0.2 环境下 infrastructure.session.session_workspace（0.1 已归档）不可 import，
插件默认走 ImportError 分支返回空 PluginResult。本测试覆盖：
1. 各早退条件（disabled / 非 tool_execute / 有 task_id / 无 workspace / 非 isolated / 无 tool_calls）
2. ImportError 时静默降级
3. 通过 sys.modules 注入假 SessionWorkspaceService，验证容器注入核心逻辑
  （bash_execute 补 _container_id 与 working_dir=/workspace；非 bash 不改）。
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _full_state(**overrides: Any) -> dict[str, Any]:
    """构造满足所有生效条件的主会话状态。"""
    base = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.TASK_ID: "",  # 主会话
        "workspace": "/host/ws",
        "isolation_level": "isolated",
        StateKeys.RAW_TOOL_CALLS: [
            {"name": "bash_execute", "args": {"command": "ls"}}
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def fake_session_workspace(monkeypatch: pytest.MonkeyPatch):
    """注入一个假的 infrastructure.session.session_workspace 模块。

    插件用 `from infrastructure.session.session_workspace import SessionWorkspaceService`
    动态导入；我们在 sys.modules 里塞一个假模块，让 import 成功。
    """
    fake_module = type(sys)("infrastructure.session.session_workspace")

    async def _get_or_create(workspace: str) -> str:
        return "container-abc"

    fake_service = type(
        "SessionWorkspaceService",
        (),
        {"get_or_create_session_container": staticmethod(_get_or_create)},
    )
    fake_module.SessionWorkspaceService = fake_service  # type: ignore[attr-defined]

    # 同时确保父包 infrastructure / infrastructure.session 存在
    for pkg in ("infrastructure", "infrastructure.session"):
        if pkg not in sys.modules:
            mod = type(sys)(pkg)
            mod.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = mod
    sys.modules["infrastructure.session.session_workspace"] = fake_module
    yield fake_module
    # 清理
    sys.modules.pop("infrastructure.session.session_workspace", None)


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_属性(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        assert p.name == "session_isolation"
        assert p.priority == 25

    def test_自定义配置(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin(config={"enabled": False, "priority": 99})
        assert p._enabled is False
        assert p.priority == 99

    def test_error_policy为SKIP(self) -> None:
        from pipeline.types import ErrorPolicy
        from plugin import SessionIsolationPlugin

        assert SessionIsolationPlugin.error_policy == ErrorPolicy.SKIP


# ============================================================
# 早退条件
# ============================================================


class TestEarlyReturns:
    @pytest.mark.asyncio
    async def test_disabled直接返回空(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin(config={"enabled": False})
        result = await p.execute(_ctx(_full_state()))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_非tool_execute返回空(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state(**{StateKeys.CORE_TYPE: "llm_call"})))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_有task_id返回空_任务管道归isolation_guard(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state(**{StateKeys.TASK_ID: "t1"})))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_无workspace返回空(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state(workspace="")))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_isolation_level非isolated返回空(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(_full_state(isolation_level="non_isolated"))
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_无tool_calls返回空(self) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(_full_state(**{StateKeys.RAW_TOOL_CALLS: []}))
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_import失败时静默降级返回空(self) -> None:
        """0.2 默认环境：session_workspace 不可 import → 返回空。"""
        from plugin import SessionIsolationPlugin

        # 确保模块不在 sys.modules（模拟 0.1 已归档）
        sys.modules.pop("infrastructure.session.session_workspace", None)
        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state()))
        assert result.state_updates == {}


# ============================================================
# 核心注入逻辑（注入假 SessionWorkspaceService）
# ============================================================


class TestContainerInjection:
    @pytest.mark.asyncio
    async def test_bash_execute注入container_id与workspace默认目录(
        self, fake_session_workspace: Any
    ) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state()))

        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["working_dir"] == "/workspace"

    @pytest.mark.asyncio
    async def test_显式working_dir保留不覆盖(self, fake_session_workspace: Any) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(
                _full_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {
                                "name": "bash_execute",
                                "args": {
                                    "command": "ls",
                                    "working_dir": "/host/custom",
                                },
                            }
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["working_dir"] == "/host/custom"

    @pytest.mark.asyncio
    async def test_非bash工具不注入container_id(self, fake_session_workspace: Any) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(
                _full_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "file_read", "args": {"path": "/x"}}
                        ]
                    }
                )
            )
        )
        # 非 bash 不注入 → injected_count=0 → 返回空
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_混合工具只改bash_execute(self, fake_session_workspace: Any) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(
                _full_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {"name": "file_read", "args": {"path": "/x"}},
                            {"name": "bash_execute", "args": {"command": "pwd"}},
                            {"name": "file_write", "args": {"path": "/y"}},
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert "_container_id" not in calls[0]["args"]
        assert calls[1]["args"]["_container_id"] == "container-abc"
        assert "_container_id" not in calls[2]["args"]

    @pytest.mark.asyncio
    async def test_args为字符串JSON时也能解析注入(
        self, fake_session_workspace: Any
    ) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(
                _full_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {
                                "name": "bash_execute",
                                "args": '{"command": "ls"}',  # JSON 字符串
                            }
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert calls[0]["args"]["_container_id"] == "container-abc"
        assert calls[0]["args"]["command"] == "ls"

    @pytest.mark.asyncio
    async def test_args为非法JSON时降级为空dict再注入(
        self, fake_session_workspace: Any
    ) -> None:
        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(
            _ctx(
                _full_state(
                    **{
                        StateKeys.RAW_TOOL_CALLS: [
                            {
                                "name": "bash_execute",
                                "args": "not-json{",
                            }
                        ]
                    }
                )
            )
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        # 解析失败 → args={} → 补 _container_id 与 working_dir
        assert calls[0]["args"]["_container_id"] == "container-abc"

    @pytest.mark.asyncio
    async def test_container获取失败返回None时降级(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_or_create_session_container 返回 None → 不注入，返回空。"""
        fake_module = type(sys)("infrastructure.session.session_workspace")

        async def _none(workspace: str) -> None:
            return None

        fake_service = type(
            "SessionWorkspaceService",
            (),
            {"get_or_create_session_container": staticmethod(_none)},
        )
        fake_module.SessionWorkspaceService = fake_service  # type: ignore[attr-defined]
        for pkg in ("infrastructure", "infrastructure.session"):
            if pkg not in sys.modules:
                mod = type(sys)(pkg)
                mod.__path__ = []  # type: ignore[attr-defined]
                sys.modules[pkg] = mod
        sys.modules["infrastructure.session.session_workspace"] = fake_module

        from plugin import SessionIsolationPlugin

        p = SessionIsolationPlugin()
        result = await p.execute(_ctx(_full_state()))
        assert result.state_updates == {}

        sys.modules.pop("infrastructure.session.session_workspace", None)
