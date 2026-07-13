"""M3/M4 安全回归：SSRF 防护（DNS 解析内网 IP 检查 + skip 旁路收紧）。

漏洞：
- M3: web 工具 _check_url_security 只做域名字符串匹配，不解析 DNS，
  可访问 http://169.254.169.254/（云 metadata）或 DNS rebinding 到 127.0.0.1。
- M4: download 工具有完整 SSRF 防护，但 skip_ssrf_check 是用户可控参数，
  传 true 即完全旁路内网 IP 检查。

修复：
- 抽公共模块 tools.common.ssrf_guard（validate_url / is_private_ip），
  download / web 共用。
- web 的 _check_url_security 改为调用公共 validate_url，获得 DNS 内网 IP 检查。
- download 的 skip_ssrf_check 即便为 true，也强制拦截"主机名是内网 IP 字面量"
  的情况（http://169.254.169.254 等直连不能被 skip 旁路）。

本测试守护：内网 IP 必须被拒，skip 不能旁路直连内网 IP。
"""
from __future__ import annotations

import pytest


class TestSsrfGuardPublicModule:
    """M3: 公共 SSRF 防护模块拦截内网 IP。"""

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("http://169.254.169.254/latest/meta-data/", id="aws_metadata"),
            pytest.param("http://127.0.0.1:8080/", id="loopback"),
            pytest.param("http://10.0.0.1/", id="rfc1918_10"),
            pytest.param("http://192.168.1.1/", id="rfc1918_192"),
            pytest.param("http://[::1]/", id="ipv6_loopback"),
        ],
    )
    def test_internal_ip_literals_blocked(self, url: str) -> None:
        from tools.common.ssrf_guard import validate_url

        ok, msg = validate_url(url)
        assert not ok, f"内网 IP 未被拦截: {url} -> {msg}"
        assert "内网" in msg or "拒绝" in msg

    def test_public_domain_allowed(self) -> None:
        from tools.common.ssrf_guard import validate_url

        # example.com 是 IANA 保留的文档演示域名，解析到公网 IP
        ok, msg = validate_url("https://example.com")
        assert ok, f"公网域名被误拦: {msg}"

    def test_unsupported_protocol_blocked(self) -> None:
        from tools.common.ssrf_guard import validate_url

        ok, _ = validate_url("file:///etc/passwd")
        assert not ok

    def test_is_private_ip_helper(self) -> None:
        from tools.common.ssrf_guard import is_private_ip

        assert is_private_ip("169.254.169.254") is True
        assert is_private_ip("127.0.0.1") is True
        assert is_private_ip("8.8.8.8") is False
        # 无法解析的 IP 视为不安全（fail-closed）
        assert is_private_ip("not-an-ip") is True


class TestDownloadSkipSsrfCannotBypassInternalIp:
    """M4: skip_ssrf_check=true 也不能旁路到直连内网 IP。

    漏洞核心：攻击者传 skip_ssrf_check=true 即可访问 169.254.169.254。
    修复后：skip 只放宽协议检查，主机名是内网 IP 字面量时仍拒绝。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("http://169.254.169.254/latest/meta-data/", id="aws_metadata"),
            pytest.param("http://127.0.0.1/", id="loopback"),
            pytest.param("http://10.0.0.1/", id="rfc1918"),
        ],
    )
    async def test_skip_ssrf_still_blocks_internal_ip(self, url: str, tmp_path) -> None:
        from tools.builtin.download.tool import DownloadTool

        t = DownloadTool()
        # skip_ssrf_check=true，但目标是直连内网 IP → 必须拒绝
        r = await t.execute({
            "url": url,
            "save_path": str(tmp_path),
            "skip_ssrf_check": True,
        })
        assert not r.success, f"skip_ssrf_check 竟旁路了内网 IP: {url}"
        assert "内网" in (r.error or "") or "skip" in (r.error or "").lower(), r.error


class TestWebToolSsrfGuard:
    """M3: web 工具复用公共 SSRF 防护，拦截内网 IP。

    修复前 web 只做域名字符串匹配，169.254.169.254 不在黑名单即可访问。
    修复后调 validate_url 做 DNS 内网 IP 检查。
    """

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("http://169.254.169.254/latest/meta-data/", id="aws_metadata"),
            pytest.param("http://127.0.0.1/", id="loopback"),
            pytest.param("http://10.0.0.1/", id="rfc1918"),
        ],
    )
    def test_web_check_url_security_blocks_internal_ip(self, url: str) -> None:
        from tools.builtin.web.tool import WebTool

        t = WebTool()
        # 即便不在 blocked_domains，内网 IP 也应被 validate_url 拦截
        ok, msg = t._check_url_security(url)
        assert not ok, f"web 工具未拦截内网 IP: {url} -> {msg}"

    def test_web_public_domain_allowed(self) -> None:
        from tools.builtin.web.tool import WebTool

        t = WebTool()
        ok, _ = t._check_url_security("https://example.com")
        assert ok
