# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""_repair_host_mount 重挂主体全覆盖（簇 E 立案缺陷修复后的行为锁定）。

历史缺陷（2026-09-15 簇 E 穷举立案，2026-09-16 修复）：守卫原以宿主
``PurePath`` 解析 WSL 侧路径——Windows 宿主 parts[0] 恒为 ``\\``、POSIX
宿主恒为 ``/``，``== "/mnt"`` 两种宿主都恒假，修复体在任何输入下不可达
（EIO 自愈静默失效）。修复 = WSL 路径按 POSIX 语法显式解析
（``PurePosixPath``，纯路径无 fs 访问），修复体按 docstring 设计在
Windows 宿主（经 wsl.exe 派发）真实可达，无需任何注入即可测。

行为契约（可观测输入→输出）：
- ``/mnt/<x>/...`` 形态 → 派发 umount -l + mount -t drvfs <X>: /mnt/<x> 的
  wsl.exe 修复命令，且以 ``ls -ld`` 验证重挂结果：验证通过才返回 True；
- 验证回显 EIO / 修复派发异常 → False（吞异常留 warning，调用方兜底重建容器）；
- 修复命令失败不阻断验证（验证是唯一权威）；
- 非 WSL 形态（Windows 盘符路径 / 单段）→ 守卫告警跳过，零派发。

外部依赖（wsl.exe 子进程）以桩替代：断言收到的 argv 命令串与返回值传播，
不真跑 WSL。
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_manager() -> Any:
    """动态加载 manager（唯一模块名，防裸名串扰——同目录既有测试同款）。"""
    name = "isolation_host_mount_repair_test"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "manager.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mgr() -> Any:
    """绕开构造器依赖的裸实例：_repair_host_mount 是无 self 状态的纯派发逻辑。"""
    mod = _load_manager()
    return mod.IsolationManager.__new__(mod.IsolationManager)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


class TestRepairHostMount:
    @pytest.mark.parametrize("letter", ["d", "e"])
    def test_wsl_shape_dispatches_repair_and_verifies(
        self, monkeypatch: pytest.MonkeyPatch, letter: str
    ) -> None:
        """/mnt/<x>/... → umount+drvfs 修复 + ls 验证，验证通过返回 True。"""
        mgr = _mgr()
        calls: list[list[str]] = []

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            calls.append(list(args))
            return _FakeCompleted(returncode=0, stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok = _run(mgr._repair_host_mount(f"/mnt/{letter}/workspaces/t1", "env-1"))

        assert ok is True
        assert len(calls) == 2
        repair_cmd = " ".join(calls[0])
        # 盘符双向解析：/mnt/e → drvfs 挂 E: 到 /mnt/e；lazy umount 防卡死
        assert f"umount -l /mnt/{letter}" in repair_cmd
        assert f"mount -t drvfs {letter.upper()}: /mnt/{letter}" in repair_cmd
        assert calls[0][:5] == ["wsl.exe", "-d", "Ubuntu", "-u", "root"]
        # 验证以 ls -ld 探活同一挂载点
        assert f"ls -ld /mnt/{letter}" in " ".join(calls[1])

    def test_verify_authority_repair_failure_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """修复命令 rc!=0 不阻断验证——验证回显干净即认定重挂成功。"""
        mgr = _mgr()
        codes = iter([1, 0])

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            return _FakeCompleted(returncode=next(codes), stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _run(mgr._repair_host_mount("/mnt/d/ws", "env-1")) is True

    def test_verify_reports_eio_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证回显 input/output error → 9p 仍坏，返回 False 不谎报。"""
        mgr = _mgr()

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            return _FakeCompleted(
                returncode=0, stderr=b"ls: /mnt/d: input/output error"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _run(mgr._repair_host_mount("/mnt/d/ws", "env-1")) is False

    def test_dispatch_exception_swallowed_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """派发异常（wsl.exe 卡死超时等）→ 吞异常留 warning，返回 False。"""
        mgr = _mgr()

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd=args, timeout=25)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _run(mgr._repair_host_mount("/mnt/d/ws", "env-1")) is False

    def test_windows_drive_shape_guard_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows 盘符路径按 POSIX 解析成单段 → 守卫告警跳过，零派发。"""
        mgr = _mgr()
        dispatched: list[Any] = []

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            dispatched.append(args)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _run(mgr._repair_host_mount(r"D:\ws\t1", "env-win")) is False
        assert dispatched == []

    def test_short_wsl_shape_guard_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺盘符段的 WSL 根（/mnt）同样守卫跳过——无盘可修。"""
        mgr = _mgr()
        assert _run(mgr._repair_host_mount("/mnt", "env-1")) is False

    def test_empty_workspace_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """workspace 为空 → 守卫告警返回 False，零派发。"""
        mgr = _mgr()
        dispatched: list[Any] = []

        def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
            dispatched.append(args)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _run(mgr._repair_host_mount(None, "env-1")) is False
        assert dispatched == []
