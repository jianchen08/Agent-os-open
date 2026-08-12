"""复盘报告持久化 TDD 测试（Step 5b）。

验证内容（与任务规格 4 个用例对齐）：
1. store_report 在 _memory_backend 注入时调用 backend.add，memory_type="review"
2. store_report 仍更新内存 _reports dict（保留给 get_report 立即轮询）
3. _memory_backend=None 时只走内存路径，不崩溃
4. store_report 后 get_report 返回 status=completed 的完整报告

唯一外部依赖是注入的 IMemoryBackend（用 AsyncMock 替身），不接入真实 hindsight/内核。

[来源: docs/tasks Step 5b 复盘报告落 Hindsight]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 hindsight_memory/test_server.py 同款 setup）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 review/server.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，隔离 _reports/_memory_backend 全局状态。
    """
    mod_name = "review_server_step5b_test"
    plugin_path = _PLUGIN_DIR / "server.py"
    assert plugin_path.exists(), f"server.py missing at {plugin_path}"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """加载 review server 模块，每个测试独立（重置 _reports 与 _memory_backend）。"""
    module = _load_module()
    # 清空模块级状态，避免跨测试污染
    module._reports.clear()
    module._run_ids.clear()
    module._memory_backend = None
    return module


@pytest.fixture
def mock_backend() -> AsyncMock:
    """构造一个 IMemoryBackend 替身（AsyncMock），add 返回一个 memory id。"""
    backend = AsyncMock()
    backend.add.return_value = "mem-review-1"
    return backend


# ═══════════════════════════════════════════════════════════
# 1. store_report 落到 backend
# ═══════════════════════════════════════════════════════════


class TestStoreReportPersists:
    async def test_store_report_persists_to_backend(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 在 _memory_backend 注入时调用 backend.add，
        memory_type="review"，且 tags 含 review_id 与 review_report。"""
        mod.set_memory_backend(mock_backend)

        report = {
            "task_id": "task-1",
            "lessons": ["lesson-a"],
            "recommendations": ["rec-a"],
        }
        await mod.store_report("review-1", report)

        mock_backend.add.assert_awaited_once()
        kwargs = mock_backend.add.call_args.kwargs
        assert kwargs["memory_type"] == "review"
        tags = kwargs.get("tags") or []
        assert any("review-1" in t for t in tags), f"tags 应含 review_id，实际: {tags}"
        assert "review_report" in tags
        # source 标注复盘来源
        assert kwargs.get("source") == "review_agent"


# ═══════════════════════════════════════════════════════════
# 2. store_report 仍更新内存 _reports
# ═══════════════════════════════════════════════════════════


class TestStoreReportInMemory:
    async def test_store_report_keeps_inmemory(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 仍把报告写入内存 _reports dict（供 get_report 立即轮询）。"""
        mod.set_memory_backend(mock_backend)

        await mod.store_report(
            "review-2", {"task_id": "task-2", "lessons": ["l1"]}
        )

        assert "review-2" in mod._reports
        entry = mod._reports["review-2"]
        assert entry["status"] == "completed"
        assert entry.get("lessons") == ["l1"]


# ═══════════════════════════════════════════════════════════
# 3. _memory_backend=None 时降级
# ═══════════════════════════════════════════════════════════


class TestStoreReportWithoutBackend:
    async def test_store_report_without_backend_degrades(self, mod: Any) -> None:
        """_memory_backend=None 时只走内存路径，不调用任何 backend，不崩溃。"""
        # 默认 mod fixture 已置 _memory_backend=None
        await mod.store_report(
            "review-3", {"task_id": "task-3", "lessons": ["l-degrade"]}
        )

        # 内存仍更新
        assert "review-3" in mod._reports
        assert mod._reports["review-3"]["status"] == "completed"


# ═══════════════════════════════════════════════════════════
# 4. store_report 后 get_report 返回完整报告
# ═══════════════════════════════════════════════════════════


class TestGetReportAfterStore:
    async def test_get_report_returns_persisted(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 后 get_report 返回 status=completed 的完整报告。"""
        mod.set_memory_backend(mock_backend)

        report = {
            "task_id": "task-4",
            "summary": "复盘摘要",
            "lessons": ["lesson-x"],
            "recommendations": ["rec-x"],
        }
        await mod.store_report("review-4", report)

        got = await mod.get_report("review-4")
        assert got["status"] == "completed"
        assert got["task_id"] == "task-4"
        assert got["lessons"] == ["lesson-x"]
