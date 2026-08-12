#!/usr/bin/env python3
# @feature: FP-MIGR 0.1→0.2迁移清理 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""第三方通道插件迁移验证测试。

验证每个通道插件的：
1. plugin.json 存在且格式有效
2. server.py 存在且可导入核心适配器类
3. 导入路径适配后无残留 channels. / pipeline.types 引用（模块级）
4. 关键类可实例化

[来源: docs/working/module_migration_plan.md §5.2, §10.1]
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

PLUGINS_BASE = Path(__file__).resolve().parent.parent.parent / "plugins" / "shared" / "system"

CHANNELS = [
    "channel_dingtalk",
    "channel_feishu",
    "channel_wecom",
    "channel_cli",
    "channel_qq",
    "channel_api",
    "channel_gateway",
]


class TestPluginJson:
    """验证每个通道插件的 plugin.json。"""

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_plugin_json_exists(self, channel: str) -> None:
        """plugin.json 文件存在。"""
        path = PLUGINS_BASE / channel / "plugin.json"
        assert path.exists(), f"plugin.json not found: {path}"

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_plugin_json_valid(self, channel: str) -> None:
        """plugin.json 是有效 JSON 且包含必需字段。"""
        path = PLUGINS_BASE / channel / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "id" in data, f"Missing 'id' in {channel}/plugin.json"
        assert "name" in data, f"Missing 'name' in {channel}/plugin.json"
        assert "plugin_type" in data, f"Missing 'plugin_type' in {channel}/plugin.json"
        assert "entry" in data, f"Missing 'entry' in {channel}/plugin.json"
        assert "capabilities" in data, f"Missing 'capabilities' in {channel}/plugin.json"
        assert "tools" in data["capabilities"], (
            f"Missing 'tools' in capabilities in {channel}/plugin.json"
        )

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_plugin_json_id_matches_dir(self, channel: str) -> None:
        """plugin.json 的 id 与目录名一致。"""
        path = PLUGINS_BASE / channel / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == channel, (
            f"id mismatch: expected {channel}, got {data['id']}"
        )


class TestServerPy:
    """验证每个通道插件的 server.py。"""

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_server_py_exists(self, channel: str) -> None:
        """server.py 文件存在。"""
        path = PLUGINS_BASE / channel / "server.py"
        assert path.exists(), f"server.py not found: {path}"

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_server_py_has_sys_path(self, channel: str) -> None:
        """server.py 包含 sys.path.insert 让平铺导入可用。"""
        path = PLUGINS_BASE / channel / "server.py"
        content = path.read_text(encoding="utf-8")
        assert "sys.path.insert" in content, (
            f"server.py must contain sys.path.insert for flat imports: {channel}"
        )


class TestImportPaths:
    """验证导入路径适配正确，无残留旧路径。"""

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_no_stale_channels_import(self, channel: str) -> None:
        """模块级无残留 from channels.xxx 导入（lazy import 除外）。"""
        ch_dir = PLUGINS_BASE / channel
        py_files = list(ch_dir.glob("*.py"))
        stale: list[str] = []
        for f in py_files:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                # Skip lazy imports inside functions
                if stripped.startswith("from channels.") and "noqa" not in stripped:
                    stale.append(f"{f.name}:{i}: {stripped}")
        assert not stale, f"Stale module-level 'from channels.' imports in {channel}:\n" + "\n".join(stale)


