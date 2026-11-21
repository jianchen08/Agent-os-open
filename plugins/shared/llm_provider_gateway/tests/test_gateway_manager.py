# @feature: FP-LLM网关随包分发 | @ci: python-coverage
"""gateway_manager 网关进程管理行为测试（设计真值=网关随包分发方案_20261121.md 修订版）。

覆盖面：配置归一化（默认值/命名空间/env 覆盖/非法形态）、端口三态判定
（①探活命中复用不拉起 ②端口空闲拉起+健康等待 ③端口被第三方占用
fail-visible 不误杀）、健康等待超时（fake clock）、意外退出退避重启与熔断、
stop 收树（外部网关不受扰）、server config 种子幂等（copy-if-missing + 令牌
替换 + 管理密钥生成）。外部依赖全替身：HTTP 探活、TCP 端口探测、进程 spawn、
树杀、sleep 全部注入 fake，时序用 fake clock（模块命名空间 ``_time`` 打桩），
零真网络零真拉起。

探活桩约定（churn 类测试）：``pending_health`` 状态机——ensure 入口探活恒失败
（网关不在），spawn 后首个健康探活命中；保证「重拉→健康→再退」链路确定性。
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gateway_manager as gm
import pytest
from gateway_manager import GatewayConfig, GatewayProcessManager, read_gateway_config

pytestmark = pytest.mark.unit

# 长活 poll 脚本：10^6 次 poll 额度（测试内单进程 poll 调用至多数百次，远不耗尽）；
# 不得用无界大列表（10^9 级指针数组 = 8GB 分配，内存压力致套件假死）。
_ALIVE_POLLS = [None] * 10**6


# ── 桩件 ─────────────────────────────────────────────────────────────────


@dataclass
class FakeProc:
    """进程句柄桩：poll 序列化脚本驱动（None=活，int=已退出；耗尽后复用末值）。"""

    pid: int = 4242
    polls: list[int | None] = field(default_factory=lambda: list(_ALIVE_POLLS))
    poll_idx: int = 0

    def poll(self) -> int | None:
        if self.poll_idx < len(self.polls):
            value = self.polls[self.poll_idx]
        else:
            value = self.polls[-1]
        self.poll_idx += 1
        return value


class FakeClock:
    """fake clock：monotonic 可控；sleep 注入件按步进推进并记录调用。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        # 让出事件循环：纯计算协程若不让出，活进程的 watcher 轮询会在单步内
        # 无限自旋（poll→sleep→poll…永不挂起），饿死同循环的测试任务。
        await asyncio.sleep(0)


