"""URL 安全公共 helper（0.2 工具共享层）。

download 与 web_ext 共用的 SSRF 防护原语集中在本层，
避免两处各自维护内网网段表与 DNS 校验逻辑漂移。

暴露接口：
- is_private_ip(ip_str) -> bool：RFC1918 / loopback / link-local / IPv6 内网段判定
- resolve_hostname_ips(hostname) -> tuple[list[str] | None, str | None]：DNS 解析
- validate_url(url, allow_domains) -> tuple[bool, str]：协议白名单 + 可选域名白名单
  + DNS 解析 + 内网 IP 比对（SSRF 防护）

本模块自包含（仅标准库），可被任何工具以平铺模块方式导入。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# RFC 1918 / loopback / link-local 网段（SSRF 防护）
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def is_private_ip(ip_str: str) -> bool:
    """检查 IP 是否属于内网地址（SSRF 防护）。

    无法解析的 IP 一律视为不安全（返回 True）。

    Args:
        ip_str: IP 地址字符串

    Returns:
        是否属于内网/不可信地址
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True  # 无法解析的 IP 视为不安全


def resolve_hostname_ips(hostname: str) -> tuple[list[str] | None, str | None]:
    """解析主机名到全部 IP（DNS 多结果）。

    Args:
        hostname: 主机名（不含端口）

    Returns:
        (解析出的 IP 列表, None)；解析失败返回 (None, 错误信息)
    """
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None, f"无法解析域名: {hostname}"
    return [entry[4][0] for entry in resolved_ips], None


def validate_url(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
    """URL 安全校验：协议白名单 + 域名白名单 + SSRF 防护。

    任何解析结果命中内网 IP 即拒绝（SSRF 防护不可旁路）。

    Args:
        url: 目标 URL
        allow_domains: 可选域名白名单（精确或子域名后缀匹配）

    Returns:
        (是否通过, 错误信息或 "OK")
    """
    parsed = urlparse(url)

    # 1. 协议白名单
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}，仅允许 http/https"

    # 2. 域名检查
    hostname = parsed.hostname
    if not hostname:
        return False, "URL 缺少主机名"

    # 3. 域名白名单（可选）
    if allow_domains and hostname not in allow_domains and not any(hostname.endswith(f".{d}") for d in allow_domains):
        return False, f"域名 {hostname} 不在白名单中"

    # 4. SSRF 防护：DNS 解析后检查是否为内网 IP
    ips, err = resolve_hostname_ips(hostname)
    if err:
        return False, err
    for ip_str in ips or []:
        if is_private_ip(ip_str):
            return False, f"域名 {hostname} 解析到内网 IP {ip_str}，已拒绝（SSRF 防护）"

    return True, "OK"
