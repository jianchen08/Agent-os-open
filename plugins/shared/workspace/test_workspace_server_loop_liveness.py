# @feature: FP-0.2.〇 工作空间文件管理 | @ci: python-coverage
"""workspace server 异步端点事件循环活性锁（合宿心跳不冻）。

事故根因（审计 A-7 / P2-2，2026-11-12）：插件 SDK ``_invoke_callable`` 只对
**同步** handler 做 ``asyncio.to_thread`` 下放，``async def`` 端点直接在合宿
宿主事件循环里执行。本插件两处同步文件操作落在 async 面内：

- ``delete_entry`` 的 ``shutil.rmtree`` 删整个目录（``node_modules``/构建产物
  十秒~分钟级）；
- ``get_file_content`` 在 ``limit is None`` 时 ``Path.read_text`` 整文件读
  （无上限，50MB 级附件秒级）。

宿主 watchdog 判主循环心跳停滞 >30s 即整组 ``os._exit(1)``（``light`` 宿主
20 成员连坐）。

锁面：目录删除与整文件读取整体出事件循环——观测指标：心跳 tick 间隔不出现
≥0.1s 空窗（假身阻塞量级 0.3s，红绿区分度充足），并核对文件操作确实发生在
宿主事件循环线程之外。

外部依赖（工作空间坐标解析）按测试纪律替换为假身；路径守卫、分页窗口、
返回体键位语义全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import sys
import threading
import time
from pathlib import Path
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


def _load_server() -> Any:
    """装载 server.py（唯一模块名，防与其它测试的裸名 server 冲突）。"""
    mod_name = "workspace_server_loop_liveness"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sdk_dir = Path(__file__).resolve().parents[3] / "sdk" / "src"
    if str(sdk_dir) not in sys.path:
        sys.path.insert(0, str(sdk_dir))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _stub_resolver(srv: Any, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """工作空间坐标解析假身（state 读面外部依赖，本锁面不涉）。"""

    async def _resolve(_task_id: str, _caller: Any = None) -> str:
        return str(workspace)

    monkeypatch.setattr(srv, "_resolve_container_path", _resolve)


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：in-loop 同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_STUB_DELAY)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁 1：delete_entry 删目录（A-7）
# ============================================================


async def test_delete_entry_rmtree_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """删除整个目录（假身 rmtree 阻塞 0.3s）期间心跳持续打点，成功报文不变。"""
    srv = _load_server()
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    (workspace / "bigdir" / "sub").mkdir(parents=True)
    _stub_resolver(srv, monkeypatch, workspace)

    rmtree_threads: list[int] = []

    def _slow_rmtree(path: Any, **_kwargs: Any) -> None:
        rmtree_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        pathlib.Path(path).as_posix()  # 触达参数，确认目录对象原样下传

    # 删除分支的 shutil 是函数内局部 import（sys.modules 单例），替身打在模块上
    # 才能同时覆盖下放前后两种实现形态；monkeypatch 用例结束即还原。
    monkeypatch.setattr(shutil, "rmtree", _slow_rmtree)

    async with _Ticker() as ticker:
        out = await srv.delete_entry("p-loop", "bigdir", caller={"sub": "user-a"})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(rmtree_threads) == 1
    assert rmtree_threads[0] != loop_thread
    assert out == {"success": True, "message": "删除成功: bigdir"}


# ============================================================
# 主锁 2：get_file_content 整文件读（P2-2）
# ============================================================


async def test_get_file_content_full_read_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """limit=None 整文件读（假身 read_text 阻塞 0.3s）期间心跳持续打点，
    内容/大小/分页键位语义不变。"""
    srv = _load_server()
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "big.txt"
    target.write_text("完整内容", encoding="utf-8")
    _stub_resolver(srv, monkeypatch, workspace)

    read_threads: list[int] = []
    slow_base: type[pathlib.Path] = type(pathlib.Path())

    class _SlowPath(slow_base):  # type: ignore[valid-type,misc]
        def read_text(self, *args: Any, **kwargs: Any) -> str:
            read_threads.append(threading.get_ident())
            time.sleep(_STUB_DELAY)
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(srv, "Path", _SlowPath)

    async with _Ticker() as ticker:
        out = await srv.get_file_content("p-loop", str(target), caller={"sub": "user-a"})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(read_threads) == 1
    assert read_threads[0] != loop_thread
    assert out["success"] is True
    assert out["content"] == "完整内容"
    assert out["size"] == len("完整内容".encode())
    assert out["loaded"] == out["size"]
    assert out["has_more"] is False


async def test_get_file_content_read_failure_still_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """下放不改变异常语义：读取异常仍归为显式 success=False（含原因）。"""
    srv = _load_server()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "boom.txt"
    target.write_text("x", encoding="utf-8")
    _stub_resolver(srv, monkeypatch, workspace)

    slow_base: type[pathlib.Path] = type(pathlib.Path())

    class _BrokenPath(slow_base):  # type: ignore[valid-type,misc]
        def read_text(self, *_args: Any, **_kwargs: Any) -> str:
            raise OSError("设备不可读")

    monkeypatch.setattr(srv, "Path", _BrokenPath)

    out = await srv.get_file_content("p-loop", str(target), caller={"sub": "user-a"})

    assert out["success"] is False
    assert "读取文件失败" in out["message"]


# ============================================================
# 同类扫描补位：offset>0 的尾部读（同属无上限读）与 move_entry 的目录移动
# ============================================================


async def test_get_file_content_tail_read_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """offset>0 且 limit=None（读到文件尾，无上限）同样出事件循环；
    窗口起点/loaded 语义不变。"""
    srv = _load_server()
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "tail.txt"
    target.write_text("0123456789", encoding="utf-8")
    _stub_resolver(srv, monkeypatch, workspace)

    read_threads: list[int] = []
    real_read_to_end = srv._read_to_end

    def _slow_read_to_end(full_path: Any, start: int) -> str:
        read_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        return real_read_to_end(full_path, start)

    monkeypatch.setattr(srv, "_read_to_end", _slow_read_to_end)

    async with _Ticker() as ticker:
        out = await srv.get_file_content(
            "p-loop", str(target), caller={"sub": "user-a"}, offset=4
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(read_threads) == 1
    assert read_threads[0] != loop_thread
    assert out["content"] == "456789"
    assert out["loaded"] == 10
    assert out["has_more"] is False


async def test_move_entry_directory_move_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """move_entry 目录移动（跨卷形态 = 整树复制+删除，假身阻塞 0.3s）期间心跳
    持续打点，移动结果报文不变。"""
    srv = _load_server()
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    (workspace / "src" / "sub").mkdir(parents=True)
    (workspace / "src" / "sub" / "a.txt").write_text("A", encoding="utf-8")
    (workspace / "dest").mkdir()
    _stub_resolver(srv, monkeypatch, workspace)

    move_threads: list[int] = []
    real_move = shutil.move

    def _slow_move(src: Any, dst: Any) -> Any:
        move_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        return real_move(src, dst)

    # 移动分支的 shutil 是函数内局部 import（sys.modules 单例），替身打在模块上。
    monkeypatch.setattr(shutil, "move", _slow_move)

    body = {"source_path": "src", "destination_dir": "dest"}
    async with _Ticker() as ticker:
        out = await srv.move_entry("p-loop", body, caller={"sub": "user-a"})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(move_threads) == 1
    assert move_threads[0] != loop_thread
    assert out["success"] is True
    assert out["destination_path"] == str(Path("dest") / "src")
    assert (workspace / "dest" / "src" / "sub" / "a.txt").read_text(encoding="utf-8") == "A"
