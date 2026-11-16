# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""triggers_ext 时区回退告警口径（问题 9 同族，与 prompt_build 侧同款）。

naive 时间按配置时区（settings.timezone=APP_TIMEZONE）解释；解析不了时回退
UTC 解释——回退必须**显式告警**且区分两类缺口，否则排障方向被误导：

- 运行期有时区库（系统 TZPATH 或 tzdata 包）→ 配置值确实不是合法 IANA 名；
- 运行期无任何时区库（Windows 无系统库 + 插件 venv 未装 tzdata）→ 配置值
  可能完全合法，缺的是数据（环境缺口），告警须给可执行修复指引。

同因只告警一次（同一时区名不刷屏）。绝不用机器本地时区兜底——那会把配置
语义偷换成本机时区（已被否决）。
"""

from __future__ import annotations

import datetime
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_tool() -> Any:
    mod_name = "triggers_ext_tool_tz_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


@pytest.fixture
def tool_mod() -> Any:
    mod = _load_tool()
    # 同因去重集合按测试隔离（模块级状态跨用例清零）
    mod._tz_fallback_warned.clear()
    return mod


def _future_naive(hours: int = 2) -> str:
    return (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")


class TestTimezoneFallbackWarning:
    """回退告警：两类缺口可区分 + 同因去重 + 不用机器时区兜底。"""

    def test_missing_tz_database_reports_environment_gap(self, tool_mod: Any, caplog) -> None:
        """运行期缺时区库 → 告警指向环境缺口并给修复指引（不误报配置非法）。"""
        with (
            patch.object(
                tool_mod, "ZoneInfo", side_effect=ZoneInfoNotFoundError("No time zone found")
            ),
            patch.object(tool_mod, "tz_database_available", return_value=False),
            caplog.at_level(logging.WARNING),
        ):
            tool_mod._warn_tz_fallback("Asia/Shanghai", ZoneInfoNotFoundError("x"))
        messages = [r.getMessage() for r in caplog.records]
        assert messages, "缺时区库必须告警"
        assert any("缺 IANA 时区库" in m for m in messages), messages
        assert any("uv sync" in m for m in messages), f"缺修复指引：{messages}"
        assert not any("无效" in m for m in messages), f"合法时区名不得被误报无效：{messages}"

    def test_invalid_config_value_reported_as_invalid(self, tool_mod: Any, caplog) -> None:
        """运行期有时区库 → 配置值非法（与缺库两类缺口可区分）。"""
        with (
            patch.object(tool_mod, "tz_database_available", return_value=True),
            caplog.at_level(logging.WARNING),
        ):
            tool_mod._warn_tz_fallback("Not/AZone", ZoneInfoNotFoundError("x"))
        messages = [r.getMessage() for r in caplog.records]
        assert any("无效" in m for m in messages), messages

    def test_same_reason_warns_once(self, tool_mod: Any, caplog) -> None:
        """同因只告警一次（同一时区名重复回退不刷屏）。"""
        with (
            patch.object(tool_mod, "tz_database_available", return_value=True),
            caplog.at_level(logging.WARNING),
        ):
            tool_mod._warn_tz_fallback("Not/AZone", ZoneInfoNotFoundError("x"))
            tool_mod._warn_tz_fallback("Not/AZone", ZoneInfoNotFoundError("x"))
        assert len([r for r in caplog.records if "APP_TIMEZONE" in r.getMessage()]) == 1

    def test_no_machine_local_timezone_fallback(self, tool_mod: Any, caplog) -> None:
        """naive 时间回退到 UTC（显式），不用机器本地时区偷换配置语义。

        性质断言：回退解释后的挂钟时刻按 UTC 落（偏移 0）——若偷换成本机本地
        时区，偏移量会随机器变，触发时刻不可预期。
        """
        from triggers.manager import TriggerManager

        fresh = TriggerManager()
        tool_mod.get_trigger_manager = lambda: fresh
        tool_mod._tz_fallback_warned.clear()
        inst = tool_mod.TriggerSetupTool()
        naive = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
        naive_str = naive.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with (
                patch.object(
                    tool_mod,
                    "ZoneInfo",
                    side_effect=ZoneInfoNotFoundError("No time zone found"),
                ),
                patch.object(tool_mod, "tz_database_available", return_value=False),
                patch.object(
                    tool_mod,
                    "get_settings",
                    lambda: types.SimpleNamespace(timezone="Asia/Shanghai"),
                ),
                caplog.at_level(logging.WARNING),
            ):
                r = _run(
                    inst.execute(
                        {
                            "trigger_type": "schedule",
                            "schedule_time": naive_str,
                            "message": "m",
                            "pipeline_id": "p",
                        }
                    )
                )
            assert r.success, r.error
            cfg = next(iter(fresh._triggers.values()))
            assert cfg.scheduled_at is not None
            # 回退到 UTC：naive 挂钟时刻原样按 UTC 落（不偏移）
            assert (
                cfg.scheduled_at.replace(tzinfo=None)
                == naive.replace(microsecond=0, tzinfo=None)
            )
            assert cfg.scheduled_at.utcoffset() == datetime.timedelta(0)
            assert any("缺 IANA 时区库" in m.getMessage() for m in caplog.records)
        finally:
            fresh.stop_check_loop()
