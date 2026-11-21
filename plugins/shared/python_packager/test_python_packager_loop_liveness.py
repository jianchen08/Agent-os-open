# @feature: FP-0.2.一 第三方插件协议·python_packager 装载 | @ci: python-coverage
"""python_packager 事件循环活性锁（异步审计 P2-4）。

根因：``install`` / ``resolve_dependencies`` / ``status`` 都是 async handler，
却在宿主事件循环里直接调同步 ``_run``（``subprocess.run(uv …, timeout=300)``）；
``uninstall`` 同位置直接 ``shutil.rmtree`` 整删 venv。独占进程插件（无
``host_group``）虽无 watchdog 连坐，但 uv 解析/装包期间整循环停摆（最坏 300s）。

锁面（断行为）：
- uv 子进程与 venv 删除都出事件循环：假身 uv 同步阻塞 0.25s、假身 rmtree
  0.25s，心跳间隔不得出现 ≥0.1s 空窗；假身线程 ≠ 事件循环线程；
- 语义不变：成功信封（venv/lock/declared_dependencies）、非零退出的
  fail-closed 错误文本、uninstall 的 removed 清单。

uv 属外部依赖边界（真实子进程按假身替换）；文件 IO 一律真实临时目录。
"""

from __future__ import annotations

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
_SHARED_DIR = _PLUGIN_DIR.parent
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402

_MODULE_NAME = "python_packager_server_loop_liveness"

_PYPROJECT_NO_DEPS = '[project]\nname = "demo-pkg"\nversion = "0.0.1"\n'


def _load_module() -> Any:
    """动态加载 server.py（独立模块名，避免与其他测试文件装载互相覆盖）。"""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """加载插件模块；包目录白名单经 env 放行（临时区非插件树内）。"""
    monkeypatch.setenv("PYTHON_PACKAGER_ALLOWED_DIRS", str(tmp_path))
    return _load_module()


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    """含 pyproject 的包目录（无声明依赖 = 不走 uv 可用性预检）。"""
    directory = tmp_path / "demo_pkg"
    directory.mkdir()
    (directory / "pyproject.toml").write_text(_PYPROJECT_NO_DEPS, encoding="utf-8")
    return directory


def _slow_uv_run(threads: list[int]):
    """假身 uv 子进程：同步阻塞 0.25s（uv 解析装包量级的最小可判别替身）。"""

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        del kwargs
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} ok", stderr="")

    return _run


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：uv 子进程与 venv 删除出事件循环
# ============================================================


async def test_install_keeps_event_loop_ticking(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv sync 0.25s 阻塞期间心跳持续打点；返回信封语义照常。"""
    threads: list[int] = []
    monkeypatch.setattr(server.subprocess, "run", _slow_uv_run(threads))
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        result = await server.install(str(pkg))

    assert ticker.max_gap < MAX_TICK_GAP
    assert result["ok"] is True
    assert result["venv"] == str(pkg / ".venv")
    assert result["lock"] == str(pkg / "uv.lock")
    assert result["declared_dependencies"] == []
    assert len(threads) == 1
    assert threads[0] != loop_thread, "uv 子进程必须在事件循环线程之外"


async def test_resolve_dependencies_keeps_event_loop_ticking(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已装态（.venv+uv.lock 就绪）的幂等 uv sync 期间心跳持续打点。"""
    (pkg / ".venv").mkdir()
    (pkg / "uv.lock").write_text("", encoding="utf-8")
    threads: list[int] = []
    monkeypatch.setattr(server.subprocess, "run", _slow_uv_run(threads))
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        result = await server.resolve_dependencies(str(pkg))

    assert ticker.max_gap < MAX_TICK_GAP
    assert result == {"ok": True, "satisfied": [], "missing": [], "resolved": True}
    assert threads
    assert all(tid != loop_thread for tid in threads)


async def test_status_keeps_event_loop_ticking(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv --version 期间心跳持续打点；版本串取自子进程 stdout。"""
    threads: list[int] = []
    monkeypatch.setattr(server.subprocess, "run", _slow_uv_run(threads))
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        result = await server.status(str(pkg))

    assert ticker.max_gap < MAX_TICK_GAP
    assert result["ok"] is True
    assert result["uv_available"] is True
    assert result["uv_version"] == "uv ok"
    assert threads
    assert all(tid != loop_thread for tid in threads)


async def test_uninstall_keeps_event_loop_ticking(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删 .venv（rmtree 0.25s）期间心跳持续打点；removed 清单语义照常。"""
    venv = pkg / ".venv"
    (venv / "site-packages" / "demo").mkdir(parents=True)
    (venv / "site-packages" / "demo" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    lock = pkg / "uv.lock"
    lock.write_text("", encoding="utf-8")
    threads: list[int] = []
    real_rmtree = shutil.rmtree

    def _slow_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        threads.append(threading.get_ident())
        time.sleep(0.25)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(server.shutil, "rmtree", _slow_rmtree)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        result = await server.uninstall(str(pkg))

    assert ticker.max_gap < MAX_TICK_GAP
    assert result["ok"] is True
    assert result["removed"] == [str(venv), str(lock)]
    assert not venv.exists()
    assert not lock.exists()
    assert len(threads) == 1
    assert threads[0] != loop_thread, "venv 删除必须在事件循环线程之外"


# ============================================================
# 失败闭面不变：异常仍 fail-closed 返回（下放不丢语义）
# ============================================================


async def test_install_non_zero_exit_keeps_fail_closed(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 非零退出 → {ok:false, error:stderr}（下放不改变 fail-closed 语义）。"""

    def _fail(cmd: list[str], **kwargs: Any) -> Any:
        del cmd, kwargs
        return SimpleNamespace(returncode=2, stdout="", stderr="resolution failed\n")

    monkeypatch.setattr(server.subprocess, "run", _fail)
    result = await server.install(str(pkg))

    assert result == {"ok": False, "rc": 2, "error": "resolution failed"}


async def test_install_missing_uv_keeps_fail_closed(
    server: Any, pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv 不存在 → {ok:false} 带可诊断错误（FileNotFoundError 分支不被下放吞掉）。"""
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "demo-pkg"\nversion = "0.0.1"\ndependencies = ["requests"]\n',
        encoding="utf-8",
    )

    def _missing(cmd: list[str], **kwargs: Any) -> Any:
        del cmd, kwargs
        raise FileNotFoundError("uv")

    monkeypatch.setattr(server.subprocess, "run", _missing)
    monkeypatch.setattr(server.shutil, "which", lambda _name: "/usr/bin/uv")
    result = await server.install(str(pkg))

    assert result["ok"] is False
    assert "找不到可执行文件" in result["error"]
