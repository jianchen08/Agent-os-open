"""SDK 共享模块测试——error_classifier / stream_watchdog / adapter_config。

这三个模块 2026-08-25 批5 从插件侧副本下沉到 SDK（插件自包含约束下禁止
跨插件 import，公共逻辑统一沉 SDK，消灭复制漂移）。行为细节由消费方测试
覆盖（tests/plugins/core/llm_core/test_error_classifier.py 等），这里锁
SDK 侧的基本契约。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agentos_plugin_sdk.adapter_config import (
    AdapterConfig,
    get_adapter_status_summary,
)
from agentos_plugin_sdk.error_classifier import ErrorKind, classify_error
from agentos_plugin_sdk.stream_watchdog import StreamHardTimeout


class TestErrorClassifier:
    def test_unknown_exception_falls_back(self) -> None:
        # 原生异常类名不在 litellm/openai 类型层次、消息无可嗅探特征 → UNKNOWN
        info = classify_error(ConnectionError("boom"))
        assert info.kind is ErrorKind.UNKNOWN

    def test_timeout_classified_as_network(self) -> None:
        # 分类口径：超时归 NETWORK（「该如何处理」视角，非原始异常类型）
        info = classify_error(TimeoutError("Read timeout"))
        assert info.kind is ErrorKind.NETWORK


def _make_watchdog(timeout: float) -> tuple[StreamHardTimeout, list[str]]:
    """构造带观测的 watchdog：on_fire 记录触发；stream 记录 aclose。

    loop 不运行——_fire 的回桥 aclose 提交失败会被 watchdog 吞掉（契约：
    watchdog 永不传播错误），on_fire 仍可观测。
    """
    fired: list[str] = []

    class FakeStream:
        async def aclose(self) -> None:
            fired.append("aclose")

    loop = asyncio.new_event_loop()
    wd = StreamHardTimeout(
        FakeStream(), loop, timeout, on_fire=lambda: fired.append("on_fire")
    )
    return wd, fired


class TestStreamHardTimeout:
    def test_disarm_prevents_fire(self) -> None:
        wd, fired = _make_watchdog(0.05)
        wd.arm()
        time.sleep(0.01)
        wd.disarm()
        time.sleep(0.15)
        assert fired == []

    def test_fires_after_hard_timeout(self) -> None:
        wd, fired = _make_watchdog(0.02)
        wd.arm()
        deadline = time.monotonic() + 2.0
        while "on_fire" not in fired and time.monotonic() < deadline:
            time.sleep(0.005)
        wd.disarm()
        assert "on_fire" in fired

    def test_reset_postpones_deadline(self) -> None:
        wd, fired = _make_watchdog(0.08)
        wd.arm()
        try:
            time.sleep(0.04)
            wd.reset()  # 新 deadline = now + 0.08
            time.sleep(0.04)  # 距 reset 0.04 < 0.08，不应触发
            assert fired == []
        finally:
            wd.disarm()


class TestAdapterConfig:
    def test_status_summary_shape(self) -> None:
        configs: dict[str, Any] = {
            "demo": AdapterConfig(
                name="demo",
                adapter_type="http",
                priority=5,
                display_name="Demo",
                capabilities=("read", "write"),
                available=True,
                has_mcp=False,
                connector_class=None,
            )
        }
        summary = get_adapter_status_summary(configs)
        assert set(summary) == {"demo"}
        entry = summary["demo"]
        assert entry["type"] == "http"
        assert entry["available"] is True
        assert entry["capabilities_count"] == 2
        assert entry["has_mcp"] is False
