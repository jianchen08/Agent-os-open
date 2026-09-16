# @feature: FP-0.2.二 isolation_guard 缺口分支补测 | @ci: python-coverage
"""IsolationGuard wsl_native 配置装载 / 探测分派 / exec_backend 注入补测。

靶行（plugin.py，HEAD coverage.xml 口径）：211-236（_load_wsl_native_config 三级
取数 + 兜底）、245-274（_detect_wsl_native 三态）、470-481（_resolve_exec_backend
环境后端取数）、570-574（_inject_container_id 的 exec_backend 注入）。

不可达行说明（本文件不为其造测试）：
- ``_decide_isolation`` 的 778-790（"policy 为 CONTAINER 且不落 metadata 分支"
  的 docker/host 二连判）为**结构性死代码**：其上方 ``if policy_isolation ==
  IsolationLevel.CONTAINER:`` 块（755-775）的两条分支均以 return 终结，故该
  条件为真时永不抵达 778——AST 静态验证（块尾 `always_returns` 为 True）。
  与 isolation/manager.py:1483-1531 同属待裁定的死分支，此处仅登记不覆盖。
- ``_detect_wsl_native`` 走真实 wsl.exe 的端到端路径需真 Linux/WSL 主机，
  本文件只覆盖命令面替身下的三态语义（shutil.which / subprocess.run 为外部
  依赖边界）。

外部依赖边界：wsl.exe / docker CLI（shutil.which + subprocess.run）、
ConfigCenter（sys.modules 注入假包）、manager（注入替身）；决策与解码逻辑全真实。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard"
_SHARED_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared"
for _d in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


class _Completed:
    """subprocess.run 替身返回值（只带调用方消费的字段）。"""

    def __init__(self, returncode: int, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _inject_config_center(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """把假 config.config_center 注入 sys.modules（配置中心属外部依赖面）。"""
    fake_pkg = types.ModuleType("config")
    fake_pkg.config_center = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config", fake_pkg)
    monkeypatch.setitem(sys.modules, "config.config_center", module)


# ═══════════════ wsl_native 配置装载 ═══════════════


class TestLoadWslNativeConfig:
    """``_load_wsl_native_config`` 三级取数（plugin.py:211-236）。

    优先级：显式 config.providers.wsl_native > ConfigCenter 单源 >
    仓库 yaml 直读兜底；任一层异常都按「未启用 docker 后端」处置，不抛。
    """

    def test_explicit_config_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式 config 命中即返回，不去读配置中心。"""
        from plugin import IsolationGuard

        def _boom() -> Any:  # pragma: no cover - 命中即失败
            raise AssertionError("显式配置命中时不得再读配置中心")

        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = _boom  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={
            "providers": {"wsl_native": {"enabled": True, "distro": "Debian"}},
        })

        assert guard._wsl_native_cfg == {"enabled": True, "distro": "Debian"}

    def test_explicit_non_dict_normalized_to_empty(self) -> None:
        """显式值非映射（误配为字符串）→ 空配置（视为未启用），不抛。"""
        from plugin import IsolationGuard

        guard = IsolationGuard(config={"providers": {"wsl_native": "Ubuntu"}})

        assert guard._wsl_native_cfg == {}

    def test_config_center_value_used_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无显式配置 + ConfigCenter 可用 → 用其返回的 wsl_native 段（218-220）。"""
        from plugin import IsolationGuard

        center = types.SimpleNamespace(
            get=lambda rel: {"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu-24.04"}}}
        )
        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = lambda: center  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={})

        assert guard._wsl_native_cfg == {"enabled": True, "distro": "Ubuntu-24.04"}

    def test_config_center_non_dict_segment_normalized_to_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ConfigCenter 返回段非映射 → 空配置（不把列表当配置用）。"""
        from plugin import IsolationGuard

        center = types.SimpleNamespace(get=lambda rel: {"providers": {"wsl_native": []}})
        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = lambda: center  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={})

        assert guard._wsl_native_cfg == {}

    def test_config_center_error_falls_back_to_repo_yaml(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ConfigCenter 抛异常 → 直读仓库 yaml 兜底（227-234），结果含 enabled 键。"""
        from plugin import IsolationGuard

        def _boom() -> Any:
            raise RuntimeError("ConfigCenter 不可达")

        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = _boom  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={})

        # 仓库 config/plugins/isolation/isolation_config.yaml 声明了该后端
        assert "enabled" in guard._wsl_native_cfg

    def test_all_sources_unavailable_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """配置中心异常 + yaml 路径不可读 → 空配置（未启用），不抛（235-236）。"""
        from plugin import IsolationGuard

        def _boom() -> Any:
            raise RuntimeError("ConfigCenter 不可达")

        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = _boom  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        def _read_text_boom(self: Path, *a: Any, **k: Any) -> str:
            raise OSError("yaml 不可读")

        monkeypatch.setattr(Path, "read_text", _read_text_boom)

        guard = IsolationGuard(config={})

        assert guard._wsl_native_cfg == {}


# ═══════════════ WSL 探测三态 ═══════════════


class TestDetectWslNativeTristate:
    """``_detect_wsl_native`` 三态语义（plugin.py:245-274）。

    wsl.exe 属外部命令面（shutil.which / subprocess.run 替身）；发行版列表
    解码与比对逻辑全真实（复用 WslNativeProvider 单源解码）。
    """

    def _guard(self, **cfg: Any) -> Any:
        from plugin import IsolationGuard

        return IsolationGuard(config={
            "providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu", **cfg}},
        })

    def test_wsl_exe_absent_returns_absent_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """wsl.exe 不在 PATH → ("absent","")，不 spawn 子进程（260）。"""
        import shutil
        import subprocess

        calls: list[Any] = []

        def _run(*a: Any, **k: Any) -> Any:
            calls.append(a)
            return _Completed(0, stdout=b"Ubuntu\n")

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", _run)

        assert self._guard()._detect_wsl_native() == ("absent", "")
        assert calls == []

    def test_list_command_nonzero_returns_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wsl -l -q 非零退出 → ("absent","")（明确不可用，267）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(1))

        assert self._guard()._detect_wsl_native() == ("absent", "")

    def test_distro_not_listed_returns_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """发行版不在列表 → ("absent","")（271）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _Completed(0, stdout="Debian\nUbuntu\n".encode("utf-8")),
        )

        assert self._guard(distro="Fedora")._detect_wsl_native() == ("absent", "")

    def test_distro_listed_returns_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """发行版在列表 → ("available","")（正例：三态不是恒 absent）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _Completed(0, stdout="Ubuntu\n".encode("utf-8")),
        )

        assert self._guard()._detect_wsl_native() == ("available", "")

    def test_utf16_list_decode_hits_distro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wsl.exe 自身的 UTF-16-LE 输出也能解码比对（不误判 absent）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _Completed(0, stdout="Ubuntu\r\n".encode("utf-16-le")),
        )

        assert self._guard()._detect_wsl_native() == ("available", "")

    def test_probe_exception_returns_probe_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """subprocess 抛异常（超时/权限）→ ("probe_error", 摘要)，fail-closed（273-274）。"""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wsl")

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise TimeoutError("wsl probe timed out")

        monkeypatch.setattr(subprocess, "run", _raise)

        status, detail = self._guard()._detect_wsl_native()
        assert status == "probe_error"
        assert detail == "wsl probe timed out"

    def test_probe_backend_dispatches_to_wsl_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_probe_backend 按 wsl_native.enabled 分派：启用 → 只探 WSL，不探 docker。"""
        import shutil
        import subprocess

        seen: list[list[str]] = []

        def _run(argv: list[str], **_k: Any) -> Any:
            seen.append(list(argv))
            return _Completed(0, stdout="Ubuntu\n".encode("utf-8"))

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(subprocess, "run", _run)

        guard = self._guard()

        assert guard._docker_available is True
        assert seen and seen[0][:2] == ["wsl", "-l"]
        assert all(argv[:1] != ["docker"] for argv in seen)


