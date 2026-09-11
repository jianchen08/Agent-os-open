# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""IsolationGuard Docker 探测三态测试（C1：探测异常 ≠ 未安装）。

锁定契约：
1. _detect_docker 三态：available / absent（含 daemon 明确不可达 rc!=0）/
   probe_error（subprocess 异常，携带异常摘要）——mock 的是 shutil/subprocess
   外部边界，不是插件内部方法。
2. probe_error → 容器要求型工具 fail-closed 拒绝，拒绝原因携带探测异常原文
   （tool_core check_tool_blocked 按 execution_contexts[].blocked 拦截并把
   reason 回传 LLM）；error 级日志留痕。
3. absent → 维持既有不可用处置（容器要求型拒绝），host 执行上下文显式标记
   isolation_mode=host（降级不再无痕）+ warn 日志。
4. probe_error 后 daemon 恢复 → 冷却复检解除（恢复链路不受三态化影响）。
"""
import logging
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests._isolation_path  # noqa: F401  # 注入 isolation_guard 插件目录到 sys.path

# 平铺共享裸名自防御（同 test_isolation_docker_recheck）：先导的测试（add_plugin_dir
# 系）会把自家目录钉在 sys.path[0] 并缓存 plugin 模块；_isolation_path 被更早文件
# 缓存后其注入不再重跑，conftest 收集期逐出后按残序重解析仍会劫持（实测
# security_check/plugin.py 抢走 `plugin` 名）。本目录置顶 + 裸名逐出后再 import。
_ISOLATION_GUARD_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard",
)
if _ISOLATION_GUARD_DIR in sys.path:
    sys.path.remove(_ISOLATION_GUARD_DIR)
sys.path.insert(0, _ISOLATION_GUARD_DIR)
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

from agentos_plugin_sdk.isolation_types import IsolationLevel  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402
import plugin as plugin_module  # noqa: E402,F401  # 模块对象钉住：裸名串扰治理 evict 后引用不断
from plugin import IsolationGuard  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_wsl_health(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """引擎自愈在插件内延迟 import wsl_health，测试环境用 fake 占位（不真实拉起 WSL）。"""
    fake = types.SimpleNamespace()
    fake.ensure_docker_engine = MagicMock()
    monkeypatch.setitem(sys.modules, "wsl_health", fake)
    return fake


def _which_result(found: bool):
    return (lambda name: "C:/Program Files/docker.exe" if found else None)


def _docker_run_ok() -> MagicMock:
    done = MagicMock()
    done.returncode = 0
    return done


def _docker_run_fail() -> MagicMock:
    done = MagicMock()
    done.returncode = 1
    return done


class TestDetectDockerTriState:
    """_detect_docker 三态：mock shutil.which / subprocess.run 外部边界。"""

    def test_which_missing_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """docker CLI 不存在 → absent（明确不可用，非异常）。"""
        monkeypatch.setattr("shutil.which", _which_result(False))
        status, detail = IsolationGuard._detect_docker()
        assert status == "absent"
        assert detail == ""

    def test_daemon_unreachable_returncode_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI 在但 daemon 明确回答不可达（rc!=0）→ absent（有确定答案）。"""
        monkeypatch.setattr("shutil.which", _which_result(True))
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _docker_run_fail())
        status, detail = IsolationGuard._detect_docker()
        assert status == "absent"
        assert detail == ""

    def test_daemon_ok_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """daemon 应答正常 → available。"""
        monkeypatch.setattr("shutil.which", _which_result(True))
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _docker_run_ok())
        status, detail = IsolationGuard._detect_docker()
        assert status == "available"
        assert detail == ""

    @pytest.mark.parametrize("exc", [
        subprocess.TimeoutExpired(cmd="docker", timeout=3),
        PermissionError("docker.exe access denied"),
        OSError("spawn failed"),
    ])
    def test_subprocess_exception_probe_error(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        """subprocess 异常（超时/权限/瞬态）→ probe_error 且携带异常摘要（多组输入）。"""
        monkeypatch.setattr("shutil.which", _which_result(True))

        def _raise(*a: object, **k: object) -> object:
            raise exc

        monkeypatch.setattr("subprocess.run", _raise)
        status, detail = IsolationGuard._detect_docker()
        assert status == "probe_error"
        assert str(exc) in detail


def _make_ctx(tool: str = "bash_execute") -> PluginContext:
    """L2 子任务 tool_execute 上下文（同 test_isolation_docker_recheck 基线）。"""
    return PluginContext(
        state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "L2",
            StateKeys.RAW_TOOL_CALLS: [{"name": tool, "args": {}}],
        },
        config={},
        _services={},
    )


