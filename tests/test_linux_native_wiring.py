# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""linux_native 接线测试：manager 槽位选择/委托/跳过 docker 清点 + guard
装载平台门/探测分派/引擎自愈门。

锁定契约（ADR 2026-10-12：Linux 默认后端替换）：
- manager：linux_native enabled 默认 true——Linux 上 CONTAINER 槽位默认换装
  LinuxNativeProvider（零配置生效）；显式 enabled=false 回 Docker（opt-out）；
  非 Linux 平台不参与竞争（nt 上 wsl 优先、macOS 落 Docker）；
  _find_existing_container / _destroy_container_by_name 委托 provider（D6
  语义单点在 provider）；_resume_containers/_stop_containers/_prune_docker_images
  原生后端早退（无常驻资源可清点）。
- guard：_load_linux_native_config 装载出口平台门单点归一（非 Linux 收敛为
  显式关闭形态）；_probe_backend 分派（Linux 默认探 bwrap）；引擎自愈门
  （原生后端跳过 ensure_docker_engine）。
- 平台门钉法：manager/guard 的 ``_is_linux_platform`` 为模块级函数，模块内
  patch（无 pathlib 全局投毒面，区别于 os.name 钉法）；wsl 门仍走 os.name，
  涉及 wsl 的用例按实际平台分支断言（真 posix 钉 os.name 会全局投毒
  pathlib，test_wsl_native_wiring 有真机实证）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests._isolation_path  # noqa: F401
from agentos_plugin_sdk.isolation_types import IsolationLevel

_MANAGER_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "isolation" / "manager.py"
)


def _load_manager_module() -> Any:
    """动态加载 manager.py（唯一模块名，防裸名冲突）。"""
    mod_name = "isolation_manager_linux_wiring_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _MANAGER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_manager_module()

from manager import IsolationManager  # noqa: E402  (tests._isolation_path 已注入路径)
from providers.docker_provider import DockerProvider  # noqa: E402
from providers.host_provider import HostProvider  # noqa: E402
from providers.linux_native_provider import LinuxNativeProvider  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_linux_provider(tmp_path: Path) -> LinuxNativeProvider:
    return LinuxNativeProvider({"state_dir": str(tmp_path / "envs"), "workspace": str(tmp_path / "ws")})


def _make_manager(providers: dict[IsolationLevel, Any]) -> IsolationManager:
    return IsolationManager(providers=providers)


# ── manager：槽位选择 ────────────────────────────────────────