# ═══════════════ exec_backend 取数 ═══════════════


class TestResolveExecBackend:
    """``_resolve_exec_backend`` 环境后端取数（plugin.py:470-481）。"""

    def _guard_with_manager(self, manager: Any) -> Any:
        from plugin import IsolationGuard

        guard = IsolationGuard(config={"docker_available": True})
        guard._manager = manager
        return guard

    async def test_manager_unavailable_returns_none(self) -> None:
        """manager 缺席 → None（不注入 exec_backend，bash 按 docker 通路兜底，472）。"""
        guard = self._guard_with_manager(None)
        guard._get_manager = lambda: None  # type: ignore[method-assign]

        assert await guard._resolve_exec_backend("c1") is None

    async def test_environment_missing_returns_none(self) -> None:
        """环境查不到 → None（478-479）。"""

        async def _get_env(_cid: str) -> Any:
            return None

        guard = self._guard_with_manager(types.SimpleNamespace(get_environment=_get_env))

        assert await guard._resolve_exec_backend("ghost") is None

    async def test_environment_lookup_error_returns_none(self) -> None:
        """get_environment 抛异常 → None（降级不阻断，475-477）。"""

        async def _boom(_cid: str) -> Any:
            raise RuntimeError("manager 抖动")

        guard = self._guard_with_manager(types.SimpleNamespace(get_environment=_boom))

        assert await guard._resolve_exec_backend("c1") is None

    async def test_exec_backend_dict_passthrough(self) -> None:
        """provider_info.exec_backend 是映射 → 原样返回（480-481）。"""
        backend = {"kind": "wsl_native", "distro": "Ubuntu"}

        async def _get_env(_cid: str) -> Any:
            return types.SimpleNamespace(provider_info={"exec_backend": backend})

        guard = self._guard_with_manager(types.SimpleNamespace(get_environment=_get_env))

        assert await guard._resolve_exec_backend("c1") == backend

    async def test_exec_backend_non_dict_returns_none(self) -> None:
        """exec_backend 非映射 → None（不把不可信形态注入工具参数）。"""

        async def _get_env(_cid: str) -> Any:
            return types.SimpleNamespace(provider_info={"exec_backend": "wsl"})

        guard = self._guard_with_manager(types.SimpleNamespace(get_environment=_get_env))

        assert await guard._resolve_exec_backend("c1") is None

    async def test_missing_provider_info_returns_none(self) -> None:
        """环境无 provider_info（或为 None）→ None。"""

        async def _get_env(_cid: str) -> Any:
            return types.SimpleNamespace(provider_info=None)

        guard = self._guard_with_manager(types.SimpleNamespace(get_environment=_get_env))

        assert await guard._resolve_exec_backend("c1") is None


