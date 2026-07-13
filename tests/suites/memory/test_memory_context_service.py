"""MemoryContextService 记忆上下文服务测试。

测试 MemoryContextService 的构造函数和配置校验。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory.memory_context_service import MemoryContextService


# ============================================================
# 辅助
# ============================================================


def _make_compressor() -> MagicMock:
    """创建 mock ContextCompressor。"""
    return MagicMock()


# ============================================================
# 1. 构造函数测试
# ============================================================


class TestMemoryContextServiceInit:
    """测试 MemoryContextService 初始化。"""

    def test_无配置时使用默认值(self) -> None:
        """不传配置时应使用默认值。"""
        svc = MemoryContextService()
        assert svc._config["context_window"] == 128000
        assert svc._config["compress_trigger_ratio"] == 0.55

    def test_有配置时覆盖默认值(self) -> None:
        """传入配置应覆盖默认值。"""
        config = {"context_window": 50000, "compress_trigger_ratio": 0.7}
        svc = MemoryContextService(config=config)
        assert svc._config["context_window"] == 50000
        assert svc._config["compress_trigger_ratio"] == 0.7

    def test_配置校验_缺少context_window时用默认值(self) -> None:
        """配置缺少 context_window 时应使用默认值 128000。"""
        svc = MemoryContextService(config={"compress_trigger_ratio": 0.5})
        assert svc._config["context_window"] == 128000

    def test_配置校验_缺少compress_trigger_ratio时用默认值(self) -> None:
        """配置缺少 compress_trigger_ratio 时应使用默认值 0.55。"""
        svc = MemoryContextService(config={"context_window": 128000})
        assert svc._config["compress_trigger_ratio"] == 0.55

    def test_注入compressor(self) -> None:
        """注入自定义压缩器。"""
        compressor = _make_compressor()
        svc = MemoryContextService(compressor=compressor)
        assert svc._compressor is compressor
