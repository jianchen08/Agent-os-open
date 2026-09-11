# @feature: FP-T07 llm api | @ci: python-coverage
"""param_inject JSON 兜底修复能力通道单元测试。

契约：``_repair_json_string`` 经 llm_service 的 ``llm.repair_json`` 能力修复
残缺 JSON；能力调用器未注入 / 信封异常 / 能力调用失败 / 响应形状异常 /
repaired 非字符串，一律返回 None——与修复函数"无法修复"既有语义一致，
调用方降级（arguments 置空 dict），不阻塞参数注入。

mock 仅用于外部依赖：capability_caller 替身模拟跨进程传输边界。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "input" / "param_inject"
)
_SHARED_DIR = _PLUGIN_DIR.parents[1]

for _d in (str(_SHARED_DIR), str(_PLUGIN_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

_MOD_NAME = "param_inject_repair_under_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubCaller:
    """可编程能力调用替身：模拟 tool-executor 传输与信封形态。"""

    def __init__(
        self,
        *,
        envelope: Any = None,
        exc: Exception | None = None,
        repaired: Any = '{"ok": 1}',
    ) -> None:
        self._envelope = envelope
        self._exc = exc
        self._repaired = repaired

    async def __call__(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG002
        if self._exc is not None:
            raise self._exc
        if self._envelope is not None:
            return self._envelope
        return {"success": True, "data": {"repaired": self._repaired}, "error": None}


def test_no_caller_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力调用器未注入（测试/降级）→ None（按不可修复处理）。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "_capability_caller", None)
    assert _run(mod._repair_json_string('{"a": 1')) is None


def test_caller_exception_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力调用失败（通道故障）→ None，不向注入主流程传播。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "_capability_caller", _StubCaller(exc=RuntimeError("bus down")))
    assert _run(mod._repair_json_string('{"a": 1')) is None


def test_failure_envelope_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """success=false 信封（服务不可用）→ None。"""
    mod = _load_module()
    monkeypatch.setattr(
        mod,
        "_capability_caller",
        _StubCaller(envelope={"success": False, "data": None, "error": "unavailable"}),
    )
    assert _run(mod._repair_json_string('{"a": 1')) is None


def test_non_dict_envelope_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """信封非 dict（形状异常）→ None。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "_capability_caller", _StubCaller(envelope="junk"))
    assert _run(mod._repair_json_string('{"a": 1')) is None


def test_non_string_repaired_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """data.repaired 非字符串（响应形状异常）→ None。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "_capability_caller", _StubCaller(repaired={"a": 1}))
    assert _run(mod._repair_json_string('{"a": 1')) is None


def test_happy_path_returns_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常通道：修复后的字符串原样返回。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "_capability_caller", _StubCaller(repaired='{"a": 1}'))
    assert _run(mod._repair_json_string('{"a": 1')) == '{"a": 1}'


# ============================================================
# execute 公开入口的修复链路行为（接线级）
# ============================================================
#
# 上节在 _repair_json_string 级钉降级语义；本节把同一契约抬到 execute
# 公开入口——经 set_capability_caller 注入能力替身（tool-executor 跨进程
# 传输边界，mock 合规），断 RAW_TOOL_CALLS 回写这一可观察结果：
# 修复成功保字段 / 不可修复降级空 dict 且注入主流程照常进行。


@pytest.fixture(autouse=True)
def _reset_global_capability_caller():
    """execute 读模块级 _capability_caller，测后清空防跨测试残留。"""
    yield
    import plugin as plugin_mod

    plugin_mod.set_capability_caller(None)


class TestRepairViaExecute:
    """残缺 JSON arguments 经 execute 的端到端行为。"""

    @staticmethod
    def _ctx(raw_args: str, *, state_extra: dict[str, Any] | None = None) -> Any:
        state: dict[str, Any] = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "file_write", "args": raw_args}],
        }
        state.update(state_extra or {})
        return PluginContext(state=state)

    @pytest.mark.asyncio
    async def test_truncated_args_repaired_fields_preserved(self) -> None:
        """截断 JSON 经修复能力恢复 → 字段保住写回 RAW_TOOL_CALLS。"""
        import plugin as plugin_mod
        from plugin import ParamInjectPlugin

        captured: dict[str, Any] = {}

        async def fake_caller(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG001
            captured.update(params)
            return {
                "success": True,
                "data": {"repaired": '{"path": "a.md", "content": "hi"}'},
                "error": None,
            }

        plugin_mod.set_capability_caller(fake_caller)

        result = await ParamInjectPlugin().execute(
            self._ctx('{"path": "a.md", "content": ')
        )
        calls = result.state_updates[plugin_mod.StateKeys.RAW_TOOL_CALLS]
        assert captured["tool_name"] == "llm.repair_json", "修复必须走 llm_service 能力"
        args = calls[0]["args"]
        # 修复保字段 + 系统注入键并存；task.id 未注入（state 无任务身份）
        assert args.get("path") == "a.md" and args.get("content") == "hi"
        assert set(args) <= {"path", "content", "session_id", "user_id", "timestamp"}
        assert calls[0].get("_args_truncated") is True, "结构性截断修复后应保留截断标记"

    @pytest.mark.asyncio
    async def test_capability_unavailable_degrades_to_empty_args_and_injects(self) -> None:
        """修复不可用（能力未注入）→ arguments 置空 dict，注入主流程照常。"""
        from plugin import ParamInjectPlugin

        result = await ParamInjectPlugin().execute(
            self._ctx('{"path": "a.md"', state_extra={"task.id": "t1"})
        )
        calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        args = calls[0]["args"]
        assert args.get("task_id") == "t1", "降级后空 dict 不得阻塞参数注入"
        assert "path" not in args, "不可修复时残缺字段不得带入"
        assert set(args) <= {"task_id", "session_id", "user_id", "timestamp"}, (
            "降级后只允许系统注入键"
        )

    @pytest.mark.asyncio
    async def test_capability_failure_degrades_same_way(self) -> None:
        """能力调用失败（通道异常）→ 同款降级，不向注入主流程传播。"""
        import plugin as plugin_mod
        from plugin import ParamInjectPlugin

        async def boom(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG001
            raise RuntimeError("bus down")

        plugin_mod.set_capability_caller(boom)

        result = await ParamInjectPlugin().execute(
            self._ctx('{"path": "a.md"', state_extra={"task.id": "t1"})
        )
        args = result.state_updates[StateKeys.RAW_TOOL_CALLS][0]["args"]
        assert args.get("task_id") == "t1"
        assert "path" not in args

    @pytest.mark.asyncio
    async def test_failure_envelope_degrades_same_way(self) -> None:
        """success=false 信封（服务不可用）→ 同款降级。"""
        import plugin as plugin_mod
        from plugin import ParamInjectPlugin

        async def unavailable(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG001
            return {"success": False, "data": None, "error": "unavailable"}

        plugin_mod.set_capability_caller(unavailable)

        result = await ParamInjectPlugin().execute(self._ctx('{"path": "a.md"'))
        args = result.state_updates[StateKeys.RAW_TOOL_CALLS][0]["args"]
        assert "path" not in args
        assert set(args) <= {"task_id", "session_id", "user_id", "timestamp"}

