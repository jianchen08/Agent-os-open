# @feature: FP-0.2.二 isolation_guard 缺口分支补测 | @ci: python-coverage
"""IsolationGuard wsl_native 配置装载 / 探测分派 / create_env 信封解析补测。

靶面（plugin.py，按方法）：_load_wsl_native_config 三级取数 + 兜底、
_detect_wsl_native 三态、_parse_create_env_response（isolation.create_env
信封 → env_id/exec_backend，业务失败形态全归纳）、_inject_container_id 的
exec_backend 注入、guard 零直连管理器静态闸（B5 职责归位：配额/熔断单实例
语义的 CI 可执法面）。

不可达行说明（本文件不为其造测试）：
- ``_decide_isolation`` 的 "policy 为 CONTAINER 且不落 metadata 分支" 的
  docker/host 二连判为**结构性死代码**：其上方 ``if policy_isolation ==
  IsolationLevel.CONTAINER:`` 块的两条分支均以 return 终结，故该条件为真时
  永不抵达——AST 静态验证（块尾 `always_returns` 为 True）。
  与 isolation/manager.py 死分支同属待裁定项，此处仅登记不覆盖。
- ``_detect_wsl_native`` 走真实 wsl.exe 的端到端路径需真 Linux/WSL 主机，
  本文件只覆盖命令面替身下的三态语义（shutil.which / subprocess.run 为外部
  依赖边界）。

外部依赖边界：wsl.exe / docker CLI（shutil.which + subprocess.run）、
ConfigCenter（sys.modules 注入假包）、环境服务（caller 信封假身）；决策与
解析逻辑全真实。
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "isolation_guard"
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
    """``_load_wsl_native_config`` 注入取数（F11 单通道，2026-10-08）。

    取数源 = 构造 config 的 providers 键（调用方显式传入）> manifest
    config_files 注入命名空间 isolation_config；两源皆缺 = 注入链断裂
    （返回断裂说明，探测按 probe_error fail-closed），无任何运行时读取回退
    （ConfigCenter/仓库 yaml 直读分支已退役）。"""

    def test_explicit_config_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式 config 命中即返回，不去读配置中心（短路由构造期 _boom 未触发证明）。

        cfg 保留原值是 nt 行为；posix 装载出口平台门归空（生产语义，ADR
        2026-10-09）。不钉 os.name：真 posix 上钉窗口是全局投毒（pathlib
        ``Path()`` 产出 WindowsPath），按实际平台分支断言真实行为。
        """
        from plugin import IsolationGuard

        def _boom() -> Any:  # pragma: no cover - 命中即失败
            raise AssertionError("显式配置命中时不得再读配置中心")

        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = _boom  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={
            "providers": {"wsl_native": {"enabled": True, "distro": "Debian"}},
        })

        if os.name == "nt":
            assert guard._wsl_native_cfg == {"enabled": True, "distro": "Debian"}
        else:
            assert guard._wsl_native_cfg == {}

    def test_explicit_non_dict_normalized_to_empty(self) -> None:
        """显式值非映射（误配为字符串）→ 空配置（视为未启用），不抛。"""
        from plugin import IsolationGuard

        guard = IsolationGuard(config={"providers": {"wsl_native": "Ubuntu"}})

        assert guard._wsl_native_cfg == {}

    def test_injected_namespace_value_used_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无显式 providers + config_files 注入命名空间在场 → 用其 wsl_native 段。

        cfg 保留原值是 nt 行为；posix 装载出口平台门归空（生产语义，ADR
        2026-10-09）。不钉 os.name（真 posix 上钉窗口全局投毒 pathlib），
        按实际平台分支断言真实行为；装载成功（无断裂说明）双平台一致。
        """
        from plugin import IsolationGuard

        def _boom() -> Any:  # pragma: no cover - 退役读取面不得触达
            raise AssertionError("注入命名空间命中时不得再读 ConfigCenter/仓库 yaml")

        fake_cfg = types.ModuleType("config.config_center")
        fake_cfg.get_config_center = _boom  # type: ignore[attr-defined]
        _inject_config_center(monkeypatch, fake_cfg)

        guard = IsolationGuard(config={
            "isolation_config": {"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu-24.04"}}},
        })

        assert guard._config_probe_error is None
        if os.name == "nt":
            assert guard._wsl_native_cfg == {"enabled": True, "distro": "Ubuntu-24.04"}
        else:
            assert guard._wsl_native_cfg == {}

    def test_injected_non_dict_segment_normalized_to_empty(self) -> None:
        """注入段内 wsl_native 非映射 → 空配置（不把列表当配置用）。"""
        from plugin import IsolationGuard

        guard = IsolationGuard(config={"isolation_config": {"providers": {"wsl_native": []}}})

        assert guard._wsl_native_cfg == {}
        assert guard._config_probe_error is None

    def test_no_injection_source_reports_chain_break(self) -> None:
        """两注入源皆缺 = 配置注入链断裂 → 断裂说明 + probe_error 三态
        （容器要求型工具 fail-closed 拒绝，不静默回落 docker 探测）。"""
        from plugin import IsolationGuard

        guard = IsolationGuard(config={})

        assert guard._wsl_native_cfg == {}
        assert guard._config_probe_error is not None
        assert "isolation_config 未注入" in guard._config_probe_error
        assert guard._docker_available is False
        assert guard._docker_probe_error == guard._config_probe_error

    def test_injected_namespace_absent_wsl_native_defaults_docker(self) -> None:
        """注入命名空间在场而 wsl_native 缺席 = 未配置该后端（合法缺省）。"""
        from plugin import IsolationGuard

        guard = IsolationGuard(config={"isolation_config": {"providers": {"cua": {}}}})

        assert guard._wsl_native_cfg == {}
        assert guard._config_probe_error is None


class TestNoLocalManagerGate:
    """guard 零直连静态闸（B5 职责归位）。

    容器获取唯一属主 = isolation 服务（sidecar 单例，配额/熔断按服务进程
    单实例计）。guard 源面零管理器引用 = N+1 份内存注册态的根因面被拔除，
    本闸以 grep 断言执法（源码文本，任何回潮即红）。
    """

    def test_guard_source_has_no_manager_references(self) -> None:
        source = (_PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        assert "IsolationManager" not in source
        assert "_get_manager" not in source
        # manager 方法面不得直调（落地只经 create_env 能力调用）
        assert "get_or_create_environment" not in source
        assert "ensure_container_alive" not in source
        assert "get_environment" not in source
        # 容器获取只走服务正门
        assert "isolation.create_env" in source

    def test_server_source_has_no_manager_references(self) -> None:
        source = (_PLUGIN_DIR / "server.py").read_text(encoding="utf-8")
        assert "IsolationManager" not in source
        assert "isolation.manager" not in source


class TestProbeBackendConfigBreakage:
    """_probe_backend 断裂分支：配置注入链断裂 → probe_error，不探测 docker。"""

    def test_recheck_with_broken_injection_reports_error_not_docker(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from plugin import IsolationGuard

        def _no_docker(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("注入链断裂时不得回落 docker 探测")

        monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_no_docker))
        guard = IsolationGuard(config={"docker_available": True})  # 注入两源皆缺
        assert guard._config_probe_error is not None

        guard._probe_backend()  # 冷却复检路径同断裂语义

        assert guard._docker_available is False
        assert guard._docker_probe_error == guard._config_probe_error


# ═══════════════════ wsl_native 平台门（装载出口单点归一） ═══════════════════


class TestWslNativePlatformGate:
    """wsl_native 平台门（ADR 2026-10-09-isolation-backend-platform-adaptive-selection）。

    ``enabled`` 语义收敛为「Windows 上启用 wsl_native」：装载出口处
    ``os.name != "nt"`` 时 ``_wsl_native_cfg`` 单点归空（等价「未配置该后端」
    合法缺省），下游探测分派与复检引擎自愈门自动继承。断言探测目标选择
    （可观察行为），平台经 monkeypatch os.name 覆盖（ubuntu CI 车道天然
    以 posix 执法本语义）。
    """

    _ENABLED_CFG = {"providers": {"wsl_native": {"enabled": True, "distro": "Ubuntu"}}}

    def test_enabled_posix_linux_probes_linux_native_not_wsl(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """enabled + posix-Linux：wsl 门让位，探测目标 = linux_native（Linux
        默认后端，ADR 2026-10-12）；wsl/docker 探测均不得被调用。"""
        import plugin as plugin_module
        from plugin import IsolationGuard

        def _no_wsl(self: Any) -> tuple[str, str]:
            raise AssertionError("posix 平台门下不得探测 WSL（wsl.exe 不存在是先验事实）")

        def _no_docker() -> tuple[str, str]:
            raise AssertionError("Linux 默认后端 linux_native 时不得探测 docker")

        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: True)
        monkeypatch.setattr(IsolationGuard, "_detect_wsl_native", _no_wsl)
        monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_no_docker))
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: f"/usr/bin/{name}")

        guard = IsolationGuard(config=dict(self._ENABLED_CFG))

        # 装载出口归空（wsl）+ 装载期探测走 bwrap 并生效
        assert guard._wsl_native_cfg == {}
        assert guard._config_probe_error is None
        assert guard._docker_available is True

    def test_enabled_macos_probes_docker_not_wsl(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """enabled + posix 非 Linux（macOS）：wsl/linux 门都让位，探测走 docker。"""
        import plugin as plugin_module
        from plugin import IsolationGuard

        def _no_wsl(self: Any) -> tuple[str, str]:
            raise AssertionError("posix 平台门下不得探测 WSL（wsl.exe 不存在是先验事实）")

        docker_calls: list[int] = []

        def _docker_ok() -> tuple[str, str]:
            docker_calls.append(1)
            return (IsolationGuard._PROBE_AVAILABLE, "")

        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: False)
        monkeypatch.setattr(IsolationGuard, "_detect_wsl_native", _no_wsl)
        monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_docker_ok))

        guard = IsolationGuard(config=dict(self._ENABLED_CFG))

        assert guard._wsl_native_cfg == {}
        assert guard._config_probe_error is None
        assert docker_calls == [1]
        assert guard._docker_available is True

    def test_enabled_nt_still_probes_wsl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enabled + nt：装载保留配置原样，探测仍走 WSL、不探 docker。"""
        import plugin as plugin_module
        from plugin import IsolationGuard

        def _no_docker() -> tuple[str, str]:
            raise AssertionError("Windows + wsl_native 启用时不得回落 docker 探测")

        def _wsl_ok(self: Any) -> tuple[str, str]:
            return (IsolationGuard._PROBE_AVAILABLE, "")

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(plugin_module, "_is_linux_platform", lambda: False)
        monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_no_docker))
        monkeypatch.setattr(IsolationGuard, "_detect_wsl_native", _wsl_ok)

        guard = IsolationGuard(config=dict(self._ENABLED_CFG))

        assert guard._wsl_native_cfg.get("enabled") is True
        assert guard._docker_available is True

    async def test_enabled_posix_recheck_runs_docker_engine_self_heal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """posix + enabled：归空后复检路径的 docker 引擎自愈（_ensure_engine）不被跳过。

        未归一时 cfg.enabled=True 会跳过引擎自愈（wsl 专属分支）；平台门单点
        归一后复检门自动对 docker 生效——经 execute() 全路径断言（可观察行为）。
        """
        from _libs.pipeline_base.plugin import PluginContext
        from _libs.pipeline_base.types import StateKeys

        from plugin import IsolationGuard

        def _docker_absent() -> tuple[str, str]:
            return (IsolationGuard._PROBE_ABSENT, "")

        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(IsolationGuard, "_detect_docker", staticmethod(_docker_absent))

        cfg = dict(self._ENABLED_CFG)
        cfg["providers"]["linux_native"] = {"enabled": False}  # 钉 docker 后端（引擎自愈门按后端分派）
        guard = IsolationGuard(config=cfg)
        assert guard._docker_auto is True  # 自动检测来源（无显式 docker_available）
        assert guard._docker_available is False

        host_policy = MagicMock()
        host_policy.isolation = "non_isolated"
        guard._decider.resolve = MagicMock(return_value=host_policy)
        guard._ensure_engine = MagicMock()  # 引擎自愈是 wsl_health 外部依赖边界
        guard._docker_checked_at = time.monotonic() - 9999  # 越过复检冷却

        ctx = PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {}}],
            },
            config={},
            _services={},
        )
        await guard.execute(ctx)

        guard._ensure_engine.assert_called_once()


