# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 皮肤目录 IO 事件循环活性锁（异步审计 P2-5）。

根因：``_post_install``（装后皮肤同步）、``_reconcile_skins``（启停皮肤目录
同步/回收）、``uninstall``（卸载皮肤目录回收）都挂在 async handler 调用链上，
却在事件循环里直接 ``shutil.copytree`` / ``shutil.rmtree``——皮肤目录体量随包
增长，复制/删除期间整个循环停摆（独占进程插件不连坐，但管理页轮询与审批
通道同循环，冻结一次就是一次管理面无响应）。

锁面（断行为）：
- 三处皮肤目录 IO 均出事件循环：假身 copytree/rmtree 同步阻塞 0.25s，心跳
  间隔不得出现 ≥0.1s 空窗；假身线程 ≠ 事件循环线程；
- 语义不变：装后 skins 落状态、禁用回收 / 启用回同步、卸载清皮肤目录。

另一半（市场索引默认抓取器）已是 ``asyncio.to_thread`` 形态——本文件补回归锁
防退化：审计 P2-5 的 ``market_index.py:107 urlopen`` 命中其实是同步内层函数
被 ``to_thread`` 包住的 AST 假阳性。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import manager as mgr  # noqa: E402
import market_index as mi  # noqa: E402

# 同插件既有测试的桩件与夹具（单仓单源，禁复制漂移）
import test_manager as tm  # noqa: E402
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402

_SKIN = "mint-test"


