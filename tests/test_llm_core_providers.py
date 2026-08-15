# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 提供者拆分回归测试（task_kernel_cleanup_and_split 3a/3b）。

结构断言：`adapter.py` 不再包含任何具体提供者 hack 与诊断机制——已迁至
`llm_provider_*` 插件（deepseek/minimax/keypool）与 `_diagnostics.py`，
防止回退（llm_core 不绑定提供者）。

行为断言：迁移后的 provider 插件与注册表（`_provider_registry`）行为与
拆分前内联实现等价：
- MiniMax 消息角色安全修正（非首位 system → user）
- openai/ 前缀模型（DeepSeek 等 OpenAI 兼容中转）reasoning_effort/thinking
  → extra_body 透传
- <think/> 标签 reasoning 提取（DeepSeek/o1 类）
- 未命中任何 provider 规则时行为不变（内置 LiteLLM 直调）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"
for _d in (_LLM_CORE_DIR, _CORE_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _provider_registry import apply_pre_send, extract_thinking_from_content  # noqa: E402
from llm_provider_deepseek import (  # noqa: E402
    extract_thinking_from_content as ds_extract_thinking,
    move_to_extra_body,
)
from llm_provider_minimax import ensure_role_safety  # noqa: E402


# ─────────────────── 结构断言（防回退） ───────────────────

def test_adapter_no_longer_contains_provider_hacks() -> None:
    """adapter.py 不得再定义提供者 hack / 诊断 / KeyPool（已拆分）。"""
    src = (_LLM_CORE_DIR / "adapter.py").read_text(encoding="utf-8")
    for forbidden in (
        "def _ensure_minimax_role_safety",
        "def _move_to_extra_body",
        "def _extract_thinking_from_content",
        "class KeyPoolAdapter",
        "def _log_final_payload",
        "def _install_payload_diag_hook",
        "def _log_prompt_body",
        "def _redact_prompt",
        "_REDACT_PATTERNS",
    ):
        assert forbidden not in src, f"adapter.py 不应再包含 {forbidden}（已拆至 provider 插件/_diagnostics）"


def test_provider_plugins_are_standalone_packages() -> None:
    """三个 provider 插件各自独立可导入。"""
    import llm_provider_keypool  # noqa: PLC0415

    assert callable(ensure_role_safety)
    assert callable(ds_extract_thinking)
    assert callable(move_to_extra_body)
    assert llm_provider_keypool.KeyPoolAdapter is not None


# ─────────────────── 行为断言（与拆分前等价） ───────────────────

def test_minimax_role_safety_fixes_non_first_system() -> None:
    """MiniMax：非首位 system 消息 → user（原地修正 + 返回引用）。"""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "injected"},
    ]
    out = ensure_role_safety("minimax/MiniMax-M2.7", msgs)
    assert out is msgs, "应原地修改并返回同一引用"
    assert msgs[1]["role"] == "user"
    assert "name" not in msgs[1]


def test_minimax_role_safety_skips_non_minimax_model() -> None:
    """非 minimax 模型：不动消息。"""
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    out = ensure_role_safety("zai/glm-5.1", msgs)
    assert out[1]["role"] == "system"


def test_move_to_extra_body_passthrough() -> None:
    """openai/ 中转端点：reasoning_effort/thinking 挪进 extra_body。"""
    kwargs = {"reasoning_effort": "max", "thinking": {"type": "enabled"}, "temperature": 0.7}
    move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))
    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs
    assert kwargs["extra_body"] == {
        "reasoning_effort": "max",
        "thinking": {"type": "enabled"},
    }
    # 已存在的 extra_body 合并而非覆盖
    assert kwargs["temperature"] == 0.7


def test_think_tag_extraction() -> None:
    """<think/> 标签提取：返回 (thinking, cleaned)，支持标准与 MiniMax 变体。"""
    thinking, cleaned = ds_extract_thinking("aaa<think>inner</think>bbb")
    assert thinking == "inner"
    assert cleaned == "aaabbb"
    # 无标签：原样返回
    assert ds_extract_thinking("plain text") == (None, "plain text")
    assert ds_extract_thinking(None) == (None, None)
    # 多标签合并
    t, c = ds_extract_thinking("a<think>1</think>b<think>2</think>c")
    assert t == "1\n2"
    assert c == "abc"


# ─────────────────── 注册表分发断言 ───────────────────

def test_registry_applies_minimax_role_safety_by_model() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    out = apply_pre_send("minimax/MiniMax-M2.7", msgs, {})
    assert out[1]["role"] == "user"


def test_registry_applies_extra_body_for_openai_prefix() -> None:
    kwargs = {"reasoning_effort": "max"}
    apply_pre_send("openai/deepseek-v4-flash", [{"role": "user", "content": "hi"}], kwargs)
    assert kwargs["extra_body"]["reasoning_effort"] == "max"


def test_registry_no_match_keeps_behavior_unchanged() -> None:
    """未命中任何 provider 规则：messages/kwargs 原样（内置 LiteLLM 直调语义）。"""
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    kwargs = {"temperature": 0.7}
    out = apply_pre_send("zai/glm-5.1", msgs, kwargs)
    assert out is msgs
    assert kwargs == {"temperature": 0.7}


def test_registry_extract_thinking_dispatches() -> None:
    assert extract_thinking_from_content("a<think>t</think>b") == ("t", "ab")
