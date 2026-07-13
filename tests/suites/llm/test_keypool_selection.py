"""验证 KeyPool 主备模式选 key 策略。

修复前 select() 按 score() 择优，token_quota=0 时 token 维度恒为 3999.6 常数，
两个 key 的 RPM 配额不同（120 vs 150）导致结构性偏向：配额大的 key ratio 下降慢，
score 永远更高，并发时全部流量涌向它，主 key 饿死。

修复后 select() 按 slots 声明顺序（llm.yaml 的 keys 段顺序）取第一个可用 key，
实现主备优先级：主 key 可用就始终选它，仅当其冷却/限流/配额耗尽才回退到备 key。
"""
from __future__ import annotations

import asyncio

import pytest

from llm.key_pool import KeyPool, KeySlot


def _make_slot(
    key_id: str,
    api_key: str,
    *,
    max_concurrent: int = 7,
    rpm_limit: int = 120,
    token_quota: int = 0,
) -> KeySlot:
    """构造一个 KeySlot，默认复刻 zhipu_coding_main 的 yaml 配置。"""
    return KeySlot(
        key_id=key_id,
        api_key=api_key,
        max_concurrent=max_concurrent,
        rpm_limit=rpm_limit,
        token_quota=token_quota,
    )


def _make_zhipu_pool() -> tuple[KeyPool, KeySlot, KeySlot]:
    """构造复刻 llm.yaml 的 zhipu_coding 双 key 池。

    slots 顺序：main 在前（主），k2 在后（备）。
    """
    main = _make_slot("zhipu_coding_main", "55fda4", max_concurrent=7, rpm_limit=120)
    k2 = _make_slot("zhipu_coding_2", "91357b", max_concurrent=3, rpm_limit=150)
    return KeyPool([main, k2], pool_id="zhipu_coding"), main, k2


class TestPrimaryPreferredSelection:
    """select() 必须优先选 slots 顺序中的第一个可用 key。"""

    def test_both_available_picks_primary(self):
        """两个 key 都可用时，始终选第一个（主 key）。"""
        pool, main, _ = _make_zhipu_pool()

        assert pool.select() is main

    @pytest.mark.asyncio
    async def test_concurrent_all_go_to_primary(self):
        """并发场景下全部请求应打主 key，备 key 零流量。

        回归 BUG-FIX-20260619: 修复前 20 并发会有 17 个打备 key。
        """
        pool, main, k2 = _make_zhipu_pool()
        picks: list[str] = []

        async def _call() -> None:
            slot = await pool.acquire_slot(timeout=5)
            picks.append(slot.key_id)
            await asyncio.sleep(0.05)  # 持有信号量一小段时间
            slot.release()

        await asyncio.gather(*[_call() for _ in range(20)])

        assert picks.count("zhipu_coding_main") == 20
        assert picks.count("zhipu_coding_2") == 0


class TestFailoverToBackup:
    """主 key 不可用时，必须回退到备 key。"""

    def test_primary_cooled_down_falls_back(self):
        """主 key 被 429 冷却时，选备 key。"""
        pool, main, k2 = _make_zhipu_pool()
        main.on_rate_limit(retry_after=300)  # main 冷却 300s

        assert pool.select() is k2

    def test_primary_rpm_exhausted_falls_back(self):
        """主 key RPM 打满（额度耗尽）时，选备 key。"""
        pool, main, k2 = _make_zhipu_pool()
        for _ in range(main.rpm_limit):
            main.record_request()  # 用满 main 的 rpm

        assert pool.select() is k2

    def test_primary_token_quota_exhausted_falls_back(self):
        """主 key token 配额耗尽时，选备 key。"""
        main = _make_slot("main", "55fda4", token_quota=1000)
        k2 = _make_slot("k2", "91357b", token_quota=1000)
        pool = KeyPool([main, k2], pool_id="test")
        main.record_usage(prompt_tokens=600, completion_tokens=500)  # 1100 > 1000

        assert pool.select() is k2

    @pytest.mark.asyncio
    async def test_concurrent_failover_to_backup(self):
        """主 key 冷却时，并发请求全部走备 key。"""
        pool, main, k2 = _make_zhipu_pool()
        main.on_rate_limit(retry_after=300)
        picks: list[str] = []

        async def _call() -> None:
            slot = await pool.acquire_slot(timeout=5)
            picks.append(slot.key_id)
            await asyncio.sleep(0.05)
            slot.release()

        await asyncio.gather(*[_call() for _ in range(5)])

        assert picks.count("zhipu_coding_main") == 0
        assert picks.count("zhipu_coding_2") == 5


class TestRecoveryToPrimary:
    """主 key 恢复可用后，必须切回主 key。"""

    def test_primary_recovers_selects_it_again(self):
        """主 key 冷却期结束后，重新被选中。"""
        pool, main, k2 = _make_zhipu_pool()
        main.on_rate_limit(retry_after=0.1)  # 很短冷却

        assert pool.select() is k2  # 冷却中走备

        # 等冷却结束（on_rate_limit 用 monotonic 时钟，sleep 后即过期）
        import time
        time.sleep(0.2)

        assert pool.select() is main  # 恢复后切回主


class TestAllExhausted:
    """所有 key 不可用时的兜底行为。"""

    def test_all_cooling_returns_none(self):
        """所有 key 都冷却中，返回 None。"""
        pool, main, k2 = _make_zhipu_pool()
        main.on_rate_limit(retry_after=300)
        k2.on_rate_limit(retry_after=300)

        assert pool.select() is None
