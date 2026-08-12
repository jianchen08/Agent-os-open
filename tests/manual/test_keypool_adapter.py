"""KeyPoolAdapter 真实调用测试。

验证：
1. 基本调用能否成功
2. 并发控制是否生效（per-key semaphore）
3. Key 轮转是否生效（429 时换 key）
4. KeyPool 状态统计

用法: cd src && python -m test_keypool_adapter
"""
import asyncio
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test")


async def main():
    from config.models import get_model_config_loader
    from llm.router_factory import (
        build_adapter,
        get_key_pool,
        reset_router,
    )

    # 1. 构建 adapter
    reset_router()
    loader = get_model_config_loader()
    adapter = build_adapter(loader)

    # 打印 KeyPool 状态
    pool = get_key_pool("zhipu_coding")
    if pool:
        logger.info("=== KeyPool 初始状态 ===")
        for key_id, stats in pool.stats().items():
            logger.info("  %s: %s", key_id, stats)

    # 2. 单次调用测试
    logger.info("\n=== 测试1: 单次调用 ===")
    t0 = time.monotonic()
    resp = await adapter.completion(
        model="zai/glm-5.1",
        messages=[{"role": "user", "content": "回复 OK 两个字"}],
        max_tokens=32,
        timeout=60,
    )
    elapsed = time.monotonic() - t0
    logger.info("  响应: %s (耗时 %.2fs, tokens=%s)", repr(resp.text), elapsed, resp.usage)

    # 打印 KeyPool 状态
    if pool:
        logger.info("=== KeyPool 单次调用后 ===")
        for key_id, stats in pool.stats().items():
            logger.info("  %s: %s", key_id, stats)

    # 3. 并发调用测试（5 个并发，看是否跑满 key 容量）
    logger.info("\n=== 测试2: 5 并发调用 ===")

    async def _call(idx: int) -> tuple[int, str, float]:
        t = time.monotonic()
        r = await adapter.completion(
            model="zai/glm-5.1",
            messages=[{"role": "user", "content": f"回复数字 {idx}"}],
            max_tokens=32,
            timeout=60,
        )
        return idx, r.text or "", time.monotonic() - t

    t0 = time.monotonic()
    results = await asyncio.gather(*[_call(i) for i in range(5)])
    total_elapsed = time.monotonic() - t0

    for idx, text, elapsed in results:
        logger.info("  [%d] %s (%.2fs)", idx, repr(text[:50]), elapsed)
    logger.info("  5 并发总耗时: %.2fs (如果串行应≈%.2fs)", total_elapsed, sum(e for _, _, e in results))

    # 打印 KeyPool 状态
    if pool:
        logger.info("=== KeyPool 并发调用后 ===")
        for key_id, stats in pool.stats().items():
            logger.info("  %s: %s", key_id, stats)

    # 4. 大量并发测试（10 个，超过 key 总容量 7）
    logger.info("\n=== 测试3: 10 并发调用（超出容量） ===")
    t0 = time.monotonic()
    results2 = await asyncio.gather(*[_call(i) for i in range(10)])
    total_elapsed2 = time.monotonic() - t0

    for idx, text, elapsed in results2:
        logger.info("  [%d] %s (%.2fs)", idx, repr(text[:50]), elapsed)
    logger.info("  10 并发总耗时: %.2fs", total_elapsed2)

    # 最终 KeyPool 状态
    if pool:
        logger.info("=== KeyPool 最终状态 ===")
        for slot in pool.slots:
            logger.info(
                "  %s: rpm_remaining=%d, is_cooling=%s, token_remaining=%d",
                slot.key_id, slot.rpm_remaining, slot.is_cooling, slot.token_remaining,
            )

    logger.info("\n=== 全部测试完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