# ═══════════════ WSL 探测三态 ═══════════════# ═══════════════ WSL 探测三态 ═══════════════


class TestDetectWslNativeTristate:
    """``_detect_wsl_native`` 三态语义（plugin.py:245-274）。

    wsl.exe 属外部命令面（shutil.which / subprocess.run 替身）；发行版列表
    解码与比对逻辑全真实（复用 WslNativeProvider 单源解码）。

    配置直注（绕开装载归一）：装载出口的平台归一是生产语义（posix 下
    ``_wsl_native_cfg`` 归空、distro 回落缺省），本组测的是探测逻辑本身
    （cfg.enabled/distro → 探测三态），故 ``_guard`` 构造后直接回填
    ``_wsl_native_cfg``——双平台全值运行、零全局污染。不钉 os.name：
    monkeypatch os.name 在真 posix 是全局投毒（pathlib ``Path.__new__``
    动态读 os.name，钉住窗口内任何 ``Path()`` 产出 WindowsPath，任何
    resolve/exists 即 NotImplementedError，WSL 真机冒烟实证会打红车道）。
    posix 下的探测分派与装载归一行为归 TestWslNativePlatformGate。
    """

    def _guard(self, **cfg: Any) -> Any:
        from plugin import IsolationGuard

        guard = IsolationGuard(config={
            "providers": {
                "wsl_native": {"enabled": True, "distro": "Ubuntu", **cfg},
                "linux_native": {"enabled": False},
            },
        })
        # 直注与构造参数同源的 cfg：探测逻辑的输入契约，不经装载归一
        guard._wsl_native_cfg = {"enabled": True, "distro": cfg.get("distro", "Ubuntu")}
        # 钉探测目标为 wsl（linux_native 默认启用会抢先分派）
        guard._linux_native_cfg = {"enabled": False}
        return guard

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
        """_probe_backend 按 cfg.enabled 分派：enabled=True → 探 WSL，不探 docker。

        cfg 经 _guard 直注（分派语义只读 cfg；平台门决定的 cfg 实际内容归
        TestWslNativePlatformGate）。断言窗口取显式 _probe_backend 之后的帧：
        构造期探测的目标由平台决定（posix 下探 docker），不计入分派断言。
        """
        import shutil
        import subprocess

        seen: list[list[str]] = []

        def _run(argv: list[str], **_k: Any) -> Any:
            seen.append(list(argv))
            return _Completed(0, stdout="Ubuntu\n".encode("utf-8"))

        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(subprocess, "run", _run)

        guard = self._guard()
        constructed = len(seen)  # 构造期探测（目标随平台），不计入分派断言

        guard._probe_backend()

        dispatched = seen[constructed:]
        assert dispatched and dispatched[0][:2] == ["wsl", "-l"]
        assert all(argv[:1] != ["docker"] for argv in dispatched)
        assert guard._docker_available is True