class TestAdapterClasses:
    """验证关键适配器类可导入和实例化。"""

    def _import_from_channel(self, channel: str, module: str) -> object:
        """从通道插件目录导入模块（模拟 server.py 的 sys.path 机制）。"""
        ch_path = str(PLUGINS_BASE / channel)
        # Save and restore sys.path
        old_path = sys.path[:]
        sys.path.insert(0, ch_path)
        try:
            # Clear cached modules to avoid cross-test pollution
            keys_to_clear = [
                k for k in sys.modules
                if k in (module, "adapter", "stream_client", "card_builder", "crypto",
                         "helpers", "output_adapter", "_base_output_adapter",
                         "onebot_client", "input_adapter", "base_combo_adapter",
                         "pipeline_types", "channel_gateway", "message_normalizer",
                         "unified_types", "session_bridge")
            ]
            for k in keys_to_clear:
                del sys.modules[k]
            return importlib.import_module(module)
        finally:
            sys.path[:] = old_path

    def test_dingtalk_adapter_importable(self) -> None:
        """DingTalkAdapter 可导入且包含关键方法。"""
        mod = self._import_from_channel("channel_dingtalk", "adapter")
        assert hasattr(mod, "DingTalkAdapter")
        assert hasattr(mod, "DingTalkInputAdapter")
        assert hasattr(mod, "DingTalkOutputAdapter")

    def test_dingtalk_stream_client_importable(self) -> None:
        """DingTalkStreamClient 可导入。"""
        mod = self._import_from_channel("channel_dingtalk", "stream_client")
        assert hasattr(mod, "DingTalkStreamClient")

    def test_feishu_adapter_importable(self) -> None:
        """FeishuAdapter 可导入且包含关键方法。"""
        mod = self._import_from_channel("channel_feishu", "adapter")
        assert hasattr(mod, "FeishuAdapter")
        assert hasattr(mod, "FeishuInputAdapter")
        assert hasattr(mod, "FeishuOutputAdapter")

    def test_feishu_card_builder_importable(self) -> None:
        """CardBuilder 可导入且有预置模板。"""
        mod = self._import_from_channel("channel_feishu", "card_builder")
        assert hasattr(mod, "CardBuilder")
        card = mod.CardBuilder.build_text_card("Test", "Content")
        assert "elements" in card

    def test_wecom_adapter_importable(self) -> None:
        """WeComAdapter 可导入且包含关键方法。"""
        mod = self._import_from_channel("channel_wecom", "adapter")
        assert hasattr(mod, "WeComAdapter")
        assert hasattr(mod, "WeComInputAdapter")

    def test_wecom_crypto_importable(self) -> None:
        """WecomCrypto 可导入（依赖 cryptography 库）。"""
        mod = self._import_from_channel("channel_wecom", "crypto")
        assert hasattr(mod, "WecomCrypto")

    def test_qq_adapter_importable(self) -> None:
        """QQAdapter 可导入且包含关键方法。"""
        mod = self._import_from_channel("channel_qq", "adapter")
        assert hasattr(mod, "QQAdapter")
        assert hasattr(mod, "QQInputAdapter")

    def test_qq_onebot_client_importable(self) -> None:
        """OneBotClient 可导入。"""
        mod = self._import_from_channel("channel_qq", "onebot_client")
        assert hasattr(mod, "OneBotClient")

    def test_gateway_importable(self) -> None:
        """ChannelGateway 及相关模块可导入。"""
        mod = self._import_from_channel("channel_gateway", "channel_gateway")
        assert hasattr(mod, "ChannelGateway")
        ut = self._import_from_channel("channel_gateway", "unified_types")
        assert hasattr(ut, "UnifiedMessage")
        assert hasattr(ut, "UnifiedResponse")
        sb = self._import_from_channel("channel_gateway", "session_bridge")
        assert hasattr(sb, "SessionBridge")
        mn = self._import_from_channel("channel_gateway", "message_normalizer")
        assert hasattr(mn, "MessageNormalizer")

    def test_dingtalk_adapter_instantiable(self) -> None:
        """DingTalkAdapter 可实例化。"""
        mod = self._import_from_channel("channel_dingtalk", "adapter")
        adapter = mod.DingTalkAdapter(
            client_id="test_id",
            client_secret="test_secret",
        )
        assert adapter.channel_type == "dingtalk"
        assert hasattr(adapter, "start")
        assert hasattr(adapter, "stop")
        assert hasattr(adapter, "input_adapter")
        assert hasattr(adapter, "output_adapter")

    def test_feishu_adapter_instantiable(self) -> None:
        """FeishuAdapter 可实例化。"""
        mod = self._import_from_channel("channel_feishu", "adapter")
        adapter = mod.FeishuAdapter(
            app_id="test_app_id",
            app_secret="test_secret",
        )
        assert adapter.channel_type == "feishu"

    def test_qq_adapter_instantiable(self) -> None:
        """QQAdapter 可实例化。"""
        mod = self._import_from_channel("channel_qq", "adapter")
        adapter = mod.QQAdapter(
            ws_port=9999,
            http_api_url="http://test:5700",
        )
        assert adapter.channel_type == "qq"

    def test_gateway_instantiable(self) -> None:
        """ChannelGateway 可实例化。"""
        mod = self._import_from_channel("channel_gateway", "channel_gateway")
        gateway = mod.ChannelGateway()
        assert hasattr(gateway, "register_adapter")
        assert hasattr(gateway, "handle_message")
        assert hasattr(gateway, "send_response")
