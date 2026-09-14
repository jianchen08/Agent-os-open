# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""wsl_native 接线测试：manager 槽位选择/委托/跳过 docker 清点 + guard 注入/探测分派
+ bash 侧 argv 构建漂移钉。

锁定契约：
- manager：providers.wsl_native.enabled 时 CONTAINER 槽位换装 WslNativeProvider；
  _find_existing_container / _destroy_container_by_name 委托 provider（D6 语义
  单点在 provider）；_resume_containers/_stop_containers/_prune_docker_images
  非 docker 后端早退（wsl_native 无常驻资源可清点）。
- guard：_inject_container_id 支持 exec_backend 注入（bash_execute 专用，
  服务端信任链与 _container_id 同级）；探测目标按 wsl_native 配置分派。
- 漂移钉：process_manager 的 wsl 传输前缀与 working_dir 映射必须与
  WslNativeProvider 同语义（两处实现，测试钉死防漂移）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests._isolation_path  # noqa: F401
from agentos_plugin_sdk.isolation_types import IsolationLevel

_MANAGER_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "system" / "isolation" / "manager.py"
)


def _load_manager_module() -> Any:
    """动态加载 manager.py（唯一模块名，防裸名冲突）。"""
    mod_name = "isolation_manager_wiring_test"
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
from providers.wsl_native_provider import WslNativeProvider  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_wsl_provider(tmp_path: Path) -> WslNativeProvider:
    return WslNativeProvider({"distro": "Ubuntu", "state_dir": str(tmp_path / "envs")})


def _make_manager(providers: dict[IsolationLevel, Any]) -> IsolationManager:
    return IsolationManager(providers=providers)


# ── manager：槽位选择 ────────────────────────────────────────


class TestProviderSlotting:
    def test_wsl_native_enabled_takes_container_slot(self, tmp_path: Path) -> None:
        providers = _MOD._create_providers_from_config(
            {
                "host": {"enabled": True},
                "cua": {"enabled": True},
                "wsl_native": {"enabled": True, "state_dir": str(tmp_path / "envs")},
            }
        )
        assert isinstance(providers[IsolationLevel.CONTAINER], WslNativeProvider)
        assert isinstance(providers[IsolationLevel.HOST], HostProvider)

    def test_wsl_native_disabled_keeps_docker(self) -> None:
        providers = _MOD._create_providers_from_config(
            {"host": {"enabled": True}, "cua": {"enabled": True}}
        )
        assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)

    def test_wsl_native_disabled_by_default(self) -> None:
        providers = _MOD._create_providers_from_config({})
        assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)


# ── manager：按名查找/销毁委托 ───────────────────────────────


class TestManagerDelegation:
    def test_find_existing_delegates_to_wsl_provider(self, tmp_path: Path) -> None:
        provider = _make_wsl_provider(tmp_path)
        provider.find_environment_by_name = AsyncMock(return_value=None)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        assert _run(manager._find_existing_container("cua-ws1")) is None
        provider.find_environment_by_name.assert_awaited_once_with("cua-ws1")

    def test_destroy_by_name_delegates_to_wsl_provider(self, tmp_path: Path) -> None:
        provider = _make_wsl_provider(tmp_path)
        provider.destroy_environment = AsyncMock(return_value=True)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        _run(manager._destroy_container_by_name("ws1"))
        provider.destroy_environment.assert_awaited_once_with("cua-ws1")

    def test_stop_containers_skipped_for_wsl_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_wsl_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(manager._stop_containers())
        docker_mod.from_env.assert_not_called()

    def test_resume_containers_skipped_for_wsl_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_wsl_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(manager._resume_containers())
        docker_mod.from_env.assert_not_called()

    def test_prune_skipped_for_wsl_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _make_wsl_provider(tmp_path)
        manager = _make_manager({IsolationLevel.CONTAINER: provider})
        docker_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "docker", docker_mod)
        _run(asyncio.wait_for(manager._prune_docker_images(), timeout=3))
        docker_mod.from_env.assert_not_called()


# ── guard：注入与探测分派 ────────────────────────────────────

_GUARD_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard"
)
# 平铺共享裸名自防御（同 test_isolation_docker_probe_tristate）：先导的测试
# 会把自家目录钉在 sys.path[0] 并缓存 plugin 模块；本目录置顶 + 裸名逐出后
# 再 import，防 security_check/plugin.py 抢走 `plugin` 名。
if str(_GUARD_DIR) in sys.path:
    sys.path.remove(str(_GUARD_DIR))
sys.path.insert(0, str(_GUARD_DIR))
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

from plugin import IsolationGuard as _Guard  # noqa: E402