# ═══════════════ create_env 信封解析 ═══════════════


class TestParseCreateEnvResponse:
    """``_parse_create_env_response`` 信封解析（isolation.create_env → 落地三元组）。

    成功 → (env_id, exec_backend)；业务失败（任一形态）→ (None, None)。
    """

    def _parse(self, raw: Any) -> tuple[str | None, dict[str, Any] | None]:
        from plugin import IsolationGuard

        return IsolationGuard._parse_create_env_response(raw, "/ws")

    def test_success_envelope_yields_env_id(self) -> None:
        env_id, backend = self._parse(
            {"success": True, "data": {"env_id": "c1", "status": "ready"}}
        )
        assert env_id == "c1"
        assert backend is None

    def test_exec_backend_dict_passthrough(self) -> None:
        """data.exec_backend 是映射 → 原样返回（原生环境 bash 传输通道）。"""
        backend = {"kind": "wsl_native", "distro": "Ubuntu"}
        env_id, parsed = self._parse(
            {"success": True, "data": {"env_id": "c1", "exec_backend": backend}}
        )
        assert env_id == "c1"
        assert parsed == backend

    def test_exec_backend_non_dict_returns_none(self) -> None:
        """exec_backend 非映射 → None（不把不可信形态注入工具参数）。"""
        _, backend = self._parse(
            {"success": True, "data": {"env_id": "c1", "exec_backend": "wsl"}}
        )
        assert backend is None

    def test_explicit_failure_envelope_returns_none(self) -> None:
        """信封 success=False（服务显式失败，error 携原因）→ (None, None)。"""
        env_id, backend = self._parse(
            {"success": False, "error": "隔离环境数量已达上限 (3/3)"}
        )
        assert env_id is None
        assert backend is None

    def test_business_error_payload_returns_none(self) -> None:
        """data 带 error 键（裸返 error dict 旧形态）→ (None, None)。"""
        env_id, backend = self._parse({"success": True, "data": {"error": "隔离服务未初始化"}})
        assert env_id is None
        assert backend is None

    def test_error_placeholder_env_returns_none(self) -> None:
        """status=error（创建失败的 ERROR 占位环境，env_id 是假 id）→ (None, None)。"""
        env_id, backend = self._parse({
            "success": True,
            "data": {"env_id": "docker-session", "status": "error", "provider_error": "boom"},
        })
        assert env_id is None
        assert backend is None

    def test_missing_env_id_returns_none(self) -> None:
        """成功信封缺 env_id（服务异常形态）→ (None, None)，不注入空 id。"""
        env_id, _ = self._parse({"success": True, "data": {"status": "ready"}})
        assert env_id is None

    def test_non_dict_raw_returns_none(self) -> None:
        """返回非映射（反序列化异常形态）→ (None, None)。"""
        assert self._parse(None) == (None, None)  # type: ignore[arg-type]
        assert self._parse("boom") == (None, None)  # type: ignore[arg-type]

    def test_missing_data_payload_returns_none(self) -> None:
        """信封缺 data 载荷 → (None, None)。"""
        assert self._parse({"success": True}) == (None, None)


