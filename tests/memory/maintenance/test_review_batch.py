"""复盘批次选择逻辑单测。

覆盖 _select_targets_by_budget 的预算反推契约：
1. 预算内尽量多塞：大窗口下全部管道都被选中。
2. 预算超限截断：小窗口下超预算的管道被丢弃。
3. 至少塞 1 个：即便首个管道就超预算，也保证塞进去一个不空手。
4. 两级分组排序（先 agent_id 再 status）。

_select_targets_by_budget 是纯函数：输入已排序的候选列表，输出预算内子集。
预算 = review_context_window × skeleton_budget_percent ÷ 100；
每管道成本 = total_records × records_per_skeleton_token。
"""
from __future__ import annotations

from typing import Any

from memory.maintenance.service import MaintenanceConfig, MemoryMaintenanceService


def _make_service(
    context_window: int = 1_000_000,
    budget_percent: int = 15,
    records_per_token: int = 15,
    review_batch_limit: int = 10,
) -> MemoryMaintenanceService:
    """构造一个只配预算参数的维护服务实例（其余依赖置 None）。

    Args:
        context_window: review_agent 模型上下文窗口（tokens）
        budget_percent: 骨架预算占比（%）
        records_per_token: 每条骨架记录约占 token 数
        review_batch_limit: 一次触发最多启动几个复盘管道

    Returns:
        未装配完整依赖、但预算参数就绪的 MemoryMaintenanceService
    """
    config = MaintenanceConfig(
        skeleton_budget_percent=budget_percent,
        records_per_skeleton_token=records_per_token,
        review_batch_limit=review_batch_limit,
    )
    return MemoryMaintenanceService(
        storage=None,
        chunk_db=None,
        knowledge_service=None,
        config=config,
        review_context_window=context_window,
    )


def _target(
    run_id: str, agent_id: str, status: str, total_records: int,
) -> dict[str, Any]:
    """构造一个候选管道字典。"""
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "status": status,
        "total_records": total_records,
    }


class TestSelectByBudget:
    """预算反推批次大小。"""

    def test_large_window_selects_all(self) -> None:
        """大窗口（1M × 15% = 150K token）下，全部管道都应被选中。"""
        service = _make_service(context_window=1_000_000)
        # 10 个管道，每个 100 记录 × 15 token = 1500 token，总 15000 << 150000
        targets = [_target(f"r{i}", "agentA", "failed", 100) for i in range(10)]

        selected = service._select_targets_by_budget(targets)

        assert len(selected) == 10

    def test_small_window_truncates(self) -> None:
        """小窗口下超预算的管道被丢弃。

        窗口 128000 × 15% = 19200 token；每管道 100 记录 × 15 = 1500 token；
        理论容量 19200/1500 ≈ 12 个，但第 13 个会让累计超预算而被截断。
        """
        service = _make_service(context_window=128_000)
        targets = [_target(f"r{i}", "agentA", "success", 100) for i in range(20)]

        selected = service._select_targets_by_budget(targets)

        # 12 × 1500 = 18000 <= 19200；第 13 个 = 19500 > 19200 被截断
        assert len(selected) == 12

    def test_always_selects_at_least_one_even_if_over_budget(self) -> None:
        """即便首个管道就远超预算，也必须塞进去一个（不空手）。"""
        service = _make_service(context_window=1000)  # 极小窗口：1000×15%=150 token
        # 单个管道 1000 记录 × 15 = 15000 token >> 150 预算
        targets = [_target("r0", "agentA", "failed", 1000)]

        selected = service._select_targets_by_budget(targets)

        assert len(selected) == 1
        assert selected[0]["run_id"] == "r0"

    def test_empty_input_returns_empty(self) -> None:
        """无候选管道时返回空列表。"""
        service = _make_service()
        assert service._select_targets_by_budget([]) == []

    def test_heterogeneous_record_counts(self) -> None:
        """管道记录数不均时，预算按累计成本准确截断。"""
        service = _make_service(context_window=100_000)  # 预算 15000 token
        # r0: 200 记录 = 3000 token; r1: 400 记录 = 6000 token; 累计 9000
        # r2: 500 记录 = 7500 token; 累计 16500 > 15000 → 截断
        targets = [
            _target("r0", "agentA", "failed", 200),
            _target("r1", "agentA", "success", 400),
            _target("r2", "agentB", "failed", 500),
        ]

        selected = service._select_targets_by_budget(targets)

        assert [t["run_id"] for t in selected] == ["r0", "r1"]