# ═══════════════ exec_backend 注入 ═══════════════


class TestExecBackendInjection:
    """exec_backend 经 ``_inject_container_id`` 注入 bash 参数（plugin.py:570-574）。"""

    def test_exec_backend_injected_for_bash_only(self) -> None:
        """bash_execute 收到 _exec_backend；browser_* 不注入（走 bridge 通路）。"""
        from plugin import IsolationGuard

        backend = {"kind": "wsl_native"}
        calls = [
            {"name": "bash_execute", "args": {"command": "ls"}},
            {"name": "browser_navigate", "args": {"url": "http://x"}},
        ]

        injected = IsolationGuard._inject_container_id(
            calls, {"bash_execute", "browser_navigate"}, "c-1", exec_backend=backend,
        )

        assert injected is not None
        assert injected[0]["args"]["_exec_backend"] == backend
        assert injected[0]["args"]["_container_id"] == "c-1"
        assert injected[1]["args"].get("_exec_backend") is None
        assert injected[1]["args"]["_container_id"] == "c-1"

    def test_no_exec_backend_omits_key(self) -> None:
        """docker 环境（无 exec_backend）→ 不写 _exec_backend 键。"""
        from plugin import IsolationGuard

        injected = IsolationGuard._inject_container_id(
            [{"name": "bash_execute", "args": {"command": "ls"}}],
            {"bash_execute"},
            "c-1",
            exec_backend=None,
        )

        assert injected is not None
        assert "_exec_backend" not in injected[0]["args"]

    def test_explicit_working_dir_preserved(self) -> None:
        """显式 working_dir 不被 /workspace 覆盖（调用方意图优先）。"""
        from plugin import IsolationGuard

        injected = IsolationGuard._inject_container_id(
            [{"name": "bash_execute", "args": {"command": "ls", "working_dir": "/data"}}],
            {"bash_execute"},
            "c-1",
            exec_backend={"kind": "wsl_native"},
        )

        assert injected is not None
        assert injected[0]["args"]["working_dir"] == "/data"

    def test_non_docker_tool_not_injected(self) -> None:
        """不在 docker 决策集内的工具不注入（返回 None，调用方不覆盖 state）。"""
        from plugin import IsolationGuard

        injected = IsolationGuard._inject_container_id(
            [{"name": "file_read", "args": {"path": "/x"}}],
            {"bash_execute"},
            "c-1",
            exec_backend={"kind": "wsl_native"},
        )

        assert injected is None
