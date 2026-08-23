# @feature: FP-0.2.〇 安全检查 fail-closed（兜底反模式审查 P14） | @ci: none-local
"""编码穿越检测 fail-closed 单测（2026-08-20 P14）。

锁一件事：URL 解码异常时按检测命中处理（fail-closed）而非静默跳过——
检查器失败语义应是"可疑/拒绝"，静默跳过等于给编码穿越留绕过通道。

[来源: docs/working/兜底反模式全库审查_20260820.md 三节 P14]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 复制本目录 test_fuzzy_tool_matching.py 的加载机制（sys.path 注入 + 按路径加载）
_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import importlib.util  # noqa: E402
import urllib.parse  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from plugins.shared.pipeline.input.security_check.plugin import SecurityCheckPlugin

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_p14", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_sc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sc_mod)
if not TYPE_CHECKING:
    SecurityCheckPlugin = _sc_mod.SecurityCheckPlugin


def _make_plugin() -> SecurityCheckPlugin:
    return SecurityCheckPlugin(config={})


class TestEncodedTraversalFailClosed:
    def test_encoded_traversal_detected(self) -> None:
        """回归：URL 编码的 ../ 正常检出。"""
        plugin = _make_plugin()
        result = plugin._check_path_traversal({"path": "%2e%2e%2fsecret"})
        assert result.startswith("Encoded path traversal detected")
        assert "%2e%2e%2fsecret" in result

    def test_decode_failure_treated_as_hit(self, caplog, monkeypatch) -> None:
        """P14：解码异常 → 按检测命中处理 + warning（不再静默跳过该检查）。"""
        import logging

        def boom(s, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"x", 0, 1, "decode boom")

        monkeypatch.setattr(urllib.parse, "unquote", boom)
        plugin = _make_plugin()
        with caplog.at_level(logging.WARNING):
            result = plugin._check_path_traversal({"path": "a%zz/b"})
        assert result.startswith("Encoded path traversal detected"), (
            "解码失败必须按命中处理（fail-closed）"
        )
        assert "decode failed" in result
        assert any("解码异常" in r.getMessage() for r in caplog.records)

    def test_clean_path_passes(self) -> None:
        """回归：无穿越/无编码的路径放行。"""
        plugin = _make_plugin()
        assert plugin._check_path_traversal({"path": "workspace/normal/file.txt"}) == ""
