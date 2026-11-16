# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""human 插件数据模型枚举契约测试。

枚举值即跨进程契约（前端/approval 插件按字符串值路由），断言：
- 每个枚举的取值集合与字符串值一一对应（值即序列化契约，改动即破坏兼容）；
- 枚举可经字符串值反解（Priority("normal") 等构造路径被 server.py 使用）；
- 非法字符串构造抛 ValueError（fail-closed，防静默降级）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_models() -> Any:
    """importlib 显式路径 + 唯一模块名加载 models.py（防裸名劫持）。"""
    name = "human_models_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "models.py")
    assert spec is not None and spec.loader is not None, "cannot load human/models.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def models() -> Any:
    return _load_models()


@pytest.mark.parametrize(
    ("enum_name", "expected"),
    [
        ("InteractionMode", {"choice", "conversation", "notification"}),
        (
            "InteractionStatus",
            {"pending", "viewed", "completed", "timeout", "cancelled", "auto_approved"},
        ),
        ("ResponseType", {"approved", "denied", "answered", "timeout", "cancelled"}),
        ("Priority", {"low", "normal", "high", "critical"}),
        ("TimeoutAction", {"reject", "auto_approve", "ignore"}),
    ],
)
def test_enum_value_sets(models: Any, enum_name: str, expected: set[str]) -> None:
    """枚举取值集合与字符串值一一对应（值即跨进程契约）。"""
    enum_cls = getattr(models, enum_name)
    assert {m.value for m in enum_cls} == expected
    # 性质断言：值集合与成员一一对应（无重复值、无幽灵成员）
    assert len(enum_cls) == len(expected)


@pytest.mark.parametrize(
    ("enum_name", "value"),
    [
        ("InteractionMode", "choice"),
        ("InteractionStatus", "pending"),
        ("ResponseType", "approved"),
        ("Priority", "normal"),
        ("TimeoutAction", "reject"),
    ],
)
def test_enum_constructible_from_value(models: Any, enum_name: str, value: str) -> None:
    """字符串值可反解为枚举成员（server.py 的 Priority(kwargs[...]) 构造路径）。"""
    enum_cls = getattr(models, enum_name)
    assert enum_cls(value).value == value


@pytest.mark.parametrize(
    ("enum_name", "bad_value"),
    [
        ("InteractionMode", "blocking"),
        ("InteractionStatus", "expired"),
        ("ResponseType", "maybe"),
        ("Priority", "urgent"),
        ("TimeoutAction", "ask"),
    ],
)
def test_enum_rejects_unknown_value(models: Any, enum_name: str, bad_value: str) -> None:
    """非法字符串构造抛 ValueError（fail-closed，不静默降级）。"""
    enum_cls = getattr(models, enum_name)
    with pytest.raises(ValueError):
        enum_cls(bad_value)


def test_enum_members_are_str_subclass(models: Any) -> None:
    """枚举成员是 str 子类（JSON 序列化/前端路由按字符串处理的前提）。"""
    for enum_name in ("InteractionMode", "InteractionStatus", "ResponseType", "Priority", "TimeoutAction"):
        enum_cls = getattr(models, enum_name)
        for member in enum_cls:
            assert isinstance(member.value, str)
            assert isinstance(member, str)
