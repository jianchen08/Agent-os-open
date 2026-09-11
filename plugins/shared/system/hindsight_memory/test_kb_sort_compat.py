# @feature: knowledge-base 域路由 | @ci: python-coverage
"""knowledge_base list_items 跨格式时间戳排序兼容回归。

时间戳格式统一（time_iso 收敛）后，元数据仓内并存两种 created_at：

- 存量：``2026-09-11T08:00:00Z``（Z 后缀秒级，格式统一前写入）；
- 新写：``2026-09-11T08:00:00.123456+00:00``（+00:00 微秒）。

字符串直排在同秒边界乱序（'Z' > '.'），list_items 必须按真实时刻倒序
（前端页面直接消费数组顺序）。不可解析值（历史夹具形态）不炸、相对序稳定。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    mod_name = "hindsight_knowledge_base_sort_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "knowledge_base.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kb(tmp_path: Path) -> Any:
    module = _load_module()
    module.set_data_dir(str(tmp_path / "kb"))
    return module


def _seed(kb: Any, items: list[dict[str, Any]]) -> None:
    meta = kb._load_meta()
    meta["items"] = items
    kb._save_meta(meta)


def _item(item_id: str, created_at: str) -> dict[str, Any]:
    return {
        "id": item_id, "name": f"{item_id}.md", "size": 1, "mime_type": "text/markdown",
        "categories": [], "tags": [], "chunk_count": 0, "chunk_ids": [],
        "source_file": "", "created_at": created_at, "updated_at": created_at,
    }


def test_list_items_mixed_formats_chronological_desc(kb: Any) -> None:
    """Z 存量与 +00:00 新格式混存 → 按真实时刻倒序（同秒边界不因字符序乱序）。"""
    _seed(kb, [
        _item("old-z-second", "2026-09-11T08:00:00Z"),             # 真实 .000000
        _item("new-late-micros", "2026-09-11T08:00:00.999999+00:00"),
        _item("old-z-earlier-day", "2026-09-10T08:00:00Z"),
        _item("new-earliest", "2026-09-09T08:00:00.000001+00:00"),
    ])
    order = [it["id"] for it in kb.list_items()]
    assert order == ["new-late-micros", "old-z-second", "old-z-earlier-day", "new-earliest"]


def test_list_items_new_format_written_by_now_iso_utc(kb: Any) -> None:
    """新写入条目（now_iso_utc 产物 +00:00 微秒）排在最前，且回读可解析。"""
    now_value = kb._now_iso()  # 收敛后即 time_iso.now_iso_utc
    assert now_value.endswith("+00:00")
    _seed(kb, [_item("legacy", "2020-01-01T00:00:00Z"), _item("fresh", now_value)])
    assert [it["id"] for it in kb.list_items()] == ["fresh", "legacy"]


def test_list_items_unparsable_values_stable(kb: Any) -> None:
    """不可解析 created_at（历史夹具形态）不炸、按字符串桶稳定排序。"""
    _seed(kb, [_item("b", "t1"), _item("a", "t0")])
    assert [it["id"] for it in kb.list_items()] == ["b", "a"]  # 倒序：t1 > t0
