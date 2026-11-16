# @feature: FP-GATE 共享根显式出口 | @ci: python-coverage
"""_root_api 出口契约单测（审查总册 E2 API 化刀，2026-10-16 拍板）。

行为契约（断输入→输出，不钉实现）：
- 名单一致性：字面 ``__all__`` 与声明映射（_EXPORTS/_MODULE_EXPORTS）同集——
  两处任一漂移（删值）即红；
- 全名单可解析：85 名逐一 hasattr 成功（出口声明与实现不失配）；
- 身份等价：转发属性/模块绑定与裸名 import 所得同对象；
- seam 语义：测试对源模块的 monkeypatch 与 sys.modules 整模块替换，
  经出口 import 在 call-time 均可见（与裸名 import 等价，API 化刀回归锚）；
- 名单外名字 AttributeError（非静默 None）。
"""

from __future__ import annotations

import sys
import types

import pytest

pytestmark = pytest.mark.unit


def test_all_matches_declaration_maps() -> None:
    """字面 __all__ 与声明映射同集（防两处漂移）。"""
    from _root_api import _EXPORTS, _MODULE_EXPORTS

    declared = set(_EXPORTS) | set(_MODULE_EXPORTS)
    import _root_api as outlet

    assert set(outlet.__all__) == declared
    assert len(outlet.__all__) == len(declared), "__all__ 有重复名"


def test_all_names_resolve() -> None:
    """全名单逐一可解析（出口声明与实现不失配）。"""
    import _root_api as outlet

    missing = [n for n in outlet.__all__ if not hasattr(outlet, n)]
    assert missing == []


def test_forwarded_identity_equivalent_to_bare_import() -> None:
    """转发属性/模块绑定与裸名 import 所得同对象。"""
    import _root_api as outlet
    import http_json
    import state_fields

    assert outlet.json_response is http_json.json_response
    assert outlet.state_fields is state_fields


def test_monkeypatch_source_module_visible_via_outlet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """seam：monkeypatch 源模块函数，出口 call-time 转发可见。"""
    import time_iso

    def _fake_now() -> str:
        return "2000-01-01T00:00:00Z"

    monkeypatch.setattr(time_iso, "now_iso_utc", _fake_now)
    from _root_api import now_iso_utc

    assert now_iso_utc() == "2000-01-01T00:00:00Z"


def test_sys_modules_replacement_visible_via_outlet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """seam：sys.modules 整模块替换（fake 模块），出口 call-time 转发可见。"""
    fake = types.ModuleType("mode_keys")
    fake.MODE_ID_RE = "fake-pattern"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mode_keys", fake)

    from _root_api import MODE_ID_RE

    assert MODE_ID_RE == "fake-pattern"


def test_unknown_name_raises_attribute_error() -> None:
    """名单外名字 AttributeError（非静默）。"""
    import _root_api as outlet

    with pytest.raises(AttributeError):
        outlet.definitely_not_exported  # noqa: B018 —— 触发 __getattr__ 即目的
