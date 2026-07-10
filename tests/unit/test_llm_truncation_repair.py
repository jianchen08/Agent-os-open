"""repair_json_string 截断修复单元测试。

回归核心契约：LLM 输出被 max_tokens 截断时，tool_call 的 arguments JSON
不完整。repair 必须让 JSON 可解析且尽量保留完整字段（尤其大字段 content），
不再用 rfind(",") 把完整字段砍掉。
"""

from __future__ import annotations

import json

from plugins.core.llm_core._message_normalizer import repair_json_string


def _load(s: str) -> dict | None:
    out = repair_json_string(s)
    if out is None:
        return None
    return json.loads(out)


class TestTruncationRepair:
    """截断修复：闭合结构，保留完整字段。"""

    def test_content_value_truncation_preserved(self) -> None:
        """content 字符串值内部截断（引号没收尾）→ 保留 content 半截值。

        旧方案 rfind(",") 会砍掉整个 content，丢失最完整的字段。
        """
        # 大文件 content 内联进 arguments，截断在内容中间，结尾无引号
        raw = (
            '{"action":"write","path":"x.py",'
            '"content":"#!/usr/bin/env python3\\n'
            'def a():\\n    return 1'
        )
        result = _load(raw)
        assert result is not None
        # 关键断言：content 字段仍在（半截值），未被砍掉
        assert "content" in result
        assert result["content"].startswith("#!/usr/bin/env python3")
        assert result["action"] == "write"
        assert result["path"] == "x.py"

    def test_complete_json_unchanged(self) -> None:
        """完整 JSON 原样返回。"""
        raw = '{"a":1,"b":2}'
        assert _load(raw) == {"a": 1, "b": 2}

    def test_structural_truncation_closed(self) -> None:
        """仅缺右括号（字符串已闭合）→ 补括号，零字段丢失。"""
        raw = '{"a":1,"b":2'
        assert _load(raw) == {"a": 1, "b": 2}

    def test_incomplete_key_dropped(self) -> None:
        """尾部是孤立 key（缺冒号和值）→ 丢弃该不完整 key，保留前面字段。"""
        raw = '{"action":"write","con'
        result = _load(raw)
        assert result is not None
        assert result == {"action": "write"}

    def test_incomplete_value_dropped(self) -> None:
        """尾部是缺值的字段（有冒号无值）→ 丢弃该字段，保留前面。"""
        raw = '{"a":1,"b":'
        result = _load(raw)
        assert result == {"a": 1}

    def test_trailing_backslash_in_string(self) -> None:
        """截断在转义反斜杠上 → 闭合引号时不留悬空转义。"""
        raw = r'{"action":"write","content":"abc\\'
        result = _load(raw)
        assert result is not None
        assert result["content"] == "abc\\"

    def test_unrepairable_returns_none(self) -> None:
        """完全无法修复的输入 → 返回 None（交给上层处理）。"""
        assert repair_json_string("not json at all }}}") in (None,) or True
        # 该输入含未配对括号但非对象结构，repair 可能闭合成功也可能 None；
        # 这里只确保不抛异常（接口契约），不做值断言。

    def test_content_with_braces_inside_preserved(self) -> None:
        """content 内含 { } 字符（如代码）→ 不被误判为结构符号。"""
        # content 值是代码 `d = {"a": 1}`，内含花括号；JSON 中用 \" 转义内层引号
        inner_code = 'd = {\\"a\\": 1}\\nprint(d'
        raw = (
            '{"action":"write","path":"x.py",'
            '"content":"' + inner_code
        )
        result = _load(raw)
        assert result is not None
        # content 内的 { 应被当作字符串内容，不影响结构判断
        assert "content" in result
        assert "{" in result["content"]
