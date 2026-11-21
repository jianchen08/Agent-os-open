# @feature: FP-0.2.ACP额度池 R6网关托管 | @ci: python-coverage
"""acp_gateway 网关探活行为测试（2026-11-21 拉起面单点化后的探活-only 面）。

覆盖面：配置归一化（默认值/命名空间/env 覆盖/非法形态）、管理页 URL 拼接
不变量、探活判据（任何 HTTP 响应=在，连接失败=不在）、ensure 探活分支
（在则 none / 不在则 handoff 指向 llm_provider_gateway，绝不本机拉起）。
外部依赖全替身：HTTP 打桩在 urlopen（外部边界，wb_signin 轮同口径），
零真网络零真拉起——launch_gateway_process 已随拉起面退役，本测试面钉住
「探活模块不再持有任何 spawn 面」。
"""

from __future__ import annotations

import asyncio
import inspect
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


# ── 拉起面退役（钉住：探活模块零 spawn 面）─────────────────────────────────


def test_launch_surface_retired() -> None:
    """拉起面退役钉子：launch_gateway_process / start_command 解析 /
    ENV_START_COMMAND / platform 路由 / {plugin_dir} 占位符全部不在模块面
    （拉起职责单点化到 llm_provider_gateway，防旧病复发）。"""
    for gone in (
        "launch_gateway_process",
        "_resolve_start_command",
        "_expand_placeholders",
        "ENV_START_COMMAND",
        "platform_key",
        "PLUGIN_DIR",
    ):
        assert not hasattr(gw, gone), f"退役面残留: {gone}"
    assert not hasattr(GatewayConfig, "__dataclass_fields__") or set(
        GatewayConfig.__dataclass_fields__
    ) == {"base_url", "health_timeout_secs"}
    assert "subprocess" not in inspect.getsource(gw)


# ── 配置归一化 ────────────────────────────────────────────────────────────


def test_read_config_none_injected_falls_back_defaults() -> None:
    config = read_gateway_config(None)
    assert config.base_url == gw.DEFAULT_BASE_URL
    assert config.health_timeout_secs > 0


def test_read_config_namespace_value_wins_over_default() -> None:
    config = read_gateway_config({"gateway": {"base_url": "http://127.0.0.1:9999/"}})
    assert config.base_url == "http://127.0.0.1:9999"  # 尾斜杠归一
    assert config.health_timeout_secs == 2.0


def test_read_config_env_overrides_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gw.ENV_BASE_URL, "http://127.0.0.1:7777")
    config = read_gateway_config({"gateway": {"base_url": "http://ignored:1"}})
    assert config.base_url == "http://127.0.0.1:7777"


def test_read_config_legacy_start_command_keys_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """存量用户副本里的 start_command_* 键（退役面）被归一化面忽略不报错。"""
    monkeypatch.delenv(gw.ENV_BASE_URL, raising=False)
    config = read_gateway_config(
        {
            "gateway": {
                "base_url": "http://127.0.0.1:9200",
                "start_command": "old-generic.sh",
                "start_command_windows": "C:\\old\\start-gateway.bat",
            }
        }
    )
    assert config.base_url == "http://127.0.0.1:9200"
    assert config.health_timeout_secs == 2.0


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


def test_read_config_nonpositive_timeout_falls_back_to_default() -> None:
    config = read_gateway_config({"gateway": {"health_timeout_secs": -1}})
    assert config.health_timeout_secs == 2.0  # 负值回默认


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


# ── ensure 探活分支（probe 注入 fake；拉起面已退役，不在 = handoff）─────────


def _fake_probe(sequence: list[tuple[bool, str]]) -> tuple[gw.Probe, list[tuple[str, float]]]:
    calls: list[tuple[str, float]] = []
    iterator = iter(sequence)

    async def probe(url: str, timeout_secs: float) -> tuple[bool, str]:
        calls.append((url, timeout_secs))
        return next(iterator)

    return probe, calls


def _config(**overrides: Any) -> GatewayConfig:
    values: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8317",
        "health_timeout_secs": 0.5,
    }
    values.update(overrides)
    return GatewayConfig(**values)


async def test_ensure_running_reports_none() -> None:
    probe, probe_calls = _fake_probe([(True, "HTTP 200")])

    result = await ensure_gateway_running(_config(), probe=probe)

    assert result["action"] == "none"
    assert result["running"] is True
    assert probe_calls == [("http://127.0.0.1:8317", 0.5)]  # 只探一次（幂等入口）


async def test_ensure_down_reports_handoff_without_launching() -> None:
    """网关不在 → handoff 指向新拉起面（llm_provider_gateway）；绝不本机拉起。"""
    probe, probe_calls = _fake_probe([(False, "URLError: refused")])

    result = await ensure_gateway_running(_config(), probe=probe)

    assert result["action"] == "handoff"
    assert result["running"] is False
    assert "llm_provider_gateway" in result["detail"]
    assert "URLError" in result["detail"]  # 病因不吞
    assert len(probe_calls) == 1
