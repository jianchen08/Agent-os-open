# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""prompt_build 时间注入的时区行为测试。

覆盖 _now_in_configured_tz（占位符路径）和 _build_dynamic_vars（兜底路径）
两条注入链路，验证：
- 时间按 APP_TIMEZONE 转换并带上 (UTC+x, 时区名) 后缀；
- 自定义时区生效；
- 无效时区降级到 UTC 并打 warning。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime as _dt
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# 复制 server.py 的 sys.path 机制：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _THIS_DIR 显式路径加载（与 test_prompt_build.py 的 _load_plugin_module 同范式）。
_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_tz_test", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_tz_test"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin

from agentos_plugin_sdk.settings import get_settings, reset_settings

# 固定一个"绝对时间"作为 now，避免依赖机器时钟。
# 2026-07-02 03:24:00 UTC → Asia/Shanghai 11:24:00、Asia/Tokyo 12:24:00
FROZEN_ISO = "2026-07-02T03:24:00+00:00"
_FROZEN = _dt.fromisoformat(FROZEN_ISO)


@pytest.fixture
def plugin() -> PromptBuildPlugin:
    return PromptBuildPlugin({})


def _make_ctx() -> PluginContext:
    return PluginContext(state={})


def _freeze_now(plugin):
    """把 plugin._current_now(tz) 钉到固定 UTC 时刻，按传入 tz 正确转换。"""

    def fake_now(tz):
        return _FROZEN.astimezone(tz)

    return patch.object(plugin, "_current_now", side_effect=fake_now)


@pytest.fixture(autouse=True)
def _restore_settings():
    """每个用例后重置 settings，避免环境变量互相污染。"""
    yield
    reset_settings()


def _set_tz(tz: str) -> None:
    """设置 APP_TIMEZONE 并重建 settings 单例使其生效。"""
    os.environ["APP_TIMEZONE"] = tz
    reset_settings()


# ── 占位符路径：_now_in_configured_tz ──────────────────────────

@pytest.mark.asyncio
async def test_timestamp_placeholder_default_tz(plugin):
    """默认 Asia/Shanghai：UTC 03:24 → 11:24，带 (UTC+8, Asia/Shanghai)。"""
    _set_tz("Asia/Shanghai")
    with _freeze_now(plugin):
        now, suffix = plugin._now_in_configured_tz()
    assert now.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-02 11:24:00"
    assert suffix == "(UTC+8, Asia/Shanghai)"


@pytest.mark.asyncio
async def test_timestamp_placeholder_custom_tz(plugin):
    """自定义 Asia/Tokyo：UTC 03:24 → 12:24，带 (UTC+9, Asia/Tokyo)。"""
    _set_tz("Asia/Tokyo")
    with _freeze_now(plugin):
        now, suffix = plugin._now_in_configured_tz()
    assert now.strftime("%H:%M:%S") == "12:24:00"
    assert suffix == "(UTC+9, Asia/Tokyo)"


@pytest.mark.asyncio
async def test_timestamp_half_hour_offset(plugin):
    """半时区 Asia/Kolkata (+0530)：显示 UTC+5:30。"""
    _set_tz("Asia/Kolkata")
    with _freeze_now(plugin):
        _, suffix = plugin._now_in_configured_tz()
    assert suffix == "(UTC+5:30, Asia/Kolkata)"


@pytest.mark.asyncio
async def test_timestamp_invalid_tz_falls_back_to_utc(plugin, caplog):
    """无效时区降级 UTC 并打 warning。"""
    _set_tz("Invalid/Foo")
    with _freeze_now(plugin), caplog.at_level("WARNING"):
        now, suffix = plugin._now_in_configured_tz()
    assert now.strftime("%H:%M:%S") == "03:24:00"
    assert suffix == "(UTC+0, UTC)"
    assert any("回退到 UTC" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_timestamp_utc_explicit(plugin):
    """显式 UTC：offset 为 0，显示 UTC+0。"""
    _set_tz("UTC")
    with _freeze_now(plugin):
        _, suffix = plugin._now_in_configured_tz()
    assert suffix == "(UTC+0, UTC)"


# ── 零兜底路径：_build_dynamic_vars（无配置 → 不注入） ──────────

@pytest.mark.asyncio
async def test_dynamic_vars_no_config_no_injection(plugin):
    """零兜底（2026-08-20 裁定）：agent 配置与插件默认皆无 → 不注入任何动态变量。

    旧硬编码兜底块（日期/时间/Agent/会话）已删——配置没声明的变量一律不注入。
    """
    _set_tz("Asia/Shanghai")
    ctx = _make_ctx()  # state 无 context.dynamic_vars，插件 config 无 dynamic_vars
    with _freeze_now(plugin):
        msg = await plugin._build_dynamic_vars(ctx)
    assert msg is None, "未声明配置 → 不注入动态变量消息"


@pytest.mark.asyncio
async def test_dynamic_vars_plugin_config_default_renders_timestamp(plugin):
    """插件配置口子（全局变量声明）：无 agent 配置时插件默认变量生效。"""
    _set_tz("Asia/Shanghai")
    plugin._config = {
        "dynamic_vars": [
            {"type": "timestamp", "name": "时间", "format": "%H:%M:%S"},
        ],
    }
    ctx = _make_ctx()
    with _freeze_now(plugin):
        msg = await plugin._build_dynamic_vars(ctx)
    assert msg is not None
    assert "- 时间: 11:24:00 (UTC+8, Asia/Shanghai)" in msg["content"]


@pytest.mark.asyncio
async def test_dynamic_vars_agent_config_overrides_plugin_default(plugin):
    """优先级：agent 配置（context.dynamic_vars）> 插件默认。"""
    _set_tz("Asia/Shanghai")
    plugin._config = {
        "dynamic_vars": [
            {"type": "timestamp", "name": "插件默认时间", "format": "%H:%M:%S"},
        ],
    }
    ctx = _make_ctx()
    ctx.state["context.dynamic_vars"] = [
        {"type": "timestamp", "name": "agent时间", "format": "%H:%M:%S"},
    ]
    with _freeze_now(plugin):
        msg = await plugin._build_dynamic_vars(ctx)
    assert msg is not None
    assert "- agent时间: 11:24:00" in msg["content"]
    assert "插件默认时间" not in msg["content"], "agent 配置优先，插件默认被覆盖"


# ── 端到端：占位符经 _resolve_single_var_content ───────────────

@pytest.mark.asyncio
async def test_resolve_single_var_timestamp_with_suffix(plugin):
    """_resolve_single_var_content 的 timestamp 分支产出带后缀字符串。"""
    _set_tz("Asia/Shanghai")
    ctx = _make_ctx()
    var_def = {"type": "timestamp", "name": "timestamp",
               "format": "%Y-%m-%d %H:%M:%S"}
    with _freeze_now(plugin):
        content = await plugin._resolve_single_var_content(
            ctx, var_def, "",
        )
    assert content == "2026-07-02 11:24:00 (UTC+8, Asia/Shanghai)"


def test_settings_default_timezone_when_no_env():
    """不设 APP_TIMEZONE 时默认 Asia/Shanghai。"""
    os.environ.pop("APP_TIMEZONE", None)
    reset_settings()
    assert get_settings().timezone == "Asia/Shanghai"
