"""SSRF 防护公共模块。

提供 URL 安全校验：协议白名单 + 域名白名单 + DNS 解析后内网 IP 检查。

被 download / web 等会发起外部请求的工具复用，避免各工具自行实现
SSRF 防护导致漏接（如 web 工具曾只做域名字符串匹配、不解析 DNS，
可被 http://169.254.169.254/ 云 metadata 或 DNS rebinding 绕过）。

使用方式::

    from tools.common.ssrf_guard import validate_url

    ok, msg = validate_url(url, allow_domains=["example.com"])
    if not ok:
        return create_failure_result(f"URL 安全校验失败: {msg}")
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# RFC 1918 / loopback / link-local / 保留网段（SSRF 防护）。
# 169.254.0.0/16 覆盖云 metadata 服务（如 AWS/GCP/Azure 的 169.254.169.254）。
SSRF_GUARD_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_private_ip(ip_str: str) -> bool:
    """检查 IP 是否属于内网/保留地址。

    无法解析的 IP 视为不安全（返回 True，fail-closed）。
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in SSRF_GUARD_NETWORKS)
    except ValueError:
        return True


def validate_url(url: str, allow_domains: list[str] | None = None) -> tuple[bool, str]:
    """URL 安全校验：协议白名单 + 域名白名单 + SSRF 内网 IP 防护。

    Args:
        url: 待校验的 URL
        allow_domains: 可选域名白名单（支持子域名后缀匹配）；None 表示不限制域名

    Returns:
        (通过与否, 描述信息)。通过返回 (True, "OK")。
    """
    parsed = urlparse(url)

    # 1. 协议白名单
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}，仅允许 http/https"

    # 2. 主机名存在性
    hostname = parsed.hostname
    if not hostname:
        return False, "URL 缺少主机名"

    # 3. 域名白名单（可选；支持子域名后缀匹配）
    if allow_domains and hostname not in allow_domains and not any(
        hostname.endswith(f".{d}") for d in allow_domains
    ):
        return False, f"域名 {hostname} 不在白名单中"

    # 4. SSRF 防护：DNS 解析后检查是否命中内网 IP。
    #    仅做域名字符串匹配是不够的——攻击者可直连 169.254.169.254（云 metadata）
    #    或用 DNS rebinding 把域名解析到 127.0.0.1。
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for entry in resolved_ips:
            ip_str = entry[4][0]
            if is_private_ip(ip_str):
                return False, f"域名 {hostname} 解析到内网 IP {ip_str}，已拒绝（SSRF 防护）"
    except socket.gaierror:
        return False, f"无法解析域名: {hostname}"

    return True, "OK"
