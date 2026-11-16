# @feature: FP-0.2.可观测性 E-1 trace_exporter | @ci: python-plugins-test
"""on_load 未配置导出目标 → 降级不装载（不 raise）。

装机环境常无 AGENTOS_TRACE_EXPORT_TOKEN（不启用上云导出），on_load 曾
fail-fast raise 使每轮装载广播刷 ERROR（kernel.log.2026-10-23）——改为同族
sync_relay / trace_ingestion 口径：WARNING + 导出面缺席。断行为断言：
- 不 raise，_exporter 保持 None（域事件不入 outbox，fail-closed 不变）；
- 配置齐备时仍正常装配（对照组，防降级吞掉合法装载）。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

# 裸名 `server` 会被同车道其他插件的 server.py 抢注（test_integration 先跑时
# sys.modules['server'] 已被占），按文件路径显式加载本插件 server。
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "trace_exporter_server", _HERE.parent / "server.py"
)
trace_exporter_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trace_exporter_server)


@pytest.fixture
def _clean_exporter():
    """隔离全局 _exporter，用后复位。"""
    trace_exporter_server._exporter = None
    yield
    trace_exporter_server._exporter = None


@pytest.mark.asyncio
async def test_on_load_without_config_degrades_not_raises(
    _clean_exporter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(trace_exporter_server.TOKEN_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger=trace_exporter_server.logger.name):
        await trace_exporter_server._on_load({})

    assert trace_exporter_server._exporter is None
    assert any(
        trace_exporter_server.TOKEN_ENV in r.message and "未配置" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_on_load_without_config_keeps_domain_events_unbuffered(
    _clean_exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """降级后域事件走 None 早退——不写任何 outbox 文件。"""
    monkeypatch.delenv(trace_exporter_server.TOKEN_ENV, raising=False)
    await trace_exporter_server._on_load({})
    await trace_exporter_server._on_domain_event({"event": "run.completed"})

    assert trace_exporter_server._exporter is None


@pytest.mark.asyncio
async def test_on_load_with_full_config_still_loads(
    _clean_exporter, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """对照组：配置齐备时装配照旧——降级不得吞掉合法装载。"""
    monkeypatch.setenv(trace_exporter_server.TOKEN_ENV, "test-token")
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        type(trace_exporter_server.plugin), "get_config",
        lambda self: {"settings": {
            "ingestion_url": "http://127.0.0.1:9/ext/trace_ingestion/events",
            "origin_host": "test-host",
        }},
        raising=False,
    )
    await trace_exporter_server._on_load({})
    try:
        assert trace_exporter_server._exporter is not None
    finally:
        await trace_exporter_server._on_unload({})
