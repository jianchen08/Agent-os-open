# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 空转熔断测试（装机 2026-11-04 verifier 循环空转回归）。

契约（2026-11-04 用户裁定「空转到时间应该掐掉」）：
验收类 agent 在同一步骤反复输出相同正文且无工具调用（verifier 实测同一
未勾选条目连发 12 轮，每轮 2 秒即翻页燃烧迭代）——llm_core 按轮计度：
连续 N 轮（正文去空白后逐字节相同且无工具调用）即判无进展并熔断本轮
execute（RuntimeError 上抛 = 任务失败面带明确原因），有工具调用或正文
变化即重置计度。state 键跨轮持久（llm_loop_streak/llm_loop_last_hash），
计度隔离在各自管道 state 内。

加载：importlib 唯一模块名装载 plugin.py（裸名 ``plugin`` 会被兄弟插件
目录串扰）；``_config_models`` 经 shared/llm 目录平铺 import（追加 sys.path
尾部，不夺 llm_core 平铺名的首位——两目录均有 adapter.py）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

if str(_LLM_DIR) not in sys.path:
    sys.path.append(str(_LLM_DIR))

_MOD_NAME = "llm_core_loop_guard_under_test"


def _load_plugin() -> Any:
    """加载 llm_core/plugin.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None, "cannot load llm_core plugin.py"
    assert spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin()
LLMCore = _mod.LLMCore
set_capability_caller = _mod.set_capability_caller


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


def _make_plugin() -> Any:
    return LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})


class _RecordingCaller:
    """可编程响应序列的 capability caller（按预设队列逐次弹出）。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        return {"success": True, "data": self._responses.pop(0)}


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


def _state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "pipeline-a",
        "run_id": "run-a",
        "agent_level": "L2",
    }
    state.update(extra)
    return state



def _apply_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    """模拟引擎的 result→state 合并：messages 走 _ops 应用，其余键直接覆盖。"""
    msgs_update = result.pop("messages", None)
    if isinstance(msgs_update, dict) and "_ops" in msgs_update:
        messages = state.setdefault("messages", [])
        for op in msgs_update["_ops"]:
            if op.get("op") == "set":
                messages.append(op["msg"])
    for k, v in result.items():
        state[k] = v

def _resp(text: str, tool_calls: list[Any] | None = None) -> dict[str, Any]:
    return {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": text,
        "tool_calls": tool_calls or [],
        "thinking_text": None,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cached_tokens": 0},
        "finish_reason": "stop",
    }


_SAME_TEXT = "- [ ] 预备步骤：落实 docs/working/function_verify_report.md + verify_reproduce"


async def test_identical_text_no_tool_calls_trips_at_threshold() -> None:
    """连续相同正文且无工具调用 → 第 4 轮（连续计度=3）熔断上抛。"""
    caller = _RecordingCaller([_resp(_SAME_TEXT)] * 6)
    set_capability_caller(caller)
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    state = _state()

    _apply_result(state, await plugin.execute(_make_ctx(state)))  # 轮1 streak=0
    _apply_result(state, await plugin.execute(_make_ctx(state)))  # 轮2 streak=1
    _apply_result(state, await plugin.execute(_make_ctx(state)))  # 轮3 streak=2
    with pytest.raises(RuntimeError, match="空转熔断"):
        await plugin.execute(_make_ctx(state))  # 轮4 streak=3 → 掐


async def test_tool_calls_present_same_text_resets_and_never_trips() -> None:
    """正文相同但每轮带工具调用 → 不计度（工作中叙述属正常形态）。"""
    caller = _RecordingCaller(
        [_resp(_SAME_TEXT, tool_calls=[{"id": "c1", "name": "bash_execute", "arguments": "{}"}])] * 6
    )
    set_capability_caller(caller)
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    state = _state()

    for _ in range(6):
        result = await plugin.execute(_make_ctx(state))
        _apply_result(state, result)
        assert result["llm_loop_streak"] == 0


async def test_alternating_text_never_trips() -> None:
    """正文交替变化 → 每轮重置，永不熔断。"""
    caller = _RecordingCaller(
        [_resp("步骤甲"), _resp("步骤乙")] * 4
    )
    set_capability_caller(caller)
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    state = _state()

    for _ in range(8):
        _apply_result(state, await plugin.execute(_make_ctx(state)))
    assert state.get("llm_loop_streak") in (0, 1)


async def test_whitespace_only_difference_counts_as_same() -> None:
    """仅空白差异的同义正文 → 同一指纹（防换行/空格刷差异绕过）。"""
    caller = _RecordingCaller(
        [_resp("同一结论。"), _resp("同一结论。 "), _resp(" 同一结论。\n"), _resp("同一结论。")]
    )
    set_capability_caller(caller)
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    state = _state()

    _apply_result(state, await plugin.execute(_make_ctx(state)))
    _apply_result(state, await plugin.execute(_make_ctx(state)))
    _apply_result(state, await plugin.execute(_make_ctx(state)))
    with pytest.raises(RuntimeError, match="空转熔断"):
        await plugin.execute(_make_ctx(state))


async def test_progress_resets_streak_midway() -> None:
    """两轮相同后正文变化 → 计度重置，后续再来两轮相同也不熔断。"""
    caller = _RecordingCaller(
        [_resp(_SAME_TEXT), _resp(_SAME_TEXT), _resp("有推进的新结论"), _resp(_SAME_TEXT), _resp(_SAME_TEXT)]
    )
    set_capability_caller(caller)
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    state = _state()

    for _ in range(5):
        _apply_result(state, await plugin.execute(_make_ctx(state)))
    assert state.get("llm_loop_streak") == 1
