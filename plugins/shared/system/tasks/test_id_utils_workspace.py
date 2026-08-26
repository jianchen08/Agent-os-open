# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""id_utils / workspace 模块行为测试。

id_utils：LLM 工具面短 id ↔ 内部全 id 解析。
- short_id：全 id 截前 12 位；短 id 幂等；空串原样。
- resolve_id：精确命中（pipeline_id / task.owned 键）原样；短 id 前缀唯一
  命中返回全 id；多命中返回 AMBIGUOUS；无命中原样返回；空候选原样。

workspace：模块导入即注册 logger（无业务逻辑，仅验证可导入）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_EVICT_NAMES = (
    "task_types",
    "state_machine",
    "storage",
    "service",
    "timer_manager",
    "agents_types",
    "enum_utils",
    "workspace",
    "service_access",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "server",
    "http_api",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_tasks_plugin.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


class TestShortId:
    def test_full_id_truncated_to_12(self) -> None:
        from id_utils import SHORT_ID_LEN, short_id

        full = "abcdef1234567890"
        assert short_id(full) == full[:SHORT_ID_LEN]
        assert len(short_id(full)) == 12

    def test_short_id_idempotent(self) -> None:
        from id_utils import short_id

        short = "abcdef123456"
        assert short_id(short) == short

    def test_empty_id_passthrough(self) -> None:
        from id_utils import short_id

        assert short_id("") == ""

    def test_short_id_is_prefix_of_full(self) -> None:
        """性质断言：短 id 恒为全 id 前缀（可回查）。"""
        from id_utils import short_id

        full = "0123456789abcdef"
        assert full.startswith(short_id(full))


class TestResolveId:
    def _rows(self) -> list[dict[str, Any]]:
        return [
            {"pipeline_id": "pipe-aaaa-1111", "task.goal": "A"},
            {"pipeline_id": "pipe-bbbb-2222", "task.owned.child-1.title": "C1"},
        ]

    async def test_exact_pipeline_id_passthrough(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "pipe-aaaa-1111") == "pipe-aaaa-1111"

    async def test_exact_owned_key_passthrough(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "child-1") == "child-1"

    async def test_prefix_unique_pipeline_hit(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "pipe-aaaa") == "pipe-aaaa-1111"

    async def test_prefix_unique_owned_hit(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "child-") == "child-1"

    async def test_ambiguous_prefix_returns_marker(self) -> None:
        from id_utils import resolve_id

        rows = [
            {"pipeline_id": "shared-aaa"},
            {"pipeline_id": "shared-bbb"},
        ]
        assert await resolve_id(rows, "shared") == "AMBIGUOUS:shared"

    async def test_no_match_returns_candidate(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "nope-xyz") == "nope-xyz"

    async def test_empty_candidate_passthrough(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(self._rows(), "") == ""

    async def test_none_rows_passthrough(self) -> None:
        from id_utils import resolve_id

        assert await resolve_id(None, "abc") == "abc"

    async def test_long_candidate_no_prefix_scan(self) -> None:
        """超过短 id 长度的候选不做前缀匹配，原样返回。"""
        from id_utils import resolve_id

        rows = [{"pipeline_id": "pipe-aaaa-1111"}]
        assert await resolve_id(rows, "pipe-aaaa-1111-extra") == "pipe-aaaa-1111-extra"


class TestWorkspaceModule:
    def test_workspace_importable(self) -> None:
        """workspace.py 可导入（模块级 logger 注册即覆盖）。"""
        import workspace

        assert workspace.logger.name == "workspace"
