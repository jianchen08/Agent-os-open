"""id_utils 短 id 工具测试（2026-08-22 用户要求：LLM 工具面 id 短化）。

覆盖：
1. short_id：全 id → 12 位短 id；短 id 原样。
2. resolve_id：精确命中（pipeline_id / task.owned.<id>）、短前缀唯一解析、
   多命中歧义、无命中原样、非 str/空原样。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TASKS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugins" / "shared" / "system" / "tasks"

if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))


@pytest.fixture(scope="module")
def id_utils():
    import id_utils  # noqa: PLC0415

    return id_utils


def test_short_id_truncates_to_12(id_utils):
    full = "a1b2c3d4e5f64789abcdef0123456789"
    assert id_utils.short_id(full) == full[:12]
    assert len(id_utils.short_id(full)) == 12


def test_short_id_passthrough_short_and_empty(id_utils):
    assert id_utils.short_id("abc123") == "abc123"
    assert id_utils.short_id("") == ""


@pytest.mark.asyncio
async def test_resolve_exact_pipeline_id_passthrough(id_utils):
    rows = [{"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789"}]
    r = await id_utils.resolve_id(rows, "a1b2c3d4e5f64789abcdef0123456789")
    assert r == "a1b2c3d4e5f64789abcdef0123456789"


@pytest.mark.asyncio
async def test_resolve_short_prefix_unique(id_utils):
    rows = [{"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789"}]
    r = await id_utils.resolve_id(rows, "a1b2c3d4e5f6")
    assert r == "a1b2c3d4e5f64789abcdef0123456789"


@pytest.mark.asyncio
async def test_resolve_owned_container_short_id(id_utils):
    rows = [
        {
            "pipeline_id": "owner-pipe-1",
            "task.owned.c1d2e3f4a5b6.title": "容器项目",
        }
    ]
    r = await id_utils.resolve_id(rows, "c1d2e3f4a5b6")
    assert r == "c1d2e3f4a5b6"  # 容器 project id 生成即短，精确命中


@pytest.mark.asyncio
async def test_resolve_ambiguous_prefix(id_utils):
    rows = [
        {"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789"},
        {"pipeline_id": "a1b2c3d4e5f6aaaaabcdef0123456789"},
    ]
    r = await id_utils.resolve_id(rows, "a1b2c3d4e5f6")
    assert r.startswith("AMBIGUOUS:")


@pytest.mark.asyncio
async def test_resolve_no_hit_passthrough(id_utils):
    rows = [{"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789"}]
    r = await id_utils.resolve_id(rows, "zzzzzzzzzzzz")
    assert r == "zzzzzzzzzzzz"


@pytest.mark.asyncio
async def test_resolve_non_str_and_empty(id_utils):
    assert await id_utils.resolve_id([], "") == ""
    assert await id_utils.resolve_id([], None) is None