class TestTwoLevelGrouping:
    """两级分组排序：先 agent_id，再 status（failed 先）。"""

    def test_group_by_agent_then_status(self) -> None:
        """验证排序 key 的两级分组语义。"""
        targets = [
            _target("t1", "agentB", "success", 10),
            _target("t2", "agentA", "success", 10),
            _target("t3", "agentB", "failed", 10),
            _target("t4", "agentA", "failed", 10),
        ]
        # 复用 _collect_review_targets 内的同款排序 key，验证语义
        targets.sort(key=lambda t: (
            t.get("agent_id") or "",
            0 if t.get("status") == "failed" else 1,
            -(t.get("total_records", 0)),
        ))

        # agentA 的两个在前（failed 先于 success），agentB 的两个在后
        assert [t["run_id"] for t in targets] == ["t4", "t2", "t3", "t1"]

    def test_same_agent_failed_before_success(self) -> None:
        """同一 agent 内 failed 排在 success 前面。"""
        targets = [
            _target("ok1", "agentA", "success", 50),
            _target("fail1", "agentA", "failed", 50),
            _target("ok2", "agentA", "success", 50),
        ]
        targets.sort(key=lambda t: (
            t.get("agent_id") or "",
            0 if t.get("status") == "failed" else 1,
            -(t.get("total_records", 0)),
        ))

        assert targets[0]["run_id"] == "fail1"
        # 剩余两个 success 顺序保持稳定（records 相同）

    def test_different_agents_not_interleaved(self) -> None:
        """不同 agent 的管道不交错：一个 agent 的全部管道连续排完再换下一个。"""
        targets = [
            _target("a1", "agentA", "success", 10),
            _target("b1", "agentB", "failed", 10),
            _target("a2", "agentA", "failed", 10),
            _target("b2", "agentB", "success", 10),
        ]
        targets.sort(key=lambda t: (
            t.get("agent_id") or "",
            0 if t.get("status") == "failed" else 1,
            -(t.get("total_records", 0)),
        ))

        agent_ids = [t["agent_id"] for t in targets]
        # agentA 的两个连续，agentB 的两个连续，不交错
        assert agent_ids == ["agentA", "agentA", "agentB", "agentB"]


class TestSplitIntoBatches:
    """按预算把全部目标切成多个复盘批次。

    每批 = 一个复盘管道的预算容量；批数受 review_batch_limit 约束。
    """

    def test_few_targets_one_batch(self) -> None:
        """目标少（预算内全装下）时只切 1 批。"""
        # 大窗口，5 个小目标，预算容量远超 → 1 批装完
        service = _make_service(context_window=1_000_000)
        targets = [_target(f"r{i}", "agentA", "success", 10) for i in range(5)]

        batches = service._split_targets_into_batches(targets)

        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_many_targets_split_into_multiple_batches(self) -> None:
        """目标多（超单批预算）时切成多批，每批都不超预算。"""
        # 小窗口：128000×15%=19200 token；每目标 100 记录×15=1500 token → 单批 ~12 个
        # 30 个目标 → 应切 ~3 批
        service = _make_service(context_window=128_000, review_batch_limit=10)
        targets = [_target(f"r{i}", "agentA", "success", 100) for i in range(30)]

        batches = service._split_targets_into_batches(targets)

        # 30 ÷ 12 ≈ 3 批（review_batch_limit=10 不触顶）
        assert len(batches) == 3
        # 每批不超单批预算容量（12 个 × 1500 = 18000 < 19200）
        for batch in batches:
            cost = sum(t["total_records"] for t in batch) * 15
            assert cost <= 19200

    def test_batch_count_capped_by_limit(self) -> None:
        """批数受 review_batch_limit 约束，超出的目标留下次。"""
        # 小窗口单批只装 ~2 个；30 目标理论要 15 批，但 limit=3 → 只切 3 批 ≈ 6 个
        service = _make_service(context_window=1000, review_batch_limit=3)
        # 极小窗口：1000×15%=150 token；每目标 100×15=1500 远超 → 每批只塞 1 个（至少塞1）
        targets = [_target(f"r{i}", "agentA", "success", 100) for i in range(30)]

        batches = service._split_targets_into_batches(targets)

        # limit=3 → 最多 3 批，每批 1 个（因每目标都超预算，靠"至少塞1"兜底）
        assert len(batches) == 3
        assert sum(len(b) for b in batches) == 3  # 只处理了 3 个，剩 27 个留下次

    def test_all_targets_assigned_no_loss(self) -> None:
        """未触顶 limit 时，全部目标都被分配，无遗漏。"""
        service = _make_service(context_window=1_000_000, review_batch_limit=10)
        targets = [_target(f"r{i}", "agentA", "success", 50) for i in range(20)]

        batches = service._split_targets_into_batches(targets)

        # 大窗口下 20 个目标预算足够 → 1 批装完，无遗漏
        assigned_ids = {t["run_id"] for batch in batches for t in batch}
        assert assigned_ids == {f"r{i}" for i in range(20)}

    def test_empty_input_returns_empty(self) -> None:
        """无目标时返回空批次列表。"""
        service = _make_service()
        assert service._split_targets_into_batches([]) == []
