# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core `state.reads` 切片投喂回归（空转熔断 + 悬空工具对账）。

两起静默降级（均由读点漏声明引起，2026-11-12 修复）：
1. **空转熔断失效**：计度键 `llm_loop_streak` / `llm_loop_last_hash` 未声明 →
   切片后上一轮计度恒读不到（None）→ 指纹比对恒不等 → streak 恒 0，连续相同
   正文永不熔断（生产切片下 breaker 形同不存在）。
2. **悬空工具对账失效**：`tool_ids`（agent 声明的工具集）未声明 → 切片后恒
   空集 → 「声明 ∖ 当前工具面」恒为空 → 注入指令引用缺席工具时既不标注也
   不 ERROR 留痕，模型只能抓在场工具编造。

本用例按 manifest 声明切片投喂（与生产 invoker 同形），断言两条行为在切片下
真实成立；声明回退即红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent

for _p in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _libs.testing.state_slice import (  # noqa: E402
    CONTROL_KEYS,
    manifest_reads,
    slice_feed,
    visible_keys,
)

_LLM_DIR = _SHARED_DIR / "llm"
if str(_LLM_DIR) not in sys.path:
    # 追加尾部：不夺 llm_core 平铺名的首位（两目录均有 adapter.py）
    sys.path.append(str(_LLM_DIR))

_MOD_NAME = "llm_core_state_reads_slice_under_test"


def _load_plugin() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None, "cannot load llm_core plugin.py"
    assert spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin()
LLMCore = _mod.LLMCore

_SAME_TEXT = "- [ ] 预备步骤：落实 docs/working/function_verify_report.md + verify_reproduce"


MANIFEST_READS = manifest_reads(_PLUGIN_DIR)


