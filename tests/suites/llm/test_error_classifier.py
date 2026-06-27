"""error_classifier 分类 + KeySlot NETWORK 策略测试。

回归 BUG-FIX-20260626_network_misclassified:
网络连接错误（死代理/断网/连接被拒）被误判为 SERVICE_DOWN，按限流方式反复
降级 key、退避重试。修复后：
1. 连接错误 → ErrorKind.NETWORK（error_classifier 层）
2. NETWORK → 冷却 key 但不并发降级（key_pool 层）
"""
from __future__ import annotations

import asyncio

import pytest

import litellm

from llm.error_classifier import ErrorInfo, ErrorKind, classify_error
from llm.key_pool import KeySlot


# ---------------------------------------------------------------------------
# classify_error：连接错误必须归 NETWORK
# ---------------------------------------------------------------------------

class TestConnectionErrorClassifiedAsNetwork:
    """连接失败（死代理/连接被拒/断网）必须归 NETWORK，不可误判 SERVICE_DOWN。"""

    def test_internal_server_error_with_connection_error_msg(self):
        """litellm 把 openai 兼容端点的 httpx 连接失败包装成
        InternalServerError（消息含 'Connection error'）→ NETWORK。

        日志证据：yichengc/apigo 流式 'OpenAIException - Connection error.'。
        """
        exc = litellm.InternalServerError(
            "OpenAIException - Connection error.",
            model="glm-5.2",
            llm_provider="openai",
        )
        assert classify_error(exc).kind == ErrorKind.NETWORK

    def test_internal_server_error_with_connect_to_host(self):
        """aiohttp 风格 'Cannot connect to host' → NETWORK。"""
        exc = litellm.InternalServerError(
            "Cannot connect to host 127.0.0.1:7993",
            model="glm-5.2",
            llm_provider="openai",
        )
        assert classify_error(exc).kind == ErrorKind.NETWORK

    def test_api_connection_error_is_network(self):
        """minimax 原生 handler 保留 APIConnectionError → NETWORK（既有逻辑回归）。"""
        exc = litellm.APIConnectionError(
            "Connection refused",
            model="MiniMax-M3",
            llm_provider="minimax",
        )
        assert classify_error(exc).kind == ErrorKind.NETWORK


# ---------------------------------------------------------------------------
# classify_error：真正的服务端错误仍归 SERVICE_DOWN / 限流不受影响（回归）
# ---------------------------------------------------------------------------

class TestRegressionClassification:
    """修复不能误伤真正的 SERVICE_DOWN 和 RATE_LIMIT。"""

    def test_internal_server_error_without_connection_keyword(self):
        """不含连接失败关键词的 500 仍归 SERVICE_DOWN。"""
        exc = litellm.InternalServerError(
            "upstream returned malformed JSON",
            model="glm-5.2",
            llm_provider="openai",
        )
        assert classify_error(exc).kind == ErrorKind.SERVICE_DOWN

    def test_rate_limit_still_rate_limit(self):
        """限流（429）分类不受影响。"""
        exc = litellm.RateLimitError(
            message="requests-per-minute limit exceeded",
            model="glm-5.2",
            llm_provider="openai",
        )
        assert classify_error(exc).kind == ErrorKind.RATE_LIMIT


# ---------------------------------------------------------------------------
# KeySlot NETWORK 策略：冷却但不并发降级
# ---------------------------------------------------------------------------

def _make_slot(max_concurrent: int = 3) -> KeySlot:
    """构造测试用 KeySlot。"""
    return KeySlot(
        key_id="test_main",
        api_key="sk-test",
        max_concurrent=max_concurrent,
    )


class TestNetworkCoolingWithoutDegrade:
    """网络错误：冷却 key（避免立即重试），但不并发降级（不按限流方式处理）。"""

    def test_network_cools_key(self):
        """NETWORK 后 key 进入冷却，select() 会绕开它。"""
        slot = _make_slot()
        assert not slot.is_cooling
        slot.handle_error(ErrorInfo(ErrorKind.NETWORK))
        assert slot.is_cooling

    async def test_network_does_not_reduce_concurrency(self):
        """NETWORK 不降级：并发容量保持初始值。

        降级的外部表现是并发容量减少（能 acquire 的许可数变少）。
        NETWORK 后容量应仍是 max_concurrent=3：第 4 次 acquire 阻塞。
        """
        slot = _make_slot(max_concurrent=3)
        slot.handle_error(ErrorInfo(ErrorKind.NETWORK))
        for _ in range(3):
            await slot.acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slot.acquire(), timeout=0.1)
        for _ in range(3):
            slot.release()

    async def test_rate_limit_does_reduce_concurrency(self):
        """对照：RATE_LIMIT 降级 1 级（max_concurrent 3→2），证明测试 setup 有效。

        若此用例失败说明对照失效，test_network_does_not_reduce_concurrency 不可信。
        """
        slot = _make_slot(max_concurrent=3)
        slot.handle_error(ErrorInfo(ErrorKind.RATE_LIMIT))
        for _ in range(2):
            await slot.acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slot.acquire(), timeout=0.1)
        for _ in range(2):
            slot.release()

    def test_network_retry_after_honored(self):
        """NETWORK 带 retry_after 时按其冷却。"""
        slot = _make_slot()
        slot.handle_error(ErrorInfo(ErrorKind.NETWORK, retry_after=30.0))
        assert slot.is_cooling
