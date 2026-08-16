# @ci: python-coverage
"""web_operate SSRF 防护测试（punch B4）。

web_operate 入口接入共享层 url_security.validate_url（与 web_ext / download
同一原语）。IP 字面量不依赖 DNS 环境即可验证内网拒绝：
- http://127.0.0.1（回环）被拒
- http://169.254.169.254（云元数据）被拒
- 非 http/https 协议被拒
- 公网 IP 通过校验进入请求阶段（用不存在的 host 断言已越过校验层）
"""

from __future__ import annotations

import pytest

from agentos_builtin_tools.result import ToolResult
from agentos_builtin_tools.web_tool import web_operate

pytestmark = pytest.mark.unit


class TestWebOperateSsrf:
    async def test_loopback_ip_rejected(self) -> None:
        """127.0.0.1 在任何 action 下都被拒（先于 aiohttp 请求）。"""
        for action in ("get", "post", "fetch"):
            result = await web_operate(action=action, url="http://127.0.0.1:8080/admin")
            assert isinstance(result, ToolResult)
            assert result.success is False
            assert "URL 安全校验失败" in result.error

    async def test_metadata_ip_rejected(self) -> None:
        """169.254.169.254（云元数据端点）被拒。"""
        result = await web_operate(action="get", url="http://169.254.169.254/latest/meta-data")
        assert result.success is False
        assert "URL 安全校验失败" in result.error

    async def test_private_range_rejected(self) -> None:
        """RFC1918 内网段（192.168.x）被拒。"""
        result = await web_operate(action="get", url="http://192.168.1.10/internal")
        assert result.success is False
        assert "URL 安全校验失败" in result.error

    async def test_bad_protocol_rejected(self) -> None:
        """非 http/https 协议（file://）被拒。"""
        result = await web_operate(action="get", url="file:///etc/passwd")
        assert result.success is False
        assert "URL 安全校验失败" in result.error

    async def test_public_ip_passes_security_layer(self) -> None:
        """公网 IP 通过校验层、进入请求阶段（DNS/连接失败属于请求层错误）。"""
        result = await web_operate(
            action="get", url="http://192.0.2.10/", timeout=2
        )
        # 校验层放行：失败（若有）只能是网络层错误，而非"URL 安全校验失败"
        if result.success is False:
            assert "URL 安全校验失败" not in result.error
