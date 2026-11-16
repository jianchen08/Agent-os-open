# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 思考内容回放契约——append-only 性质锁定（线① 2026-10-21）。

契约（全量回放裁定 2026-10-21，取消"最近 N 轮"滚动窗口）：

- 出站消息构造（``_build_messages``）不剥离、不改写 assistant 消息的
  ``reasoning_content``——是否回传由 provider 适配层按 ``replay_reasoning``
  策略裁决（plugins/shared/llm/provider_adapters/）；
- **append-only 缓存友好性质**：某条 assistant 消息一旦带 rc 出站，随对话
  轮次推进，它在后续出站视图中必须原样在场（同一内容、同一位置语义）——
  滚动窗口会在每轮 retroactive 改写"最老保留条"，提示词前缀从该处失配、
  后段缓存逐轮失效（本任务实测 cache_hit_ratio=0.992，窗口即缓存毒药）；
  本测试锁定该性质防回归；
- 上下文增长由既有压缩层承担（压缩块消息不带 rc 负担，一次性缓存重置
  优于逐轮失效）。

外部依赖：无（纯出站构造，不触网）。
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

# 平铺 import 路径与 test_llm_core_execute_paths.py 同款：llm_core 版 adapter.py
# 必须压过 system 侧 llm 同名件（平铺 `import adapter` 取 LLMResponse 类型）。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

_MOD_NAME = "llm_core_reasoning_replay_under_test"


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


def _make_plugin() -> Any:
    return LLMCore({"provider": "openai", "model_name": "deepseek-v3", "default_params": {}})


def _assistant_turn(content: str, rc: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "reasoning_content": rc}


def _state_with_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "system_message": {"role": "system", "content": "sys"},
        "messages": copy.deepcopy(history),
    }


def test_outbound_view_preserves_rc_verbatim() -> None:
    """出站视图原样保留 rc（剥离决策权在 provider 适配层，llm_core 不预剥离）。"""
    plugin = _make_plugin()
    rc_text = "用户意图是 X，先查证 A 再推导 B。"
    state = _state_with_history(
        [
            {"role": "user", "content": "问题"},
            _assistant_turn("回答", rc_text),
        ]
    )

    out = plugin._build_messages(state)

    assistant = [m for m in out if m.get("role") == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == rc_text
    # 出站构造不改写 state 持久层（新视图，非就地剥离）
    assert state["messages"][1]["reasoning_content"] == rc_text


def test_rc_replay_is_append_only_across_turns() -> None:
    """append-only 性质：带 rc 的 assistant 消息随轮次推进不被 retroactive 改写。

    轮 T 的 assistant(rc=T_thinking) 出站后，轮 T+1 / T+2 历史增长（其前部
    不变、尾部追加），它在每轮出站视图中都必须原样在场——任何"只保留最近
    N 轮"的滚动窗口都会在本测试现形（最老保留条被剥 rc = 前缀失配）。
    """
    plugin = _make_plugin()
    rc_t1 = "第1轮思考：定位到根因 R。"
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "q1"},
        _assistant_turn("a1", rc_t1),
    ]

    # 轮 T：出站带 rc
    out_t = plugin._build_messages(_state_with_history(history))
    assistants_t = [m for m in out_t if m.get("role") == "assistant"]
    assert assistants_t[0]["reasoning_content"] == rc_t1

    # 轮 T+1 / T+2：历史只追加不回写（append-only 推进）
    for round_no in (2, 3):
        history += [
            {"role": "user", "content": f"q{round_no}"},
            _assistant_turn(f"a{round_no}", f"第{round_no}轮思考：补充证据 E{round_no}。"),
        ]
        out = plugin._build_messages(_state_with_history(history))
        assistants = [m for m in out if m.get("role") == "assistant"]
        # 首条 assistant 的 rc 与轮 T 出站逐字节一致（缓存前缀稳定的锚）
        assert assistants[0]["reasoning_content"] == rc_t1
        # 每条 assistant 都带各自 rc（全量回放，无窗口裁剪）
        assert len(assistants) == round_no
        for idx, msg in enumerate(assistants, start=1):
            expected = rc_t1 if idx == 1 else f"第{idx}轮思考：补充证据 E{idx}。"
            assert msg["reasoning_content"] == expected
        # state 持久层全程未被改写（增长列表的首条 assistant rc 原样）
        assert history[1]["reasoning_content"] == rc_t1


def test_engine_synthetic_messages_still_excluded() -> None:
    """回归护栏：rc 全量回放不放松既有出站黑名单——synthetic 助手消息仍整条剔除。"""
    plugin = _make_plugin()
    state = _state_with_history(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "失败终态说明",
                "synthetic": True,
                "reasoning_content": "不应出站",
            },
            _assistant_turn("正常回答", "正常 rc"),
        ]
    )

    out = plugin._build_messages(state)

    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["reasoning_content"] == "正常 rc"
