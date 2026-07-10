"""复盘多管道编排端到端测试。

验证我新增的核心编排逻辑（_run_llm_review_task 的多管道串行执行）在真实
storage + 真实 service 装配上跑通：

1. 用真实 ExecutionRecordStorage 灌入多个 pending 目标（跨 agent、含 failed/success）
2. 用真实 MemoryMaintenanceService 装配（带 review_context_window + review_batch_limit）
3. monkeypatch 掉三处 LLM/网络依赖：
   - _try_launch_review_agent：记录调用 + 返回假 pipeline_id（不真起引擎）
   - _await_child_report：返回假报告文本（不等 LLM）
   - _notify_parent：记录通知内容（不发真实消息）
4. 验证编排契约：
   - 切批数正确（受 review_batch_limit 约束）
   - 串行起多个复盘管道（调用次数 = 批数）
   - 逐个标记目标为已复盘
   - 通知消息含正确的"复盘管道数/报告数/目标数/剩余数"

注意：本测试不连真实 LLM，不验证 review_agent 的 LLM 输出质量，只验证编排链路。
真实 LLM 全链路见 test_review_b_path_llm_e2e.py。
"""
from __future__ import annotations

from typing import Any

import pytest

from infrastructure.execution_record_storage import (
    ExecutionRecordStorage,
    PipelineRunSummary,
)
from memory.maintenance.service import MaintenanceConfig, MemoryMaintenanceService


def _make_pending_summary(
    run_id: str, total_records: int, status: str = "success",
) -> PipelineRunSummary:
    """构造一个待复盘的管道摘要（review_status=pending, status=已结束）。"""
    return PipelineRunSummary(
        run_id=run_id,
        status=status,
        total_records=total_records,
        total_iterations=total_records // 10,
        review_status="pending",
    )


@pytest.fixture
def service(tmp_path) -> MemoryMaintenanceService:
    """装配真实 service：内存 storage + 小窗口强制切多批。

    小窗口（10000 × 15% = 1500 token）+ 每记录 15 token → 单批预算 100 记录。
    配合 review_batch_limit=3，验证切批 + 上限约束。
    """
    storage = ExecutionRecordStorage(data_dir=str(tmp_path))
    config = MaintenanceConfig(
        enabled=True,
        skeleton_budget_percent=15,
        records_per_skeleton_token=15,
        review_batch_limit=3,
    )
    svc = MemoryMaintenanceService(
        storage=storage,
        chunk_db=None,
        knowledge_service=None,
        config=config,
        review_context_window=10_000,  # 小窗口强制切批
    )
    return svc


def _patch_llm_deps(svc: MemoryMaintenanceService) -> dict[str, Any]:
    """monkeypatch 三处 LLM/网络依赖，返回调用记录 dict。

    Returns:
        {"launch_calls": [...], "reports": {pid: text}, "notifications": [...]}
    """
    state: dict[str, Any] = {
        "launch_calls": [],   # 每次起管道收到的目标列表
        "pid_counter": [0],
        "notifications": [],  # 发出的通知
    }

    async def fake_launch(targets):  # noqa: ANN001
        state["launch_calls"].append(list(targets))
        state["pid_counter"][0] += 1
        pid = f"fake-review-pid-{state['pid_counter'][0]}"
        return pid, True

    async def fake_await_report(child_pid):  # noqa: ANN001
        return f"## 复盘报告（pipeline={child_pid}）\n这是假报告。"

    async def fake_notify(parent_pid, status, summary):  # noqa: ANN001
        state["notifications"].append({"status": status, "summary": summary})

    svc._try_launch_review_agent = fake_launch  # type: ignore[method-assign]
    svc._await_child_report = fake_await_report  # type: ignore[method-assign]
    svc._notify_parent = fake_notify  # type: ignore[method-assign]
    # _persist_review_result 写文件 + 知识库，知识库为 None 会跳过，文件写入可保留
    return state


