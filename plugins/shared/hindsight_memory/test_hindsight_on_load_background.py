# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: python-coverage
"""on_load 就绪等待后台化行为测试（不阻内核 boot）。

装机实测（kernel.log.2026-11-20）：02:08:53 hindsight on_load 起，02:09:37
才见「on_load 完成」——/health 就绪轮询（_API_READY_WAIT_S=150s 慢档）阻塞
内核 boot 约 44s，期间前端无端口可连。本套件锁定修复契约：

1. api 未就绪时 on_load 秒级返回（pending 态：_client=None + 初始化事件在途），
   注入就绪窗 30s/90s 两组输入下返回时限 <5s——旧实现必等满注入窗（区分度）；
2. 后台自连成功路径可达：on_load 返回后工具调用等待（不即时降级），api 就绪
   后以成功收场（BUG-63 冷启动窗语义不变，正向业务键双锚）；
3. 后台自连失败：warn 留痕 + 等待者快速唤醒降级（诚实失败，不空耗窗）；
4. 未移交后台任务的 on_load 异常出口：事件 set + 清引用，异常照抛。

mock 仅限外部依赖（spawn/健康轮询/hindsight client）；时序断言用短注入窗 +
真实事件时序不变量，禁真实长 sleep。

[来源: plugins/shared/hindsight_memory/server.py]
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
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

_SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_server_on_load_bg_test"
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


def _mock_client() -> MagicMock:
    """最小 mock client（外部依赖替身）：aretain 成功 + bank 创建幂等。"""
    client = MagicMock()
    client.aretain = AsyncMock(return_value=SimpleNamespace(success=True, id="m1"))
    client.acreate_bank = AsyncMock(return_value=None)
    return client


def _patch_slow_api(srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait_impl: Any) -> None:
    """钉死主路径前置：辅助 venv 在位、api 不在位、spawn 即回（不真起进程）。"""
    monkeypatch.setattr(srv, "_hindsight_api_up", lambda _base_url: False)
    monkeypatch.setattr(srv, "_needs_aux_provision", lambda _root: False)
    monkeypatch.setattr(
        srv,
        "_start_api_server",
        lambda _port, _data_dir: (SimpleNamespace(pid=1), str(tmp_path / "stderr.log")),
    )
    monkeypatch.setattr(srv, "_wait_api_ready", wait_impl)
    monkeypatch.setenv("HINDSIGHT_DATA_DIR", str(tmp_path / "data"))


class TestOnLoadReturnsFastWhileApiNotReady:
    @pytest.mark.parametrize("ready_window_s", [30, 90])
    async def test_on_load_seconds_not_window(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready_window_s: int
    ) -> None:
        """api 慢启动整窗不就绪 → on_load 秒级返回，不等待就绪轮询窗。

        时序不变量：旧实现同步 await _wait_api_ready 必等满注入窗（30s/90s）；
        新实现 on_load 返回须远小于窗（<5s）。两组注入窗输入保证断言对
        「等待时长」有区分度（禁硬编码期望值刷绿）。
        """
        srv._API_READY_WAIT_S = ready_window_s

        async def _slow_wait(base_url: str, process: Any, stderr_path: str) -> None:
            await asyncio.sleep(ready_window_s)  # 模拟整窗不就绪

        _patch_slow_api(srv, monkeypatch, tmp_path, _slow_wait)

        started = time.monotonic()
        await srv._on_load({})
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"on_load 阻塞 {elapsed:.2f}s（注入就绪窗 {ready_window_s}s，须秒级返回）"
        assert srv._client is None  # pending 态：未就绪即返回
        assert srv._init_inflight is not None  # 冷启动等待面仍在（工具面语义不变）
        task = srv._init_task
        assert task is not None  # 后台初始化在途
        assert not task.done()
        # 清场：取消在途任务（CancelledError 穿过 except Exception 只收口事件面）
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestBackgroundConnectSuccess:
    async def test_waiters_succeed_after_backend_ready(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """后台自连成功路径：on_load 先返回，api 就绪后冷启动窗内等待的工具以成功收场。

        装机时间线回归（boot 不阻 + BUG-63 等待面合体）：on_load 秒回 → 工具
        调用等待而非降级 → api 就绪 → 正向业务键成功（非降级双锚）。
        """
        release = asyncio.Event()

        async def _ready_when_released(base_url: str, process: Any, stderr_path: str) -> None:
            await release.wait()

        _patch_slow_api(srv, monkeypatch, tmp_path, _ready_when_released)
        client = _mock_client()
        _stub_hindsight_client(monkeypatch, client)

        started = time.monotonic()
        load_task = asyncio.ensure_future(srv._on_load({}))
        await asyncio.sleep(0.05)
        assert load_task.done(), "api 未就绪时 on_load 必须已返回"
        assert time.monotonic() - started < 5.0

        retain_task = asyncio.ensure_future(_call(srv, "hindsight.retain", content="c"))
        await asyncio.sleep(0.05)
        assert not retain_task.done(), "冷启动窗内工具应等待而非即时降级"

        release.set()  # api 就绪 → 后台任务完成自连
        result = await retain_task
        await load_task

        assert result["stored"] is True
        assert "initialized" not in result
        assert srv._client is client
        assert srv._init_inflight is None  # 后台出口清引用


class TestBackgroundConnectFailure:
    async def test_failure_warns_and_wakes_waiters(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """后台自连失败 → warn 留痕（含错误原文）+ 等待者快速唤醒降级。"""
        fail_gate = asyncio.Event()

        async def _fail_when_gated(base_url: str, process: Any, stderr_path: str) -> None:
            await fail_gate.wait()
            raise RuntimeError("hindsight-api 注入未就绪")

        _patch_slow_api(srv, monkeypatch, tmp_path, _fail_when_gated)
        srv._COLD_START_WAIT_S = 60.0

        load_task = asyncio.ensure_future(srv._on_load({}))
        await asyncio.sleep(0.05)
        assert load_task.done(), "api 未就绪时 on_load 必须已返回"

        recall_task = asyncio.ensure_future(_call(srv, "hindsight.recall", query="q"))
        await asyncio.sleep(0.05)
        assert not recall_task.done(), "失败前工具应等待而非即时降级"

        with caplog.at_level(logging.WARNING):
            started = time.monotonic()
            fail_gate.set()
            result = await recall_task
            wake_elapsed = time.monotonic() - started
        await load_task

        assert result["initialized"] is False
        assert result["operation"] == "recall"
        assert wake_elapsed < 5.0, "失败必快速唤醒等待者（不空耗冷启动窗）"
        assert srv._client is None
        assert srv._init_inflight is None
        warns = [r for r in caplog.records if "后台初始化失败" in r.getMessage()]
        assert len(warns) == 1
        assert "hindsight-api 注入未就绪" in warns[0].getMessage()


class TestOnLoadErrorExitBeforeHandoff:
    async def test_config_error_sets_event_and_raises(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未移交后台任务的异常出口（配置装配期抛错）→ 事件 set+清引用，异常照抛。"""
        monkeypatch.setattr(srv, "_needs_aux_provision", lambda _root: False)
        monkeypatch.setattr(srv.plugin, "get_config", MagicMock(side_effect=RuntimeError("config 炸")))
        srv._COLD_START_WAIT_S = 60.0

        load_task = asyncio.ensure_future(srv._on_load({}))
        await asyncio.sleep(0.05)
        assert load_task.done()
        exc = load_task.exception()
        assert isinstance(exc, RuntimeError)
        assert "config 炸" in str(exc)

        result = await _call(srv, "hindsight.recall", query="q")
        assert result["initialized"] is False
        assert srv._init_inflight is None
