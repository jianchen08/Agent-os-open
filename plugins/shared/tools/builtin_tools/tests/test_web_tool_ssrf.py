# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""web_operate SSRF 防护测试（punch B4，指向存活实现 web_ext）。

2026-08-24 双轨清理：builtin_tools 的 bash_execute/web_operate 副本删除，
web_operate 唯一实现收敛到 plugins/shared/tools/web_ext（httpx + trafilatura +
共享层 url_security 原语）。本文件改测 web_ext.WebTool 的 _check_url_security：
IP 字面量不依赖 DNS 环境即可验证内网拒绝：
- http://127.0.0.1（回环）被拒
- http://169.254.169.254（云元数据）被拒
- 非 http/https 协议被拒
- 公网 IP 通过校验进入请求阶段（用不存在的 host 断言已越过校验层）
"""

from __future__ import annotations

import pytest

from web_ext.tool import WebTool

pytestmark = pytest.mark.unit


class TestWebOperateSsrf:
    async def test_loopback_ip_rejected(self) -> None:
        """127.0.0.1 在任何 action 下都被拒（先于 httpx 请求）。"""
        tool = WebTool()
        for action in ("get", "post", "fetch"):
            result = await tool.execute({"action": action, "url": "http://127.0.0.1:8080/admin"})
            assert result.success is False
            assert "URL 安全检查失败" in result.error
            assert "内网/回环" in result.error

    async def test_metadata_ip_rejected(self) -> None:
        """169.254.169.254（云元数据端点）被拒。"""
        tool = WebTool()
        result = await tool.execute({"action": "get", "url": "http://169.254.169.254/latest/meta-data"})
        assert result.success is False
        assert "URL 安全检查失败" in result.error

    async def test_private_range_rejected(self) -> None:
        """RFC1918 内网段（192.168.x）被拒。"""
        tool = WebTool()
        result = await tool.execute({"action": "get", "url": "http://192.168.1.10/internal"})
        assert result.success is False
        assert "URL 安全检查失败" in result.error

    async def test_bad_protocol_rejected(self) -> None:
        """非 http/https 协议（file://）被拒。"""
        tool = WebTool()
        result = await tool.execute({"action": "get", "url": "file:///etc/passwd"})
        assert result.success is False
        assert "URL 安全检查失败" in result.error

    async def test_public_ip_passes_security_layer(self) -> None:
        """公网 IP 通过校验层、进入请求阶段（DNS/连接失败属于请求层错误）。"""
        tool = WebTool()
        result = await tool.execute({"action": "get", "url": "http://192.0.2.10/", "timeout": 2})
        # 校验层放行：失败（若有）只能是网络层错误，而非"URL 安全检查失败"
        if result.success is False:
            assert "URL 安全检查失败" not in result.error
