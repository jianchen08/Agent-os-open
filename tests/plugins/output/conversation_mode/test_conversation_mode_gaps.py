# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""conversation_mode 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py 缺行）：

1. **tool_results 非列表形态**（80）：脏 state（标量/None/dict）下不崩溃、
   零产出——对话激活信号只可能来自结果列表。
2. **游标收缩防御**（86）：``conversation_mode.seen_tool_results_len`` 大于实际
   列表长度（run 重置/截断）时游标对齐现状（按 0 处理）——否则新结果全被跳过、
   激活信号永久漏检。对照：游标在范围内时只扫新增段。
3. **非 dict 结果条目跳过**（92）：条目脏数据不阻断扫描，后续合法条目仍可激活。
4. **data 非 dict 跳过**（98）：``data`` 为字符串/列表等非映射时跳过该条，
   不因 ``.get`` 调用崩溃。

上述四行都是防御性早退分支，均以真实 state 输入经 execute 直达（非死代码）。
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
_PLUGIN_DIR = _DIR.parents[3] / "plugins" / "shared" / "pipeline" / "output" / "conversation_mode"
_SHARED = _DIR.parents[3] / "plugins" / "shared"

for _d in (str(_PLUGIN_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "conversation_mode_gaps_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_plugin_module()
ConversationModeDetector = _mod.ConversationModeDetector

from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402

_SEEN_KEY = "conversation_mode.seen_tool_results_len"


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _run(state: dict[str, Any]) -> Any:
    return asyncio.run(ConversationModeDetector().execute(_ctx(state)))


def _conv_result(where: str = "data") -> dict[str, Any]:
    """success=True 且带 conversation_mode=True 的合法 tool_result。"""
    payload = {"conversation_mode": True}
    if where == "data":
        return {"success": True, "data": payload}
    return {"success": True, "data": {"output": payload}}


class TestToolResultsShape:
    """tool_results 非列表：脏 state 零产出不崩溃。"""

    @pytest.mark.parametrize(
        "tool_results",
        ["not-a-list", 42, None, {"0": {"success": True}}],
    )
    def test_non_list_tool_results_returns_empty(self, tool_results: Any) -> None:
        result = _run({StateKeys.TOOL_RESULTS: tool_results})
        assert result.state_updates == {}
        assert result.skip_remaining is False

    def test_list_shape_is_scanned(self) -> None:
        """对照：列表形态正常激活（非列表早退确有区分度）。"""
        result = _run({StateKeys.TOOL_RESULTS: [_conv_result()]})
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is True


class TestCursorShrinkDefense:
    """游标 > 列表长度（run 重置/截断）时对齐现状，不永久漏检。"""

    def test_shrunk_list_still_detects_new_signal(self) -> None:
        """游标 9 但列表只剩 1 条合法信号 → 按 0 重扫并激活。"""
        state = {StateKeys.TOOL_RESULTS: [_conv_result()], _SEEN_KEY: 9}
        result = _run(state)
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is True
        assert result.state_updates[_SEEN_KEY] == 1

    def test_cursor_at_boundary_is_not_shrunk(self) -> None:
        """对照：游标恰好等于长度（非大于）→ 只扫新增段（此处为空）→ 零产出。"""
        state = {StateKeys.TOOL_RESULTS: [_conv_result()], _SEEN_KEY: 1}
        result = _run(state)
        assert result.state_updates == {}

    def test_cursor_beyond_list_of_junk_still_scans(self) -> None:
        """列表收缩到全脏条目：重扫后无激活，但游标推进到现状。"""
        state = {StateKeys.TOOL_RESULTS: ["junk"], _SEEN_KEY: 5}
        result = _run(state)
        assert result.state_updates[_SEEN_KEY] == 1
        assert StateKeys.CONVERSATION_MODE not in result.state_updates


class TestEntryShapeSkipping:
    """条目级脏数据跳过：不阻断扫描，后续合法条目仍可激活。"""

    @pytest.mark.parametrize("junk", ["junk", 42, None, ["nested"]])
    def test_non_dict_entries_skipped(self, junk: Any) -> None:
        """纯脏条目列表 → 无激活、无异常。"""
        result = _run({StateKeys.TOOL_RESULTS: [junk]})
        assert StateKeys.CONVERSATION_MODE not in result.state_updates

    def test_valid_signal_after_junk_entries_activates(self) -> None:
        """脏条目在前、合法信号在后 → 仍激活（跳过 ≠ 中断扫描）。"""
        state = {StateKeys.TOOL_RESULTS: ["junk", 42, _conv_result()]}
        result = _run(state)
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is True
        assert result.state_updates[_SEEN_KEY] == 3
        assert result.skip_remaining is True

    @pytest.mark.parametrize("data", ["plain-string", ["list"], 42, None])
    def test_non_dict_data_skipped(self, data: Any) -> None:
        """data 非映射（字符串/列表/标量）→ 跳过该条，不崩溃。"""
        result = _run({StateKeys.TOOL_RESULTS: [{"success": True, "data": data}]})
        assert StateKeys.CONVERSATION_MODE not in result.state_updates

    def test_valid_data_after_non_dict_data_activates(self) -> None:
        """对照：非映射 data 之后跟合法条目 → 仍激活（两条可区分）。"""
        state = {
            StateKeys.TOOL_RESULTS: [
                {"success": True, "data": "plain-string"},
                _conv_result("data.output"),
            ]
        }
        result = _run(state)
        assert result.state_updates[StateKeys.CONVERSATION_MODE] is True

    def test_unsuccessful_result_with_signal_not_activated(self) -> None:
        """失败结果即便带标志也不激活（success 门在 data 形态门之前）。"""
        result = _run(
            {StateKeys.TOOL_RESULTS: [{"success": False, "data": {"conversation_mode": True}}]}
        )
        assert StateKeys.CONVERSATION_MODE not in result.state_updates
