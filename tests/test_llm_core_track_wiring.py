# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""llm_core ← track 并入的接线测试（ADR 2026-09-15-track-merged-into-llm-core）。

钉死并入后的接线契约（``track_stats`` 自身语义由 ``test_track_stats_contract.py``
覆盖，本文件只管「有没有正确接上」）：

1. **成功轮写入** ``track.llm_usage`` / ``track.total_tokens``
   ——与原 post 步骤插件同键同量；
2. **本轮用量取自本次调用结果**，而非 ``state["llm_usage"]``（core 阶段 state
   里还是上一轮的值——从 state 重读会把上一轮重复累加）；
3. **累计不丢基准**：state 已有 ``track.llm_usage`` 时相加（切片投喂下该键必须
   在 ``state.reads`` 声明，见 manifest 契约测试）；
4. **无用量轮不写假值**：上游未回 usage → 继承上一轮，不覆盖成 0；
5. **统计异常隔离**：观测面抛错不得丢弃本轮成功的 assistant 消息（原独立插件
   出错时引擎 warn+继续，并入后必须保持同等韧性）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _SHARED_DIR / "system" / "llm"

while str(_SHARED_DIR) in sys.path:
    sys.path.remove(str(_SHARED_DIR))
for _d in (_SYSTEM_LLM_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))


def _load_plugin_module():
    """唯一名动态加载 plugin.py（裸名 plugin 会被兄弟插件目录串扰）。"""
    for stale in list(sys.modules):
        if stale == "llm_core" or stale.startswith("llm_core."):
            sys.modules.pop(stale, None)
    name = "_llm_core_track_wiring_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin_mod = _load_plugin_module()
sys.modules.pop("adapter", None)
LLMCore = plugin_mod.LLMCore


class _Ctx:
    """最小插件执行上下文（state 为真实 dict）。"""

    def __init__(self, state: dict[str, Any]):
        self.state = state


class _RecordingCaller:
    """伪 capability caller：记录调用并返回预设信封。"""

    def __init__(self, reply: Any):
        self.reply = reply
        self.calls: list[tuple[str, dict, float | None]] = []

    async def __call__(self, method: str, params: dict, timeout: float | None = None):
        self.calls.append((method, params, timeout))
        return self.reply


def _llm_data(**over: Any) -> dict[str, Any]:
    """tool-executor.invoke 成功信封（llm.complete_stream 聚合响应形态）。"""
    data: dict[str, Any] = {
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "finish_reason": "stop",
        "partial": None,
    }
    data.update(over)
    return {"success": True, "data": data}


def _plugin(caller: _RecordingCaller, config: dict[str, Any] | None = None) -> Any:
    plugin_mod.set_capability_caller(caller)
    return LLMCore(config or {"provider": "openai", "model_name": "gpt-4"})


def _base_state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "core_type": "llm_call",
        "iteration": 1,
        "pipeline_id": "p1",
        "session_id": "s1",
        "message_id": "m1",
    }
    state.update(over)
    return state


@pytest.fixture(autouse=True)
def _isolate_caller():
    """退出时清掉能力调用句柄（防跨测试串扰）。"""
    yield
    plugin_mod.set_capability_caller(None)


# ─────────────────── 成功轮写入三键 ───────────────────


async def test_success_writes_track_blocks() -> None:
    """成功轮产出 track.llm_usage / track.total_tokens / track.execution_stats。"""
    result = await _plugin(_RecordingCaller(_llm_data())).execute(_Ctx(_base_state()))

    usage = result["track.llm_usage"]
    assert usage["total_input_tokens"] == 100
    assert usage["total_output_tokens"] == 20
    assert usage["total_tokens"] == 120
    assert usage["last_input_tokens"] == 100
    assert result["track.total_tokens"] == 120
    assert "track.execution_stats" not in result  # 已退役（用户裁定 2026-09-15）


async def test_accumulates_on_top_of_state_baseline() -> None:
    """state 已有累计基准 → 相加（切片投喂下该键须在 manifest state.reads 声明）。"""
    state = _base_state(
        **{
            "track.llm_usage": {
                "total_input_tokens": 900,
                "total_output_tokens": 80,
                "total_cached_tokens": 500,
            }
        }
    )
    result = await _plugin(_RecordingCaller(_llm_data())).execute(_Ctx(state))

    usage = result["track.llm_usage"]
    assert usage["total_input_tokens"] == 1000  # 900 + 100
    assert usage["total_output_tokens"] == 100  # 80 + 20
    assert result["track.total_tokens"] == 1100


async def test_uses_current_call_usage_not_stale_state() -> None:
    """本轮用量取自本次调用结果，不是 state 里上一轮的残留。

    回归守护：core 阶段 state["llm_usage"] 是上一轮的值（本轮结果由引擎在
    步骤返回后才合并）；从 state 重读会把上一轮重复累加。
    """
    stale = {"input_tokens": 9999, "output_tokens": 9999, "total_tokens": 19998}
    state = _base_state(llm_usage=stale)
    result = await _plugin(_RecordingCaller(_llm_data())).execute(_Ctx(state))

    assert result["llm_usage"]["input_tokens"] == 100  # 本轮 API 返回
    assert result["track.llm_usage"]["total_input_tokens"] == 100  # 不累加残留
    assert result["track.llm_usage"]["last_input_tokens"] == 100


async def test_no_usage_round_inherits_previous_values() -> None:
    """上游未回 usage → 继承上一轮累计，不覆盖成 0（前端不得显示假 0）。"""
    state = _base_state(
        **{
            "track.llm_usage": {
                "total_input_tokens": 900,
                "total_output_tokens": 80,
                "total_tokens": 980,
                "last_input_tokens": 300,
                "last_output_tokens": 40,
                "last_cached_tokens": 150,
                "last_missed_tokens": 150,
                "last_cache_hit_ratio": 0.5,
            }
        }
    )
    result = await _plugin(_RecordingCaller(_llm_data(usage={}))).execute(_Ctx(state))

    usage = result["track.llm_usage"]
    assert result["llm_usage"] == {}
    assert usage["total_tokens"] == 980
    assert usage["last_input_tokens"] == 300
    assert usage["last_cache_hit_ratio"] == 0.5


# ─────────────────── 统计异常隔离 ───────────────────


async def test_track_failure_does_not_discard_round_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统计抛错 → 本轮成功的 assistant 消息照常返回（观测面降级不截肢）。

    原 track 是独立 post 插件，出错时引擎 warn+继续；并入后若放任其上抛，
    会被 execute 的 except 当作「LLM 调用失败」丢掉整轮结果——对话直接断。
    """

    async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("stats exploded")

    plugin = _plugin(_RecordingCaller(_llm_data()))
    monkeypatch.setattr(plugin._track, "run", _boom)

    result = await plugin.execute(_Ctx(_base_state()))

    assert result["raw_result"] == "ok"
    assert result["llm_usage"]["total_tokens"] == 120
    assert "messages" in result  # assistant 消息 op 未丢
    assert "track.llm_usage" not in result  # 统计面整体缺席（降级）
