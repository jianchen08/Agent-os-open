# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""ToolProgressReporter 单元测试——工具进度构建与发布逻辑。

覆盖：_build_progress 的状态映射（success/failed/pending）、
摘要截断、调用与结果数不匹配、event_bus 发布与缺失静默、enabled 开关。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    return PluginContext(state=state or {}, config={}, _services=services or {})


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_默认配置(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        assert r.name == "tool_progress_reporter"
        assert r.priority == 30
        assert r._enabled is True
        assert r._summary_max_length == 200

    def test_自定义配置(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter(
            config={"enabled": False, "summary_max_length": 50, "priority": 7}
        )
        assert r._enabled is False
        assert r._summary_max_length == 50
        assert r.priority == 7

# ============================================================
# _build_progress
# ============================================================


class TestBuildProgress:
    def test_成功结果映射为success(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = [{"name": "file_write"}]
        results = [{"content": "done"}]
        prog = r._build_progress(calls, results)

        assert len(prog) == 1
        assert prog[0]["tool_name"] == "file_write"
        assert prog[0]["status"] == "success"
        assert "done" in prog[0]["result_summary"]

    def test_带error的结果映射为failed(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = [{"name": "git_commit"}]
        results = [{"error": "permission denied"}]
        prog = r._build_progress(calls, results)

        assert prog[0]["status"] == "failed"
        assert "permission denied" in prog[0]["result_summary"]

    def test_None结果映射为pending(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = [{"name": "slow_tool"}]
        results = [None]
        prog = r._build_progress(calls, results)

        assert prog[0]["status"] == "pending"

    def test_摘要截断到max_length(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter(config={"summary_max_length": 10})
        calls = [{"name": "t"}]
        results = ["x" * 100]
        prog = r._build_progress(calls, results)

        assert len(prog[0]["result_summary"]) == 10

    def test_调用多于结果时未匹配的标pending(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        results = ["ok"]  # 只有 1 个结果
        prog = r._build_progress(calls, results)

        assert len(prog) == 3
        assert prog[0]["status"] == "success"
        assert prog[1]["status"] == "pending"
        assert prog[1]["tool_name"] == "b"
        assert prog[2]["status"] == "pending"

    def test_结果多于调用时tool_name为空但状态填充(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = []  # 无调用记录
        results = ["r1", "r2"]
        prog = r._build_progress(calls, results)

        assert len(prog) == 2
        assert prog[0]["tool_name"] == ""
        assert prog[0]["status"] == "success"

    def test_调用名缺失用unknown(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        calls = [{}]  # 无 name
        results = ["ok"]
        prog = r._build_progress(calls, results)
        assert prog[0]["tool_name"] == "unknown"

    def test_空输入返回空列表(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        assert r._build_progress([], []) == []


# ============================================================
# execute 端到端
# ============================================================


class TestExecute:
    @pytest.mark.asyncio
    async def test_disabled时返回空OutputResult(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter(config={"enabled": False})
        result = await r.execute(
            _ctx({StateKeys.TOOL_RESULTS: ["x"], StateKeys.RAW_TOOL_CALLS: [{"name": "t"}]})
        )
        # 无 state_updates（默认空 dict）
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_正常执行写入tool_progress到state(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        result = await r.execute(
            _ctx(
                {
                    StateKeys.RAW_TOOL_CALLS: [{"name": "t"}],
                    StateKeys.TOOL_RESULTS: ["ok"],
                }
            )
        )
        assert "tool_progress" in result.state_updates
        assert result.state_updates["tool_progress"][0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_无工具数据时写空列表(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        result = await r.execute(_ctx({}))
        assert result.state_updates["tool_progress"] == []

    @pytest.mark.asyncio
    async def test_event_bus可用时发布事件(self) -> None:
        from plugin import ToolProgressReporter

        bus = MagicMock()
        bus.emit = MagicMock()
        r = ToolProgressReporter()
        await r.execute(
            _ctx(
                {
                    StateKeys.RAW_TOOL_CALLS: [{"name": "t"}],
                    StateKeys.TOOL_RESULTS: ["ok"],
                    StateKeys.SESSION_ID: "sess-1",
                    StateKeys.TASK_ID: "task-9",
                },
                services={"event_bus": bus},
            )
        )

        bus.emit.assert_called_once()
        args = bus.emit.call_args
        assert args.args[0] == "tool_progress"
        payload = args.args[1]
        assert payload["session_id"] == "sess-1"
        assert payload["task_id"] == "task-9"
        assert payload["progress"][0]["tool_name"] == "t"

    @pytest.mark.asyncio
    async def test_event_bus缺失时静默不抛(self) -> None:
        from plugin import ToolProgressReporter

        r = ToolProgressReporter()
        # 不注册 event_bus 服务
        result = await r.execute(
            _ctx(
                {
                    StateKeys.RAW_TOOL_CALLS: [{"name": "t"}],
                    StateKeys.TOOL_RESULTS: ["ok"],
                }
            )
        )
        # 仍正常返回，不抛 KeyError
        assert "tool_progress" in result.state_updates

    @pytest.mark.asyncio
    async def test_event_bus无emit方法时不发布(self) -> None:
        from plugin import ToolProgressReporter

        bus = object()  # 无 emit 方法
        r = ToolProgressReporter()
        result = await r.execute(
            _ctx(
                {
                    StateKeys.RAW_TOOL_CALLS: [{"name": "t"}],
                    StateKeys.TOOL_RESULTS: ["ok"],
                },
                services={"event_bus": bus},
            )
        )
        # hasattr(emit) 为 False，跳过发布但结果照常
        assert result.state_updates["tool_progress"][0]["status"] == "success"