def _container_policy(guard: IsolationGuard) -> None:
    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    guard._decider.resolve = MagicMock(return_value=mock_policy)


def _guard_with_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    which_found: bool,
    run: object,
) -> IsolationGuard:
    """在外部探测边界被 mock 的环境下构造走自动检测路径的 guard。"""
    monkeypatch.setattr("shutil.which", _which_result(which_found))
    monkeypatch.setattr("subprocess.run", run)
    with patch("decider.IsolationDecider"):
        return IsolationGuard(config={})


class TestProbeErrorFailClosed:
    async def test_probe_error_rejects_container_tool_with_exception_text(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """探测异常 → bash_execute 被拒，原因含异常原文；error 日志留痕。"""
        exc = subprocess.TimeoutExpired(cmd="docker version", timeout=3)
        guard = _guard_with_probe(
            monkeypatch,
            which_found=True,
            run=lambda *a, **k: (_ for _ in ()).throw(exc),
        )
        assert guard._docker_available is False
        assert guard._docker_probe_error == str(exc)
        _container_policy(guard)

        with caplog.at_level(logging.ERROR, logger=plugin_module.__name__):
            result = await guard.execute(_make_ctx())

        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "denied"
        assert ctx["blocked"] is True
        assert "docker_probe_error" in ctx["reason"]
        assert str(exc) in ctx["reason"]
        assert "isolation_mode" not in ctx  # denied 不执行，无 host 标记
        assert any("探测异常" in r.message and r.levelno == logging.ERROR for r in caplog.records)

    async def test_probe_error_does_not_silently_run_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """探测异常期容器要求型工具绝不降级 provider=host（fail-open 回归）。"""
        exc = PermissionError("access denied")
        guard = _guard_with_probe(
            monkeypatch,
            which_found=True,
            run=lambda *a, **k: (_ for _ in ()).throw(exc),
        )
        _container_policy(guard)

        result = await guard.execute(_make_ctx())
        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "denied"
        assert ctx["blocked"] is True

    async def test_probe_error_recovers_after_recheck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """探测异常后 daemon 恢复：冷却复检清掉 probe_error 并解除拦截。"""
        guard = _guard_with_probe(
            monkeypatch,
            which_found=True,
            run=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="docker", timeout=3)),
        )
        _container_policy(guard)
        guard._get_or_create_container = AsyncMock(return_value="mock-container-1")

        # 越过冷却窗口；复检时探测边界转为健康
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _docker_run_ok())
        guard._docker_checked_at = time.monotonic() - 9999
        result = await guard.execute(_make_ctx())

        assert guard._docker_available is True
        assert guard._docker_probe_error is None
        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "docker"
        assert not ctx.get("blocked")


class TestAbsentDegradeMarker:
    async def test_absent_marks_host_context_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """docker 未安装 → host 上下文显式带 isolation_mode=host + warn 日志。"""
        guard = _guard_with_probe(monkeypatch, which_found=False, run=lambda *a, **k: None)
        assert guard._docker_available is False
        assert guard._docker_probe_error is None

        mock_policy = MagicMock()
        mock_policy.isolation = IsolationLevel.HOST
        guard._decider.resolve = MagicMock(return_value=mock_policy)

        with caplog.at_level(logging.WARNING, logger=plugin_module.__name__):
            result = await guard.execute(_make_ctx("file_write"))

        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "host"
        assert ctx["isolation_mode"] == "host"
        assert not ctx.get("blocked")
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    async def test_absent_still_denies_container_required_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker 未安装 → 容器要求型工具仍拒绝（既有语义不变），原因不含 probe_error。"""
        guard = _guard_with_probe(monkeypatch, which_found=False, run=lambda *a, **k: None)
        _container_policy(guard)

        result = await guard.execute(_make_ctx())
        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "denied"
        assert ctx["blocked"] is True
        assert ctx["reason"] == "docker_unavailable_container_required"

    async def test_docker_available_host_context_has_no_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker 可用时 host 执行属策略常态，不带降级标记。"""
        guard = _guard_with_probe(monkeypatch, which_found=True, run=lambda *a, **k: _docker_run_ok())
        mock_policy = MagicMock()
        mock_policy.isolation = IsolationLevel.HOST
        guard._decider.resolve = MagicMock(return_value=mock_policy)

        result = await guard.execute(_make_ctx("file_write"))
        ctx = result.state_updates["execution_contexts"][0]
        assert ctx["provider"] == "host"
        assert "isolation_mode" not in ctx
