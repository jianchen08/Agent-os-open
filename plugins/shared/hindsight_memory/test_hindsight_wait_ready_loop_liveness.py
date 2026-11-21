# @feature: FP-0.2.六 记忆检索(BUG-63 冷启动窗) | @ci: python-coverage
"""hindsight-api 就绪轮询的事件循环活性锁（异步审计 P2-6）。

根因：``_wait_api_ready``（async）在轮询循环里直接 ``urllib.request.urlopen
(timeout=2)``——单次 ≤2s 且循环内有 ``await asyncio.sleep(1)`` 让出，不构成
>30s 单点，但每次探测都让整个循环同步停摆（独占进程插件不连坐，降一档）。

锁面（断行为）：
- 健康探针出事件循环：假身 urlopen 同步阻塞 0.25s，心跳间隔不得出现 ≥0.1s
  空窗；假身线程 ≠ 事件循环线程；就绪后返回（不改变循环语义）；
- 失败面不变：进程已退出 → 带 stderr tail 的 RuntimeError；进程仍活 →
  继续轮询到窗口耗尽才报「未就绪」。

``subprocess.Popen``（``_start_api_server``）不阻塞，不在本锁面。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402

_MODULE_NAME = "hindsight_server_wait_ready_loop_liveness"


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        del args


def _alive_process() -> Any:
    """子进程替身：仍在跑（poll=None）——轮询循环继续。"""
    return SimpleNamespace(poll=lambda: None, returncode=None)


def _exited_process(code: int = 3) -> Any:
    return SimpleNamespace(poll=lambda: code, returncode=code)


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：健康探针出事件循环
# ============================================================


async def test_wait_api_ready_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """urlopen 0.25s 阻塞期间心跳持续打点；就绪即返回。"""
    module = _load_module()
    threads: list[int] = []

    def _slow_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        del url, timeout
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", _slow_urlopen)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        await module._wait_api_ready("http://127.0.0.1:1", _alive_process(), "unused.log")

    assert ticker.max_gap < MAX_TICK_GAP
    assert len(threads) == 1
    assert threads[0] != loop_thread, "健康探针必须在事件循环线程之外"


async def test_wait_api_ready_exited_process_reports_stderr_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程已退出 → RuntimeError 带 stderr tail（失败面语义不变）。"""
    module = _load_module()
    stderr_log = tmp_path / "stderr.log"
    stderr_log.write_text("boom: pg0 init failed\n", encoding="utf-8")

    def _refuse(url: str, timeout: float | None = None) -> _FakeResponse:
        del url, timeout
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    with pytest.raises(RuntimeError) as ei:
        await module._wait_api_ready(
            "http://127.0.0.1:1", _exited_process(3), str(stderr_log)
        )

    assert "子进程已退出 code=3" in str(ei.value)
    assert "pg0 init failed" in str(ei.value)


async def test_wait_api_ready_alive_process_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程仍活但探针持续失败 → 轮询到窗口耗尽报「未就绪」（不提前放弃）。"""
    module = _load_module()
    monkeypatch.setattr(module, "_API_READY_WAIT_S", 2)
    attempts: list[int] = []

    def _refuse(url: str, timeout: float | None = None) -> _FakeResponse:
        del url, timeout
        attempts.append(1)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    with pytest.raises(RuntimeError) as ei:
        await module._wait_api_ready("http://127.0.0.1:1", _alive_process(), "unused.log")

    assert "内未就绪" in str(ei.value)
    assert len(attempts) == 2
