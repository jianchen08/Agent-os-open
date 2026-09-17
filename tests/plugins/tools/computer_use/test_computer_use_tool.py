# @feature: FP-0.2.二 工具插件面 | @ci: python-coverage
"""computer_use 工具核心逻辑测试——上游映射 / 结果归一 / 图片回传信封。

Bridge 传输层（BridgeClient）以 monkeypatch 打桩（外部 HTTP 依赖），tool.py
的映射/归一/信封构造全真实执行；截图落盘走真实文件系统（tmp_path）。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage

from agentos_plugin_sdk.bridge_client import BridgeClient

pytestmark = pytest.mark.unit

HERE = Path(__file__).parent
PLUGIN_DIR = HERE.parents[3] / "plugins" / "shared" / "tools" / "computer_use"


def _load_tool_module() -> Any:
    """按显式路径加载 tool.py（裸名 tool 槽位防跨插件劫持）。"""
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    name = "computer_use_tool_impl"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TOOL = _load_tool_module()


def _make_png(width: int = 4, height: int = 3, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    img = PILImage.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeBridge:
    """BridgeClient.call 打桩：记录调用并回放 canned MCP result。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result: dict = {"content": [{"type": "text", "text": "ok"}]}

    def install(self, monkeypatch: Any) -> _FakeBridge:
        test = self

        def fake_call(self: Any, tool: str, arguments: dict) -> dict:
            test.calls.append((tool, arguments))
            return test.result

        # token 在构造期解析（Browser surface 测试同款钉桩），一并钉住
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "test-token"))
        monkeypatch.setattr(BridgeClient, "call", fake_call)
        return self

    def upstream_name(self) -> str:
        assert self.calls, "上游未被调用"
        return self.calls[-1][0]

    def upstream_args(self) -> dict:
        assert self.calls, "上游未被调用"
        return self.calls[-1][1]


def _new_tool() -> Any:
    return TOOL.ComputerUseTool()


def _error_code(result: Any) -> str:
    assert not result.success, f"期望失败，实际成功: {result.output}"
    assert result.error_code is not None
    return result.error_code


@pytest.fixture
def fake_bridge(monkeypatch: Any) -> _FakeBridge:
    return _FakeBridge().install(monkeypatch)


# ── 上游映射 ──────────────────────────────────────────


class TestUpstreamMapping:
    @pytest.mark.parametrize(
        ("agent_tool", "upstream_tool"),
        [
            ("computer_snapshot", "Snapshot"),
            ("computer_take_screenshot", "Screenshot"),
            ("computer_click", "Click"),
            ("computer_type", "Type"),
            ("computer_scroll", "Scroll"),
            ("computer_move", "Move"),
            ("computer_key", "Shortcut"),
            ("computer_wait", "Wait"),
            ("computer_app", "App"),
        ],
    )
    async def test_tool_routes_to_upstream_name(self, fake_bridge, agent_tool, upstream_tool):
        result = await _new_tool().execute({"_tool_name": agent_tool})
        assert result.success
        assert fake_bridge.upstream_name() == upstream_tool

    async def test_click_args_passthrough_and_none_dropped(self, fake_bridge):
        result = await _new_tool().execute(
            {"_tool_name": "computer_click", "loc": [120, 80], "button": "right", "clicks": 2, "label": None}
        )
        assert result.success
        assert fake_bridge.upstream_args() == {"loc": [120, 80], "button": "right", "clicks": 2}

    async def test_internal_keys_never_reach_upstream(self, fake_bridge, tmp_path):
        result = await _new_tool().execute(
            {
                "_tool_name": "computer_type",
                "text": "hi",
                "workspace": str(tmp_path),
                "session_id": "s1",
                "_container_id": "",
            }
        )
        assert result.success
        assert fake_bridge.upstream_args() == {"text": "hi"}

    async def test_app_full_args_passthrough(self, fake_bridge):
        result = await _new_tool().execute(
            {
                "_tool_name": "computer_app",
                "mode": "resize",
                "name": "记事本",
                "window_loc": [10, 20],
                "window_size": [800, 600],
            }
        )
        assert result.success
        assert fake_bridge.upstream_args() == {
            "mode": "resize",
            "name": "记事本",
            "window_loc": [10, 20],
            "window_size": [800, 600],
        }


# ── 结果归一与图片信封 ──────────────────────────────────────