class Harness:
    """manager 测试装具：全部边界替身 + 调用记录。

    spawn_polls 控制每次 spawn 出的进程 poll 脚本（默认长活）；
    probe 状态机：入口探活失败、spawn 后首个健康探活命中。
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        spawn_polls: list[int | None] | None = None,
        port_free: bool = True,
        spawner_error: OSError | None = None,
    ) -> None:
        self.clock = FakeClock()
        # fake clock 打桩：模块命名空间 _time（项目约定），不改全局 time.monotonic
        monkeypatch.setattr(gm, "_time", self.clock)
        self.probe_calls: list[str] = []
        self.port_calls: list[int] = []
        self.port_free = port_free
        self.spawn_calls: list[GatewayConfig] = []
        self.spawned_procs: list[FakeProc] = []
        self.kill_calls: list[int] = []
        self.spawner_error = spawner_error
        self._spawn_polls = spawn_polls
        self._pending_health = False

    def _probe(self, base_url: str, timeout_secs: float) -> tuple[bool, str]:
        self.probe_calls.append(base_url)
        if self._pending_health:
            self._pending_health = False
            return (True, "HTTP 200")
        return (False, "refused")

    async def _port_probe(self, host: str, port: int, timeout_secs: float) -> tuple[bool, str]:
        self.port_calls.append(port)
        if self.port_free:
            return True, "refused"
        return False, "connected"

    def _spawner(self, config: GatewayConfig) -> FakeProc:
        self.spawn_calls.append(config)
        if self.spawner_error is not None:
            raise self.spawner_error
        self._pending_health = True
        proc = FakeProc(pid=4000 + len(self.spawned_procs), polls=list(self._spawn_polls or _ALIVE_POLLS))
        self.spawned_procs.append(proc)
        return proc

    def _killer(self, pid: int) -> None:
        self.kill_calls.append(pid)

    def manager(self, config: GatewayConfig) -> GatewayProcessManager:
        return GatewayProcessManager(
            config,
            probe=self._probe,
            port_probe=self._port_probe,
            spawner=self._spawner,
            killer=self._killer,
            sleep=self.clock.sleep,
        )


def _config(tmp_path: Path, **overrides: Any) -> GatewayConfig:
    base = dict(
        base_url="http://127.0.0.1:8317",
        port=8317,
        binary_path=tmp_path / "bin" / "cli-proxy-api.exe",
        server_config_path=tmp_path / "user" / "config" / "plugins" / "llm_provider_gateway" / "gateway_server.yaml",
        auth_dir=tmp_path / "user" / "config" / "gateway" / "auths",
        log_path=tmp_path / "user" / "logs" / "llm_gateway.log",
    )
    base.update(overrides)
    return GatewayConfig(**base)


def _touch_binary(config: GatewayConfig) -> None:
    config.binary_path.parent.mkdir(parents=True, exist_ok=True)
    config.binary_path.write_bytes(b"fake-exe")


async def _drive(clock: FakeClock, ready: Any, rounds: int = 500, step: float = 0.05) -> bool:
    """让真实 asyncio 任务（watcher）推进直到 ready() 为真；返回是否达成。"""
    for _ in range(rounds):
        if ready():
            return True
        await asyncio.sleep(0)
        await clock.sleep(step)
    return ready()


# ── 配置归一化 ───────────────────────────────────────────────────────────


def test_read_config_defaults_when_no_injection(tmp_path: Path) -> None:
    config = read_gateway_config(None, system="Windows", user_root_dir=tmp_path / "user")
    assert config.base_url == gm.DEFAULT_BASE_URL
    assert config.port == 8317
    assert config.autostart is True
    assert config.binary_path.name == "cli-proxy-api.exe"
    assert config.server_config_path == (
        tmp_path / "user" / "config" / "plugins" / "llm_provider_gateway" / "gateway_server.yaml"
    )
    assert config.auth_dir == tmp_path / "user" / "config" / "gateway" / "auths"
    assert config.log_path == tmp_path / "user" / "logs" / "llm_gateway.log"


def test_read_config_namespace_values_and_posix_binary(tmp_path: Path) -> None:
    config = read_gateway_config(
        {"gateway": {"base_url": "http://127.0.0.1:9100/", "autostart": False}},
        system="Linux",
        user_root_dir=tmp_path / "user",
    )
    assert config.base_url == "http://127.0.0.1:9100"
    assert config.port == 9100
    assert config.autostart is False
    assert config.binary_path.name == "cli-proxy-api"


def test_read_config_env_overrides_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(gm.ENV_BASE_URL, "http://127.0.0.1:7777")
    monkeypatch.setenv(gm.ENV_CLIENT_API_KEY, "sk-env")
    config = read_gateway_config(
        {"gateway": {"base_url": "http://ignored:1", "client_api_key": "sk-ns"}},
        system="Windows",
        user_root_dir=tmp_path / "user",
    )
    assert config.base_url == "http://127.0.0.1:7777"
    assert config.client_api_key == "sk-env"


@pytest.mark.parametrize("bad", ["ftp://127.0.0.1:8317", "not-a-url"])
def test_read_config_invalid_base_url_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="http"):
        read_gateway_config(
            {"gateway": {"base_url": bad}},
            system="Windows",
            user_root_dir=tmp_path / "user",
        )


def test_read_config_missing_user_root_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="user_root_dir"):
        read_gateway_config(None, system="Windows", user_root_dir=None)


def test_read_config_factory_seed_path_from_config_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "configroot"))
    config = read_gateway_config(None, system="Windows", user_root_dir=tmp_path / "user")
    assert config.factory_server_config_path == (
        tmp_path / "configroot" / "plugins" / "llm_provider_gateway" / "gateway_server.yaml"
    )


# ── 端口三态 ─────────────────────────────────────────────────────────────


async def test_state1_probe_hit_reuses_external_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """①探活命中 → 复用外部网关：不 spawn、不探测端口、不树杀。"""
    h = Harness(monkeypatch)

    async def _probe(url: str, t: float) -> tuple[bool, str]:
        return (True, "HTTP 200")

    mgr = gm.GatewayProcessManager(
        _config(tmp_path), probe=_probe, port_probe=h._port_probe, spawner=h._spawner, killer=h._killer,
        sleep=h.clock.sleep,
    )
    result = await mgr.ensure_running()
    assert result["action"] == "reuse"
    assert result["running"] is True
    assert result["owned"] is False
    assert h.spawn_calls == []
    assert h.port_calls == []
    assert h.kill_calls == []


async def test_owned_alive_short_circuits_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """本插件已持有存活进程 → owned_alive，不重复探活不重复拉起。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path)
    _touch_binary(cfg)
    mgr = h.manager(cfg)
    mgr._proc = FakeProc(pid=321)
    result = await mgr.ensure_running()
    assert result["action"] == "owned_alive"
    assert result["running"] is True
    assert result["owned"] is True
    assert h.probe_calls == []
    assert h.spawn_calls == []


