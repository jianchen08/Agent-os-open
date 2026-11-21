# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
"""isolation.checkpoint 事件循环活性锁（合宿心跳不冻）。

事故根因（审计 A-2，2026-11-12）：插件 SDK ``_invoke_callable`` 只对**同步**
handler 做 ``asyncio.to_thread`` 下放，``async def`` handler 直接在合宿宿主
事件循环里执行。``isolation_checkpoint`` 的 create/restore/cleanup 三个分支
同步调用 ``CheckpointManager``：create 走 ``_collect_workspace_files`` 全树
``rglob`` + ``_backup_one_file`` 逐文件 ``copy2``，cleanup 走 ``rmtree``——
工作量与工作空间体量线性（未命中 ignore 规则时 ``node_modules`` 级目录可达
十万级文件）→ 分钟级阻塞。

宿主 watchdog 判主循环心跳停滞 >30s 即整组 ``os._exit(1)``；``isolation`` 在
``light_stable`` 宿主与 ``task``/``tasks`` 同宿，阻塞即任务管理面全断。

锁面：三个分支的管理器调用整体出事件循环——观测指标：心跳 tick 间隔不出现
≥0.1s 空窗（假身阻塞量级 0.3s，红绿区分度充足），并核对管理器调用确实发生在
宿主事件循环线程之外。

外部依赖（工作空间全树扫描/复制）按测试纪律替换为假身；分支判定、参数校验、
返回载荷与「管理器未初始化」早退语义全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.3s 必破此界。
_MAX_TICK_GAP = 0.1

_STUB_DELAY = 0.3


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻。

    末段必须计入：阻塞期间停摆的心跳在收尾时无法补点（stop 已置位），
    只比 tick 与 tick 的间隔会把整段停滞漏判成 0。
    """

    def __init__(self, interval: float = 0.01) -> None:
        self._interval = interval
        self._ticks: list[float] = []
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._end: float = 0.0

    async def __aenter__(self) -> _Ticker:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        # 预热：让至少两 tick 落在被测调用之前（阻塞段前有基线 tick）
        await asyncio.sleep(self._interval * 3)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        assert self._stop is not None
        assert self._task is not None
        self._end = time.monotonic()
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            self._ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    @property
    def max_gap(self) -> float:
        points = [*self._ticks, self._end]
        return max(
            (b - a for a, b in zip(points, points[1:], strict=False)), default=0.0
        )


def _load_mod() -> Any:
    """动态加载 server.py（唯一模块名，防与其它测试的裸名 server 冲突）。"""
    for _bare in ("server",):
        sys.modules.pop(_bare, None)
    mod_name = "isolation_server_loop_liveness_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


class _SlowCheckpointManager:
    """检查点管理器假身：三动作同步阻塞 delay 秒并记录调用线程。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.threads: dict[str, int] = {}

    def create_checkpoint(
        self, task_id: str, workspace: str, files_to_backup: list[str] | None = None
    ) -> Any:
        self.threads["create"] = threading.get_ident()
        time.sleep(self.delay)
        return SimpleNamespace(task_id=task_id, files=[], workspace=workspace)

    def restore_checkpoint(self, task_id: str) -> bool:
        self.threads["restore"] = threading.get_ident()
        time.sleep(self.delay)
        return True

    def cleanup_checkpoint(self, task_id: str) -> bool:
        self.threads["cleanup"] = threading.get_ident()
        time.sleep(self.delay)
        return True


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：in-loop 同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_STUB_DELAY)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：create / restore / cleanup 三动作不冻事件循环
# ============================================================


@pytest.mark.parametrize(
    ("action", "extra_kwargs", "expected"),
    [
        ("create", {"workspace": "task-ws"}, {"created": True}),
        ("restore", {}, {"restored": True}),
        ("cleanup", {}, {"cleaned": True}),
    ],
)
async def test_checkpoint_actions_keep_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    extra_kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """管理器动作（假身阻塞 0.3s）期间心跳持续打点，返回载荷语义不变。"""
    loop_thread = threading.get_ident()
    fake_mgr = _SlowCheckpointManager(delay=_STUB_DELAY)
    monkeypatch.setattr(_MOD, "_checkpoint_mgr", fake_mgr)

    async with _Ticker() as ticker:
        out = await _MOD.isolation_checkpoint(
            action=action, task_id="t-liveness", **extra_kwargs
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert fake_mgr.threads[action] != loop_thread
    for key, value in expected.items():
        assert out[key] is value


async def test_checkpoint_uninitialised_manager_still_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理器未初始化早退语义不变（不触线程池、不触磁盘）。"""
    monkeypatch.setattr(_MOD, "_checkpoint_mgr", None)

    out = await _MOD.isolation_checkpoint(action="create", task_id="t", workspace="ws")

    assert out == {"error": "检查点管理器未初始化"}
