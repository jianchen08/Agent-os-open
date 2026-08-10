"""ParamInjectPlugin 工具 arguments 解析失败的失败原因分类测试。

钉死：``json.loads`` 失败时，不再一律归因为「max_tokens 截断」。

背景（生产误报）：某次 task_submit 的 arguments 长度仅 283 字符，根本不可能
触达任何模型的 max_tokens 上限，但日志却打印「疑似输出被 max_tokens 截断」，
误导排查方向。``json.loads`` 失败的原因有多种：
- markdown 代码块包裹（```json ... ```）
- 前导自然语言 / 多余文字
- 字符串内未转义字符
- 真正的结构性截断（末尾残缺，缺闭合括号）

本测试覆盖 _classify_args_parse_failure 对不同原始串的分类结果。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.input.param_inject.plugin import (  # noqa: E402
    ParamInjectPlugin,
    classify_args_parse_failure,
)


class TestClassifyArgsParseFailure:
    """_classify_args_parse_failure 分类正确性。"""

    def test_markdown_code_block(self) -> None:
        """markdown 代码块包裹 → MARKDOWN_WRAPPED。"""
        s = '```json\n{"goal": "x"}\n```'
        assert classify_args_parse_failure(s) == "markdown_wrapped"

    def test_leading_natural_language(self) -> None:
        """前导自然语言（不以 { 开头，也不是 markdown）→ LEADING_NOISE。"""
        s = '这是参数：{"goal": "x"}'
        assert classify_args_parse_failure(s) == "leading_noise"

    def test_structural_truncation_missing_close(self) -> None:
        """末尾明显残缺（有 { 但无匹配 }）→ TRUNCATED。"""
        s = '{"goal": {"title": "很长的标题", "description": "描述...'
        assert classify_args_parse_failure(s) == "truncated"

    def test_empty_string(self) -> None:
        """空串 → EMPTY。"""
        assert classify_args_parse_failure("") == "empty"

    def test_short_non_truncated_is_not_truncated(self) -> None:
        """短串但有完整 { } → 不是 TRUNCATED（回归：283 字符的误报）。

        生产案例：长度 283，内容是合法结构但 json 解析失败（如未转义字符），
        不应被判为「max_tokens 截断」。
        """
        # 含未转义换行的字符串值 → 解析失败，但结构完整（有 { }），长度小
        s = '{"goal": "line1\nline2", "ok": true}'
        result = classify_args_parse_failure(s)
        # 既不是截断（有匹配括号），也不是 markdown/前导噪声 → 归到通用解析错误
        assert result == "malformed"

    def test_truly_truncated_long(self) -> None:
        """长串且末尾残缺 → TRUNCATED（真正的截断场景仍能识别）。"""
        # 模拟一个长 JSON 被从中间截断：有 { 无 }
        s = '{"goal": {"title": "x", "description": "' + "A" * 5000
        assert classify_args_parse_failure(s) == "truncated"

    def test_whitespace_only(self) -> None:
        """纯空白 → EMPTY。"""
        assert classify_args_parse_failure("   \n\t  ") == "empty"


class TestParamInjectPluginWarnsHonestReason:
    """集成：plugin 解析失败时记录的日志应反映真实分类，不再默认截断。"""

    def test_plugin_does_not_default_to_truncation_for_short_args(
        self, capsys: object
    ) -> None:
        """短串解析失败时，日志不应包含「max_tokens 截断」误导措辞。

        回归：原日志固定打印「疑似输出被 max_tokens 截断」，不管真实原因。
        """
        import asyncio
        import logging

        from pipeline.plugin import PluginContext
        from pipeline.types import StateKeys

        plugin = ParamInjectPlugin(config={})

        # 构造一个 tool_execute 上下文，工具参数是带 markdown 包裹的非法 JSON 串
        bad_args = '```json\n{"goal": "x"\n```'  # markdown + 残缺
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "tc1", "name": "task_submit", "args": bad_args}
            ],
            StateKeys.SESSION_ID: "s1",
        }
        ctx = PluginContext(state=state, config={})

        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        logger = logging.getLogger("plugins.input.param_inject.plugin")
        logger.addHandler(handler)
        try:
            asyncio.run(plugin.execute(ctx))
        finally:
            logger.removeHandler(handler)

        warnings = [r for r in records if r.levelno == logging.WARNING]
        # 精确找「arguments JSON 解析失败」那条（task_id 注入失败是另一条无关告警）
        parse_warnings = [r for r in warnings if "arguments JSON 解析失败" in r.getMessage()]
        assert len(parse_warnings) == 1
        msg = parse_warnings[0].getMessage()
        # 核心断言：不得再出现「疑似输出被 max_tokens 截断」这种无依据的归因
        assert "max_tokens 截断" not in msg
        # 应该出现真实分类标识
        assert "reason=" in msg
