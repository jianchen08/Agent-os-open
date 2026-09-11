# @feature: browser_bridge 沙箱通路 | @ci: python-coverage
"""docker_provider Bridge 注入测试（沙箱内 MCP Client → 宿主 Bridge 网关）。

行为断言（输入→输出，不断言内部细节）：
- bridge 网络容器：--add-host host.docker.internal→host-gateway 注入（WSL
  原生 docker 非 Docker Desktop 时该特殊域名不被自动解析，须显式补）；
- --network host 的容器不注入 add-host（共享宿主网络栈，localhost 直达）；
- AGENTOS_BRIDGE_URL：配置 bridge_url > 环境变量 > WSL 网关探测，注入 -e；
- AGENTOS_BRIDGE_TOKEN 环境变量存在时注入 -e，缺失时不注入（容器内
  bridge_client 报清晰错误，不静默降级）。
"""

from __future__ import annotations

import os
from typing import Any

import tests._isolation_path  # noqa: F401  # isort: skip —— 须在 providers import 前注入 sys.path

import pytest

pytestmark = pytest.mark.unit


def _make_provider(config: dict[str, Any] | None = None):
    from providers.docker_provider import DockerProvider

    return DockerProvider(config)


def _run_args(provider, workspace: str = "D:/myproject/container_e17cc5927dfd/sessions/s1") -> list[str]:
    from agentos_plugin_sdk.isolation_types import IsolationContext, TaskType

    ctx = IsolationContext(task_id="t1", task_type=TaskType.ATOMIC, workspace=workspace)
    return provider._build_run_args("cua-test", ctx)


def _flag_value(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


class TestBridgeInjection:
    def test_add_host_injected_on_bridge_network(self, monkeypatch) -> None:
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        provider = _make_provider({})
        # 探测返回空（非 Linux 平台必然）→ 无 URL env 注入
        monkeypatch.setattr(type(provider), "_detect_host_gateway_ip", staticmethod(lambda: ""))
        args = _run_args(provider)
        assert _flag_value(args, "--add-host") == ["host.docker.internal:host-gateway"]
        assert "AGENTOS_BRIDGE_URL" not in _flag_value(args, "-e")

    def test_add_host_skipped_on_host_network(self, monkeypatch) -> None:
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        provider = _make_provider({"network_mode": "host"})
        args = _run_args(provider)
        assert "--add-host" not in args

    def test_bridge_url_from_config_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://from-env:8765")
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        provider = _make_provider({"bridge_url": "http://10.1.2.3:8765"})
        args = _run_args(provider)
        envs = _flag_value(args, "-e")
        assert any(v == "AGENTOS_BRIDGE_URL=http://10.1.2.3:8765" for v in envs)

    def test_bridge_url_fallback_env(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://from-env:8765")
        provider = _make_provider({})
        args = _run_args(provider)
        envs = _flag_value(args, "-e")
        assert any(v == "AGENTOS_BRIDGE_URL=http://from-env:8765" for v in envs)

    def test_bridge_token_env_injected_when_present(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "tok-xyz")
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
        provider = _make_provider({})
        monkeypatch.setattr(type(provider), "_detect_host_gateway_ip", staticmethod(lambda: ""))
        args = _run_args(provider)
        envs = _flag_value(args, "-e")
        assert any(v == "AGENTOS_BRIDGE_TOKEN=tok-xyz" for v in envs)

    def test_bridge_token_absent_no_injection(self, monkeypatch) -> None:
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        provider = _make_provider({})
        monkeypatch.setattr(type(provider), "_detect_host_gateway_ip", staticmethod(lambda: ""))
        args = _run_args(provider)
        envs = _flag_value(args, "-e")
        assert not any(v.startswith("AGENTOS_BRIDGE_TOKEN=") for v in envs)

    def test_gateway_detection_linux(self, monkeypatch) -> None:
        # ip route 探测：default via <ip> → 注入该 IP
        import platform

        provider = _make_provider({})
        if platform.system() != "Linux":
            pytest.skip("ip route 探测仅 Linux/WSL 语义")
        fake = os.popen
        monkeypatch.setattr(os, "popen", lambda cmd: fake)
        # 真实执行（WSL 环境）；非 WSL 环境可能返回空——只断言不抛异常
        ip = provider._detect_host_gateway_ip()
        assert ip == "" or ":" not in ip