async def test_state3_port_conflict_fails_visible_without_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """③探活失败且端口被第三方占用 → port_conflict fail-visible：不拉起不误杀。"""
    h = Harness(monkeypatch, port_free=False)
    result = await h.manager(_config(tmp_path)).ensure_running()
    assert result["action"] == "port_conflict"
    assert result["running"] is False
    assert "8317" in result["detail"]
    assert h.spawn_calls == []
    assert h.kill_calls == []


async def test_state3_port_conflict_reports_other_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """三态判定对非默认端口同样成立（≥2 组区分度输入）。"""
    h = Harness(monkeypatch, port_free=False)
    result = await h.manager(_config(tmp_path, base_url="http://127.0.0.1:9200", port=9200)).ensure_running()
    assert result["action"] == "port_conflict"
    assert "9200" in result["detail"]


async def test_state2_port_free_spawns_and_waits_healthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """②探活失败且端口空闲 → 拉起内置网关并健康等待成功。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path)
    _touch_binary(cfg)
    mgr = h.manager(cfg)
    result = await mgr.ensure_running()
    # ensure 阶段零树杀（拉起+健康等待，无误伤）；收树只发生在显式 stop
    assert h.kill_calls == []
    await mgr.stop()  # 收 watcher + 收树 owned 进程，防 pending task
    assert result["action"] == "spawned"
    assert result["running"] is True
    assert result["owned"] is True
    assert len(h.spawn_calls) == 1
    assert h.port_calls == [8317]
    assert h.kill_calls == [h.spawned_procs[0].pid]


async def test_binary_missing_fails_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """端口空闲但 bin 未填充（未跑配方脚本）→ binary_missing 明确报错不 spawn。"""
    h = Harness(monkeypatch)
    result = await h.manager(_config(tmp_path)).ensure_running()
    assert result["action"] == "binary_missing"
    assert result["running"] is False
    assert h.spawn_calls == []
    assert "ensure_gateway_binary" in result["detail"]


async def test_spawn_oserror_translates_to_spawn_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = Harness(monkeypatch, spawner_error=OSError("boom"))
    cfg = _config(tmp_path)
    _touch_binary(cfg)
    result = await h.manager(cfg).ensure_running()
    assert result["action"] == "spawn_failed"
    assert result["running"] is False
    assert "boom" in result["detail"]


async def test_unhealthy_spawn_is_killed_and_fail_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """拉起后健康等待一直失败 → 超时树杀、fail-visible、不阻塞。

    Harness 默认探活状态机（spawn 后首探命中）到不了 unhealthy 臂——本测直构
    manager 并注入恒失败探活，专测健康等待超时分支。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path, startup_timeout_secs=0.2, poll_interval_secs=0.05)
    _touch_binary(cfg)
    mgr = gm.GatewayProcessManager(
        cfg,
        probe=lambda url, t: (False, "refused"),
        port_probe=h._port_probe,
        spawner=h._spawner,
        killer=h._killer,
        sleep=h.clock.sleep,
    )
    result = await mgr.ensure_running()
    assert result["action"] == "unhealthy"
    assert result["running"] is False
    assert "超时" in result["detail"]
    assert h.kill_calls == [h.spawned_procs[0].pid]