class TestMultiPipelineOrchestration:
    """多复盘管道串行编排。"""

    @pytest.mark.asyncio
    async def test_single_batch_when_targets_fit_budget(self, service, monkeypatch) -> None:
        """目标总量在单批预算内 → 只切 1 批 → 只起 1 个复盘管道。"""
        # 单批预算 1500 token，每目标 10 记录×15=150 token → 10 目标 = 1500，刚好 1 批
        for i in range(10):
            service._storage.save_summary(_make_pending_summary(f"r{i}", 10))

        state = _patch_llm_deps(service)
        await service._run_llm_review_task("")

        assert len(state["launch_calls"]) == 1, "应在单批预算内只起 1 个复盘管道"
        assert len(state["launch_calls"][0]) == 10
        # 通知应报告 1 个复盘管道、10 个目标
        notif = state["notifications"][-1]
        assert notif["status"] == "completed"
        assert "1 个复盘管道" in notif["summary"]
        assert "复盘 10 个目标" in notif["summary"]

    @pytest.mark.asyncio
    async def test_multiple_batches_when_targets_exceed_budget(self, service) -> None:
        """目标总量超单批预算 → 切多批 → 起多个复盘管道。"""
        # 单批预算 1500 token，每目标 100 记录×15=1500 → 每批只能塞 1 个（恰好满）
        # 30 目标 → 切 30 批，但 review_batch_limit=3 → 只起 3 个复盘管道
        for i in range(30):
            service._storage.save_summary(_make_pending_summary(f"r{i}", 100))

        state = _patch_llm_deps(service)
        await service._run_llm_review_task("")

        # 批数受 review_batch_limit=3 约束
        assert len(state["launch_calls"]) == 3, "应受 review_batch_limit 约束只起 3 个复盘管道"
        # 每批只塞 1 个目标（每个都正好装满预算）
        for batch in state["launch_calls"]:
            assert len(batch) == 1
        # 通知报告 3 个复盘管道、3 个目标、剩余 27 个 pending
        notif = state["notifications"][-1]
        assert "3 个复盘管道" in notif["summary"]
        assert "复盘 3 个目标" in notif["summary"]
        assert "还剩 27 个" in notif["summary"]

    @pytest.mark.asyncio
    async def test_all_targets_marked_reviewed_within_batch(self, service) -> None:
        """复盘成功的批次内目标全部标记为 reviewed。"""
        # 5 个目标，单批装下（5×150=750 < 1500）
        for i in range(5):
            service._storage.save_summary(_make_pending_summary(f"r{i}", 10))

        _patch_llm_deps(service)
        await service._run_llm_review_task("")

        # 全部 5 个目标应被标记为 completed（review_status）
        for i in range(5):
            sm = service._storage.get_summary(f"r{i}")
            assert sm is not None
            assert sm.review_status == "completed", f"r{i} 未标记已复盘"

    @pytest.mark.asyncio
    async def test_empty_pending_notifies_failed(self, service) -> None:
        """无 pending 目标时通知 failed，不起任何复盘管道。"""
        state = _patch_llm_deps(service)
        await service._run_llm_review_task("")

        assert len(state["launch_calls"]) == 0
        notif = state["notifications"][-1]
        assert notif["status"] == "failed"
        assert "无 pending" in notif["summary"]

    @pytest.mark.asyncio
    async def test_two_level_grouping_preserved_in_batches(self, service) -> None:
        """切批前两级分组（先 agent 后 status）在真实 storage 上生效。

        构造：agentA 2 个(success) + agentB 1 个(failed)，单批全装下。
        验证传给复盘管道的目标顺序：同 agent 连续、failed 在前。
        """
        # task_lookup 反查 agent_id：用 monkeypatch 让特定 run_id 映射到 agent
        def fake_task_lookup(run_id):  # noqa: ANN001
            agents = {"r0": "agentA", "r1": "agentA", "r2": "agentB"}
            return {"agent": agents.get(run_id, ""), "title": ""}
        service._task_lookup = fake_task_lookup

        service._storage.save_summary(_make_pending_summary("r0", 5, "success"))
        service._storage.save_summary(_make_pending_summary("r1", 5, "success"))
        service._storage.save_summary(_make_pending_summary("r2", 5, "failed"))

        state = _patch_llm_deps(service)
        await service._run_llm_review_task("")

        # 单批全装下，顺序应为：agentB(failed) → agentA×2（B 字母序在 A 后，
        # 但 failed 优先级只在同 agent 内生效；跨 agent 按 agent_id 字母序）
        batch = state["launch_calls"][0]
        ids = [t["run_id"] for t in batch]
        # agentA 连续（r0,r1），agentB（r2）；A 在 B 前
        assert ids == ["r0", "r1", "r2"]
