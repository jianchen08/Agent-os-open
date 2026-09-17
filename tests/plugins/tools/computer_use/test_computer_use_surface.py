# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
"""computer_use 插件面测试——manifest 声明 / server 注册一致性 / 使用契约入 description。

锁死三条契约：
1. plugin.json 是唯一声明源，server.py 的 tools/list 注册与 manifest 逐字
   一致（双写漂移会被内核 G2 净化剔除，BUG-5 判例）。
2. 使用契约（操作循环/label 优先）写在工具 description——工具面自描述，
   不依赖技能文档（用户裁定 2026-09-18）。
3. server handler 返回 ToolExecutionResult 对象本身（SDK to_dict 整包序列化
   success/output/metadata）——截图的 multimodal_content 在 metadata 里，
   改成 ``.output`` 会把它丢掉、图片注入断链。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent
PLUGIN_DIR = HERE.parents[3] / "plugins" / "shared" / "tools" / "computer_use"

EXPECTED_TOOLS = {
    "computer_snapshot",
    "computer_take_screenshot",
    "computer_click",
    "computer_type",
    "computer_scroll",
    "computer_move",
    "computer_key",
    "computer_wait",
    "computer_app",
}


def _manifest() -> dict[str, Any]:
    with open(PLUGIN_DIR / "plugin.json", encoding="utf-8") as f:
        return json.load(f)


def _manifest_tools() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _manifest()["capabilities"]["tools"]}


def _load_server() -> Any:
    """动态加载 server.py（先逐出实现模块缓存，防跨测试劫持）。"""
    mod_name = "computer_use_server_test"
    sys.modules.pop(mod_name, None)
    sys.modules.pop("computer_use_tool_impl", None)
    spec = importlib.util.spec_from_file_location(mod_name, PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── manifest 声明 ──────────────────────────────────────────


class TestManifest:
    def test_nine_tools_declared(self):
        assert set(_manifest_tools()) == EXPECTED_TOOLS

    def test_every_tool_has_contract(self):
        for t in _manifest()["capabilities"]["tools"]:
            schema = t["input_schema"]
            assert schema["type"] == "object"
            assert schema.get("additionalProperties") is False
            # required 必须是已声明属性的子集（空参工具可省 required 键）
            assert set(schema.get("required", [])) <= set(schema.get("properties", {}))
            assert t["output_schema"]["required"] == ["status"]
            assert t["render"]["card"] in {"image", "generic"}

    def test_usage_contract_lives_in_descriptions(self):
        """用户裁定：说明并入工具 description，不设技能文档。"""
        tools = _manifest_tools()
        assert "操作循环" in tools["computer_snapshot"]["description"]
        assert "重新看屏" in tools["computer_snapshot"]["description"]
        assert "label" in tools["computer_click"]["description"]
        assert "优先用" in tools["computer_click"]["description"]

    def test_bridge_whitelist_covers_declared_tools(self):
        """plugin.json 声明的九工具的上游名必须在 bridge.yaml computer 白名单里。"""
        import yaml

        with open(PLUGIN_DIR.parents[3] / "config" / "bridge" / "bridge.yaml", encoding="utf-8") as f:
            bridge = yaml.safe_load(f)
        upstream = bridge["upstreams"]["computer"]
        assert upstream["command"] == "uvx"
        # 与 tool.py 的 _UPSTREAM_TOOLS 映射对齐（映射本身由路由测试锁定）
        expected_upstream = {"Snapshot", "Screenshot", "Click", "Type", "Scroll", "Move", "Shortcut", "Wait", "App"}
        assert expected_upstream <= set(upstream["tools"])
        # 上游 shell 类工具绝不放行（本仓自有 bash 与安全治理，不开旁路）
        banned = {"Shell", "PowerShell", "FileSystem", "Registry", "Process", "Clipboard", "Notify", "Scrape"}
        assert not banned & set(upstream["tools"])

    def test_plugin_identity(self):
        manifest = _manifest()
        assert manifest["id"] == "computer_use"
        assert manifest["plugin_type"] == "tool"
        assert manifest["host_type"] == "sidecar"
        assert manifest["entry"] == "python server.py"


# ── server 注册面 ──────────────────────────────────────────


class TestServerSurface:
    def test_registration_matches_manifest(self):
        mod = _load_server()
        registered = mod.plugin._tools
        assert set(registered) == EXPECTED_TOOLS
        manifest_tools = _manifest_tools()
        for name, tool_def in registered.items():
            spec = manifest_tools[name]
            assert tool_def.schema == spec["input_schema"]
            assert tool_def.description == spec["description"]
            assert tool_def.output_schema == spec["output_schema"]
            assert tool_def.render == spec["render"]

    def test_screenshot_handler_returns_metadata_envelope(self, tmp_path, monkeypatch):
        raw_png = _png_bytes()
        from agentos_plugin_sdk.bridge_client import BridgeClient

        def fake_call(self: Any, tool: str, arguments: dict) -> dict:
            assert tool == "Screenshot"
            return {
                "content": [
                    {"type": "image", "data": base64.b64encode(raw_png).decode("ascii"), "mimeType": "image/png"}
                ]
            }

        monkeypatch.setattr(BridgeClient, "call", fake_call)
        monkeypatch.setattr(
            "agentos_plugin_sdk.bridge_client.BridgeClient._resolve_token", staticmethod(lambda _cfg: "tok")
        )
        mod = _load_server()
        raw_result = asyncio.run(mod.plugin._tools["computer_take_screenshot"].handler(workspace=str(tmp_path / "ws")))

        # 关键契约：handler 返回值带 to_dict，metadata 承载 multimodal_content
        assert hasattr(raw_result, "to_dict")
        serialized = raw_result.to_dict()
        assert serialized["success"] is True
        assert "metadata" in serialized
        blocks = serialized["metadata"]["multimodal_content"]
        assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert Path(serialized["output"]["file_path"]).exists()

    def test_failure_handler_shape(self, tmp_path, monkeypatch):
        from agentos_plugin_sdk.bridge_client import BridgeClient

        def fake_call(self: Any, tool: str, arguments: dict) -> dict:
            return {"isError": True, "content": [{"type": "text", "text": "Either loc or label must be provided."}]}

        monkeypatch.setattr(BridgeClient, "call", fake_call)
        monkeypatch.setattr(
            "agentos_plugin_sdk.bridge_client.BridgeClient._resolve_token", staticmethod(lambda _cfg: "tok")
        )
        mod = _load_server()
        result = asyncio.run(mod.plugin._tools["computer_click"].handler(workspace=str(tmp_path)))
        assert hasattr(result, "to_dict")
        serialized = result.to_dict()
        assert serialized["success"] is False
        assert serialized["error_code"] == "UPSTREAM_TOOL_ERROR"

    def test_impl_class_cached_across_loads(self):
        mod = _load_server()
        first = mod._load_computer_tool()
        again = mod._load_computer_tool()
        assert again is first


def _png_bytes() -> bytes:
    import io

    from PIL import Image as PILImage

    img = PILImage.new("RGB", (32, 32), (10, 10, 10))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
