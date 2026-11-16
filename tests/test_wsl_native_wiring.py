# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""wsl_native 接线测试：manager 槽位选择/委托/跳过 docker 清点 + guard 注入/探测分派
+ bash 侧 argv 构建漂移钉。

锁定契约：
- manager：providers.wsl_native.enabled 时 CONTAINER 槽位换装 WslNativeProvider
  （平台门：仅 Windows；posix 平台门让位、CONTAINER 槽自动落 Docker，ADR
  2026-10-09）；_find_existing_container / _destroy_container_by_name 委托
  provider（D6 语义单点在 provider）；_resume_containers/_stop_containers/
  _prune_docker_images 非 docker 后端早退（wsl_native 无常驻资源可清点）。
- guard：_inject_container_id 支持 exec_backend 注入（bash_execute 与
  browser_* 均消费——browser 沙箱内 bridge 通路据 backend 选 wsl 传输）；
  探测目标按 wsl_native 配置分派。
- 漂移钉：process_manager 的 wsl 传输前缀与 working_dir 映射必须与
  WslNativeProvider 同语义（两处实现，测试钉死防漂移）。
"""

from __future__ import annotations

import asyncio
import json
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import tests._isolation_path  # noqa: F401
from agentos_plugin_sdk.isolation_types import IsolationLevel

_MANAGER_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "isolation" / "manager.py"
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
from providers.linux_native_provider import LinuxNativeProvider  # noqa: E402
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
        """enabled 占 CONTAINER 槽（nt）/ 平台门让位（posix：Linux 默认落
        linux_native，macOS 落 Docker）——分支都是生产真实路径。

        平台门语义（ADR 2026-10-09 + 2026-10-12 修订）：wsl_native enabled 仅
        Windows 生效；Linux 上 linux_native enabled 默认 true 接管 CONTAINER
        槽。按实际平台断言真实行为（读 os.name/sys.platform 不写）：posix 分支
        在 ubuntu 车道天然执法。不钉 os.name——真 posix 上钉窗口内生产代码
        （HostProvider → Path.resolve）会产出 WindowsPath 直接 NotImplementedError
        （真机冒烟实锤），Windows 本机绿是假阴性。
        """
        providers = _MOD._create_providers_from_config(
            {
                "host": {"enabled": True},
                "cua": {"enabled": True},
                "wsl_native": {"enabled": True, "state_dir": str(tmp_path / "envs")},
            }
        )
        if os.name == "nt":
            assert isinstance(providers[IsolationLevel.CONTAINER], WslNativeProvider)
        elif sys.platform == "linux":
            assert isinstance(providers[IsolationLevel.CONTAINER], LinuxNativeProvider)
        else:
            assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)
        assert isinstance(providers[IsolationLevel.HOST], HostProvider)

    def test_wsl_native_disabled_slot_falls_through(self) -> None:
        """wsl 关闭不占槽：nt/mac 落 Docker；Linux 落 linux_native（默认启用）。"""
        providers = _MOD._create_providers_from_config(
            {"host": {"enabled": True}, "cua": {"enabled": True}}
        )
        if os.name == "nt" or sys.platform != "linux":
            assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)
        else:
            assert isinstance(providers[IsolationLevel.CONTAINER], LinuxNativeProvider)

    def test_wsl_native_disabled_by_default(self) -> None:
        providers = _MOD._create_providers_from_config({})
        if os.name == "nt" or sys.platform != "linux":
            assert isinstance(providers[IsolationLevel.CONTAINER], DockerProvider)
        else:
            assert isinstance(providers[IsolationLevel.CONTAINER], LinuxNativeProvider)


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
    / "plugins" / "shared" / "isolation_guard"
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
    def test_inject_exec_backend_for_declared_tools(self) -> None:
        """声明了 isolation_capable 的工具均注入 exec_backend（契约表单源，
        声明即消费：bash 经 process_manager 三路分派，browser_* 经
        bridge_client 据 backend 选 wsl 传输）。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu", "workspace_wsl": "/mnt/d/ws"}
        tool_calls = [
            {"name": "bash_execute", "args": {"command": "ls"}},
            {"name": "browser_navigate", "args": {"url": "http://x"}},
            {"name": "task_submit", "args": {"title": "t"}},
        ]
        result = _Guard._inject_container_id(
            tool_calls,
            {"bash_execute", "browser_navigate"},
            "cua-ws1",
            exec_backend=backend,
            exec_backend_capable={"bash_execute", "browser_navigate"},
        )
        assert result is not None
        by_name = {tc["name"]: tc["args"] for tc in result}
        assert by_name["bash_execute"]["_container_id"] == "cua-ws1"
        assert by_name["bash_execute"]["_exec_backend"] == backend
        # 声明工具同样拿 exec_backend（wsl 传输选路依据）
        assert by_name["browser_navigate"]["_container_id"] == "cua-ws1"
        assert by_name["browser_navigate"]["_exec_backend"] == backend
        # 非容器工具不注入
        assert "_container_id" not in by_name["task_submit"]

    def test_inject_undeclared_tool_gets_container_id_only(self) -> None:
        """进容器但未声明 isolation_capable 的工具：只拿 _container_id，
        不拿 _exec_backend（声明缺席 = 能力未声明，fail-closed）。"""
        backend = {"backend": "wsl_native", "distro": "Ubuntu", "workspace_wsl": "/mnt/d/ws"}
        tool_calls = [
            {"name": "bash_execute", "args": {"command": "ls"}},
            {"name": "third_party_tool", "args": {"x": 1}},
        ]
        result = _Guard._inject_container_id(
            tool_calls,
            {"bash_execute", "third_party_tool"},
            "cua-ws1",
            exec_backend=backend,
            exec_backend_capable={"bash_execute"},
        )
        assert result is not None
        by_name = {tc["name"]: tc["args"] for tc in result}
        assert by_name["third_party_tool"]["_container_id"] == "cua-ws1"
        assert "_exec_backend" not in by_name["third_party_tool"]

    def test_inject_browser_without_exec_backend(self) -> None:
        """docker 环境 exec_backend=None：browser 只拿 _container_id（原形态）。"""
        tool_calls = [{"name": "browser_navigate", "args": {"url": "http://x"}}]
        result = _Guard._inject_container_id(
            tool_calls,
            {"browser_navigate"},
            "cua-ws1",
            exec_backend_capable={"browser_navigate"},
        )
        assert result is not None
        assert result[0]["args"]["_container_id"] == "cua-ws1"
        assert "_exec_backend" not in result[0]["args"]

    def test_inject_without_exec_backend_keeps_legacy_shape(self) -> None:
        tool_calls = [{"name": "bash_execute", "args": {"command": "ls"}}]
        result = _Guard._inject_container_id(
            tool_calls,
            {"bash_execute"},
            "cua-ws1",
            exec_backend_capable={"bash_execute"},
        )
        assert result is not None
        assert result[0]["args"]["_container_id"] == "cua-ws1"
        assert "_exec_backend" not in result[0]["args"]

    def test_probe_dispatch_wsl_native(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cfg.enabled → _probe_backend 分派 WSL、不探 docker（白盒：cfg 直注）。

        分派语义只读 cfg；装载出口的平台归一（posix 归空）属生产语义，故
        构造后直注 cfg 再复探。shutil/subprocess 构造前替身化：双平台构造期
        探测（目标随平台）全部密封不 spawn 真子进程，分派断言取显式复探
        窗口（构造期帧不计入）。
        """
        import shutil
        import subprocess

        seen: list[list[str]] = []

        def _run(argv: Any, **_k: Any) -> Any:
            seen.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout="Ubuntu\n".encode("utf-8"))

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(subprocess, "run", _run)

        guard = _Guard(
            config={"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu"}}}
        )
        # cfg 直注（探测逻辑输入契约）：posix 下装载归一已把 cfg 归空
        guard._wsl_native_cfg = {"enabled": True, "distro": "Ubuntu"}
        # 钉探测目标为 wsl（linux_native 在 Linux 默认启用会抢先分派）
        guard._linux_native_cfg = {"enabled": False}
        constructed = len(seen)  # 构造期探测（目标随平台），不计入分派断言

        guard._probe_backend()

        dispatched = seen[constructed:]
        assert dispatched and dispatched[0][:2] == ["wsl", "-l"]
        assert all(argv[:1] != ["docker"] for argv in dispatched)
        assert guard._docker_available is True

    def test_probe_dispatch_wsl_absent_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WSL 探测明确不可用 → fail-closed（available=False，无 probe_error）。"""
        import shutil
        import subprocess

        # 命令面全替身：wsl -l -q 非零退出（密封，双平台构造期探测不 spawn）
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1)
        )

        guard = _Guard(
            config={"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu"}}}
        )
        guard._wsl_native_cfg = {"enabled": True, "distro": "Ubuntu"}
        # 钉探测目标为 wsl（linux_native 在 Linux 默认启用会抢先分派）
        guard._linux_native_cfg = {"enabled": False}

        guard._probe_backend()

        assert guard._docker_available is False
        assert guard._docker_probe_error is None

    def test_probe_dispatch_default_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 显式关闭 wsl_native/linux_native（钉 docker 探测目标，不依赖真实注入配置）
        def _cfg_off(self: Any) -> None:
            self._wsl_native_cfg = {"enabled": False}
            return None

        monkeypatch.setattr(_Guard, "_load_wsl_native_config", _cfg_off)
        monkeypatch.setattr(
            _Guard, "_load_linux_native_config",
            lambda self: setattr(self, "_linux_native_cfg", {"enabled": False}),
        )
        # 分派目标测试：_detect_docker 替身化（不依赖真机 daemon 在位）
        monkeypatch.setattr(
            _Guard, "_detect_docker", staticmethod(lambda: ("available", ""))
        )
        guard = _Guard(config={})
        assert guard._docker_available is True


