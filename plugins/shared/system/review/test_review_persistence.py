# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-plugins-test
"""复盘报告持久化 TDD 测试（Step 5b + F-REVIEW-2）。

验证内容（与任务规格 4 个用例对齐）：
1. store_report 在 _memory_backend 注入时调用 backend.add，memory_type="review"
2. store_report 仍更新内存 _reports dict（保留给 get_report 立即轮询）
3. _memory_backend=None 时只走内存路径，不崩溃
4. store_report 后 get_report 返回 status=completed 的完整报告

F-REVIEW-2 扩展（review 真实完成事件，轮询语义）：
- get_report 经 pipeline-executor.get_run_status 能力查子管道 run 状态，
  run 真实完成才落 completed（不再"启动即 completed（乐观，空 lessons）"）
- run 失败落 failed；进行中/挂起/查询失败保持 running（不崩）

唯一外部依赖是注入的 IMemoryBackend（用 AsyncMock 替身）与 fake pipeline
能力（CapabilityHandle 注入），不接入真实 hindsight/内核。

[来源: docs/tasks Step 5b 复盘报告落 Hindsight]
[来源: F-REVIEW-2 review 真实完成事件]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentos_plugin_sdk.capability import CapabilityHandle

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


def _inject_pipeline_capability(
    mod: Any, status: str = "running", run_id: str = "run-abc"
) -> None:
    """注入 fake pipeline-executor 能力（F-REVIEW-2 轮询链路）。

    call_fn 支持 start_run（返回 run_id）与 get_run_status（返回配置的 status）。
    """

    async def fake_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "start_run":
            return {"status": "started", "run_id": run_id}
        if method == "get_run_status":
            return {"run_id": run_id, "status": status}
        raise AssertionError(f"unexpected capability method: {method}")

    mod.plugin._capabilities["pipeline-executor"] = CapabilityHandle(
        "pipeline-executor", call_fn=fake_call
    )


def _seed_running_report(mod: Any, review_id: str, run_id: str = "run-abc") -> None:
    """按 trigger_review 成功路径登记 running 报告 + run_id。"""
    mod._run_ids[review_id] = run_id
    mod._reports[review_id] = {
        "review_id": review_id,
        "task_id": "task-x",
        "summary": "s",
        "artifacts": [],
        "metrics": {},
        "status": "running",
        "run_id": run_id,
        "created_at": 1.0,
    }


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


# ═══════════════════════════════════════════════════════════
# F-REVIEW-2: review 真实完成事件（轮询子管道 run 状态）
# 语义：completed 只由子管道真实完成触发，不再"启动即 completed（乐观，空 lessons）"
# ═══════════════════════════════════════════════════════════


class TestGetReportRunStatusPolling:
    """get_report 经 pipeline-executor.get_run_status 轮询子管道真实状态。"""

    async def test_run_completed_finalizes_report(self, mod: Any) -> None:
        """run 状态 completed → get_report 把 report 落为 completed。"""
        _inject_pipeline_capability(mod, status="completed")
        _seed_running_report(mod, "review-5")

        got = await mod.get_report("review-5")
        assert got["status"] == "completed"
        assert got["run_status"] == "completed"
        assert "completed_at" in got
        assert got["run_id"] == "run-abc"

    async def test_run_still_running_keeps_running(self, mod: Any) -> None:
        """run 仍 running → report 保持 running，不提前 completed。"""
        _inject_pipeline_capability(mod, status="running")
        _seed_running_report(mod, "review-6")

        got = await mod.get_report("review-6")
        assert got["status"] == "running"
        assert got.get("run_status") == "running"

    async def test_run_suspended_keeps_running(self, mod: Any) -> None:
        """run 挂起 → report 保持 running（记录 run_status 供调用方）。"""
        _inject_pipeline_capability(mod, status="suspended")
        _seed_running_report(mod, "review-6b")

        got = await mod.get_report("review-6b")
        assert got["status"] == "running"
        assert got.get("run_status") == "suspended"

    async def test_run_failed_marks_report_failed(self, mod: Any) -> None:
        """run 失败 → report 落 failed（不再无限 running）。"""
        _inject_pipeline_capability(mod, status="failed")
        _seed_running_report(mod, "review-7")

        got = await mod.get_report("review-7")
        assert got["status"] == "failed"
        assert got["run_status"] == "failed"
        assert "failed_at" in got

    async def test_no_capability_keeps_running_degrades(self, mod: Any) -> None:
        """能力未注入（独立进程/降级）→ 查询失败，保持 running，不崩。"""
        _seed_running_report(mod, "review-8")

        got = await mod.get_report("review-8")
        assert got["status"] == "running"
        assert got.get("run_status") is None

    async def test_run_status_call_failure_keeps_running(self, mod: Any) -> None:
        """内核 get_run_status 报错 → 降级保持 running，不崩。"""

        async def failing_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("kernel unreachable")

        mod.plugin._capabilities["pipeline-executor"] = CapabilityHandle(
            "pipeline-executor", call_fn=failing_call
        )
        _seed_running_report(mod, "review-8b")

        got = await mod.get_report("review-8b")
        assert got["status"] == "running"


class TestTriggerReviewRealCompletion:
    """完整链路：trigger_review 起子管道 → get_report 轮询到真实完成。"""

    async def test_trigger_then_poll_to_completed(self, mod: Any) -> None:
        """start_run 返回 run_id → status running；run 完成后 get_report 落 completed。"""
        _inject_pipeline_capability(mod, status="completed")

        triggered = await mod.trigger_review(
            task_id="task-9", summary="已完成任务复盘"
        )
        assert triggered["status"] == "running"
        assert triggered["run_id"] == "run-abc"
        assert triggered["review_id"] in mod._run_ids

        got = await mod.get_report(triggered["review_id"])
        assert got["status"] == "completed"
        assert got["run_status"] == "completed"
        assert got["task_id"] == "task-9"

    async def test_trigger_keeps_running_while_pipeline_inflight(self, mod: Any) -> None:
        """子管道仍进行中 → get_report 保持 running（不提前 completed）。"""
        _inject_pipeline_capability(mod, status="running")

        triggered = await mod.trigger_review(task_id="task-10", summary="复盘中")
        got = await mod.get_report(triggered["review_id"])
        assert got["status"] == "running"
        assert got.get("run_status") == "running"

    async def test_trigger_without_capability_degrades_locally(self, mod: Any) -> None:
        """能力未注入 → 降级本地报告（status=completed, mode=local_degrade）。"""
        triggered = await mod.trigger_review(
            task_id="task-11",
            summary="无能力环境",
            metrics={"accuracy": 0.3},
        )
        assert triggered["status"] == "completed"
        assert triggered["mode"] == "local_degrade"
        got = await mod.get_report(triggered["review_id"])
        assert got["mode"] == "local_degrade"
        assert got["status"] == "completed"
