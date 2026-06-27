"""param_inject 截断修复单元测试。

回归核心断点：LLM 输出被 max_tokens 截断时，tool_call 的 arguments JSON
不完整，param_inject 在 json.loads 失败时旧实现直接 raw_args={} 把半截
content 全部丢弃。新实现调 repair_json_string 保住可用字段（含半截 content），
并打 _args_truncated 结构性标记，供下游 validator 识别并提示「文件太大」。
"""

from __future__ import annotations

from typing import Any

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


def _make_plugin() -> Any:
    from plugins.input.param_inject.plugin import ParamInjectPlugin
    return ParamInjectPlugin()


def _make_ctx(tool_calls: list[dict[str, Any]]) -> PluginContext:
    return PluginContext(
        state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: tool_calls,
            StateKeys.SESSION_ID: "s1",
            StateKeys.PIPELINE_ID: "p1",
        },
        _services={},
    )


class TestParamInjectTruncationRecovery:
    """截断的 arguments 字符串 → repair 保住字段 + 打标记。"""

    async def test_truncated_args_repaired_and_flagged(self) -> None:
        """截断 JSON（content 字符串未闭合）→ repair 保住 content 半截值，打 _args_truncated。"""
        plugin = _make_plugin()
        # 截断在 content 字符串内部：引号没收尾
        truncated_json = (
            r'{"action":"write","path":"/a.py",'
            r'"content":"#!/usr/bin/env python3\ndef a():\n    return 1'
        )
        tc = {"name": "file_write", "args": truncated_json, "id": "call_1"}
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        calls = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])

        assert len(calls) == 1
        args = calls[0]["args"]
        # 关键：半截 content 被保住，不是空字典
        assert args.get("action") == "write"
        assert args.get("path") == "/a.py"
        assert "content" in args
        assert args["content"].startswith("#!/usr/bin/env python3")
        # 关键：打了截断标记
        assert calls[0].get("_args_truncated") is True

    async def test_unrepairable_args_becomes_empty_no_flag(self) -> None:
        """完全无法 repair 的字符串 → 回退 {}，且不标记截断（无可保内容）。

        设计：_args_truncated 标记语义是「截断了但保住了部分字段」。
        无法 repair 时无可保内容，标记无意义 → 不打。
        """
        plugin = _make_plugin()
        # 既不是合法 JSON 也无法 repair
        tc = {"name": "file_write", "args": "}}}not json at all", "id": "call_2"}
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        calls = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(calls) == 1
        # 无法 repair → 业务字段（action/path/content）丢失，仅剩注入的运行时参数
        args = calls[0]["args"]
        assert "action" not in args
        assert "path" not in args
        assert "content" not in args
        # 无截断标记（无可保内容）
        assert calls[0].get("_args_truncated") is None

    async def test_valid_args_not_flagged(self) -> None:
        """合法 JSON（未截断）→ 正常解析，不打截断标记。"""
        plugin = _make_plugin()
        tc = {
            "name": "file_write",
            "args": '{"action":"write","path":"/a","content":"hi"}',
            "id": "call_3",
        }
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        calls = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(calls) == 1
        assert calls[0]["args"]["action"] == "write"
        # 未截断 → 无标记
        assert calls[0].get("_args_truncated") is None
