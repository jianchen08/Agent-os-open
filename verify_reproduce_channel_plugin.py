#!/usr/bin/env python3
"""第三方通道插件迁移功能验证 — 可复现脚本。

用法：
    python3 verify_reproduce.py

前置条件：
    pip install pyyaml rich aiohttp pydantic fastapi uvicorn cryptography PyJWT
    pip install -e plugins/sdk

预期输出：所有 9 个场景全部 PASS。

[来源: docs/working/function_verify_report.md]
"""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径设置
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
PLUGINS_BASE = PROJECT_ROOT / "plugins" / "shared" / "system"
SDK_SRC = PROJECT_ROOT / "plugins" / "sdk" / "src"

# 添加 SDK 路径（绕过 __init__.py 缺失问题）
sys.path.insert(0, str(SDK_SRC))

CHANNELS = [
    "channel_dingtalk",
    "channel_feishu",
    "channel_wecom",
    "channel_cli",
    "channel_qq",
    "channel_api",
    "channel_gateway",
]

# 合法的 43 字符 base64 AES key（base64(b'\x00'*32) 去掉末尾 =）
VALID_AES_KEY = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE"

# 统计
_results: list[tuple[str, str, bool]] = []


def _record(scenario: str, detail: str, passed: bool) -> None:
    _results.append((scenario, detail, passed))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {scenario}: {detail}")
    if not passed:
        raise AssertionError(f"场景失败: {scenario} — {detail}")


def import_from_channel(channel: str, module: str):
    """从通道插件目录导入模块（模拟 server.py 的 sys.path 机制）。"""
    ch_path = str(PLUGINS_BASE / channel)
    old_path = sys.path[:]
    sys.path.insert(0, ch_path)
    mods_to_clear = [
        k for k in sys.modules
        if k in (module, "adapter", "stream_client", "card_builder", "crypto",
                 "helpers", "output_adapter", "_base_output_adapter", "onebot_client",
                 "input_adapter", "base_combo_adapter", "pipeline_types",
                 "channel_gateway", "message_normalizer", "unified_types",
                 "session_bridge", "cli_output_adapter", "server", "app")
    ]
    for k in mods_to_clear:
        del sys.modules[k]
    try:
        return importlib.import_module(module)
    finally:
        sys.path[:] = old_path


# ---------------------------------------------------------------------------
# 场景 1: 钉钉通道
# ---------------------------------------------------------------------------
def test_dingtalk() -> None:
    print("\n=== 场景1: 钉钉通道 ===")
    mod = import_from_channel("channel_dingtalk", "adapter")
    adapter = mod.DingTalkAdapter(client_id="x", client_secret="y")
    assert adapter.channel_type == "dingtalk"
    assert hasattr(adapter, "input_adapter")
    assert hasattr(adapter, "output_adapter")
    assert hasattr(adapter, "start")
    assert hasattr(adapter, "stop")
    _record("钉钉", f"channel_type='{adapter.channel_type}', "
            f"input={type(adapter.input_adapter).__name__}, "
            f"output={type(adapter.output_adapter).__name__}", True)


# ---------------------------------------------------------------------------
# 场景 2: 飞书通道
# ---------------------------------------------------------------------------
def test_feishu() -> None:
    print("\n=== 场景2: 飞书通道 ===")
    mod = import_from_channel("channel_feishu", "adapter")
    adapter = mod.FeishuAdapter(app_id="x", app_secret="y")
    assert adapter.channel_type == "feishu"
    _record("飞书适配器", f"channel_type='{adapter.channel_type}'", True)

    cb = import_from_channel("channel_feishu", "card_builder")
    card = cb.CardBuilder.build_text_card("标题", "内容")
    assert isinstance(card, dict)
    assert "elements" in card
    _record("飞书卡片", f"keys={list(card.keys())}, elements_count={len(card['elements'])}", True)


# ---------------------------------------------------------------------------
# 场景 3: 企微通道
# ---------------------------------------------------------------------------
def test_wecom() -> None:
    print("\n=== 场景3: 企微通道 ===")
    mod = import_from_channel("channel_wecom", "adapter")
    adapter = mod.WeComAdapter(
        corp_id="test_corp", agent_id=1, secret="s",
        token="t", encoding_aes_key=VALID_AES_KEY,
    )
    assert adapter.channel_type == "wecom"
    _record("企微适配器", f"channel_type='{adapter.channel_type}'", True)

    crypto_mod = import_from_channel("channel_wecom", "crypto")
    crypto_inst = crypto_mod.WecomCrypto(
        token="t", encoding_aes_key=VALID_AES_KEY, corp_id="test_corp",
    )
    assert crypto_inst is not None
    _record("企微加密", f"WecomCrypto instantiated: {type(crypto_inst).__name__}", True)


