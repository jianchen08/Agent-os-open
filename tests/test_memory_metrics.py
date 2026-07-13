"""记忆系统监控指标模块测试。

覆盖场景：
- 检索延迟：记录耗时、计算 P50/P95/P99 分位数、边界值
- 命中率：命中/未命中统计、百分比计算、极端值
- 存储容量：条目数和字节数追踪、增减操作
- 统一接口：get_metrics() 返回完整快照
- 重置能力：reset() 清空所有指标
- 线程安全：并发操作不破坏状态
- 轻量级：无外部依赖，有限内存占用
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict

import pytest

from memory.memory_metrics import MemoryMetrics


class TestRetrievalLatency:
    """检索延迟监控测试。"""

    def test_single_latency_p50_returns_value(self) -> None:
        """记录单次延迟后 P50 返回该值。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.1, hit=True)

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["p50"] == 0.1

    def test_single_latency_all_percentiles_same(self) -> None:
        """单次采样时，所有分位数应返回相同值。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.25, hit=True)

        lat = metrics.get_metrics()["retrieval_latency"]
        assert lat["p50"] == 0.25
        assert lat["p95"] == 0.25
        assert lat["p99"] == 0.25

    def test_no_latency_data_returns_none(self) -> None:
        """无延迟数据时分位数返回 None。"""
        metrics = MemoryMetrics()

        lat = metrics.get_metrics()["retrieval_latency"]
        assert lat["p50"] is None
        assert lat["p95"] is None
        assert lat["p99"] is None
        assert lat["avg"] is None
        assert lat["min"] is None
        assert lat["max"] is None
        assert lat["count"] == 0

    def test_p50_with_many_samples(self) -> None:
        """大量采样后 P50 接近中位数。

        意图：验证分位数计算的基本正确性——
        P50 应该将数据集分成大约相等的两半。
        """
        metrics = MemoryMetrics()
        # 记录 1-100 毫秒的延迟
        for i in range(1, 101):
            metrics.record_retrieval(latency_seconds=i / 1000.0, hit=True)

        lat = metrics.get_metrics()["retrieval_latency"]
        # P50 应约 0.050 (第50个值附近)
        assert 0.045 <= lat["p50"] <= 0.055
        # P95 应约 0.095 (第95个值附近)
        assert 0.090 <= lat["p95"] <= 0.096
        # P99 应约 0.099 (第99个值附近)
        assert 0.098 <= lat["p99"] <= 0.100

    def test_latency_min_max_avg(self) -> None:
        """延迟的最小值、最大值、平均值正确。"""
        metrics = MemoryMetrics()
        for v in [0.01, 0.05, 0.10, 0.20, 0.50]:
            metrics.record_retrieval(latency_seconds=v, hit=True)

        lat = metrics.get_metrics()["retrieval_latency"]
        assert lat["min"] == 0.01
        assert lat["max"] == 0.50
        assert abs(lat["avg"] - 0.172) < 0.001
        assert lat["count"] == 5

    def test_latency_bounded_storage(self) -> None:
        """延迟存储有上限，超过后丢弃旧数据。

        意图：确保监控模块本身不会造成内存泄漏——
        即使记录海量数据，内存占用也应恒定。
        """
        metrics = MemoryMetrics(max_latency_samples=100)
        for i in range(200):
            metrics.record_retrieval(latency_seconds=i / 1000.0, hit=True)

        lat = metrics.get_metrics()["retrieval_latency"]
        # 只保留最后 100 个采样（0.100-0.199）
        assert lat["count"] == 100
        assert lat["min"] == pytest.approx(0.100, abs=0.001)

    def test_latency_zero_value(self) -> None:
        """零延迟（缓存命中场景）应被正确记录。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.0, hit=True)

        lat = metrics.get_metrics()["retrieval_latency"]
        assert lat["p50"] == 0.0
        assert lat["min"] == 0.0