class TestLinuxNativeSlotting:
    def test_linux_defaults_to_native_zero_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled 缺席 + Linux → LinuxNative（零配置默认接管，ADR 2026-10-12）。"""
        mod = _load_manager_module()
        monkeypatch.setattr(mod, "_is_linux_platform", lambda: True)
        providers = mod._create_providers_from_config({"host": {"enabled": True}})
        assert isinstance(providers[IsolationLevel.CONTAINER], LinuxNativeProvider)
        assert isinstance(providers[IsolationLevel.HOST], HostProvider)

    def test_explicit_opt_out_falls_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式 enabled=false + Linux → Docker（opt-out 语义，复用 enabled 词汇）。"""
        mod = _load_manager_module()
        monkeypatch.setattr(mod, "_is_linux_platform", lambda: True)
        providers = mod._create_providers_from_config(
            {"host": {"enabled": True}, "linux_native": {"enabled": False}}
        )
        assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)

    def test_wsl_wins_over_linux_native_on_windows(self) -> None:
        """Windows：wsl_native 优先、linux_native 门让位——路径零变化。"""
        providers = _MOD._create_providers_from_config(
            {
                "wsl_native": {"enabled": True, "state_dir": ".ai_workspaces/wsl_native_envs"},
                "linux_native": {"enabled": True},
            }
        )
        if os.name == "nt":
            from providers.wsl_native_provider import WslNativeProvider

            assert isinstance(providers[IsolationLevel.CONTAINER], WslNativeProvider)
        elif sys.platform == "linux":
            # 非 Windows：wsl 门让位，Linux 上 linux_native 默认接管
            assert isinstance(providers[IsolationLevel.CONTAINER], LinuxNativeProvider)
        else:
            assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)

    def test_macos_falls_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enabled + 非 Linux posix（macOS）→ Docker（平台门排除）。"""
        mod = _load_manager_module()
        monkeypatch.setattr(mod, "_is_linux_platform", lambda: False)
        providers = mod._create_providers_from_config(
            {"linux_native": {"enabled": True}}
        )
        assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)


# ── manager：按名查找/销毁委托 + docker 清点跳过 ─────────────


class TestManagerDelegation:
    def test_find_existing_delegates_to_linux_provider(self, tmp_path: Path) -> None:
        provider = _make_linux_provider(tmp_path)
        provider.find_environment_by_name = AsyncMock(return_value=None)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        assert _run(manager._find_existing_container("cua-ws1")) is None
        provider.find_environment_by_name.assert_awaited_once_with("cua-ws1")

    def test_destroy_by_name_delegates_to_linux_provider(self, tmp_path: Path) -> None:
        provider = _make_linux_provider(tmp_path)
        provider.destroy_environment = AsyncMock(return_value=True)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        _run(manager._destroy_container_by_name("ws1"))
        provider.destroy_environment.assert_awaited_once_with("cua-ws1")

    def test_stop_containers_skipped_for_linux_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_linux_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(manager._stop_containers())
        docker_mod.from_env.assert_not_called()

    def test_resume_containers_skipped_for_linux_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_linux_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(manager._resume_containers())
        docker_mod.from_env.assert_not_called()

    def test_prune_skipped_for_linux_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _make_linux_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(asyncio.wait_for(manager._prune_docker_images(), timeout=3))
        docker_mod.from_env.assert_not_called()


# ── guard：装载平台门 + 探测分派 + 引擎自愈门 ────────────────

_GUARD_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "isolation_guard"
)
# 平铺共享裸名自防御（同 test_wsl_native_wiring）：本目录置顶 + 裸名逐出后再 import。
if str(_GUARD_DIR) in sys.path:
    sys.path.remove(str(_GUARD_DIR))
sys.path.insert(0, str(_GUARD_DIR))
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

import plugin as plugin_module  # noqa: E402  模块对象钉住：裸名串扰治理 evict 后引用不断
from plugin import IsolationGuard as _Guard  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_wsl_health(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """引擎自愈在插件内延迟 import wsl_health，测试环境用 fake 占位。"""
    fake = types.SimpleNamespace()
    fake.ensure_docker_engine = MagicMock()
    monkeypatch.setitem(sys.modules, "wsl_health", fake)
    return fake


class TestGuardLinuxNativeConfig:
    def test_platform_gate_normalizes_to_explicit_off_on_non_linux(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 Linux 装载出口归一为显式关闭形态（enabled 缺省 true 盖不过置空）。"""
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: False)
        guard = _Guard(config={"providers": {"linux_native": {"enabled": True, "network": True}}})
        assert guard._linux_native_cfg == {"enabled": False}

    def test_linux_keeps_config_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        cfg = {"enabled": True, "network": False, "bwrap_path": "/usr/bin/bwrap"}
        guard = _Guard(config={"providers": {"linux_native": cfg}})
        assert guard._linux_native_cfg == cfg

    def test_absent_key_defaults_enabled_true_on_linux(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """源在而 linux_native 键缺席 = 未配置（合法形态，enabled 缺省 true）。"""
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        guard = _Guard(config={"providers": {"wsl_native": {"enabled": False}}})
        assert guard._linux_native_cfg == {}
        assert guard._linux_native_cfg.get("enabled", True) is True

    def test_injected_namespace_source_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        cfg = {"enabled": False}
        guard = _Guard(config={"isolation_config": {"providers": {"linux_native": cfg}}})
        assert guard._linux_native_cfg == cfg

    def test_non_dict_segment_normalized_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        guard = _Guard(config={"providers": {"linux_native": "bwrap"}})
        assert guard._linux_native_cfg == {}


class TestGuardProbeDispatch:
    def test_linux_native_probes_bwrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linux 默认（enabled 缺省 true）→ 探 bwrap，不探 docker。"""
        import shutil

        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        seen: list[str] = []
        monkeypatch.setattr(shutil, "which", lambda name: seen.append(name) or f"/usr/bin/{name}")
        guard = _Guard(config={"providers": {"wsl_native": {"enabled": False}}})
        constructed = list(seen)  # 构造期探测（目标随分派），不计入分派断言

        guard._probe_backend()

        dispatched = seen[len(constructed):]
        assert dispatched == ["bwrap"]
        assert guard._docker_available is True

    def test_linux_native_absent_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """bwrap 不在 PATH → absent（明确不可用，无 probe_error）。"""
        import shutil

        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        guard = _Guard(config={"providers": {"wsl_native": {"enabled": False}}})
        guard._probe_backend()
        assert guard._docker_available is False
        assert guard._docker_probe_error is None

    def test_explicit_opt_out_probes_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式 enabled=false → 分派 docker 探测（opt-out 语义贯通探测端）。"""
        import shutil

        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        seen: list[str] = []
        monkeypatch.setattr(shutil, "which", lambda name: seen.append(name) or f"/usr/bin/{name}")
        guard = _Guard(
            config={
                "providers": {
                    "wsl_native": {"enabled": False},
                    "linux_native": {"enabled": False},
                }
            }
        )
        constructed = list(seen)

        guard._probe_backend()

        dispatched = seen[len(constructed):]
        assert dispatched == ["docker"]

    def test_probe_exception_reports_probe_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探测异常（可用性不可验证）→ probe_error 三态，fail-closed 携带原文。"""
        import shutil

        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)

        def _which_boom(name: str) -> str:
            raise OSError("PATH scan failed")

        monkeypatch.setattr(shutil, "which", _which_boom)
        guard = _Guard(config={"providers": {"wsl_native": {"enabled": False}}})
        guard._probe_backend()
        assert guard._docker_available is False
        assert guard._docker_probe_error is not None
        assert "PATH scan failed" in guard._docker_probe_error

    def test_bwrap_path_config_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        seen: list[str] = []
        monkeypatch.setattr(shutil, "which", lambda name: seen.append(name) or None)
        guard = _Guard(
            config={
                "providers": {
                    "wsl_native": {"enabled": False},
                    "linux_native": {"enabled": True, "bwrap_path": "/opt/bwrap"},
                }
            }
        )
        constructed = list(seen)

        guard._probe_backend()

        assert seen[len(constructed):] == ["/opt/bwrap"]
        assert guard._docker_available is False


class TestGuardEngineHealGate:
    def test_native_backend_active_truth_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """原生后端在场 → 跳过 docker 引擎自愈；双缺席 → 自愈照常。"""
        guard = _Guard(config={"docker_available": False})
        # Linux 默认形态（真实谓词不 patch：非 Linux 平台装载门已归一关闭）
        if sys.platform == "linux":
            assert guard._native_backend_active() is True
        else:
            assert guard._native_backend_active() is False
        guard._linux_native_cfg = {"enabled": True}
        assert guard._native_backend_active() is True
        guard._linux_native_cfg = {"enabled": False}
        guard._wsl_native_cfg = {"enabled": True}
        assert guard._native_backend_active() is True
        guard._wsl_native_cfg = {"enabled": False}
        assert guard._native_backend_active() is False
