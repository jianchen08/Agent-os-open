# @ci: python-coverage
"""url_security 共享层 SSRF 防护测试（punch B2）。

覆盖：
- 网段表新增 0.0.0.0/8 与 100.64.0.0/10（CGNAT）
- IPv4-mapped IPv6 归一后再比对 IPv4 网段（::ffff:127.0.0.1 / ::ffff:169.254.169.254）
- 公网 IP / 原生 IPv6 内网段（::1、fc00::/7）不受归一化影响
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from url_security import is_private_ip  # noqa: E402


class TestIsPrivateIpHardened:
    def test_unspecified_ipv4_is_private(self) -> None:
        """0.0.0.0（0.0.0.0/8）判私网——"this network" 常被解析为本机。"""
        assert is_private_ip("0.0.0.0") is True
        assert is_private_ip("0.1.2.3") is True

    def test_cgnat_range_is_private(self) -> None:
        """100.64.0.0/10（RFC 6598 CGNAT）判私网。"""
        assert is_private_ip("100.64.0.1") is True
        assert is_private_ip("100.127.255.254") is True

    def test_cgnat_neighbors_stay_public(self) -> None:
        """CGNAT 段两侧相邻公网 IP 不误伤。"""
        assert is_private_ip("100.63.255.254") is False
        assert is_private_ip("100.128.0.1") is False

    def test_ipv4_mapped_loopback_is_private(self) -> None:
        """::ffff:127.0.0.1 归一为 127.0.0.1 后判私网。"""
        assert is_private_ip("::ffff:127.0.0.1") is True

    def test_ipv4_mapped_metadata_is_private(self) -> None:
        """::ffff:169.254.169.254（云元数据地址映射形式）判私网。"""
        assert is_private_ip("::ffff:169.254.169.254") is True

    def test_ipv4_mapped_private_ranges(self) -> None:
        """其余 RFC1918 段的 IPv4-mapped 形式同样命中。"""
        assert is_private_ip("::ffff:10.0.0.1") is True
        assert is_private_ip("::ffff:192.168.1.1") is True
        assert is_private_ip("::ffff:0.0.0.0") is True
        assert is_private_ip("::ffff:100.64.0.1") is True

    def test_ipv4_mapped_public_stays_public(self) -> None:
        """IPv4-mapped 公网地址归一后不误伤。"""
        assert is_private_ip("::ffff:8.8.8.8") is False

    def test_native_ipv6_private_ranges(self) -> None:
        """原生 IPv6 内网段（::1 / ULA / link-local）仍判私网。"""
        assert is_private_ip("::1") is True
        assert is_private_ip("fd00::1") is True
        assert is_private_ip("fe80::1") is True

    def test_public_ipv6_stays_public(self) -> None:
        """公网 IPv6 不误伤。"""
        assert is_private_ip("2001:4860:4860::8888") is False

    def test_unparseable_ip_is_private(self) -> None:
        """无法解析的输入一律视为不安全。"""
        assert is_private_ip("not-an-ip") is True