# ═══════════════ exec_backend 注入 ═══════════════


class TestExecBackendInjection:
    """exec_backend 经 ``_inject_container_id`` 注入 bash 参数（plugin.py:570-574）。"""

    def test_exec_backend_injected_for_bash_and_browser(self) -> None:
        """bash_execute 与 browser_* 均收到 _exec_backend（沙箱内 bridge 通路
        据 backend 选 wsl 传输——v1 缺口补全后的契约）。"""
        from plugin import IsolationGuard

        backend = {"kind": "wsl_native"}
        calls = [
            {"name": "bash_execute", "args": {"command": "ls"}},
            {"name": "browser_navigate", "args": {"url": "http://x"}},
        ]

        injected = IsolationGuard._inject_container_id(
            calls,
            {"bash_execute", "browser_navigate"},
            "c-1",
            exec_backend=backend,
            exec_backend_capable={"bash_execute", "browser_navigate"},
        )

        assert injected is not None
        assert injected[0]["args"]["_exec_backend"] == backend
        assert injected[0]["args"]["_container_id"] == "c-1"
        assert injected[1]["args"]["_exec_backend"] == backend
        assert injected[1]["args"]["_container_id"] == "c-1"

    def test_no_exec_backend_omits_key(self) -> None:
        """docker 环境（无 exec_backend）→ 不写 _exec_backend 键。"""
        from plugin import IsolationGuard

        injected = IsolationGuard._inject_container_id(
            [{"name": "bash_execute", "args": {"command": "ls"}}],
            {"bash_execute"},
            "c-1",
            exec_backend=None,
            exec_backend_capable={"bash_execute"},
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
            exec_backend_capable={"bash_execute"},
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
            exec_backend_capable={"bash_execute"},
        )

        assert injected is None
