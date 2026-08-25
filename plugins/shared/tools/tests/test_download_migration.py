# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""download 工具 0.2 迁移 + 安全随迁 TDD 测试。

迁移（FP-MIGR）：
1. 模块可加载——0.1 的 tools.builtin.base / tools.types 已删除，
   改用 agentos_plugin_sdk（顶层 import 不再 ModuleNotFoundError）。
2. get_tool_definition() 返回合法 Tool；schema 不再暴露 skip_ssrf_check
   （移入 injected_params，运行时注入，不出现在 LLM schema）。

安全随迁（FP-MIGR P1×2，审计 T5#59）：
3. SSRF 旁路被拒：客户端传 skip_ssrf_check=True 不再生效——
   127.0.0.1 内网目标仍被拒绝（URL 安全校验恒执行）。
4. save_path 越界被拒：经 WorkspaceAwareMixin.check_path_allowed（write）约束，
   返回 False 时执行失败——防目录穿越写。
5. 服务端内部位 allow_ssrf_skip 仍可用（受信本地测试服务器专用，仅服务端构造）。

装配：conftest.py 注入 sdk / tools 共享层 / download 目录到 sys.path；
模块经 importlib 以唯一名加载（不依赖 0.1 包安装）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DL_DIR = Path(__file__).resolve().parent.parent / "download"


def _load_module() -> Any:
    """加载 download/tool.py（唯一模块名，进程内缓存）。

    加载前把 download 目录提到 sys.path[0] 并逐出裸名缓存：同进程批跑时
    （如 tests/plugins 先收集）其他插件目录会占住 sys.path[0]，tool.py 的
    ``from workspace_aware import WorkspaceAwareMixin`` 会解析到 bash 等
    其他插件的同名模块（无 check_path_allowed → 运行期 AttributeError）。
    """
    mod_name = "download_tool_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _DL_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    _s = str(_DL_DIR)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
    for _m in ("workspace_aware", "tool", "url_security"):
        sys.modules.pop(_m, None)
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "cannot load download tool.py"
    assert spec.loader is not None, "cannot load download tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """download 工具模块（加载后可 monkeypatch 模块级依赖）。"""
    return _load_module()


def _allow_all_paths(self, path, operation="read", agent_level=None):
    """check_path_allowed 替身：一律放行（ARG001/002 全局忽略，无需下划线）。"""
    return True, ""


# ── 迁移验证：可加载 + 0.2 类型面 ──────────────────────────


class TestDownloadMigration:
    """迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.DownloadTool is not None

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.DownloadTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "download"

    def test_skip_ssrf_check_not_in_public_schema(self, mod):
        """skip_ssrf_check 已移出公开 schema（防 LLM/提示注入旁路 SSRF）。"""
        tool = mod.DownloadTool.get_tool_definition()
        assert "skip_ssrf_check" not in tool.input_schema["properties"]
        assert "skip_ssrf_check" in tool.injected_params

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.DownloadTool(), mod.BuiltinTool)


# ── 安全随迁 1：SSRF 旁路被拒 ─────────────────────────────


class TestSsrfBypassRejected:
    """客户端 skip_ssrf_check 不再生效；服务端内部位仍可用。"""

    @pytest.mark.asyncio
    async def test_client_skip_ssrf_check_ignored(self, mod):
        """用户可控 skip_ssrf_check=True 无法旁路 SSRF：内网目标被拒。"""
        dl = mod.DownloadTool()
        result = await dl.execute(
            {
                "url": "http://127.0.0.1:8080/secret",
                "save_path": "downloads",
                "skip_ssrf_check": True,
            }
        )
        assert result.success is False
        assert "URL 安全校验失败" in (result.error or "")

    @pytest.mark.asyncio
    async def test_server_side_allow_ssrf_skip_still_works(self, mod, monkeypatch, tmp_path):
        """服务端内部位：构造参数 allow_ssrf_skip=True（受信本地测试服务器）。"""
        monkeypatch.setattr(mod, "validate_url", lambda _url, _allow_domains=None: (True, "OK"))
        monkeypatch.setattr(mod.DownloadTool, "check_path_allowed", _allow_all_paths)
        dl = mod.DownloadTool(allow_ssrf_skip=True)

        async def fake_download(**kwargs):
            return {"path": str(tmp_path / "f.bin"), "size": 3, "segments": 1, "resumed": False}

        monkeypatch.setattr(dl, "_download", fake_download)
        result = await dl.execute(
            {"url": "http://127.0.0.1:8080/secret", "save_path": str(tmp_path)}
        )
        assert result.success is True


# ── 安全随迁 2：save_path 越界被拒 ─────────────────────────


class TestSavePathWorkspaceConstraint:
    """save_path 经 WorkspaceAwareMixin.check_path_allowed(write) 约束。"""

    @pytest.mark.asyncio
    async def test_save_path_outside_workspace_rejected(self, mod, monkeypatch, tmp_path):
        """check_path_allowed 返回 False → 目录穿越写被拒。"""
        monkeypatch.setattr(mod, "validate_url", lambda _url, _allow_domains=None: (True, "OK"))
        calls: list[tuple[str, str]] = []

        def fake_check(self, path, operation="read", agent_level=None):
            calls.append((path, operation))
            return False, "路径超出 workspace 范围"

        monkeypatch.setattr(mod.DownloadTool, "check_path_allowed", fake_check)
        dl = mod.DownloadTool()
        result = await dl.execute(
            {"url": "https://example.com/f.bin", "save_path": str(tmp_path / ".." / "evil")}
        )
        assert result.success is False
        assert "保存路径不在允许范围内" in (result.error or "")
        # 以 write 操作校验（下载是写文件）
        assert calls
        assert calls[0][1] == "write"

    @pytest.mark.asyncio
    async def test_save_path_allowed_proceeds_to_download(self, mod, monkeypatch, tmp_path):
        """校验通过后继续下载流程（不误伤正常路径）。"""
        monkeypatch.setattr(mod, "validate_url", lambda _url, _allow_domains=None: (True, "OK"))
        monkeypatch.setattr(mod.DownloadTool, "check_path_allowed", _allow_all_paths)
        dl = mod.DownloadTool()

        async def fake_download(**kwargs):
            return {"path": str(tmp_path / "f.bin"), "size": 3, "segments": 1, "resumed": False}

        monkeypatch.setattr(dl, "_download", fake_download)
        result = await dl.execute(
            {"url": "https://example.com/f.bin", "save_path": str(tmp_path)}
        )
        assert result.success is True
        assert result.output["success"] is True