async def test_autostart_false_disables_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """autostart=false → 探活失败后不拉起（disabled），不探测端口。"""
    h = Harness(monkeypatch)
    result = await h.manager(_config(tmp_path, autostart=False)).ensure_running()
    assert result["action"] == "disabled"
    assert result["running"] is False
    assert h.port_calls == []
    assert h.spawn_calls == []


# ── 健康等待（fake clock） ───────────────────────────────────────────────


async def test_wait_healthy_polls_until_probe_hits_with_fake_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """健康等待轮询：第 3 次探活命中即返回；轮询间隔经注入 sleep 推进 fake clock。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path, startup_timeout_secs=5.0, poll_interval_secs=1.0)
    calls = {"n": 0}

    def _probe(url: str, t: float) -> tuple[bool, str]:
        calls["n"] += 1
        return (calls["n"] >= 3, "HTTP 200")

    monkeypatch.setattr(gm, "_time", h.clock)
    mgr = gm.GatewayProcessManager(
        cfg, probe=_probe, port_probe=h._port_probe, spawner=h._spawner, killer=h._killer, sleep=h.clock.sleep
    )
    mgr._proc = FakeProc()
    ok, detail = await mgr._wait_healthy()
    assert ok is True
    assert detail == "HTTP 200"
    assert calls["n"] == 3
    assert h.clock.sleeps == [1.0, 1.0]
    assert h.clock.now == 1002.0


async def test_wait_healthy_deadline_expires_with_fake_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """健康等待超时界：startup_timeout 耗尽后返回失败，不再无限轮询。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path, startup_timeout_secs=2.0, poll_interval_secs=1.0)
    monkeypatch.setattr(gm, "_time", h.clock)
    mgr = gm.GatewayProcessManager(
        cfg,
        probe=lambda url, t: (False, "refused"),
        port_probe=h._port_probe,
        spawner=h._spawner,
        killer=h._killer,
        sleep=h.clock.sleep,
    )
    mgr._proc = FakeProc(polls=[None] * 50)
    ok, detail = await mgr._wait_healthy()
    assert ok is False
    assert "超时" in detail
    assert h.clock.sleeps == [1.0, 1.0]


async def test_wait_healthy_detects_early_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """进程提前退出（rc 非零）→ 立即判败，不等超时界。"""
    h = Harness(monkeypatch)
    cfg = _config(tmp_path, startup_timeout_secs=60.0, poll_interval_secs=1.0)
    monkeypatch.setattr(gm, "_time", h.clock)
    mgr = gm.GatewayProcessManager(
        cfg,
        probe=lambda url, t: (False, "refused"),
        port_probe=h._port_probe,
        spawner=h._spawner,
        killer=h._killer,
        sleep=h.clock.sleep,
    )
    mgr._proc = FakeProc(polls=[None, 1])
    ok, detail = await mgr._wait_healthy()
    assert ok is False
    assert "提前退出" in detail
    # 第 1 轮探活失败且进程仍活 → 休眠一个轮询间隔；第 2 轮即探到退出（不等 60s 超时界）
    assert h.clock.sleeps == [1.0]


# ── 退避重启与熔断 ───────────────────────────────────────────────────────


async def test_unexpected_exit_triggers_backoff_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """意外退出（存活未达稳定界）→ 退避后重拉成功；新进程长活不再扰动。"""
    h = Harness(monkeypatch)
    cfg = _config(
        tmp_path,
        poll_interval_secs=0.01,
        restart_backoff_base_secs=0.5,
        restart_backoff_max_secs=4.0,
        stable_uptime_secs=60.0,
    )
    _touch_binary(cfg)
    mgr = h.manager(cfg)
    first = await mgr.ensure_running()
    assert first["action"] == "spawned"
    assert len(h.spawn_calls) == 1
    # 首个进程意外退出
    h.spawned_procs[0].polls = [1]
    h.spawned_procs[0].poll_idx = 0
    reached = await _drive(h.clock, lambda: len(h.spawn_calls) >= 2)
    assert reached, "退避重拉未发生"
    assert len(h.spawn_calls) == 2
    assert 0.5 in h.clock.sleeps  # 退避间隔（base）
    assert mgr._breaker_open is False
    await mgr.stop()