# ── guard：wsl_native 配置降级可观测（F4，2026-09-16 审查）────


class TestWslNativeConfigInjection:
    """_load_wsl_native_config 注入单通道契约（F11，2026-10-08）。

    ConfigCenter/仓库 yaml 直读回退已退役：取数只认注入面（providers 显式
    键 / config_files 注入命名空间），两源皆缺返回断裂说明（fail-closed）。
    """

    def test_injected_namespace_loads_cleanly(self) -> None:
        """注入命名空间装载成功：返回 None（无断裂说明）双平台一致。

        cfg 保留原值是 nt 行为；posix 装载出口平台门归空（生产语义，ADR
        2026-10-09）——按实际平台断言真实行为，不钉 os.name（真 posix 上
        钉窗口会全局投毒 pathlib）。
        """
        expected = {"enabled": True, "distro": "UnitTest"}
        guard = _Guard(config={"isolation_config": {"providers": {"wsl_native": expected}}})
        assert guard._load_wsl_native_config() is None
        if os.name == "nt":
            assert guard._wsl_native_cfg == expected
        else:
            assert guard._wsl_native_cfg == {}

    def test_explicit_providers_short_circuits(self) -> None:
        """显式 providers 配置短路：命中即返回（不读配置中心），无断裂说明。

        短路契约（返回 None）双平台一致；cfg 保留值是 nt 行为，posix 装载
        出口平台门归空（等价「未配置该后端」合法缺省，enabled 值不参与门
        判定）——按实际平台断言真实行为。
        """
        guard = _Guard(config={"providers": {"wsl_native": {"enabled": False}}})
        assert guard._load_wsl_native_config() is None
        if os.name == "nt":
            assert guard._wsl_native_cfg == {"enabled": False}
        else:
            assert guard._wsl_native_cfg == {}

    def test_no_source_returns_breakage_notice(self) -> None:
        guard = _Guard(config={"docker_available": False})
        notice = guard._load_wsl_native_config()
        assert isinstance(notice, str)
        assert "isolation_config 未注入" in notice
        assert guard._wsl_native_cfg == {}


