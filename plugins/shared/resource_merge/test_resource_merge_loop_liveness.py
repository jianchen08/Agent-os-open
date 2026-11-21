# @feature: FP-0.2.五 资源治理 | @ci: python-coverage
"""resource_merge 事件循环活性锁（合宿心跳不冻）。

事故根因（审计 A-6，2026-11-12）：插件 SDK ``_invoke_callable`` 只对**同步**
handler 做 ``asyncio.to_thread`` 下放，本工具的 ``execute`` 是 ``async def``，
merge/cleanup 主路径上的同步文件操作直接跑在合宿宿主事件循环里：

- merge（copy 策略）：``_scan_workspace_files`` 全树 ``rglob`` + 逐文件
  ``shutil.copy2``——worktree 全树（含 ``node_modules`` 类目录）时分钟级；
- cleanup：``shutil.rmtree`` 整个 worktree（git worktree remove 失败后的
  手动兜底与 ``.git`` 目录删除）——同为分钟级。

宿主 watchdog 判主循环心跳停滞 >30s 即整组 ``os._exit(1)``（``light`` 宿主
20 成员连坐）。

锁面：扫描、逐文件复制、目录删除三处文件操作整体出事件循环——观测指标：
心跳 tick 间隔不出现 ≥0.1s 空窗（假身阻塞量级 0.3s，红绿区分度充足）。

外部依赖（git 命令）按测试纪律替换为假身；合并分派、变更报告分类、清理
失败留痕语义全真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
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


def _load_tool() -> Any:
    """按文件路径加载 tool.py（唯一模块名，防与其它测试的裸名 tool 冲突）。"""
    mod_name = "resource_merge_tool_loop_liveness"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _GitStub:
    """GitHelpers 假身：仅提供本锁面用到的两个协程（execute 的分派表会取全部
    直通方法，故其余方法以显式拒绝占位——被实际调用即暴露）。"""

    def __init__(self, is_worktree: bool, remove_returncode: int = 1) -> None:
        self._is_worktree = is_worktree
        self._remove_returncode = remove_returncode
        self.git_calls: list[tuple[str, ...]] = []

    async def is_worktree(self, _workspace: Path) -> bool:
        return self._is_worktree

    async def run_git(self, *args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        self.git_calls.append(tuple(str(a) for a in args))
        if args and args[0] == "worktree":
            return self._remove_returncode, "", "worktree remove 失败（假身）"
        return 0, "", ""

    async def _unused(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("直通 git 操作不应在本锁面被调用")

    git_status = _unused
    git_commit = _unused
    git_diff = _unused
    git_log = _unused
    git_merge_abort = _unused


def _tool(tmp_path: Path, git: _GitStub) -> tuple[Any, Any]:
    """装载模块并构造被测工具（返回 (模块, 工具)——模块级 shutil 打桩须同源）。"""
    tool_mod = _load_tool()
    tool = tool_mod.ResourceMergeTool(str(tmp_path / "base"))
    tool._git_helpers = git
    return tool_mod, tool


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：in-loop 同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_STUB_DELAY)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁 1：merge 的全树扫描（直接复制模式）
# ============================================================


async def test_merge_workspace_scan_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非 worktree 且无 target_files → 全树扫描（假身阻塞 0.3s）期间心跳持续
    打点，扫描到的文件照常复制并计入 added 报告。"""
    loop_thread = threading.get_ident()
    _tool_mod, tool = _tool(tmp_path, _GitStub(is_worktree=False))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("payload", encoding="utf-8")
    scan_threads: list[int] = []

    def _slow_scan(_workspace: Path) -> list[str]:
        scan_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        return ["a.txt"]

    monkeypatch.setattr(tool, "_scan_workspace_files", _slow_scan)

    out_dir = tmp_path / "out"
    async with _Ticker() as ticker:
        result = await tool.execute(
            {"action": "merge", "workspace": str(workspace), "target_dir": str(out_dir)}
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert scan_threads[0] != loop_thread
    assert result.error is None
    assert result.output["merged_files"] == ["a.txt"]
    assert result.output["change_report"]["added"] == ["a.txt"]
    assert (out_dir / "a.txt").read_text(encoding="utf-8") == "payload"


# ============================================================
# 主锁 2：merge 的逐文件 copy2（含覆盖/删除分类）
# ============================================================


async def test_merge_file_copy_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """target_files 直给 → 逐文件 copy2（假身阻塞 0.3s）期间心跳持续打点，
    覆盖/新增/删除三类分类结果不变。"""
    tool_mod, tool = _tool(tmp_path, _GitStub(is_worktree=False))
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "new.txt").write_text("new", encoding="utf-8")
    (workspace / "mod.txt").write_text("mod", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "mod.txt").write_text("old", encoding="utf-8")
    (out_dir / "gone.txt").write_text("gone", encoding="utf-8")

    copy_threads: list[int] = []
    real_copy2 = shutil.copy2

    def _slow_copy2(src: Any, dst: Any) -> Any:
        copy_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        return real_copy2(src, dst)

    monkeypatch.setattr(
        tool_mod, "shutil", SimpleNamespace(copy2=_slow_copy2, rmtree=shutil.rmtree)
    )

    async with _Ticker() as ticker:
        result = await tool.execute(
            {
                "action": "merge",
                "workspace": str(workspace),
                "target_dir": str(out_dir),
                "target_files": ["new.txt", "mod.txt", "gone.txt"],
            }
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert copy_threads
    assert all(tid != loop_thread for tid in copy_threads)
    assert result.error is None
    assert sorted(result.output["merged_files"]) == ["mod.txt", "new.txt"]
    report = result.output["change_report"]
    assert report["added"] == ["new.txt"]
    assert report["modified"] == ["mod.txt"]
    assert report["deleted"] == ["gone.txt"]
    assert (out_dir / "new.txt").read_text(encoding="utf-8") == "new"
    assert (out_dir / "mod.txt").read_text(encoding="utf-8") == "mod"


# ============================================================
# 主锁 3：cleanup 的 rmtree（worktree remove 失败后的手动兜底）
# ============================================================


async def test_cleanup_rmtree_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """worktree remove 失败 → 手动 rmtree（假身阻塞 0.3s）期间心跳持续打点，
    清理失败的如实标注语义不变。"""
    tool_mod, tool = _tool(tmp_path, _GitStub(is_worktree=True))
    loop_thread = threading.get_ident()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rmtree_threads: list[int] = []

    def _failing_rmtree(_path: Any, onerror: Any = None, **_kwargs: Any) -> None:
        assert onerror is not None  # 只读文件兜底回调随调用一起下放
        rmtree_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        raise OSError("删除被拒绝（假身）")

    monkeypatch.setattr(
        tool_mod, "shutil", SimpleNamespace(copy2=shutil.copy2, rmtree=_failing_rmtree)
    )

    async with _Ticker() as ticker:
        result = await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(rmtree_threads) == 1
    assert rmtree_threads[0] != loop_thread
    assert result.error is None
    # 删除失败如实标注（不谎报"已清理"），异常不逸出
    assert "worktree 删除失败" in result.output["cleanup_failed"][0]
    assert result.output["message"].startswith("清理部分失败")
    assert result.output["branch_name"] == "task/ws"
