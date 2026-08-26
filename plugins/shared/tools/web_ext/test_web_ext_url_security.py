# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""web_ext url_security 共享层 SSRF 原语测试。

覆盖 plugins/shared/tools/web_ext/url_security.py：
1. is_private_ip：IPv4 内网各网段 / 公网 / IPv6 内网 / IPv4-mapped IPv6 归一 /
   无法解析视为不安全
2. resolve_hostname_ips：成功多结果 / DNS 失败
3. validate_url：协议白名单 / 缺主机名 / 域名白名单 / DNS 失败 / 内网拒绝 / 通过

DNS 解析（socket.getaddrinfo）属外部依赖，用 monkeypatch 打桩；
is_private_ip 为纯函数走真实实现。

[来源: 车道实测 web_ext 54.8% → 补测]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 url_security.py（唯一模块名，避免与 download 副本互踩）。"""
    mod_name = "web_ext_url_security_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "url_security.py")
    assert spec is not None and spec.loader is not None, "Cannot load url_security.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()
is_private_ip = _MOD.is_private_ip
resolve_hostname_ips = _MOD.resolve_hostname_ips
validate_url = _MOD.validate_url


# ═══════════════════════════════════════════════════════════
# is_private_ip
# ═══════════════════════════════════════════════════════════


class TestIsPrivateIp:
    @pytest.mark.parametrize(
        "ip_str",
        [
            "10.1.2.3",  # RFC1918 10/8
            "172.16.0.1",  # RFC1918 172.16/12
            "172.31.255.254",  # RFC1918 172.16/12 上界
            "192.168.0.1",  # RFC1918 192.168/16
            "127.0.0.1",  # 回环
            "169.254.169.254",  # 云元数据 link-local
            "0.0.0.0",  # this network
            "100.64.0.1",  # CGNAT
            "100.127.255.254",  # CGNAT 上界
            "::1",  # IPv6 回环
            "fc00::1",  # IPv6 ULA
            "fe80::1",  # IPv6 link-local
        ],
    )
    def test_private_networks(self, ip_str: str) -> None:
        assert is_private_ip(ip_str) is True

    @pytest.mark.parametrize(
        "ip_str",
        [
            "8.8.8.8",
            "93.184.216.34",
            "172.32.0.1",  # 172.16/12 之外
            "100.128.0.1",  # CGNAT 之外
            "2001:4860:4860::8888",  # 公网 IPv6
        ],
    )
    def test_public_ips(self, ip_str: str) -> None:
        assert is_private_ip(ip_str) is False

    def test_ipv4_mapped_ipv6_normalized(self) -> None:
        """IPv4-mapped IPv6（::ffff:127.0.0.1）归一为 IPv4 后命中回环网段。"""
        assert is_private_ip("::ffff:127.0.0.1") is True
        assert is_private_ip("::ffff:8.8.8.8") is False

    def test_unparseable_treated_unsafe(self) -> None:
        assert is_private_ip("not-an-ip") is True
        assert is_private_ip("") is True


# ═══════════════════════════════════════════════════════════
# resolve_hostname_ips
# ═══════════════════════════════════════════════════════════


class TestResolveHostnameIps:
    def test_success_multiple_ips(self, monkeypatch) -> None:
        monkeypatch.setattr(
            _MOD.socket,
            "getaddrinfo",
            lambda host, port: [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("93.184.216.35", 0)),
            ],
        )
        ips, err = resolve_hostname_ips("example.com")
        assert err is None
        assert ips == ["93.184.216.34", "93.184.216.35"]

    def test_dns_failure(self, monkeypatch) -> None:
        import socket

        def fail(host: str, port: Any) -> Any:
            raise socket.gaierror("name or service not known")

        monkeypatch.setattr(_MOD.socket, "getaddrinfo", fail)
        ips, err = resolve_hostname_ips("nope.invalid")
        assert ips is None
        assert err is not None and "无法解析域名" in err


# ═══════════════════════════════════════════════════════════
# validate_url
# ═══════════════════════════════════════════════════════════


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)"],
    )
    def test_bad_protocol(self, url: str) -> None:
        ok, msg = validate_url(url)
        assert ok is False and "不支持的协议" in msg

    def test_missing_hostname(self) -> None:
        ok, msg = validate_url("http:///path")
        assert ok is False and "缺少主机名" in msg

    def test_domain_whitelist_reject(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda host: (["93.184.216.34"], None))
        ok, msg = validate_url("http://other.com/x", allow_domains=["example.com"])
        assert ok is False and "不在白名单" in msg

    def test_domain_whitelist_subdomain_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda host: (["93.184.216.34"], None))
        ok, msg = validate_url("http://sub.example.com/x", allow_domains=["example.com"])
        assert ok is True and msg == "OK"

    def test_dns_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda host: (None, "无法解析域名: nope"))
        ok, msg = validate_url("http://nope.invalid/x")
        assert ok is False and "无法解析域名" in msg

    def test_private_ip_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda host: (["127.0.0.1"], None))
        ok, msg = validate_url("http://localhost/x")
        assert ok is False and "SSRF" in msg and "127.0.0.1" in msg

    def test_ok(self, monkeypatch) -> None:
        monkeypatch.setattr(_MOD, "resolve_hostname_ips", lambda host: (["93.184.216.34"], None))
        ok, msg = validate_url("https://example.com/x")
        assert ok is True and msg == "OK"
