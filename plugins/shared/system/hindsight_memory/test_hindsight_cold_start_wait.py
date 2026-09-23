# @feature: FP-0.2.六 记忆检索(BUG-63 冷启动窗) | @vision: V1 可进化 | @audit: T5#4 | @ci: python-coverage
"""hindsight 工具面冷启动窗有界等待（BUG-63）。

现象：idle-unload 后首条记忆调用落在 on_load in-flight 窗内（hindsight-api
子进程首启 26-40s 级），旧实现 `_client is None` 即时返回降级 dict → 冷启动
窗内必失败。修复契约：

1. on_load in-flight 期间工具面有界等待共享就绪事件（不阻塞事件循环），
   到期或 on_load 结束后复查 _client——就绪则正常执行，仍 None 才降级；
2. 多调用共享同一事件，on_load 单次执行只 spawn 一次 api；
3. on_load 失败（venv 缺失等）立即唤醒等待者 → 快速降级，不空耗窗口；
4. 无 in-flight on_load（永久降级态）不加人为延迟，行为与旧实现一致；
5. 等待可观测：info 日志「等待后端初始化（已等 Ns）」。

等待时长用短超时注入（_COLD_START_WAIT_S）+ 真实 asyncio 事件时序断言
不变量，禁止真实长 sleep。

[来源: plugins/shared/system/hindsight_memory/server.py]
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_server_cold_start_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv() -> Any:
    """每个测试独立 server 模块实例（_client 初始 None）。"""
    module = _load_module()
    module._client = None
    module._api_process = None
    module._bank_default_warned = False
    return module


async def _call(module: Any, tool_name: str, **kwargs: Any) -> dict[str, Any]:
    """在当前事件循环内调用插件工具并 await 结果。"""
    td = module.plugin._tools[tool_name]
    return await td.handler(**kwargs)


def _stub_hindsight_client(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    client_mod = types.ModuleType("hindsight_client")
    client_mod.Hindsight = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "hindsight_client", client_mod)


# 各工具的最小合法入参（其余参数均有缺省）
_TOOL_KWARGS: dict[str, dict[str, Any]] = {
    "hindsight.retain": {"content": "c"},
    "hindsight.recall": {"query": "q"},
    "hindsight.reflect": {},
    "hindsight.summarize": {},
    "hindsight.delete": {},
    "hindsight.import_document": {"text": "x"},
    "hindsight.get_documents": {},
}

# 工具成功（非降级）响应的正向锚点键：降级 dict 只含 error/initialized，
# 正向路径必含各工具的业务键——双锚防「只要不报错就算成功」的假绿。
_TOOL_POSITIVE_KEY: dict[str, str] = {
    "hindsight.retain": "stored",
    "hindsight.recall": "results",
    "hindsight.reflect": "facts",
    "hindsight.summarize": "summary",
    "hindsight.delete": "deleted",
    "hindsight.import_document": "chunks_imported",
    "hindsight.get_documents": "documents",
}


def _full_mock_client() -> MagicMock:
    """全工具面 mock client（外部依赖替身，业务逻辑走真实实现）。"""
    client = MagicMock()
    client.aretain = AsyncMock(return_value=SimpleNamespace(success=True, id="m1"))
    client.arecall = AsyncMock(return_value=MagicMock(results=[{"id": "r1"}]))
    client.areflect = AsyncMock(return_value=MagicMock(model_dump=lambda: {"facts": ["f"]}))
    client.adelete_bank = AsyncMock(return_value=None)
    client.acreate_bank = AsyncMock(return_value=None)
    client.documents = MagicMock()
    client.documents.list_documents = AsyncMock(return_value=SimpleNamespace(items=[], total=0))
    return client


class TestToolFaceWaitsDuringColdStart:
    @pytest.mark.parametrize("tool_name", sorted(_TOOL_KWARGS))
    async def test_tool_waits_for_ready_then_executes(self, srv: Any, tool_name: str) -> None:
        """_client is None 且 on_load in-flight → 工具有界等待就绪后正常执行。"""
        ready = asyncio.Event()
        srv._init_inflight = ready
        client = _full_mock_client()

        async def _become_ready() -> None:
            await asyncio.sleep(0.02)
            srv._client = client
            ready.set()

        readier = asyncio.ensure_future(_become_ready())
        result = await _call(srv, tool_name, **_TOOL_KWARGS[tool_name])
        await readier

        # 非降级 + 正向业务键双锚
        assert "initialized" not in result
        assert _TOOL_POSITIVE_KEY[tool_name] in result

    async def test_wait_times_out_then_degrades(self, srv: Any, caplog: pytest.LogCaptureFixture) -> None:
        """等待到期 on_load 仍未就绪 → 降级 dict（诚实失败，不无限等）。"""
        srv._COLD_START_WAIT_S = 0.3  # 短超时注入（禁真实长等待）
        srv._init_inflight = asyncio.Event()  # 永不 set：模拟初始化卡死

        with caplog.at_level(logging.INFO):
            started = time.monotonic()
            result = await _call(srv, "hindsight.recall", query="q")
            elapsed = time.monotonic() - started

        assert result["initialized"] is False
        assert result["operation"] == "recall"
        # 时序不变量：确实等满窗口（下界留 OS 调度抖动余量；即时降级路径
        # 实测 ~1e-4s，与 0.2s 隔两个数量级），且未失控（上界）
        assert 0.2 <= elapsed < 5.0
        wait_logs = [r for r in caplog.records if "等待后端初始化" in r.getMessage()]
        assert len(wait_logs) == 1
        assert re.search(r"已等 \d+\.\d+s", wait_logs[0].getMessage())
        assert "initialized=False" in wait_logs[0].getMessage()

    async def test_no_inflight_degrades_without_delay(self, srv: Any) -> None:
        """无 on_load in-flight（永久降级态）→ 即时降级，不叠加人为延迟。"""
        srv._COLD_START_WAIT_S = 30.0  # 大窗口也必须立即返回

        started = time.monotonic()
        result = await _call(srv, "hindsight.retain", content="c")
        elapsed = time.monotonic() - started

        assert result["initialized"] is False
        assert elapsed < 1.0


class TestOnLoadColdStartLifecycle:
    async def test_concurrent_calls_share_event_and_spawn_once(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """并发调用共享同一就绪事件且 on_load 只 spawn 一次 api。

        冷启动窗内调用不降级而是等待；on_load 完成后全部以成功收场——
        装机版时间线（retain 早到 26s 必失败）的本 Bug 直接回归。
        """
        release = asyncio.Event()

        async def _slow_wait(base_url: str, process: Any, stderr_path: str) -> None:
            await release.wait()

        monkeypatch.setattr(srv, "_hindsight_api_up", lambda _base_url: False)
        spawn_calls: list[tuple[int, str]] = []

        def _fake_spawn(port: int, data_dir: str) -> tuple[Any, str]:
            spawn_calls.append((port, data_dir))
            return SimpleNamespace(pid=1), str(tmp_path / "stderr.log")

        monkeypatch.setattr(srv, "_start_api_server", _fake_spawn)
        monkeypatch.setattr(srv, "_wait_api_ready", _slow_wait)
        monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))
        client = _full_mock_client()
        _stub_hindsight_client(monkeypatch, client)

        load_task = asyncio.ensure_future(srv._on_load({}))
        retain_task = asyncio.ensure_future(_call(srv, "hindsight.retain", content="c"))
        recall_task = asyncio.ensure_future(_call(srv, "hindsight.recall", query="q"))
        await asyncio.sleep(0.05)

        # 冷启动窗内：两个调用都在等待而非已降级完成（旧实现此处即失败）
        assert not retain_task.done()
        assert not recall_task.done()
        assert srv._init_inflight is not None
        assert len(spawn_calls) == 1  # 等待者不触发重复 spawn

        release.set()
        retain_result = await retain_task
        recall_result = await recall_task
        await load_task

        assert retain_result["stored"] is True
        assert "initialized" not in recall_result
        assert srv._init_inflight is None  # on_load 任意出口清引用
        assert len(spawn_calls) == 1

    async def test_on_load_failure_wakes_waiters_promptly(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """on_load 失败（venv 缺失）→ 等待者立即醒来快速降级，不空耗窗口。"""
        srv._COLD_START_WAIT_S = 60.0
        monkeypatch.setattr(srv, "_hindsight_api_up", lambda _base_url: False)
        monkeypatch.setattr(os.path, "isfile", MagicMock(return_value=False))
        monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))

        started = time.monotonic()
        load_task = asyncio.ensure_future(srv._on_load({}))
        result = await _call(srv, "hindsight.retain", content="c")
        await load_task
        elapsed = time.monotonic() - started

        assert result["initialized"] is False
        assert elapsed < 5.0  # 失败即唤醒，不等满 60s 窗口
        assert srv._client is None
        assert srv._init_inflight is None
