# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""result_format 插件行为测试——门判据/格式化双路径/消息截断。

契约（plugin.py，2026-10-02 静态化迁移后）：
- 门判据 = tool_results 非空（for-each collect 置换键：非空 ⟺ 本轮执行过
  工具；无工具轮零产出）——旧 core_type 门已随动态核心槽退役；
- 成功结果 → _format_success，失败结果 → _format_error；
- messages 中 role=tool 的超长内容按 context_window 5% 截断；
  context_window 未设置跳过截断（警告日志）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "result_format_plugin_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_plugin_module()
ResultFormatPlugin = _MOD.ResultFormatPlugin


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_ctx(state: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config=config or {})


def _plugin() -> Any:
    return ResultFormatPlugin(config={})


class TestResultFormat:
    def test_empty_tool_results_yields_noop(self) -> None:
        """门判据：tool_results 空（纯文本轮）→ 零产出 {}。"""
        ctx = _make_ctx({"tool_results": [], "messages": []})
        result = _run(_plugin().execute(ctx))
        assert result.state_updates == {}

    def test_missing_tool_results_yields_noop(self) -> None:
        """门判据：键缺失（纯文本首轮）→ 零产出 {}（缺省容错）。"""
        ctx = _make_ctx({})
        result = _run(_plugin().execute(ctx))
        assert result.state_updates == {}

    def test_success_and_failure_formatted(self) -> None:
        """双路径：成功 → _format_success；失败 → _format_error（同名不同形）。"""
        ctx = _make_ctx(
            {
                "tool_results": [
                    {"name": "bash", "success": True, "result": "file1\nfile2"},
                    {"name": "file_read", "success": False, "error": "not found"},
                ],
                "messages": [],
            }
        )
        result = _run(_plugin().execute(ctx))
        formatted = result.state_updates["tool.formatted_results"]
        assert len(formatted) == 2
        assert formatted[0]["role"] == "tool"
        assert formatted[0]["name"] == "bash"
        assert "file1" in formatted[0]["content"]
        assert formatted[1]["name"] == "file_read"
        assert "not found" in formatted[1]["content"]

    def test_truncates_long_tool_messages_with_context_window(self) -> None:
        """context_window 已设置：超长 tool 消息按 5% 阈值截断（就地改 messages）。"""
        long_content = "x" * 10000
        state = {
            "context_window": 1000,  # 5% → 50 字符阈值
            "tool_results": [{"name": "bash", "success": True, "result": "ok"}],
            "messages": [{"role": "tool", "content": long_content, "seq": 1}],
        }
        ctx = _make_ctx(state)
        result = _run(_plugin().execute(ctx))
        assert "tool.formatted_results" in result.state_updates
        # 截断写在 ctx.state["messages"]（就地突变形态）。
        after = ctx.state["messages"][0]["content"]
        assert len(after) < len(long_content), "截断后内容必须短于原文"

    def test_no_context_window_skips_truncation(self) -> None:
        """context_window 未设置：跳过截断（警告），消息原样、格式化照常产出。"""
        long_content = "y" * 10000
        state = {
            "tool_results": [{"name": "bash", "success": True, "result": "ok"}],
            "messages": [{"role": "tool", "content": long_content, "seq": 1}],
        }
        ctx = _make_ctx(state)
        result = _run(_plugin().execute(ctx))
        assert "tool.formatted_results" in result.state_updates
        assert ctx.state["messages"][0]["content"] == long_content, "无窗不得截断"