class TestHitRate:
    """命中率监控测试。"""

    def test_initial_hit_rate_zero(self) -> None:
        """初始状态命中率为 0.0（无查询时）。"""
        metrics = MemoryMetrics()

        hr = metrics.get_metrics()["hit_rate"]
        assert hr["rate"] == 0.0
        assert hr["hits"] == 0
        assert hr["total"] == 0

    def test_all_hits_rate_100(self) -> None:
        """全部命中时命中率为 100.0。"""
        metrics = MemoryMetrics()
        for _ in range(10):
            metrics.record_retrieval(latency_seconds=0.01, hit=True)

        hr = metrics.get_metrics()["hit_rate"]
        assert hr["rate"] == 100.0
        assert hr["hits"] == 10
        assert hr["total"] == 10

    def test_all_misses_rate_0(self) -> None:
        """全部未命中时命中率为 0.0。"""
        metrics = MemoryMetrics()
        for _ in range(10):
            metrics.record_retrieval(latency_seconds=0.05, hit=False)

        hr = metrics.get_metrics()["hit_rate"]
        assert hr["rate"] == 0.0
        assert hr["hits"] == 0
        assert hr["total"] == 10

    def test_mixed_hits_rate_correct(self) -> None:
        """混合命中/未命中时命中率计算正确。

        意图：验证命中率 = hits / total * 100 的核心业务规则。
        """
        metrics = MemoryMetrics()
        # 7 次命中，3 次未命中 → 命中率 70%
        for hit in [True, True, False, True, True, False, True, False, True, True]:
            metrics.record_retrieval(latency_seconds=0.01, hit=hit)

        hr = metrics.get_metrics()["hit_rate"]
        assert hr["rate"] == 70.0
        assert hr["hits"] == 7
        assert hr["total"] == 10

    def test_single_hit_rate_100(self) -> None:
        """单次命中时命中率为 100.0。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.01, hit=True)

        assert metrics.get_metrics()["hit_rate"]["rate"] == 100.0

    def test_single_miss_rate_0(self) -> None:
        """单次未命中时命中率为 0.0。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.01, hit=False)

        assert metrics.get_metrics()["hit_rate"]["rate"] == 0.0


class TestStorageCapacity:
    """存储容量监控测试。"""

    def test_initial_storage_zero(self) -> None:
        """初始状态存储容量为零。"""
        metrics = MemoryMetrics()

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 0
        assert storage["total_bytes"] == 0

    def test_record_add_entries(self) -> None:
        """记录新增条目后容量正确。"""
        metrics = MemoryMetrics()
        metrics.record_storage_change(delta_entries=5, delta_bytes=1024)

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 5
        assert storage["total_bytes"] == 1024

    def test_record_remove_entries(self) -> None:
        """记录删除条目后容量正确。"""
        metrics = MemoryMetrics()
        metrics.record_storage_change(delta_entries=10, delta_bytes=2048)
        metrics.record_storage_change(delta_entries=-3, delta_bytes=-512)

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 7
        assert storage["total_bytes"] == 1536

    def test_storage_cannot_go_negative(self) -> None:
        """存储容量不会变为负数。

        意图：防止因统计误差或并发竞态导致负数出现，
        这在监控面板上会造成困惑。
        """
        metrics = MemoryMetrics()
        metrics.record_storage_change(delta_entries=-5, delta_bytes=-100)

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 0
        assert storage["total_bytes"] == 0

    def test_multiple_storage_changes(self) -> None:
        """多次存储变更后累计正确。"""
        metrics = MemoryMetrics()
        changes = [
            (10, 5000),
            (5, 2000),
            (-3, -1000),
            (20, 10000),
            (-2, -500),
        ]
        for entries, bytes_ in changes:
            metrics.record_storage_change(
                delta_entries=entries, delta_bytes=bytes_
            )

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 30  # 10+5-3+20-2
        assert storage["total_bytes"] == 15500  # 5000+2000-1000+10000-500


class TestGetMetrics:
    """统一 get_metrics() 接口测试。"""

    def test_returns_dict_with_all_sections(self) -> None:
        """get_metrics 返回包含所有指标分区的字典。"""
        metrics = MemoryMetrics()
        snapshot = metrics.get_metrics()

        assert "retrieval_latency" in snapshot
        assert "hit_rate" in snapshot
        assert "storage" in snapshot

    def test_snapshot_is_independent_copy(self) -> None:
        """返回的快照是独立副本，修改不影响内部状态。

        意图：防止外部代码意外修改监控数据。
        """
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.1, hit=True)

        snap1 = metrics.get_metrics()
        snap1["hit_rate"]["hits"] = 999  # 修改快照

        snap2 = metrics.get_metrics()
        assert snap2["hit_rate"]["hits"] == 1  # 内部状态未受影响

    def test_all_latency_fields_present(self) -> None:
        """延迟快照包含所有必要字段。"""
        metrics = MemoryMetrics()
        lat = metrics.get_metrics()["retrieval_latency"]

        required_fields = {"p50", "p95", "p99", "avg", "min", "max", "count"}
        assert required_fields.issubset(set(lat.keys()))

    def test_all_hit_rate_fields_present(self) -> None:
        """命中率快照包含所有必要字段。"""
        metrics = MemoryMetrics()
        hr = metrics.get_metrics()["hit_rate"]

        required_fields = {"rate", "hits", "total"}
        assert required_fields.issubset(set(hr.keys()))

    def test_all_storage_fields_present(self) -> None:
        """存储快照包含所有必要字段。"""
        metrics = MemoryMetrics()
        storage = metrics.get_metrics()["storage"]

        required_fields = {"entry_count", "total_bytes"}
        assert required_fields.issubset(set(storage.keys()))


