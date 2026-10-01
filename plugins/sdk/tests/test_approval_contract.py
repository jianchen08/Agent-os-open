# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""审批决策契约测试（ADR 2026-10-01）：semantics 封闭枚举 + 交互错误码。

断行为：枚举成员稳定、校验函数对未知词形拒绝（fail-closed 方向）、
错误码三值齐备。枚举是 ADR 级契约——成员集合变化必须伴随 ADR 修订，
本测试即「枚举不许静默长成员」的看门断言。
"""

from __future__ import annotations

import pytest

from agentos_plugin_sdk.approval_contract import (
    InteractionErrorCode,
    OptionSemantics,
    is_valid_semantics,
    semantics_values,
)

pytestmark = pytest.mark.unit

# ADR 2026-10-01 决策 2 拍板的封闭集合（+grant 永久/会话两轴）。
# 新增成员 = 修订 ADR + 同步改本断言（防枚举变第二张别名表）。
_ADR_SEMANTICS = frozenset(
    {
        "approve_once",
        "approve_and_remember",
        "deny",
        "grant_write",
        "grant_write_permanent",
        "grant_read",
        "grant_read_permanent",
        "cancel",
    }
)


def test_semantics_enum_is_exactly_the_adr_closed_set() -> None:
    """枚举成员与 ADR 拍板集合精确相等：多一个成员（未修 ADR）即红。"""
    assert semantics_values() == _ADR_SEMANTICS
    assert {s.value for s in OptionSemantics} == _ADR_SEMANTICS


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("approve_once", True),
        ("deny", True),
        ("grant_write_permanent", True),
        ("cancel", True),
        ("approved", False),  # 历史别名不是语义——归一前一律非法
        ("approve", False),
        ("仅本次执行", False),  # label 不是语义
        ("", False),
        ("APPROVE_ONCE", False),  # 大小写敏感（词形精确）
        (None, False),
        (123, False),
    ],
)
def test_is_valid_semantics_rejects_unknown_word_forms(raw: object, expect: bool) -> None:
    """声明校验：枚举成员放行；别名/label/空值/非字符串一律拒（fail-closed）。"""
    assert is_valid_semantics(raw) is expect


def test_interaction_error_codes_complete() -> None:
    """错误码三值齐备：超时/拒绝/取消（消费方按码分类，不再嗅探消息子串）。"""
    assert {c.value for c in InteractionErrorCode} == {
        "INTERACTION_TIMEOUT",
        "INTERACTION_DENIED",
        "INTERACTION_CANCELLED",
    }
