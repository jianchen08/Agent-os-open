"""llm_core tool_call 配对测试——猜测型匹配反模式收口（ADR 2026-08-21 同族裁决）。

契约：tool result 的 tool_call_id 精确匹配失败时一律丢弃并 warn（fail-closed），
绝不 positional 改写（把 B 调用的产出冠上 A 的 id）；期望集合不被坏 id 消耗
（重放区静默吞错配同步清除）。

加载：_message_normalizer.py 经 importlib 唯一模块名加载（同
test_llm_core_multimodal_resolve.py 的 0.2 装配语义），配对缓存按
(provider, name, pipeline_id) 键控，测试用唯一 pipeline_id 隔离。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
)


def _load_module() -> Any:
    """加载 _message_normalizer.py（唯一模块名，进程内缓存）。"""
    mod_name = "message_normalizer_pairing_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _PLUGIN_DIR / "_message_normalizer.py"
    assert module_path.exists(), f"module missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


def _assistant(call_ids: str | list[str]) -> dict[str, Any]:
    ids = [call_ids] if isinstance(call_ids, str) else call_ids
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": f"tool_{cid}", "arguments": "{}"}}
            for cid in ids
        ],
    }


def _tool_result(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestToolCallPairingFailsClosed:
    """tool_call_id 失配一律丢弃，绝不 positional 改写（2026-08-22 裁决）。"""

    def test_unknown_id_dropped_not_rewritten(self, mod) -> None:
        """未知 id 的结果被丢弃且不被改写为待配对 id。"""
        messages = [
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            _assistant("call_bbb"),
            _tool_result("call_yyy", "B 调用的产出"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-1", pipeline_id="t-unknown-dropped"
        )
        # 完好的第一轮保留；失配轮整轮清理（Phase B）——绝不 positional 改写
        assert len(final) == 2, f"失配轮应整轮丢弃，实际保留: {final}"
        assert final[0]["role"] == "assistant"
        assert "B 调用的产出" not in str(final)

    def test_unknown_id_dropped_when_other_results_still_pending(self, mod) -> None:
        """增量路径：失配结果不消耗期望集合（call_b 的结果仍待配对）。"""
        first = [
            _assistant("call_a"),
            _tool_result("call_a"),
            _assistant("call_b"),
            _tool_result("call_b"),
        ]
        validated = mod._validate_tool_call_pairing(
            first, "deepseek", "pairing-fails-closed-2", pipeline_id="t-pending-others"
        )
        assert len(validated) == 4

        second = first + [_assistant("call_c"), _tool_result("call_zzz")]
        final = mod._validate_tool_call_pairing(
            second, "deepseek", "pairing-fails-closed-2", pipeline_id="t-pending-others"
        )
        # 契约：失配结果整轮丢弃（不改写为 call_c 并入），期望集合不被消耗
        assert len(final) == 4, f"失配结果应被丢弃且不消耗期望集合，实际: {final}"
        assert all(
            m.get("role") != "tool" or m.get("tool_call_id") != "call_zzz" for m in final
        )

    def test_exact_pair_kept(self, mod) -> None:
        """精确匹配路径不受影响（防过度删除）。"""
        messages = [
            _assistant("call_a"),
            _tool_result("call_a"),
        ]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-3", pipeline_id="t-exact-kept"
        )
        assert len(final) == 2
        assert final[1]["tool_call_id"] == "call_a"

    def test_orphan_result_dropped(self, mod) -> None:
        """无前置 assistant 的孤儿结果 → 丢弃（既有语义回归护栏）。"""
        messages = [_tool_result("call_orphan")]
        final = mod._validate_tool_call_pairing(
            messages, "deepseek", "pairing-fails-closed-4", pipeline_id="t-orphan"
        )
        assert final == []