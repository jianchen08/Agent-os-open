# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""wsl_health.ensure_docker_engine 引擎自愈测试。

背景：WSL2 VM 空闲回收 → dockerd 随 systemd 关闭 → docker 不可达；
插件运行时经 ensure_docker_engine 自愈（幂等保活会话 + 冷却防风暴 +
等待 daemon 就绪），供 DockerProvider.is_available / isolation_guard 复检调用。

隔离策略：powershell/wsl/docker 均属外部依赖，mock 仅限 subprocess
（run/Popen）与同模块探测函数（is_docker_reachable/_keepalive_running）；
time.sleep 掐零避免真等。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_mod() -> Any:
    """动态加载 wsl_health.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_wsl_health_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "wsl_health.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """掐零等待循环里的真睡，测试不拖延。"""
    monkeypatch.setattr(_MOD.time, "sleep", lambda _: None)
    _MOD._last_engine_ensure = 0.0  # 每个用例从可自愈状态出发


def _probe_seq(*values: bool) -> Any:
    """按序返回可达性探测结果的迭代器（不足时恒 False）。"""
    it = iter(values)

    def _probe(timeout: float = 5.0) -> bool:
        try:
            return next(it)
        except StopIteration:
            return False

    return _probe


def _spawn_recorder() -> tuple[list[Any], Any]:
    spawned: list[Any] = []

    def _record_spawn(*args: Any, **kwargs: Any) -> None:
        spawned.append(args)

    return spawned, _record_spawn


class TestEnsureDockerEngine:
    def test_reachable_fast_path_no_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """引擎已可达 → 直接 True，零额外探测/spawn。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert spawned == []

    def test_missing_keepalive_spawns_then_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无保活会话 → detached 拉起 sleep infinity 持有 VM，等 daemon 就绪后 True。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert len(spawned) == 1
        args = spawned[0][0]
        assert "wsl.exe" in args and "sleep infinity" in args

    def test_keepalive_present_waits_without_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """保活会话在但 daemon 未就绪 → 不重复拉起，等待后 True。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", _probe_seq(False, False, True))
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine() is True
        assert spawned == []

    def test_cooldown_prevents_repeated_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """冷却窗口内不可重复拉起（防多插件/多轮次风暴），直接 False。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: False)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False  # 首轮拉起，等满超时
        assert len(spawned) == 1
        assert _MOD.ensure_docker_engine(timeout=5.0) is False  # 冷却内：不拉起不等待
        assert len(spawned) == 1

    def test_unhealthy_timeout_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """等待超时仍不可达 → False（拉起已做，冷却留给下一轮）。"""
        monkeypatch.setattr(_MOD, "is_docker_reachable", lambda timeout=5.0: False)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: False)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False
        assert len(spawned) == 1

    def test_probe_exception_treated_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探测依赖抛异常（docker CLI 缺失等）→ is_docker_reachable 捕获视为不可达。"""
        def _boom(*a: Any, **kw: Any) -> Any:
            raise FileNotFoundError

        monkeypatch.setattr(_MOD.subprocess, "run", _boom)
        monkeypatch.setattr(_MOD, "_keepalive_running", lambda: True)
        spawned, popen = _spawn_recorder()
        monkeypatch.setattr(_MOD.subprocess, "Popen", popen)
        assert _MOD.ensure_docker_engine(timeout=0.001) is False
        assert spawned == []  # 保活在，只等待不拉起
