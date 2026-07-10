"""prompt_build 时间注入的时区行为测试。

覆盖 _now_in_configured_tz（占位符路径）和 _build_dynamic_vars（兜底路径）
两条注入链路，验证：
- 时间按 APP_TIMEZONE 转换并带上 (UTC+x, 时区名) 后缀；
- 自定义时区生效；
- 无效时区降级到 UTC 并打 warning。
"""

from __future__ import annotations

import os
from datetime import datetime as _dt
from unittest.mock import patch

import pytest

from pipeline.plugin import PluginContext
from src.config.settings import get_settings, reset_settings
from src.plugins.input.prompt_build.plugin import PromptBuildPlugin

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


# ── 兜底路径：_build_dynamic_vars（无 dynamic_vars 配置） ───────

@pytest.mark.asyncio
async def test_dynamic_vars_fallback_has_tz_suffix(plugin):
    """兜底分支的时间行也带时区后缀。"""
    _set_tz("Asia/Shanghai")
    ctx = _make_ctx()  # state 无 context.dynamic_vars → 走兜底
    with _freeze_now(plugin):
        msg = await plugin._build_dynamic_vars(ctx)
    assert msg is not None
    content = msg["content"]
    assert "- 时间: 11:24:00 (UTC+8, Asia/Shanghai)" in content
    assert "- 日期: 2026-07-02" in content


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
            ctx, var_def, "", {"hard": [], "soft": []},
        )
    assert content == "2026-07-02 11:24:00 (UTC+8, Asia/Shanghai)"


def test_settings_default_timezone_when_no_env():
    """不设 APP_TIMEZONE 时默认 Asia/Shanghai。"""
    os.environ.pop("APP_TIMEZONE", None)
    reset_settings()
    assert get_settings().timezone == "Asia/Shanghai"