def _slow_copy_op(threads: list[int]):
    """假身皮肤目录 IO：同步阻塞 0.25s 后调用真实实现（语义不替身）。"""
    real_copytree = shutil.copytree
    real_rmtree = shutil.rmtree

    def _copytree(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return real_copytree(src, dst, *args, **kwargs)

    def _rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        threads.append(threading.get_ident())
        time.sleep(0.25)
        real_rmtree(path, *args, **kwargs)

    return _copytree, _rmtree


def _seed_installed_skin(m: Any, tmp_path: Path, pkg: str = "skin-pkg") -> Path:
    """工作区已装包 + 状态记 skins + 皮肤目录已同步（启用态基线）。"""
    ws = Path(m.runtime_root)
    pkg_dir = tm._mk_workspace_pkg(ws, pkg, pkg, "1.0.0", skins=[_SKIN], blob="")
    skins_dir = Path(m.skins_dir)
    real_copytree = shutil.copytree
    real_copytree(pkg_dir / "skins" / _SKIN, skins_dir / _SKIN)
    mgr.mark_package_state(ws, pkg, skins=[_SKIN], status="installed")
    return pkg_dir


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：三处皮肤目录 IO 出事件循环
# ============================================================


async def test_install_skin_sync_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批通过后的装后皮肤同步（copytree 0.25s）期间心跳持续打点。

    观测面 = 审计坐标函数 ``_approve_then_install``（审批 → 官方 installBundle
    → ``_post_install`` → 皮肤同步）；预览构建不在本锁面内。
    """
    approval = tm.ApprovalStub(decision="approve")
    m, _stub = tm._make_manager(tmp_path, approval=approval)
    ws = Path(m.runtime_root)
    tm._mk_workspace_pkg(ws, "ok-pkg", "ok-pkg", "1.0.0", skins=[_SKIN], blob="")
    local = str(tm._local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0", skins=[_SKIN]))
    threads: list[int] = []
    slow_copytree, slow_rmtree = _slow_copy_op(threads)
    monkeypatch.setattr(mgr.shutil, "copytree", slow_copytree)
    monkeypatch.setattr(mgr.shutil, "rmtree", slow_rmtree)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        await m._approve_then_install(  # noqa: SLF001 — 审计坐标函数本身
            ws, "ok-pkg", local, {"spec": local, "method": "translator-dry-run"}
        )
        await m.wait_background()  # 裁决续行（皮肤同步在背景收尾里）

    assert ticker.max_gap < MAX_TICK_GAP
    assert threads
    assert all(tid != loop_thread for tid in threads), "皮肤复制必须在事件循环线程之外"
    assert (Path(m.skins_dir) / _SKIN / "skin.json").is_file()
    state = mgr.package_state(ws, "ok-pkg")
    assert state["status"] == "installed"
    assert state["skins"] == [_SKIN]


async def test_disable_skin_cleanup_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """禁用回收皮肤目录（rmtree 0.25s）期间心跳持续打点；目录确实被删。"""
    m, _stub = tm._make_manager(tmp_path)
    _seed_installed_skin(m, tmp_path)
    threads: list[int] = []
    slow_copytree, slow_rmtree = _slow_copy_op(threads)
    monkeypatch.setattr(mgr.shutil, "copytree", slow_copytree)
    monkeypatch.setattr(mgr.shutil, "rmtree", slow_rmtree)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        out = await m.disable("skin-pkg")

    assert ticker.max_gap < MAX_TICK_GAP
    assert out["enabled"] is False
    assert not (Path(m.skins_dir) / _SKIN).exists()
    assert threads
    assert all(tid != loop_thread for tid in threads)


async def test_enable_skin_sync_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启用回同步皮肤目录（copytree 0.25s）期间心跳持续打点；目录确实回来。"""
    m, _stub = tm._make_manager(tmp_path)
    _seed_installed_skin(m, tmp_path)
    real_rmtree = shutil.rmtree
    real_rmtree(Path(m.skins_dir) / _SKIN)  # 先落禁用态（皮肤目录不存在）
    threads: list[int] = []
    slow_copytree, slow_rmtree = _slow_copy_op(threads)
    monkeypatch.setattr(mgr.shutil, "copytree", slow_copytree)
    monkeypatch.setattr(mgr.shutil, "rmtree", slow_rmtree)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        out = await m.enable("skin-pkg")

    assert ticker.max_gap < MAX_TICK_GAP
    assert out["enabled"] is True
    assert (Path(m.skins_dir) / _SKIN / "skin.json").is_file()
    assert threads
    assert all(tid != loop_thread for tid in threads)


async def test_enable_skin_resync_overwrites_stale_copy(tmp_path: Path) -> None:
    """启用回同步覆盖陈旧皮肤副本（旧目录先删再复制，M2「即现」语义）。"""
    m, _stub = tm._make_manager(tmp_path)
    _seed_installed_skin(m, tmp_path)
    stale = Path(m.skins_dir) / _SKIN / "skin.css"
    stale.write_text("/* stale copy */\n", encoding="utf-8")

    out = await m.enable("skin-pkg")

    assert out["enabled"] is True
    assert "stale copy" not in stale.read_text(encoding="utf-8")
    assert (Path(m.skins_dir) / _SKIN / "skin.json").is_file()


async def test_uninstall_skin_cleanup_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """卸载回收皮肤目录（rmtree 0.25s）期间心跳持续打点；状态与目录同清。"""
    m, _stub = tm._make_manager(tmp_path)
    _seed_installed_skin(m, tmp_path)
    threads: list[int] = []
    slow_copytree, slow_rmtree = _slow_copy_op(threads)
    monkeypatch.setattr(mgr.shutil, "copytree", slow_copytree)
    monkeypatch.setattr(mgr.shutil, "rmtree", slow_rmtree)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        out = await m.uninstall("skin-pkg")

    assert ticker.max_gap < MAX_TICK_GAP
    assert out["ok"] is True
    assert not (Path(m.skins_dir) / _SKIN).exists()
    assert mgr.package_state(Path(m.runtime_root), "skin-pkg") == {}
    assert threads
    assert all(tid != loop_thread for tid in threads)


# ============================================================
# 回归锁：市场索引默认抓取器（urllib.urlopen）出事件循环
# ============================================================


class _FakeUrlResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeUrlResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        del args

    def read(self) -> bytes:
        return self._payload


async def test_market_default_fetcher_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 fetcher 的 urlopen 0.25s 阻塞期间心跳持续打点；payload 原样返回。"""
    threads: list[int] = []
    payload = json.dumps({"plugins": [{"name": "pkg-a", "versions": []}]}).encode("utf-8")

    def _slow_urlopen(req: Any, timeout: float | None = None) -> _FakeUrlResponse:
        del req, timeout
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return _FakeUrlResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _slow_urlopen)
    fetch = mi._default_fetcher()  # noqa: SLF001 — 被锁对象即默认实现本身
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        got = await fetch("https://example.invalid/index.json", 5.0)

    assert ticker.max_gap < MAX_TICK_GAP
    assert got == payload
    assert len(threads) == 1
    assert threads[0] != loop_thread
