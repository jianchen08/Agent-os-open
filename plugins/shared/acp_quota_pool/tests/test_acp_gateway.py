# @feature: FP-0.2.ACP额度池 R6网关托管 | @ci: python-coverage
"""acp_gateway 网关生命周期托管行为测试。

覆盖三面：配置归一化（yaml 命名空间/env 覆盖/非法形态）、探活判据（任何
HTTP 响应=在，连接失败=不在）、ensure 拉起分支（已在则不拉/拉起后复验/
spawn 失败 fail-soft/未配置命令降级）。外部依赖全替身：HTTP 打桩在
urlopen（外部边界，wb_signin 轮同口径）、进程 spawn 与等待经注入 fake，
零真网络零真拉起。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import urllib.error
from typing import Any

import acp_gateway as gw
import pytest
from acp_gateway import (
    GatewayConfig,
    ensure_gateway_running,
    management_url,
    read_gateway_config,
)

pytestmark = pytest.mark.unit


# ── 配置归一化 ────────────────────────────────────────────────────────────


def test_read_config_none_injected_falls_back_defaults() -> None:
    config = read_gateway_config(None)
    assert config.base_url == gw.DEFAULT_BASE_URL
    assert config.start_command == ""
    assert config.health_timeout_secs > 0
    assert config.startup_grace_secs > 0


def test_read_config_namespace_values_win_over_defaults() -> None:
    config = read_gateway_config(
        {"gateway": {"base_url": "http://127.0.0.1:9999/", "start_command": "run.bat"}}
    )
    assert config.base_url == "http://127.0.0.1:9999"
    assert config.start_command == "run.bat"


def test_read_config_env_overrides_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gw.ENV_BASE_URL, "http://127.0.0.1:7777")
    monkeypatch.setenv(gw.ENV_START_COMMAND, "env.bat")
    config = read_gateway_config({"gateway": {"base_url": "http://ignored:1", "start_command": "x.bat"}})
    assert config.base_url == "http://127.0.0.1:7777"
    assert config.start_command == "env.bat"


@pytest.mark.parametrize(
    ("bad_url", "reason"),
    [
        ("ftp://127.0.0.1:8317", "非 http scheme"),
        ("127.0.0.1:8317", "缺 scheme"),
    ],
)
def test_read_config_invalid_base_url_raises(monkeypatch: pytest.MonkeyPatch, bad_url: str, reason: str) -> None:
    monkeypatch.delenv(gw.ENV_BASE_URL, raising=False)
    with pytest.raises(ValueError, match="base_url"):
        read_gateway_config({"gateway": {"base_url": bad_url}})
    assert reason  # 区分度标注（各踩一种形态）


def test_read_config_empty_base_url_behaves_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # 空串按 falsy 链视为未设置 → 回默认（不抛：空=未设置语义，非配置错误）
    monkeypatch.delenv(gw.ENV_BASE_URL, raising=False)
    config = read_gateway_config({"gateway": {"base_url": ""}})
    assert config.base_url == gw.DEFAULT_BASE_URL


@pytest.mark.parametrize("base_url", ["http://h:1", "http://h:1/"])
def test_management_url_invariant_to_trailing_slash(base_url: str) -> None:
    assert management_url(base_url) == "http://h:1" + gw.MANAGEMENT_PAGE


def test_read_config_nonpositive_timeouts_fall_back_to_defaults() -> None:
    config = read_gateway_config(
        {"gateway": {"health_timeout_secs": -1, "startup_grace_secs": "bogus"}}
    )
    assert config.health_timeout_secs == 2.0  # 负值回默认
    assert config.startup_grace_secs == 3.0  # 不可解析回默认


# ── 探活判据（urlopen 打桩 = 外部 HTTP 边界）────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_probe_alive_on_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    # urlopen 以 timeout= 关键字调用，打桩收 *args/**kwargs 原样兼容
    def _fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(200)

    monkeypatch.setattr(gw.urllib.request, "urlopen", _fake_urlopen)
    running, detail = asyncio.run(gw.probe_gateway("http://127.0.0.1:8317", 0.5))
    assert running is True
    assert "200" in detail


def test_probe_alive_on_http_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(req: object, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError("http://x", 401, "unauthorized", None, None)

    monkeypatch.setattr(gw.urllib.request, "urlopen", _raise)
    running, detail = asyncio.run(gw.probe_gateway("http://127.0.0.1:8317", 0.5))
    assert running is True  # 有 HTTP 响应即存活（管理页要凭据不改判活）
    assert "401" in detail


def test_probe_down_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(req: object, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _raise)
    running, detail = asyncio.run(gw.probe_gateway("http://127.0.0.1:8317", 0.5))
    assert running is False
    assert "URLError" in detail


def test_probe_targets_management_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _spy(req: Any, timeout: float) -> _FakeResponse:
        seen.append(getattr(req, "full_url", ""))
        return _FakeResponse(200)

    monkeypatch.setattr(gw.urllib.request, "urlopen", _spy)
    asyncio.run(gw.probe_gateway("http://127.0.0.1:8317/", 0.5))
    assert seen == ["http://127.0.0.1:8317/management.html"]


# ── ensure 拉起分支（probe/launch/sleep 全注入 fake，零真拉起）───────────────


def _fake_probe(sequence: list[tuple[bool, str]]) -> tuple[gw.Probe, list[tuple[str, float]]]:
    calls: list[tuple[str, float]] = []
    iterator = iter(sequence)

    async def probe(url: str, timeout_secs: float) -> tuple[bool, str]:
        calls.append((url, timeout_secs))
        return next(iterator)

    return probe, calls


def _fake_launch(
    fail_with: OSError | None = None,
) -> tuple[gw.Launcher, list[str]]:
    commands: list[str] = []

    def launch(command: str) -> None:
        if fail_with is not None:
            raise fail_with
        commands.append(command)

    return launch, commands


def _fake_sleep() -> tuple[gw.Sleeper, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep, slept


def _config(**overrides: Any) -> GatewayConfig:
    values: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8317",
        "start_command": "start-gateway.bat",
        "health_timeout_secs": 0.5,
        "startup_grace_secs": 3.0,
    }
    values.update(overrides)
    return GatewayConfig(**values)


async def test_ensure_already_running_skips_launch() -> None:
    probe, probe_calls = _fake_probe([(True, "HTTP 200")])
    launch, launched = _fake_launch()
    sleep, slept = _fake_sleep()

    result = await ensure_gateway_running(_config(), probe=probe, launch=launch, sleep=sleep)

    assert result["action"] == "none"
    assert result["running"] is True
    assert launched == []  # 在运行绝不重复拉起（幂等判据）
    assert slept == []
    assert probe_calls == [("http://127.0.0.1:8317", 0.5)]  # 只探一次


async def test_ensure_launches_then_reverifies_running() -> None:
    probe, _ = _fake_probe([(False, "URLError: refused"), (True, "HTTP 200")])
    launch, launched = _fake_launch()
    sleep, slept = _fake_sleep()

    result = await ensure_gateway_running(_config(), probe=probe, launch=launch, sleep=sleep)

    assert result["action"] == "launched"
    assert result["running"] is True
    assert launched == ["start-gateway.bat"]
    assert slept == [3.0]  # 拉起与复验之间按 grace 等待一次


async def test_ensure_launch_spawn_failure_is_fail_soft() -> None:
    probe, probe_calls = _fake_probe([(False, "URLError: refused")])
    launch, _ = _fake_launch(fail_with=OSError("bat 不存在"))
    sleep, slept = _fake_sleep()

    result = await ensure_gateway_running(_config(), probe=probe, launch=launch, sleep=sleep)

    assert result["action"] == "launch_failed"
    assert result["running"] is False
    assert "bat 不存在" in result["detail"]
    assert slept == []  # spawn 失败不进复验
    assert len(probe_calls) == 1  # 也只探一次


async def test_ensure_reverify_still_down_reports_honestly() -> None:
    probe, _ = _fake_probe([(False, "URLError: refused"), (False, "URLError: refused")])
    launch, launched = _fake_launch()
    sleep, _ = _fake_sleep()

    result = await ensure_gateway_running(_config(), probe=probe, launch=launch, sleep=sleep)

    assert result["action"] == "launched"  # 拉起动作用发生了
    assert result["running"] is False  # 复验结论如实为未起（不硬凑成功）
    assert launched == ["start-gateway.bat"]


async def test_ensure_without_command_degrades_explicitly() -> None:
    probe, probe_calls = _fake_probe([(False, "URLError: refused")])
    launch, launched = _fake_launch()
    sleep, slept = _fake_sleep()

    result = await ensure_gateway_running(
        _config(start_command=""), probe=probe, launch=launch, sleep=sleep
    )

    assert result["action"] == "launch_disabled"
    assert result["running"] is False
    assert launched == []
    assert slept == []
    assert len(probe_calls) == 1


# ── 真身拉起器（subprocess.Popen 打桩 = 进程 spawn 外部边界，零真拉起）──────


def test_launch_gateway_process_spawns_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class _FakePopen:
        def __init__(self, command: str, **kwargs: Any) -> None:
            calls.append({"command": command, **kwargs})

    monkeypatch.setattr(gw.subprocess, "Popen", _FakePopen)
    before = len(gw._LAUNCHED)

    gw.launch_gateway_process("start-gateway.bat")

    assert len(calls) == 1
    spawned = calls[0]
    assert spawned["command"] == "start-gateway.bat"
    assert spawned["shell"] is True  # bat 经 shell 执行
    assert spawned["stdin"] == subprocess.DEVNULL  # 流全断（不挂管道）
    assert spawned["stdout"] == subprocess.DEVNULL
    assert spawned["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        assert spawned["creationflags"] == subprocess.CREATE_NO_WINDOW  # 隐藏控制台
    else:
        assert spawned["creationflags"] == 0
    assert len(gw._LAUNCHED) == before + 1  # 句柄留模块引用（防 ResourceWarning）