class TestResultNormalization:
    async def test_text_content_becomes_snapshot_text(self, fake_bridge):
        fake_bridge.result = {"content": [{"type": "text", "text": "Clicked at (1,2)."}]}
        result = await _new_tool().execute({"_tool_name": "computer_click", "loc": [1, 2]})
        assert result.success
        assert result.output["snapshot_text"] == "Clicked at (1,2)."
        assert result.output["tool"] == "computer_click"

    async def test_non_dict_content_items_skipped(self, fake_bridge):
        fake_bridge.result = {"content": ["garbage", None, {"type": "text", "text": "fine"}]}
        result = await _new_tool().execute({"_tool_name": "computer_key", "shortcut": "ctrl+s"})
        assert result.success
        assert result.output["snapshot_text"] == "fine"

    async def test_is_error_maps_to_upstream_tool_error(self, fake_bridge):
        fake_bridge.result = {
            "isError": True,
            "content": [{"type": "text", "text": "Either loc or label must be provided."}],
        }
        result = await _new_tool().execute({"_tool_name": "computer_click"})
        assert _error_code(result) == "UPSTREAM_TOOL_ERROR"
        assert "loc or label" in result.error

    async def test_image_content_saved_and_multimodal(self, fake_bridge, tmp_path):
        raw = _make_png(64, 48)
        fake_bridge.result = {
            "content": [
                {"type": "text", "text": "Screenshot captured."},
                {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"},
            ]
        }
        result = await _new_tool().execute({"_tool_name": "computer_take_screenshot", "workspace": str(tmp_path)})
        assert result.success
        out = result.output
        saved = Path(out["file_path"])
        assert saved.parent == tmp_path
        assert saved.exists()
        assert saved.read_bytes() == raw
        assert out["image_mime"] == "image/png"
        assert out["snapshot_text"] == "Screenshot captured."
        url = result.metadata["multimodal_content"][0]["image_url"]["url"]
        assert url == f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"

    async def test_image_too_large_rejected(self, fake_bridge, tmp_path, monkeypatch):
        monkeypatch.setattr("agentos_plugin_sdk.multimodal.MAX_IMAGE_BYTES", 10)
        fake_bridge.result = {
            "content": [{"type": "image", "data": base64.b64encode(_make_png(64, 48)).decode("ascii")}]
        }
        result = await _new_tool().execute({"_tool_name": "computer_take_screenshot", "workspace": str(tmp_path)})
        assert _error_code(result) == "IMAGE_TOO_LARGE"

    async def test_bad_base64_rejected(self, fake_bridge, tmp_path):
        fake_bridge.result = {"content": [{"type": "image", "data": "not-base64!!", "mimeType": "image/png"}]}
        result = await _new_tool().execute({"_tool_name": "computer_take_screenshot", "workspace": str(tmp_path)})
        assert _error_code(result) == "IMAGE_DECODE_FAILED"


# ── 传输失败 / 前置条件 ──────────────────────────────────────


class TestTransportAndGuards:
    async def test_token_missing_is_clean_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)
        monkeypatch.setattr("agentos_plugin_sdk.bridge_client._find_project_root", lambda: "")
        result = await _new_tool().execute({"_tool_name": "computer_snapshot", "workspace": str(tmp_path)})
        assert _error_code(result) == "BRIDGE_TOKEN_MISSING"

    async def test_bridge_unreachable_is_clean_error(self, monkeypatch, tmp_path):
        # 不用 fake_bridge fixture：本用例要打传输层（_send_direct），
        # call 必须走真实实现才能抵达传输层
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok"))
        monkeypatch.setattr(
            BridgeClient,
            "_send_direct",
            lambda _self, _payload: {"ok": False, "status": 0, "body": {"error": "bridge unreachable"}},
        )
        result = await _new_tool().execute({"_tool_name": "computer_snapshot", "workspace": str(tmp_path)})
        assert _error_code(result) == "BRIDGE_CALL_FAILED"

    async def test_bridge_governance_rejection_passthrough(self, monkeypatch, tmp_path):
        monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda _cfg: "tok"))
        monkeypatch.setattr(
            BridgeClient,
            "_send_direct",
            lambda _self, _payload: {"ok": False, "status": 403, "body": {"error": "工具不在白名单"}},
        )
        result = await _new_tool().execute({"_tool_name": "computer_snapshot", "workspace": str(tmp_path)})
        assert _error_code(result) == "BRIDGE_CALL_FAILED"
        assert "白名单" in result.error

    async def test_isolated_task_refused_before_any_io(self, tmp_path, monkeypatch):
        called: list[bool] = []

        def _boom(*args: Any, **kwargs: Any) -> None:
            called.append(True)
            raise AssertionError("隔离任务不应触达 Bridge")

        monkeypatch.setattr(BridgeClient, "call", _boom)
        result = await _new_tool().execute(
            {"_tool_name": "computer_snapshot", "workspace": str(tmp_path), "_container_id": "c-123"}
        )
        assert _error_code(result) == "ISOLATED_HOST_ONLY"
        assert not called

    async def test_unknown_tool(self, fake_bridge):
        result = await _new_tool().execute({"_tool_name": "computer_format_disk"})
        assert _error_code(result) == "UNKNOWN_COMPUTER_TOOL"

    def test_get_tool_definition(self):
        tool_def = TOOL.ComputerUseTool.get_tool_definition()
        assert tool_def.name == "computer_use"
        assert tool_def.category == TOOL.ToolCategory.SYSTEM