async def test_consecutive_quick_deaths_open_breaker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """连续短命进程（存活未达稳定界）超过阈值 → 熔断：不再重拉（防 crash-loop）。"""
    h = Harness(monkeypatch, spawn_polls=[1])
    cfg = _config(
        tmp_path,
        poll_interval_secs=0.01,
        restart_backoff_base_secs=0.01,
        restart_backoff_max_secs=0.02,
        max_consecutive_failures=2,
        stable_uptime_secs=60.0,
    )
    _touch_binary(cfg)
    mgr = h.manager(cfg)
    await mgr.ensure_running()
    assert mgr._breaker_open is False
    reached = await _drive(h.clock, lambda: mgr._breaker_open, rounds=800)
    assert reached, "熔断未发生"
    assert mgr._consecutive_failures > 2
    spawns_at_breaker = len(h.spawn_calls)
    await h.clock.sleep(1.0)
    await asyncio.sleep(0)
    assert len(h.spawn_calls) == spawns_at_breaker


async def test_long_uptime_exit_resets_failure_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """存活达稳定界后的退出视为新故障：计数复位后重拉（不误熔断）。"""
    h = Harness(monkeypatch)
    cfg = _config(
        tmp_path,
        poll_interval_secs=0.01,
        restart_backoff_base_secs=0.01,
        max_consecutive_failures=2,
        stable_uptime_secs=5.0,
    )
    _touch_binary(cfg)
    mgr = h.manager(cfg)
    await mgr.ensure_running()
    proc = h.spawned_procs[0]
    # 存活 10s（> 稳定界 5s）后退出
    await h.clock.sleep(10.0)
    proc.polls = [1]
    proc.poll_idx = 0
    reached = await _drive(h.clock, lambda: mgr._consecutive_failures >= 1)
    assert reached
    assert mgr._consecutive_failures == 1
    assert mgr._breaker_open is False
    await mgr.stop()


# ── stop 收树 ────────────────────────────────────────────────────────────


async def test_stop_kills_owned_process_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = Harness(monkeypatch)
    cfg = _config(tmp_path)
    mgr = h.manager(cfg)
    result = await mgr.stop()
    assert result["action"] == "not_owned"
    assert h.kill_calls == []

    mgr._proc = FakeProc(pid=7777)
    result = await mgr.stop()
    assert result["action"] == "stopped"
    assert h.kill_calls == [7777]
    assert mgr._proc is None


async def test_stop_reports_killer_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = Harness(monkeypatch)
    cfg = _config(tmp_path)

    def _killer(pid: int) -> None:
        raise OSError("access denied")

    monkeypatch.setattr(gm, "_time", h.clock)
    mgr = gm.GatewayProcessManager(
        cfg, probe=h._probe, port_probe=h._port_probe, spawner=h._spawner, killer=_killer, sleep=h.clock.sleep
    )
    mgr._proc = FakeProc(pid=8888)
    result = await mgr.stop()
    assert result["action"] == "stop_failed"
    assert "access denied" in result["detail"]