class TestGuardInjection:
    def test_inject_exec_backend_for_bash_only(self) -> None:
        backend = {"backend": "wsl_native", "distro": "Ubuntu", "workspace_wsl": "/mnt/d/ws"}
        tool_calls = [
            {"name": "bash_execute", "args": {"command": "ls"}},
            {"name": "browser_navigate", "args": {"url": "http://x"}},
            {"name": "task_submit", "args": {"title": "t"}},
        ]
        result = _Guard._inject_container_id(
            tool_calls, {"bash_execute", "browser_navigate"}, "cua-ws1", exec_backend=backend
        )
        assert result is not None
        by_name = {tc["name"]: tc["args"] for tc in result}
        assert by_name["bash_execute"]["_container_id"] == "cua-ws1"
        assert by_name["bash_execute"]["_exec_backend"] == backend
        # browser 工具只拿 _container_id，不拿 exec_backend（bridge 通路 v1 不变）
        assert by_name["browser_navigate"]["_container_id"] == "cua-ws1"
        assert "_exec_backend" not in by_name["browser_navigate"]
        # 非容器工具不注入
        assert "_container_id" not in by_name["task_submit"]

    def test_inject_without_exec_backend_keeps_legacy_shape(self) -> None:
        tool_calls = [{"name": "bash_execute", "args": {"command": "ls"}}]
        result = _Guard._inject_container_id(tool_calls, {"bash_execute"}, "cua-ws1")
        assert result is not None
        assert result[0]["args"]["_container_id"] == "cua-ws1"
        assert "_exec_backend" not in result[0]["args"]

    def test_probe_dispatch_wsl_native(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard(
            config={"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu"}}}
        )
        # wsl_native 启用时探测目标是 WSL 而非 docker
        def _fail_docker() -> tuple[str, str]:
            raise AssertionError("docker 被探测")

        def _fake_wsl_probe(self: Any) -> tuple[str, str]:
            return (_Guard._PROBE_AVAILABLE, "")

        monkeypatch.setattr(_Guard, "_detect_docker", staticmethod(_fail_docker))
        monkeypatch.setattr(_Guard, "_detect_wsl_native", _fake_wsl_probe)
        guard._probe_backend()
        assert guard._docker_available is True

    def test_probe_dispatch_wsl_absent_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard(
            config={"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu"}}}
        )
        def _absent_wsl_probe(self: Any) -> tuple[str, str]:
            return (_Guard._PROBE_ABSENT, "")

        monkeypatch.setattr(_Guard, "_detect_wsl_native", _absent_wsl_probe)
        guard._probe_backend()
        assert guard._docker_available is False
        assert guard._docker_probe_error is None

    def test_probe_dispatch_default_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 显式关闭 wsl_native（否则回退读真实仓库 yaml，本机启用时会走 wsl 探针）
        monkeypatch.setattr(
            _Guard,
            "_load_wsl_native_config",
            lambda self: {"enabled": False},
        )
        guard = _Guard(config={})
        # 默认（未启用 wsl_native）走 docker 探测：本机 docker 可用性与
        # _detect_docker 一致即可，这里只锁分派目标。
        import shutil as _shutil

        if _shutil.which("docker"):
            assert guard._docker_available is True


# ── 漂移钉：bash 侧 wsl 构建与 provider 同语义 ───────────────

_BASH_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "tools" / "bash"
)


def _load_process_manager_module() -> Any:
    if str(_BASH_DIR) in sys.path:
        sys.path.remove(str(_BASH_DIR))
    sys.path.insert(0, str(_BASH_DIR))
    # 裸名逐出：bash 插件测试可能已缓存同名/兄弟模块
    for _bare in ("process_manager", "bash_types", "tool", "encoding", "input_handler", "log_compressor"):
        sys.modules.pop(_bare, None)
    mod_name = "process_manager_wiring_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _BASH_DIR / "process_manager.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestArgvDriftPin:
    """process_manager 的 wsl 传输前缀/working_dir 映射与 provider 锁同语义。"""

    def test_transport_prefix_matches_provider(self, tmp_path: Path) -> None:
        pm = _load_process_manager_module()
        backend = {
            "backend": "wsl_native",
            "wsl_exe": "wsl",
            "distro": "Ubuntu",
            "user": "agentos",
            "sandbox_cmd": ["bwrap"],
            "workspace_wsl": "/mnt/d/ws",
        }
        provider = WslNativeProvider(
            {"distro": "Ubuntu", "user": "agentos", "sandbox_cmd": ["bwrap"], "state_dir": str(tmp_path)}
        )
        # 传输前缀（wsl_exe/-d/-u）两侧一致
        assert pm._wsl_transport(backend) == provider._wsl_argv([])

    def test_working_dir_mapping_matches_provider(self) -> None:
        pm = _load_process_manager_module()
        backend = {"backend": "wsl_native", "workspace_wsl": "/mnt/d/ws"}
        provider = WslNativeProvider({"state_dir": "x"})
        provider._workspace_wsl = "/mnt/d/ws"
        for wd in ("/workspace", "/workspace/sub", "/tmp/other", None, "", "\workspace"):
            assert pm._map_wsl_working_dir(backend, wd) == provider._map_working_dir(wd)
