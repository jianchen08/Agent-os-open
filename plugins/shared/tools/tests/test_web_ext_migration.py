# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""web_ext 工具 0.2 迁移 + SSRF 随迁 TDD 测试。

迁移（FP-MIGR）：
1. 模块可加载——0.1 的 core.results / tools.builtin.base / tools.types 已删除，
   改用 agentos_plugin_sdk（顶层 import 不再 ModuleNotFoundError）。
2. get_tool_definition() 返回合法 Tool；execute 返回 ToolExecutionResult。

安全随迁（FP-MIGR P1，审计 T5#59）：
3. _check_url_security 增加 SSRF 防护：DNS 解析 + 内网 IP 比对——
   localhost / 127.0.0.1 / 云元数据 169.254.169.254 均被拒（防触达元数据服务）。
4. 允许列表仍生效（公共地址放行）。
5. execute 对不安全 URL 返回 SECURITY_CHECK_FAILED。

装配：conftest.py 注入 sdk / tools 共享层 / web_ext 目录到 sys.path；
模块经 importlib 以唯一名加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_WEB_DIR = Path(__file__).resolve().parent.parent / "web_ext"


def _load_module() -> Any:
    """加载 web_ext/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "web_ext_tool_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _WEB_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "cannot load web_ext tool.py"
    assert spec.loader is not None, "cannot load web_ext tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


# ── 迁移验证：可加载 + 0.2 类型面 ──────────────────────────


class TestWebExtMigration:
    """迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    def test_module_imports_ok(self, mod):
        assert mod.WebTool is not None

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.WebTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "fetch"

    def test_web_tool_constructs(self, mod):
        assert isinstance(mod.WebTool(), mod.BuiltinTool)


# ── 安全随迁：SSRF 防护（内网目标被拒） ─────────────────────


class TestWebExtSsrf:
    """_check_url_security 拒绝内网/回环/元数据地址。"""

    def test_rejects_loopback(self, mod):
        ok, err = mod.WebTool()._check_url_security("http://127.0.0.1:8080/admin")
        assert ok is False
        assert "SSRF" in (err or "")

    def test_rejects_localhost(self, mod):
        ok, err = mod.WebTool()._check_url_security("http://localhost/secret")
        assert ok is False
        assert "SSRF" in (err or "")

    def test_rejects_cloud_metadata(self, mod):
        ok, err = mod.WebTool()._check_url_security("http://169.254.169.254/latest/meta-data")
        assert ok is False
        assert "SSRF" in (err or "")

    def test_rejects_private_lan(self, mod):
        ok, err = mod.WebTool()._check_url_security("http://192.168.1.10/internal")
        assert ok is False
        assert "SSRF" in (err or "")

    def test_allows_public_target(self, mod):
        ok, err = mod.WebTool()._check_url_security("http://8.8.8.8/")
        assert ok is True
        assert err is None

    def test_allowed_domains_still_enforced(self, mod):
        tool = mod.WebTool(allowed_domains=["8.8.8.8"])
        ok, err = tool._check_url_security("http://8.8.8.8/")
        assert ok is True
        ok, err = tool._check_url_security("http://example.com/")
        assert ok is False
        assert "不在允许列表中" in (err or "")

    def test_blocked_domains_still_enforced(self, mod):
        tool = mod.WebTool(blocked_domains=["example.com"])
        ok, err = tool._check_url_security("http://example.com/")
        assert ok is False
        assert "禁止列表" in (err or "")


class TestWebExtExecuteSecurity:
    """execute 对不安全 URL 直接拒绝（不发起网络请求）。"""

    @pytest.mark.asyncio
    async def test_execute_rejects_private_url(self, mod):
        result = await mod.WebTool().execute(
            {"action": "get", "url": "http://169.254.169.254/latest/meta-data"}
        )
        assert result.success is False
        assert result.error_code == "SECURITY_CHECK_FAILED"

    @pytest.mark.asyncio
    async def test_execute_requires_url(self, mod):
        result = await mod.WebTool().execute({"action": "get"})
        assert result.success is False
        assert result.error_code == "MISSING_URL"
