# @feature: FP-RP面板v3.1刀3 | @vision: V1 可进化 | @ci: python-coverage
"""RP 流式光标换装声明面测试（气泡卡退役后残存契约单置）。

契约（原 v3.1 刀3，卡型页退役 2026-10-17 后仅 client_styles 部分存续）：
- contributes.client_styles 增 rp_stream_cursor（modes 门 + styles/stream_cursor.css）；
- stream_cursor.css：目标宿主现役流式光标 .md-cursor，@import/外联 url() 零出现
  （pluginStyles 消毒白名单兼容，注入不整段被拒）。
（消息卡声明契约见 test_mode_panel_pages.test_roleplay_message_card_pages_retired
 ——气泡/状态卡两 webview 卡 2026-10-17 退役，消息流宿主原生渲染。）

纯声明面/静态文件测试。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLUGIN_ROOT = Path(MODES_DIR) / "mode_roleplay"
_MANIFEST = _PLUGIN_ROOT / "plugin.json"
_CURSOR_CSS = _PLUGIN_ROOT / "styles" / "stream_cursor.css"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


class TestStreamCursorDeclaration:
    """client_styles 光标换装声明契约。"""

    def test_client_styles_cursor_declared(self, manifest: dict) -> None:
        styles = {s["id"]: s for s in manifest["contributes"].get("client_styles", [])}
        cursor = styles["rp_stream_cursor"]
        assert cursor["modes"] == ["roleplay"]
        assert cursor["path"] == "styles/stream_cursor.css"
        assert (_PLUGIN_ROOT / "styles" / "stream_cursor.css").is_file()


class TestStreamCursorCss:
    """流式光标换装 CSS 静态契约。"""

    def test_targets_host_cursor(self) -> None:
        css = _CURSOR_CSS.read_text(encoding="utf-8")
        assert ".md-cursor" in css
        assert "@keyframes" in css

    def test_sanitizer_compatible(self) -> None:
        """无 @import / 外联 url() / expression —— pluginStyles 消毒白名单兼容
        （命中任一构造整段拒绝注入，换装静默失效）。"""
        css = _CURSOR_CSS.read_text(encoding="utf-8")
        assert not re.search(r"@import\b", css, re.IGNORECASE)
        assert not re.search(r"url\s*\(", css, re.IGNORECASE)
        assert "expression" not in css.lower()
        assert "javascript:" not in css.lower()
