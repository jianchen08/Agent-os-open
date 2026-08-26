# @feature: 评估闸门插件裁决 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder server.py 适配层测试——execute 工具入口与生命周期钩子。

server.py 是纯接口适配层：把 MCP 工具调用翻译成 TaskReminder.execute 的
PluginContext/OutputResult 信封（core 型 dict 与 PluginResult 两形态都要能回传）。
本文件覆盖：

1. execute 工具入口：dict 返回（pending→running 推进走 core 分支）、
   OutputResult 返回（route_signal/skip_remaining 展开）、config 覆盖传入；
2. on_load 预热单例、on_unload 清缓存；
3. get_instance 单例缓存复用（同实例对象）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_server() -> Any:
    """按显式路径加载 server.py（裸名 server 全车道共跑会被劫持）。"""
    mod_name = "task_reminder_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestExecuteToolEntry:
    async def test_dict_result_passthrough(self) -> None:
        """pending 状态 → 插件返回 OutputResult（仅 state_updates），信封原样展开。"""
        srv = _load_server()
        resp = await srv.execute(
            state={"task.status": "pending", "iteration": 1, "raw_tool_calls": [], "raw_result": ""},
        )
        assert resp == {"state_updates": {"task.status": "running"}}

    async def test_output_result_expands_route_signal(self) -> None:
        """L2 纯文本无子任务 → OutputResult 展开 state_updates + route_signal。"""
        srv = _load_server()
        resp = await srv.execute(
            state={
                "task.id": "task-x",
                "task.status": "running",
                "agent_level": "L2",
                "core_type": "llm_call",
                "iteration": 1,
                "raw_tool_calls": [],
                "raw_result": "阶段性输出",
                "messages": [],
            },
        )
        assert "evaluate_reminder_count" in resp["state_updates"]
        assert resp["route_signal"]["route_type"] == "next_llm"
        assert "task_reminder" in resp["route_signal"]["reason"]

    async def test_skip_remaining_flag_propagates(self) -> None:
        """OutputResult.skip_remaining=True → 信封带 skip_remaining。"""
        srv = _load_server()
        # 用不可达的自定义 result 形状：直接把 get_instance 换成假插件验证信封展开
        fake = _FakeOutput()
        orig = srv.get_instance
        srv.get_instance = lambda: fake  # type: ignore[method-assign]
        try:
            resp = await srv.execute(state={"task.id": "t", "task.status": "running"})
        finally:
            srv.get_instance = orig  # type: ignore[method-assign]
        assert resp["state_updates"] == {"k": "v"}
        assert resp["route_signal"] == {"route_type": "success", "target": None, "reason": "r"}
        assert resp["skip_remaining"] is True

    async def test_config_override_passed_to_plugin(self) -> None:
        """config 参数传入 PluginContext（构造单例时也带）。"""
        srv = _load_server()
        seen: dict[str, Any] = {}

        class _Recording:
            async def execute(self, ctx: Any) -> Any:
                seen["config"] = ctx.config
                from pipeline.plugin import OutputResult

                return OutputResult(state_updates={})

        orig = srv.get_instance
        srv.get_instance = lambda: _Recording()  # type: ignore[method-assign]
        try:
            await srv.execute(state={"task.id": "t"}, config={"max_reminders": 3})
        finally:
            srv.get_instance = orig  # type: ignore[method-assign]
        assert seen["config"] == {"max_reminders": 3}


class _FakeOutput:
    """伪造 OutputResult 形状（route_signal 用 dict 形态验证展开）。"""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.state_updates = {"k": "v"}
        self.route_signal = SimpleNamespace(route_type="success", target=None, reason="r")
        self.skip_remaining = True

    async def execute(self, ctx: Any) -> Any:
        return self


class TestLifecycleHooks:
    async def test_on_load_preheats_and_unload_clears_cache(self) -> None:
        srv = _load_server()
        await srv._on_load({})
        first = srv.get_instance()
        assert first is srv.get_instance()  # 预热 + 单例缓存
        await srv._on_unload({})
        second = srv.get_instance()
        assert second is not first  # 缓存已清，重新构建

    async def test_get_instance_reads_plugin_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server()
        monkeypatch.setattr(srv.plugin, "get_config", lambda: {"max_reminders": 7})
        inst = srv.get_instance()
        assert inst._max_reminders == 7