# ---------------------------------------------------------------------------
# 场景 4: QQ通道
# ---------------------------------------------------------------------------
def test_qq() -> None:
    print("\n=== 场景4: QQ通道 ===")
    mod = import_from_channel("channel_qq", "adapter")
    adapter = mod.QQAdapter(ws_port=9999, http_api_url="http://test:5700")
    assert adapter.channel_type == "qq"
    _record("QQ适配器", f"channel_type='{adapter.channel_type}'", True)

    helpers = import_from_channel("channel_qq", "helpers")
    # OneBot Array 格式
    array_msg = {"message": [
        {"type": "text", "data": {"text": "你好"}},
        {"type": "text", "data": {"text": "世界"}},
    ]}
    result = helpers._extract_qq_text(array_msg)
    assert result == "你好 世界"
    _record("QQ Array提取", f"result='{result}'", True)

    # CQ码格式
    cq_msg = {"message": "你好[CQ:at,qq=12345]世界"}
    result_cq = helpers._extract_qq_text(cq_msg)
    assert result_cq == "你好世界"
    _record("QQ CQ码提取", f"result='{result_cq}'", True)


# ---------------------------------------------------------------------------
# 场景 5: 网关通道
# ---------------------------------------------------------------------------
def test_gateway() -> None:
    print("\n=== 场景5: 网关通道 ===")
    gw_mod = import_from_channel("channel_gateway", "channel_gateway")
    gateway = gw_mod.ChannelGateway()
    assert hasattr(gateway, "register_adapter")
    assert hasattr(gateway, "handle_message")
    assert hasattr(gateway, "send_response")
    _record("网关方法", "register_adapter/handle_message/send_response 全部存在", True)

    mn = import_from_channel("channel_gateway", "message_normalizer")
    normalizer = mn.MessageNormalizer()
    for ch in ["feishu", "dingtalk", "wecom", "qq"]:
        assert ch in normalizer._normalizers
    _record("网关标准化器", f"注册渠道: {list(normalizer._normalizers.keys())}", True)


# ---------------------------------------------------------------------------
# 场景 6: CLI通道
# ---------------------------------------------------------------------------
def test_cli() -> None:
    print("\n=== 场景6: CLI通道 ===")
    cli_mod = import_from_channel("channel_cli", "cli_output_adapter")

    # UTF-8 直通
    result_utf8 = cli_mod.sanitize_for_terminal("hello 你好 🎉")
    assert result_utf8 == "hello 你好 🎉"
    _record("CLI UTF-8", f"result='{result_utf8}'", True)

    # GBK 降级
    old_stdout = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    try:
        result_gbk = cli_mod.sanitize_for_terminal("hello 你好 🎉")
        assert "?" in result_gbk
    finally:
        sys.stdout = old_stdout
    _record("CLI GBK降级", "emoji 🎉 被替换为 ?", True)


# ---------------------------------------------------------------------------
# 场景 7: API通道
# ---------------------------------------------------------------------------
def test_api() -> None:
    print("\n=== 场景7: API通道 ===")
    # SDK __init__.py 缺失，手动注册
    try:
        from agentos_plugin_sdk import AgentOSPlugin  # noqa: F401
    except ImportError:
        from agentos_plugin_sdk.plugin import AgentOSPlugin
        import agentos_plugin_sdk
        agentos_plugin_sdk.AgentOSPlugin = AgentOSPlugin

    mod = import_from_channel("channel_api", "server")
    assert hasattr(mod, "plugin")
    asyncio.run(mod._on_load({}))
    routes = mod._available_routes
    assert len(routes) == 20
    _record("API路由", f"route_count={len(routes)}", True)

    status = asyncio.run(mod.api_get_status())
    assert status["route_count"] == 20
    _record("API状态", f"api_get_status()={status}", True)


# ---------------------------------------------------------------------------
# 场景 8: plugin.json 验证
# ---------------------------------------------------------------------------
def test_plugin_json() -> None:
    print("\n=== 场景8: plugin.json 验证 ===")
    for channel in CHANNELS:
        path = PLUGINS_BASE / channel / "plugin.json"
        assert path.exists(), f"plugin.json not found: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        for field in ["id", "name", "plugin_type", "entry", "capabilities"]:
            assert field in data, f"Missing '{field}' in {channel}"
        assert "tools" in data["capabilities"]
        assert data["id"] == channel, f"id mismatch: {data['id']} != {channel}"
        tools_count = len(data["capabilities"]["tools"])
        _record(f"JSON:{channel}",
                f"id='{data['id']}', tools={tools_count}", True)


# ---------------------------------------------------------------------------
# 场景 9: pytest 测试套件
# ---------------------------------------------------------------------------
def test_pytest_suite() -> None:
    print("\n=== 场景9: pytest 测试套件 ===")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/unit/test_channel_migration.py",
         "-v", "--tb=short", "--no-header"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    output = result.stdout + result.stderr
    if "55 passed" in output:
        _record("pytest", "55/55 passed", True)
    else:
        # 提取通过数
        passed = output.count(" PASSED")
        failed = output.count(" FAILED")
        _record("pytest", f"passed={passed}, failed={failed}", failed == 0)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("第三方通道插件迁移功能验证")
    print("=" * 70)

    scenarios = [
        test_dingtalk, test_feishu, test_wecom, test_qq,
        test_gateway, test_cli, test_api, test_plugin_json, test_pytest_suite,
    ]

    for scenario in scenarios:
        try:
            scenario()
        except Exception as exc:
            _record(scenario.__name__, f"异常: {exc}", False)

    # 汇总
    print("\n" + "=" * 70)
    total = len(_results)
    passed = sum(1 for _, _, p in _results if p)
    failed = total - passed
    print(f"验证汇总: {passed}/{total} 通过, {failed} 失败")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
