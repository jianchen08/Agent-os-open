# @feature: FP-0.2.二 isolation_guard 缺口分支补测 | @ci: python-coverage
"""IsolationGuard 探测三态与 force_host 拒绝分支补测（coverage.xml 缺口行靶单）。

覆盖（断输入→输出/副作用，不钉实现）：

1. **Docker 探测三态**（``_detect_docker``，subprocess/shutil 为外部依赖）：
   - CLI 缺失 → ("absent", "")；
   - CLI 在位 + returncode==0 → ("available", "")；
   - CLI 在位 + returncode!=0 → ("absent", "")（daemon 明确回答不可达）；
   - subprocess 抛异常（超时/权限）→ ("probe_error", 异常摘要)（可用性不可
     验证，容器要求型工具 fail-closed）。
2. **探测结果归一**（``_apply_probe_result``）：available/absent/probe_error
   三态写回实例状态，probe_error 携带摘要、其余清空摘要。
3. **构造期后端探测**：未显式给 docker_available 时走真实探测（_docker_auto=True），
   显式给则信任不刷新（_docker_auto=False）。
4. **force_host 语义**（P0-安全）：要求容器隔离的工具在 force_host 下
   **拒绝执行**（denied + blocked），本身走 host 的工具仍路由 host。
5. **decider.resolve**：直接返回工具策略（不做可用性检查，None 默认参数路径）。

不可达/环境依赖残留（逐条说明）：
- ``_detect_wsl_native`` 的深度分支（发行版列表解码命中/未命中）依赖真实
  wsl.exe 与发行版状态，属跨进程外部环境；本文件以 docker 三态覆盖同构
  语义（三态归一与 fail-closed 判据由 ``_apply_probe_result`` 共用）。
- ``IsolationGuard.__init__`` 中 ``self._docker_probe_error`` 非空的 ERROR
  日志仅在真实探测异常时出现，由 ``_apply_probe_result`` 用例覆盖同一状态。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard"
_SHARED_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared"
for _d in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from agentos_plugin_sdk.isolation_types import IsolationLevel  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402


def _guard(**config: Any) -> Any:
    """构造守卫（钉 docker 后端，避免真身 wsl_native 配置改变探测目标）。"""
    from plugin import IsolationGuard

    return IsolationGuard(config={
        "providers": {"wsl_native": {"enabled": False}},
        **config,
    })


def _patch_docker_probe(
    monkeypatch: pytest.MonkeyPatch, *, which: str | None, run: Any,
) -> None:
    """替换探测的两个外部依赖：shutil.which 与 subprocess.run（均为外部命令面）。"""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: which)
    if isinstance(run, BaseException):
        def _raise(*_a: Any, **_k: Any) -> Any:
            raise run

        monkeypatch.setattr(subprocess, "run", _raise)
    else:
        monkeypatch.setattr(subprocess, "run", run)


class _Completed:
    def __init__(self, returncode: int, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


# ═══════════════ Docker 探测三态 ═══════════════


class TestDetectDockerTristate:
    """psutil/subprocess 之外的命令面探测：三态区分安全语义（探测故障 ≠ 未安装）。"""

    def test_cli_absent_returns_absent_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """docker CLI 缺失 → ("absent","")，且不 spawn 子进程。"""
        from plugin import IsolationGuard

        calls: list[Any] = []

        def _run(*a: Any, **k: Any) -> Any:
            calls.append((a, k))
            return _Completed(0)

        _patch_docker_probe(monkeypatch, which=None, run=_run)

        assert IsolationGuard._detect_docker() == ("absent", "")
        assert calls == [], "CLI 缺失时不得发起探测命令"

    def test_daemon_ok_returns_available(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """docker version 返回 0 → ("available","")（daemon 应答正常）。"""
        from plugin import IsolationGuard

        seen: list[list[str]] = []

        def _run(argv: list[str], **_k: Any) -> Any:
            seen.append(argv)
            return _Completed(0)

        _patch_docker_probe(monkeypatch, which="/usr/bin/docker", run=_run)

        assert IsolationGuard._detect_docker() == ("available", "")
        assert seen and seen[0][:2] == ["docker", "version"]

    def test_daemon_unreachable_returns_absent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """docker version 非零 → ("absent","")（确定答案：明确不可用）。"""
        from plugin import IsolationGuard

        _patch_docker_probe(
            monkeypatch, which="/usr/bin/docker", run=lambda *a, **k: _Completed(1)
        )

        assert IsolationGuard._detect_docker() == ("absent", "")

    @pytest.mark.parametrize(
        "error",
        [TimeoutError("probe timed out"), PermissionError("access denied")],
    )
    def test_probe_exception_returns_probe_error_with_detail(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception,
    ) -> None:
        """subprocess 异常（超时/权限）→ ("probe_error", 摘要)：可用性不可验证。"""
        from plugin import IsolationGuard

        _patch_docker_probe(monkeypatch, which="/usr/bin/docker", run=error)

        status, detail = IsolationGuard._detect_docker()

        assert status == "probe_error"
        assert detail == str(error)
        assert detail, "摘要必须非空（调用方据此记日志与判 fail-closed）"


class TestApplyProbeResult:
    """探测三态写回实例状态：available=True / absent=False / probe_error=False+摘要。"""

    @pytest.mark.parametrize(
        ("probe", "expect_available", "expect_error"),
        [
            (("available", ""), True, None),
            (("absent", ""), False, None),
            (("probe_error", "timeout"), False, "timeout"),
            # 从 probe_error 态恢复到 available → 摘要必须清空（不残留旧故障）
            (("available", "stale-detail"), True, None),
        ],
    )
    def test_result_normalization(
        self, probe: tuple[str, str], expect_available: bool, expect_error: str | None,
    ) -> None:
        guard = _guard(docker_available=False, force_host=False)
        guard._docker_probe_error = "previous-error"

        guard._apply_probe_result(probe)

        assert guard._docker_available is expect_available
        assert guard._docker_probe_error == expect_error

    def test_constructor_trusts_explicit_config(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """显式 docker_available → 信任不刷新（_docker_auto=False，不发起探测）。"""
        from plugin import IsolationGuard

        calls: list[str] = []
        monkeypatch.setattr(
            IsolationGuard, "_detect_docker",
            staticmethod(lambda: (calls.append("probe"), ("available", ""))[1]),
        )

        guard = IsolationGuard(config={"docker_available": False})

        assert guard._docker_available is False
        assert calls == []
        assert guard._docker_auto is False

    def test_constructor_probes_when_config_absent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """未显式给 → 构造期真实探测并标 auto 来源（daemon 恢复可经复检解除）。"""
        from plugin import IsolationGuard

        _patch_docker_probe(
            monkeypatch, which="/usr/bin/docker", run=lambda *a, **k: _Completed(0)
        )

        guard = IsolationGuard(config={"providers": {"wsl_native": {"enabled": False}}})

        assert guard._docker_auto is True
        assert guard._docker_available is True

    def test_constructor_records_probe_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """构造期探测异常 → 可用性 False + 摘要留存（fail-closed 拒绝容器工具）。"""
        from plugin import IsolationGuard

        _patch_docker_probe(
            monkeypatch, which="/usr/bin/docker", run=TimeoutError("daemon hung")
        )

        guard = IsolationGuard(config={"providers": {"wsl_native": {"enabled": False}}})

        assert guard._docker_available is False
        assert guard._docker_probe_error == "daemon hung"


# ═══════════════ force_host 语义 ═══════════════


def _tool_state(tool_name: str, **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        "workspace": "/host/ws",
        StateKeys.RAW_TOOL_CALLS: [{"name": tool_name, "args": {"command": "ls"}}],
        "execution_context": {"isolation": {"level": "isolated"}},
    }
    state.update(extra)
    return state


class TestForceHostPolicy:
    @pytest.fixture
    def guard(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        g = _guard(docker_available=True, force_host=True)
        # 策略真值：bash_execute 要求容器、file_read 走 host
        class _Policy:
            def __init__(self, isolation: Any) -> None:
                self.isolation = isolation

        def _resolve(tool_name: str, category: Any = None) -> Any:
            return _Policy(
                IsolationLevel.CONTAINER if tool_name == "bash_execute" else IsolationLevel.HOST
            )

        monkeypatch.setattr(g._decider, "resolve", _resolve)
        return g

    async def test_container_tool_denied_not_downgraded(self, guard: Any) -> None:
        """要求容器的工具在 force_host 下拒绝执行（blocked），绝不降级宿主裸跑。"""
        result = await guard.execute(
            PluginContext(state=_tool_state("bash_execute"), config={})
        )

        ctx0 = (result.state_updates or {}).get("execution_contexts", [{}])[0]
        assert ctx0["provider"] == "denied"
        assert ctx0["reason"] == "force_host_denied_by_policy"
        assert ctx0["blocked"] is True
        assert ctx0.get("workspace") == "/host/ws"

    async def test_host_tool_stays_host_under_force_host(self, guard: Any) -> None:
        """本身走 host 的工具在 force_host 下照常 host（force_host 只约束容器工具）。"""
        result = await guard.execute(
            PluginContext(state=_tool_state("file_read"), config={})
        )

        ctx0 = (result.state_updates or {}).get("execution_contexts", [{}])[0]
        assert ctx0["provider"] == "host"
        assert ctx0["reason"] == "force_host"
        assert not ctx0.get("blocked")

    async def test_denied_context_is_observable_state(self, guard: Any) -> None:
        """denied 是显式 state（blocked + provider），供下游路由拦截（非日志级信号）。"""
        result = await guard.execute(
            PluginContext(state=_tool_state("bash_execute"), config={})
        )

        updates = result.state_updates or {}
        contexts = updates.get("execution_contexts")
        assert isinstance(contexts, list) and contexts
        assert contexts[0]["blocked"] is True
        assert contexts[0]["tool_name"] == "bash_execute"


# ═══════════════ decider.resolve ═══════════════


class TestDeciderResolve:
    def test_resolve_returns_policy_without_availability_check(self) -> None:
        """``resolve`` 不做可用性检查（可用性检查属 check_availability 路径）。"""
        import decider as decider_mod

        d = decider_mod.IsolationDecider()

        from_short = d.resolve("bash_execute")
        from_none = d.resolve("bash_execute", None)

        assert from_short.isolation == from_none.isolation
        assert from_short == from_none, "缺省 category 与显式 None 走同一解析路径"

    def test_policy_loader_property_exposes_loader(self) -> None:
        """policy_loader 属性暴露同一加载器实例（调用方复用同一策略真值源）。"""
        import decider as decider_mod

        d = decider_mod.IsolationDecider()

        assert d.policy_loader is d.policy_loader
        assert d.policy_loader.resolve("bash_execute").isolation == d.resolve("bash_execute").isolation