class _RecordingCaller:
    """可编程响应序列 + 记录每次调用参数的 capability caller。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        return {"success": True, "data": self._responses.pop(0)}


def _ctx(state: dict[str, Any]) -> Any:
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=state)


def _full_state(**extra: Any) -> dict[str, Any]:
    """任务管道真实形态 state（含上一轮计度键与 agent 工具声明）。"""
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "pipeline-a",
        "run_id": "run-a",
        "agent_level": "L2",
        "tool_schemas": [],
        "tool_ids": ["file_read"],
    }
    state.update(extra)
    return state


def _fed(state: dict[str, Any]) -> dict[str, Any]:
    """切片投喂（生产 invoker 同形：控制键白名单 ∪ 声明键）。"""
    return slice_feed(state, MANIFEST_READS)


def _merge(state: dict[str, Any], result: dict[str, Any]) -> None:
    """引擎 merge 语义：messages 走 _ops 应用，其余键直写。"""
    msgs_update = result.pop("messages", None)
    if isinstance(msgs_update, dict) and "_ops" in msgs_update:
        messages = state.setdefault("messages", [])
        for op in msgs_update["_ops"]:
            if op.get("op") == "set":
                messages.append(op["msg"])
    state.update(result)


def _resp(text: str, tool_calls: list[Any] | None = None) -> dict[str, Any]:
    return {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": text,
        "tool_calls": tool_calls or [],
        "thinking_text": None,
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cached_tokens": 0,
        },
        "finish_reason": "stop",
    }


@pytest.fixture(autouse=True)
def _llm_model_config(monkeypatch: Any) -> None:
    """注入最小 llm.yaml 视图：模型空解析现在显式报错（本模块不测模型解析）。"""
    import _config_models

    monkeypatch.setattr(
        _config_models,
        "_config",
        {
            "llm": {
                "models": {
                    "stub-model": {
                        "provider": "stub",
                        "model_name": "stub-model",
                        "api_base": "https://stub.example.com",
                        "api_key": "stub-key",
                    }
                },
                "defaults": {"chat": "stub-model"},
            }
        },
    )


def _plugin() -> Any:
    return LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})


class TestDeclaration:
    def test_slice_carried_keys_declared(self) -> None:
        """计度键与工具声明必须声明（否则切片后恒读不到）。"""
        declared = visible_keys(MANIFEST_READS)
        for key in ("llm_loop_streak", "llm_loop_last_hash", "tool_ids"):
            assert key in declared, f"{key} 未在 state.reads 声明 → 切片后恒缺省"

    def test_slice_feed_keeps_loop_state_keys(self) -> None:
        """切片投喂实际携带计度键（声明生效的当轮观测面）。"""
        fed = _fed(_full_state(llm_loop_streak=2, llm_loop_last_hash="abc"))
        assert fed["llm_loop_streak"] == 2
        assert fed["llm_loop_last_hash"] == "abc"
        assert fed["tool_ids"] == ["file_read"]


class TestLoopBreakerUnderSliceFeed:
    """空转熔断：切片投喂下计度跨轮持续，连续相同正文必须熔断。"""

    async def test_identical_text_trips_under_slice_feed(self) -> None:
        caller = _RecordingCaller([_resp(_SAME_TEXT)] * 6)
        _mod.set_capability_caller(caller)
        plugin = _plugin()
        state = _full_state()

        for _ in range(3):
            _merge(state, await plugin.execute(_ctx(_fed(state))))

        assert state["llm_loop_streak"] == 2, f"计度未跨轮累积：{state['llm_loop_streak']}"
        with pytest.raises(RuntimeError, match="空转熔断"):
            await plugin.execute(_ctx(_fed(state)))

    async def test_changed_text_resets_streak_under_slice_feed(self) -> None:
        """正文变化即重置——切片修复不得把正常推进误判为空转。"""
        caller = _RecordingCaller([_resp(_SAME_TEXT), _resp(_SAME_TEXT), _resp("新推进：已改完 A 文件")])
        _mod.set_capability_caller(caller)
        plugin = _plugin()
        state = _full_state()

        for _ in range(3):
            _merge(state, await plugin.execute(_ctx(_fed(state))))

        assert state["llm_loop_streak"] == 0, "正文变化后计度必须归零"
        assert state["llm_loop_last_hash"] == LLMCore._loop_fingerprint("新推进：已改完 A 文件", [])


class TestDanglingToolReconciliationUnderSliceFeed:
    """悬空工具对账：agent 声明的工具集与当前工具面之差必须可见。"""

    async def test_injected_instruction_annotated_under_slice_feed(self) -> None:
        assert CONTROL_KEYS, "控制键白名单镜像非空"
        caller = _RecordingCaller([_resp("收到")])
        _mod.set_capability_caller(caller)
        plugin = _plugin()
        state = _full_state(
            messages=[
                {
                    "role": "user",
                    "content": "[系统通知] 请先调 file_read 读 (ID: t-1)",
                }
            ],
            tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        )

        await plugin.execute(_ctx(_fed(state)))

        outbound = caller.calls[0][1]["args"]["messages"]
        contents = [m.get("content") for m in outbound if m.get("role") == "user"]
        assert any("[系统对账]" in str(c) and "file_read" in str(c) for c in contents), (
            f"出站消息未标注悬空工具引用（tool_ids 未声明则恒不判定）：{contents}"
        )

    async def test_no_annotation_when_tool_present_under_slice_feed(self) -> None:
        """工具真在场时不标注（对账不得误伤）。"""
        caller = _RecordingCaller([_resp("收到")])
        _mod.set_capability_caller(caller)
        plugin = _plugin()
        state = _full_state(
            messages=[{"role": "user", "content": "[系统通知] 请先调 file_read 读 (ID: t-1)"}],
            tool_schemas=[{"type": "function", "function": {"name": "file_read"}}],
        )

        await plugin.execute(_ctx(_fed(state)))

        outbound = caller.calls[0][1]["args"]["messages"]
        assert not any("[系统对账]" in str(m.get("content")) for m in outbound)