class TestReset:
    """重置功能测试。"""

    def test_reset_clears_all_metrics(self) -> None:
        """重置后所有指标归零。"""
        metrics = MemoryMetrics()
        # 填充数据
        for i in range(10):
            metrics.record_retrieval(latency_seconds=0.01 * i, hit=(i % 2 == 0))
        metrics.record_storage_change(delta_entries=5, delta_bytes=1024)

        metrics.reset()
        snapshot = metrics.get_metrics()

        # 延迟归零
        assert snapshot["retrieval_latency"]["count"] == 0
        assert snapshot["retrieval_latency"]["p50"] is None

        # 命中率归零
        assert snapshot["hit_rate"]["hits"] == 0
        assert snapshot["hit_rate"]["total"] == 0
        assert snapshot["hit_rate"]["rate"] == 0.0

        # 存储归零
        assert snapshot["storage"]["entry_count"] == 0
        assert snapshot["storage"]["total_bytes"] == 0

    def test_reset_then_record_works(self) -> None:
        """重置后可以继续正常记录。"""
        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.5, hit=True)
        metrics.reset()

        metrics.record_retrieval(latency_seconds=0.1, hit=False)
        metrics.record_storage_change(delta_entries=3, delta_bytes=512)

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == 1
        assert snapshot["hit_rate"]["total"] == 1
        assert snapshot["storage"]["entry_count"] == 3

    def test_reset_idempotent(self) -> None:
        """连续重置不会出错。"""
        metrics = MemoryMetrics()
        metrics.reset()
        metrics.reset()
        metrics.reset()

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == 0


class TestThreadSafety:
    """线程安全测试。"""

    def test_concurrent_record_retrieval(self) -> None:
        """并发记录检索操作不破坏状态。

        意图：监控模块可能被多个检索线程同时调用，
        必须保证计数和延迟数据的完整性。
        """
        metrics = MemoryMetrics()
        num_threads = 10
        records_per_thread = 100
        barrier = threading.Barrier(num_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(records_per_thread):
                metrics.record_retrieval(
                    latency_seconds=0.001, hit=True
                )

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == num_threads * records_per_thread
        assert snapshot["hit_rate"]["hits"] == num_threads * records_per_thread
        assert snapshot["hit_rate"]["total"] == num_threads * records_per_thread

    def test_concurrent_storage_changes(self) -> None:
        """并发存储变更不丢失计数。"""
        metrics = MemoryMetrics()
        num_threads = 10
        barrier = threading.Barrier(num_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(100):
                metrics.record_storage_change(
                    delta_entries=1, delta_bytes=10
                )

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 1000
        assert storage["total_bytes"] == 10000


class TestLightweight:
    """轻量级验证测试。"""

    def test_no_external_dependencies(self) -> None:
        """模块不引入外部依赖（仅 stdlib）。"""
        import importlib

        mod = importlib.import_module("memory.memory_metrics")
        # 检查模块源文件中没有外部 import
        source_file = mod.__file__
        assert source_file is not None

        with open(source_file, encoding="utf-8") as f:
            content = f.read()

        # 模块中不应出现第三方库导入
        forbidden = ["numpy", "pandas", "prometheus", "redis", "sqlalchemy"]
        for lib in forbidden:
            assert f"import {lib}" not in content, f"不应依赖 {lib}"

    def test_record_retrieval_is_fast(self) -> None:
        """单次记录操作应在 100 微秒内完成。"""
        metrics = MemoryMetrics()
        start = time.perf_counter()
        for _ in range(10000):
            metrics.record_retrieval(latency_seconds=0.001, hit=True)
        elapsed = time.perf_counter() - start

        avg_us = (elapsed / 10000) * 1_000_000
        # 平均每次记录应小于 100 微秒
        assert avg_us < 100, f"平均记录耗时 {avg_us:.1f}μs，超过 100μs 阈值"

    def test_get_metrics_is_fast(self) -> None:
        """get_metrics 快照获取应在 1 毫秒内完成。"""
        metrics = MemoryMetrics()
        for i in range(1000):
            metrics.record_retrieval(
                latency_seconds=i / 10000.0, hit=(i % 3 != 0)
            )

        start = time.perf_counter()
        for _ in range(100):
            metrics.get_metrics()
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / 100) * 1000
        assert avg_ms < 1.0, f"平均 get_metrics 耗时 {avg_ms:.3f}ms，超过 1ms 阈值"
