# @feature: FP-T07 llm api | @ci: python-coverage
"""接口方言注册表契约测试（LLM 两类收敛批 3；主方案 §6.4）。

契约：
- 方言键 = llm.yaml ``providers.<name>.type``（批 1 已对齐的键位空间）：
  ``openai-compat`` 首发实现以 type 词表（openai/deepseek/minimax/zai）解析；
- 未注册的 type 键 fail-closed（``DialectNotFoundError``，不回退默认方言）；
- 扩展点 ``register_dialect``/``unregister_dialect`` 可注册新方言；
  键位重复默认拒绝（不静默覆盖），``replace=True`` 是显式换装；
- 语法键唯一、type 索引唯一（同一 type 词不指向两个方言实现）。

≥2 组有区分度输入做性质断言（type 空间封闭性），非硬编码单一期望刷绿。
外部依赖全桩（不触网）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"
# 平铺 import：llm_core 目录必须压过 llm 目录（两处 adapter.py 同名）。
for _d in (_SYSTEM_LLM_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_SYSTEM_LLM_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

import llm_dialects  # noqa: E402
import llm_wire  # noqa: E402


def _fake_dialect(key: str, type_keys: tuple[str, ...] = ()) -> llm_dialects.Dialect:
    """构造测试专用方言（callable 只需可调用，行为不重要）。"""
    return llm_dialects.Dialect(
        key=key,
        type_keys=type_keys,
        build_payload=llm_wire.build_body,
        stream=llm_wire.stream_chat,
        post=llm_wire.post_chat,
    )


# ─────────────────────────── openai-compat 首发 ───────────────────────────


def test_openai_compat_registered_as_first_dialect() -> None:
    """openai-compat 在库且可解析（首发方言）。"""
    dialect = llm_dialects.get_dialect(llm_dialects.OPENAI_COMPAT)
    assert dialect.key == "openai-compat"
    assert "openai-compat" in llm_dialects.dialect_keys()


@pytest.mark.parametrize("type_key", ["openai", "deepseek", "minimax", "zai"])
def test_wire_type_keys_resolve_to_openai_compat(type_key: str) -> None:
    """llm.yaml providers.type 的四类 wire 词全部解析到 openai-compat。

    这四类在网关现状里走同一条 OpenAI chat.completions wire（消息适配差异在
    provider adapter 层，不在 wire 形态）——注册表如实承载该事实。
    """
    assert llm_dialects.resolve_dialect_key(type_key) == llm_dialects.OPENAI_COMPAT


def test_unknown_type_key_fails_closed() -> None:
    """未注册 type（如 anthropic 原生协议未被 wire 覆盖）→ fail-closed。"""
    with pytest.raises(llm_dialects.DialectNotFoundError, match="anthropic"):
        llm_dialects.get_dialect("anthropic")
    assert "anthropic" not in llm_dialects.type_keys()


def test_empty_dialect_key_fails_closed() -> None:
    """空方言键（配置缺 type）→ fail-closed，不静默落任意默认。"""
    with pytest.raises(llm_dialects.DialectNotFoundError, match="空"):
        llm_dialects.get_dialect("")


# ─────────────────────────── 扩展注册点 ───────────────────────────


def test_register_and_unregister_extension_dialect() -> None:
    """扩展方言注册 → 解析 → 注销后回到未注册（注册表可扩展且可回退）。"""
    dialect = _fake_dialect("dsh-test", ("dsh-test-type",))
    llm_dialects.register_dialect(dialect)
    try:
        assert llm_dialects.get_dialect("dsh-test").key == "dsh-test"
        assert llm_dialects.resolve_dialect_key("dsh-test-type") == "dsh-test"
        assert "dsh-test-type" in llm_dialects.type_keys()
    finally:
        llm_dialects.unregister_dialect("dsh-test")
    with pytest.raises(llm_dialects.DialectNotFoundError):
        llm_dialects.get_dialect("dsh-test")
    assert "dsh-test-type" not in llm_dialects.type_keys()


def test_duplicate_dialect_key_rejected_without_replace() -> None:
    """重复方言键默认拒绝（不静默覆盖已注册实现）。"""
    with pytest.raises(ValueError, match="已注册"):
        llm_dialects.register_dialect(_fake_dialect(llm_dialects.OPENAI_COMPAT))


def test_empty_dialect_key_registration_rejected() -> None:
    """空方言实现键不得注册（语法键唯一且非空）。"""
    with pytest.raises(ValueError, match="不能为空"):
        llm_dialects.register_dialect(_fake_dialect(""))


def test_unregister_unknown_dialect_is_noop() -> None:
    """注销未注册键幂等（可回退面不因重复注销报错）。"""
    llm_dialects.unregister_dialect("never-registered-dialect")
    assert "never-registered-dialect" not in llm_dialects.dialect_keys()


def test_duplicate_type_key_rejected_across_dialects() -> None:
    """type 词归属唯一：已被 openai-compat 占用 → 另一方言注册即拒绝。"""
    with pytest.raises(ValueError, match="type"):
        llm_dialects.register_dialect(_fake_dialect("other-dialect", ("openai",)))


def test_replace_registration_swaps_implementation() -> None:
    """replace=True 是显式换装：同键实现被替换，type 索引仍指向该键。"""
    original = llm_dialects.get_dialect(llm_dialects.OPENAI_COMPAT)
    replacement = _fake_dialect(llm_dialects.OPENAI_COMPAT, original.type_keys)
    llm_dialects.register_dialect(replacement, replace=True)
    try:
        assert llm_dialects.get_dialect("openai") is replacement
    finally:
        llm_dialects.register_dialect(original, replace=True)


def test_type_space_closed_under_registry() -> None:
    """type 空间封闭性：每个已登记 type 都解析到一个已登记实现键。"""
    keys = set(llm_dialects.dialect_keys())
    for type_key in llm_dialects.type_keys():
        assert llm_dialects.resolve_dialect_key(type_key) in keys
    # 区分度：type 词表 ⊆ 实现键的别名面，且不止一个词（防单值刷绿）
    assert len(llm_dialects.type_keys()) >= 2


def test_openai_dialect_payload_build_matches_wire_builder() -> None:
    """openai-compat 的 payload 构建即 wire 机器的 build_body（单点绑定）。"""
    dialect = llm_dialects.get_dialect("openai")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "你好🌊"},
    ]
    extra = {"temperature": 0.2, "reasoning_effort": "high"}
    assert dialect.build_payload("up-model", messages, tools=None, stream=True, extra=extra) == (
        llm_wire.build_body("up-model", messages, tools=None, stream=True, extra=extra)
    )