async def test_status_reflects_owned_and_external(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    h = Harness(monkeypatch)
    cfg = _config(tmp_path)

    async def _probe(url: str, t: float) -> tuple[bool, str]:
        return (True, "HTTP 200")

    mgr = gm.GatewayProcessManager(
        cfg, probe=_probe, port_probe=h._port_probe, spawner=h._spawner, killer=h._killer, sleep=h.clock.sleep
    )
    external = await mgr.status()
    assert external["running"] is True
    assert external["owned"] is False

    mgr2 = h.manager(_config(tmp_path))
    mgr2._proc = FakeProc(pid=9999)
    owned = await mgr2.status()
    assert owned["running"] is True
    assert owned["owned"] is True
    assert owned["pid"] == 9999


# ── 探活代理面（回环零代理）──────────────────────────────────────────────


def test_probe_sync_uses_empty_proxy_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """探活 opener 恒零代理（ProxyHandler({})）：env/注册表系统代理不得劫持
    本机回环探活（Windows 注册表代理经 urllib.getproxies 进默认 opener）。"""
    captured: dict[str, Any] = {}

    class _FakeOpener:
        def __init__(self, handlers: list[Any]) -> None:
            captured["handlers"] = handlers

        def open(self, request: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError("http://x", 401, "unauthorized", None, None)

    def _fake_build_opener(*handlers: Any) -> _FakeOpener:
        return _FakeOpener(list(handlers))

    monkeypatch.setattr(gm.urllib.request, "build_opener", _fake_build_opener)
    alive, detail = gm._probe_sync("http://127.0.0.1:8317/management.html", 0.5)
    assert alive is True  # 401 有响应即存活（判活只看连接层）
    assert detail == "HTTP 401"
    proxy_handlers = [h for h in captured["handlers"] if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


# ── server config 种子（幂等） ───────────────────────────────────────────


SEED_TEMPLATE = """\
config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
oauth:
  auth-dir: "__AGENTOS_AUTH_DIR__"
management:
  allow-remote: false
  secret-key: ""
"""


def _seed_env(tmp_path: Path) -> tuple[GatewayConfig, Path]:
    cfg = _config(tmp_path)
    factory = tmp_path / "configroot" / "plugins" / "llm_provider_gateway" / "gateway_server.yaml"
    factory.parent.mkdir(parents=True, exist_ok=True)
    factory.write_text(SEED_TEMPLATE, encoding="utf-8")
    cfg = GatewayConfig(**{**cfg.__dict__, "factory_server_config_path": factory})
    return cfg, factory


def test_seed_copies_from_factory_and_fixes_tokens(tmp_path: Path) -> None:
    """用户侧缺失 → 从出厂种子播种 + 令牌替换 + 空管理密钥生成。"""
    cfg, _factory = _seed_env(tmp_path)
    result = gm.seed_server_config(cfg)
    assert result["seeded"] is True
    assert result["token_replaced"] is True
    assert result["secret_generated"] is True
    written = cfg.server_config_path.read_text(encoding="utf-8")
    assert gm.AUTH_DIR_TOKEN not in written
    assert str(cfg.auth_dir) in written
    assert 'secret-key: ""' not in written
    assert re.search(r'secret-key: "[0-9a-f]{32}"', written)
    assert cfg.auth_dir.is_dir()
    assert cfg.log_path.parent.is_dir()


def test_seed_is_idempotent_and_preserves_user_edits(tmp_path: Path) -> None:
    """二次调用零改动（幂等）；用户已编辑的文件（无令牌）永不被覆盖。"""
    cfg, _factory = _seed_env(tmp_path)
    gm.seed_server_config(cfg)
    before = cfg.server_config_path.read_text(encoding="utf-8")
    cfg.server_config_path.write_text(before + "\n# 用户手动加的注释\n", encoding="utf-8")
    user_edit = cfg.server_config_path.read_text(encoding="utf-8")

    result = gm.seed_server_config(cfg)
    assert result["seeded"] is False
    assert result["token_replaced"] is False
    assert result["secret_generated"] is False
    assert cfg.server_config_path.read_text(encoding="utf-8") == user_edit


def test_seed_replaces_token_inplace_without_touching_user_edits(tmp_path: Path) -> None:
    """内核 verbatim 播种（含令牌）后插件修补：仅替换令牌，其余原样保留。"""
    cfg, _factory = _seed_env(tmp_path)
    cfg.server_config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.server_config_path.write_text(SEED_TEMPLATE + "# 用户注释\n", encoding="utf-8")
    result = gm.seed_server_config(cfg)
    assert result["seeded"] is False
    assert result["token_replaced"] is True
    written = cfg.server_config_path.read_text(encoding="utf-8")
    assert "# 用户注释" in written
    assert gm.AUTH_DIR_TOKEN not in written


def test_seed_keeps_existing_nonempty_secret(tmp_path: Path) -> None:
    """管理密钥已有值（用户设过）→ 不重新生成。"""
    cfg, _factory = _seed_env(tmp_path)
    cfg.server_config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.server_config_path.write_text(
        SEED_TEMPLATE.replace('secret-key: ""', 'secret-key: "user-key"'), encoding="utf-8"
    )
    result = gm.seed_server_config(cfg)
    assert result["secret_generated"] is False
    assert 'secret-key: "user-key"' in cfg.server_config_path.read_text(encoding="utf-8")


def test_seed_missing_both_sources_fails_visible(tmp_path: Path) -> None:
    cfg, _factory = _seed_env(tmp_path)
    cfg = GatewayConfig(**{**cfg.__dict__, "factory_server_config_path": None})
    result = gm.seed_server_config(cfg)
    assert result["seeded"] is False
    assert "缺失" in result["detail"]