# ── 漂移钉：bash 侧 wsl 构建与 provider 同语义 ───────────────
# 工具隔离声明化 Slice 2 起，bash 侧构建单源迁入 SDK process_runtime.transports
# （bash 工具与 SDK 共用）；本钉改为钉「SDK 单源 ↔ provider 副本」同语义。


def _load_process_manager_module() -> Any:
    import agentos_plugin_sdk.process_runtime.manager as mgr

    return mgr


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
        for wd in (
            "/workspace", "/workspace/sub", "/tmp/other", None, "", "\\workspace",
            # WSL 挂载混入形态：两实现同语义归一
            r"\mnt\d\repo", r"D:\mnt\e\x", "/mnt/d/repo",
            # 相对形态（wsl --cd 只收绝对路径；两实现同语义拼 workspace 根）
            ".", "./", "frontend", "frontend/src", "..",
        ):
            assert pm._map_wsl_working_dir(backend, wd) == provider._map_working_dir(wd)


class TestBridgeClientWslTransport:
    """browser 沙箱内 bridge 通路：wsl_native 传输选路/argv 契约/候选拓扑。

    锁定契约（bridge_client 与 bash process_manager 的 wsl 传输同语义）：
    - backend=wsl_native → wsl argv（--exec 逐字传递，禁 --），spec 走 stdin；
    - 候选含 127.0.0.1（mirrored）+ 默认网关（NAT 宿主 vEthernet）；
    - backend=linux_native → 显式未覆盖错误（优于误导性 docker 报错）；
    - exec_backend 缺省 → docker 通路原形态（零回归）。
    """

    @staticmethod
    def _client(monkeypatch: pytest.MonkeyPatch, exec_backend: dict[str, Any] | None) -> Any:
        from agentos_plugin_sdk.bridge_client import BridgeClient

        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-abc"))
        return BridgeClient(
            {"bridge_base": "http://127.0.0.1:8765", "upstream": "browser"},
            session_id="s1",
            caller="sandbox",
            container_id="cua-ws1",
            exec_backend=exec_backend,
        )

    def test_wsl_native_routes_to_wsl_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        argv_seen: list[list[str]] = []

        def fake_run(argv: Any, **kwargs: Any) -> Any:
            if "input" not in kwargs:
                # 网关探测（ip route show default）——空回包让候选仅剩 127.0.0.1
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            argv_seen.append(list(argv))
            inner = json.loads(kwargs["input"].decode("utf-8"))
            assert inner["caller"] == "sandbox"
            assert inner["token"] == "tok-abc"
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"ok": True, "status": 200, "body": {"result": {}}}).encode("utf-8"),
                stderr=b"",
            )

        monkeypatch.setattr("agentos_plugin_sdk.bridge_client.subprocess.run", fake_run)
        c = self._client(
            monkeypatch,
            {
                "backend": "wsl_native",
                "wsl_exe": "wsl",
                "distro": "Ubuntu",
                "user": "cua_ws1",
                "workspace_wsl": "/mnt/d/ws/x",
                "sandbox_cmd": [],
            },
        )
        result = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        assert result["ok"] is True
        argv = argv_seen[0]
        # --exec 逐字传递契约（禁 --：登录 shell 重解析吞 stdin）
        assert argv[:2] == ["wsl", "-d"]
        assert "Ubuntu" in argv
        assert "-u" in argv and "cua_ws1" in argv
        assert "--cd" in argv and "/mnt/d/ws/x" in argv
        assert "--exec" in argv
        assert "--" not in argv
        assert argv[-3:-1] == ["python3", "-c"]

    def test_wsl_candidates_include_loopback_and_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._client(
            monkeypatch,
            {"backend": "wsl_native", "distro": "Ubuntu", "workspace_wsl": "/mnt/d/ws/x"},
        )
        monkeypatch.setattr(
            c, "_wsl_gateway_ip", lambda: "172.20.144.1", raising=False
        )
        candidates = c._wsl_base_candidates()
        assert "http://127.0.0.1:8765" in candidates
        assert "http://172.20.144.1:8765" in candidates

    def test_linux_native_explicit_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentos_plugin_sdk.bridge_client import BridgeClientError

        c = self._client(monkeypatch, {"backend": "linux_native", "workspace": "/srv/ws"})
        with pytest.raises(BridgeClientError, match="linux_native"):
            c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})

    def test_no_exec_backend_keeps_docker_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """exec_backend 缺省（docker 环境）→ docker exec 通路零回归。"""
        called: list[str] = []
        c = self._client(monkeypatch, None)
        monkeypatch.setattr(
            c, "_send_via_container",
            lambda payload: (called.append("docker"), {"ok": True, "status": 200, "body": {}})[1],
            raising=False,
        )
        result = c._send({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        assert called == ["docker"]
        assert result["ok"] is True
