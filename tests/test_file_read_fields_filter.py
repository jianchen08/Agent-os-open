"""
FileReadTool fields 筛选功能增强测试

覆盖范围：
- _parse_field_path：路径解析（普通键、筛选键、混合路径）
- _resolve_segment：单段执行（字典访问、列表筛选）
- _get_nested_field：完整字段获取（向后兼容、筛选语法、数字转换、多匹配）
- _try_match_value：值匹配（字符串、整数、浮点数、布尔值）
- _extract_fields：端到端字段提取（YAML/JSON 文件）
"""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from tools.builtin.file_read.tool import FileReadTool, _try_match_value


# =====================================================================
# _try_match_value 测试
# =====================================================================


class TestTryMatchValue:
    """值匹配辅助函数测试"""

    def test_string_match(self):
        """字符串值直接匹配"""
        assert _try_match_value("abc", "abc") is True
        assert _try_match_value("abc", "def") is False

    def test_int_match(self):
        """整数值自动转换匹配"""
        assert _try_match_value(13, "13") is True
        assert _try_match_value(13, "12") is False
        assert _try_match_value(0, "0") is True

    def test_float_match(self):
        """浮点数值自动转换匹配"""
        assert _try_match_value(3.14, "3.14") is True
        assert _try_match_value(3.14, "3.15") is False

    def test_bool_match(self):
        """布尔值匹配（bool 必须在 int 之前判断）"""
        assert _try_match_value(True, "true") is True
        assert _try_match_value(True, "1") is True
        assert _try_match_value(False, "false") is True
        assert _try_match_value(False, "0") is True
        assert _try_match_value(True, "false") is False

    def test_string_fallback(self):
        """非数字字符串走字符串比较"""
        assert _try_match_value("hello", "hello") is True

    def test_str_conversion_match(self):
        """str() 转换后匹配"""
        assert _try_match_value(123, "123") is True  # str(123) == "123"


# =====================================================================
# _parse_field_path 测试
# =====================================================================


class TestParseFieldPath:
    """字段路径解析测试"""

    def test_simple_key(self):
        """普通单键路径"""
        result = FileReadTool._parse_field_path("name")
        assert result == [("key", "name")]

    def test_dotted_keys(self):
        """点号分隔的多级键"""
        result = FileReadTool._parse_field_path("summary.total_tokens")
        assert result == [("key", "summary"), ("key", "total_tokens")]

    def test_filter_only(self):
        """仅筛选语法"""
        result = FileReadTool._parse_field_path("records{record_id=abc}")
        assert result == [("filter", "records", "record_id", "abc")]

    def test_filter_with_subfield(self):
        """筛选后取子字段"""
        result = FileReadTool._parse_field_path("records{iteration=13}.thinking_content")
        assert result == [
            ("filter", "records", "iteration", "13"),
            ("key", "thinking_content"),
        ]

    def test_key_then_filter(self):
        """键访问后接筛选"""
        result = FileReadTool._parse_field_path("data.items{type=error}")
        assert result == [
            ("key", "data"),
            ("filter", "items", "type", "error"),
        ]

    def test_complex_path(self):
        """复杂路径：多级键 + 筛选 + 子字段"""
        result = FileReadTool._parse_field_path("a.b{c=d}.e")
        assert result == [
            ("key", "a"),
            ("filter", "b", "c", "d"),
            ("key", "e"),
        ]


# =====================================================================
# _resolve_segment 测试
# =====================================================================


class TestResolveSegment:
    """单段路径执行测试"""

    def test_key_from_dict(self):
        """从字典中按键取值"""
        data = {"name": "test", "value": 42}
        assert FileReadTool._resolve_segment(data, ("key", "name")) == "test"
        assert FileReadTool._resolve_segment(data, ("key", "value")) == 42

    def test_key_missing_returns_none(self):
        """键不存在返回 None"""
        data = {"name": "test"}
        assert FileReadTool._resolve_segment(data, ("key", "missing")) is None

    def test_filter_single_match(self):
        """列表筛选单条匹配返回对象"""
        data = {
            "records": [
                {"record_id": "abc", "content": "hello"},
                {"record_id": "def", "content": "world"},
            ]
        }
        result = FileReadTool._resolve_segment(
            data, ("filter", "records", "record_id", "abc")
        )
        assert result == {"record_id": "abc", "content": "hello"}

    def test_filter_multiple_match(self):
        """列表筛选多条匹配返回列表"""
        data = {
            "records": [
                {"type": "error", "msg": "e1"},
                {"type": "ok", "msg": "ok1"},
                {"type": "error", "msg": "e2"},
            ]
        }
        result = FileReadTool._resolve_segment(
            data, ("filter", "records", "type", "error")
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["msg"] == "e1"
        assert result[1]["msg"] == "e2"

    def test_filter_no_match(self):
        """列表筛选无匹配返回 None"""
        data = {"records": [{"type": "ok"}]}
        result = FileReadTool._resolve_segment(
            data, ("filter", "records", "type", "error")
        )
        assert result is None

    def test_filter_int_match(self):
        """列表筛选数字值自动转换匹配"""
        data = {"records": [{"iteration": 13, "text": "hello"}]}
        result = FileReadTool._resolve_segment(
            data, ("filter", "records", "iteration", "13")
        )
        assert result == {"iteration": 13, "text": "hello"}

    def test_filter_non_list_returns_none(self):
        """筛选目标不是列表时返回 None"""
        data = {"records": "not a list"}
        result = FileReadTool._resolve_segment(
            data, ("filter", "records", "key", "val")
        )
        assert result is None


# =====================================================================
# _get_nested_field 集成测试
# =====================================================================


class TestGetNestedField:
    """完整字段获取集成测试"""

    def setup_method(self):
        """初始化测试工具和数据"""
        self.tool = FileReadTool()
        self.data = {
            "summary": {"total_tokens": 100, "model": "gpt-4"},
            "records": [
                {"record_id": "abc", "iteration": 1, "content": "first"},
                {"record_id": "def", "iteration": 13, "content": "second",
                 "thinking_content": "deep thought"},
                {"record_id": "ghi", "iteration": 13, "content": "third",
                 "thinking_content": "another thought"},
            ],
            "items": [
                {"type": "error", "msg": "e1"},
                {"type": "ok", "msg": "ok1"},
                {"type": "error", "msg": "e2"},
            ],
        }

    def test_backward_compat_simple_key(self):
        """向后兼容：简单键访问"""
        assert self.tool._get_nested_field(self.data, "summary") == {
            "total_tokens": 100, "model": "gpt-4"
        }

    def test_backward_compat_dotted_key(self):
        """向后兼容：点号分隔嵌套访问"""
        assert self.tool._get_nested_field(self.data, "summary.total_tokens") == 100
        assert self.tool._get_nested_field(self.data, "summary.model") == "gpt-4"

    def test_backward_compat_missing_key(self):
        """向后兼容：键不存在返回 None"""
        assert self.tool._get_nested_field(self.data, "nonexistent") is None
        assert self.tool._get_nested_field(self.data, "summary.nonexistent") is None

    def test_filter_single_match(self):
        """筛选语法：单条匹配返回对象"""
        result = self.tool._get_nested_field(
            self.data, "records{record_id=abc}"
        )
        assert result == {"record_id": "abc", "iteration": 1, "content": "first"}

    def test_filter_with_subfield(self):
        """筛选语法：筛选后取子字段"""
        result = self.tool._get_nested_field(
            self.data, "records{record_id=def}.thinking_content"
        )
        assert result == "deep thought"

    def test_filter_multiple_match(self):
        """筛选语法：多条匹配返回列表"""
        result = self.tool._get_nested_field(
            self.data, "records{iteration=13}"
        )
        assert isinstance(result, list)
        assert len(result) == 2

    def test_filter_multiple_match_subfield(self):
        """筛选语法：多条匹配后取子字段返回列表"""
        result = self.tool._get_nested_field(
            self.data, "items{type=error}.msg"
        )
        assert result == ["e1", "e2"]

    def test_filter_int_value_match(self):
        """筛选语法：数字值自动转换"""
        result = self.tool._get_nested_field(
            self.data, "records{iteration=13}"
        )
        assert isinstance(result, list)
        assert len(result) == 2

    def test_filter_no_match(self):
        """筛选语法：无匹配返回 None"""
        result = self.tool._get_nested_field(
            self.data, "records{record_id=nonexistent}"
        )
        assert result is None


# =====================================================================
# _extract_fields 端到端测试（通过文件）
# =====================================================================


class TestExtractFieldsEndToEnd:
    """端到端字段提取测试，通过实际文件验证"""

    def setup_method(self):
        """初始化测试工具和临时文件"""
        self.tool = FileReadTool()
        self.tmp_dir = tempfile.mkdtemp()

    def _write_yaml(self, filename: str, data: dict) -> Path:
        """写入 YAML 测试文件"""
        path = Path(self.tmp_dir) / filename
        path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
        return path

    def _write_json(self, filename: str, data: dict) -> Path:
        """写入 JSON 测试文件"""
        path = Path(self.tmp_dir) / filename
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    @pytest.mark.asyncio
    async def test_yaml_filter_single_match(self):
        """YAML 文件：筛选单条匹配"""
        data = {
            "records": [
                {"record_id": "r1", "content": "hello"},
                {"record_id": "r2", "content": "world"},
            ]
        }
        path = self._write_yaml("test.yaml", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(content, path, ["records{record_id=r1}"])
        assert result.success
        assert result.output["records{record_id=r1}"]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_json_filter_with_subfield(self):
        """JSON 文件：筛选后取子字段"""
        data = {
            "records": [
                {"iteration": 5, "thinking_content": "thought-5"},
                {"iteration": 10, "thinking_content": "thought-10"},
            ]
        }
        path = self._write_json("test.json", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(
            content, path, ["records{iteration=10}.thinking_content"]
        )
        assert result.success
        assert result.output["records{iteration=10}.thinking_content"] == "thought-10"

    @pytest.mark.asyncio
    async def test_yaml_filter_multiple_match(self):
        """YAML 文件：筛选多条匹配返回列表"""
        data = {
            "items": [
                {"type": "error", "msg": "e1"},
                {"type": "ok", "msg": "ok1"},
                {"type": "error", "msg": "e2"},
            ]
        }
        path = self._write_yaml("test.yaml", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(content, path, ["items{type=error}"])
        assert result.success
        matched = result.output["items{type=error}"]
        assert isinstance(matched, list)
        assert len(matched) == 2
        assert matched[0]["msg"] == "e1"
        assert matched[1]["msg"] == "e2"

    @pytest.mark.asyncio
    async def test_yaml_backward_compat(self):
        """YAML 文件：向后兼容普通字段访问"""
        data = {"summary": {"total_tokens": 100, "model": "gpt-4"}}
        path = self._write_yaml("test.yaml", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(
            content, path, ["summary.total_tokens", "summary.model"]
        )
        assert result.success
        assert result.output["summary.total_tokens"] == 100
        assert result.output["summary.model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_yaml_mixed_fields(self):
        """YAML 文件：混合使用普通字段和筛选字段"""
        data = {
            "name": "test-session",
            "records": [
                {"record_id": "r1", "content": "hello"},
            ],
        }
        path = self._write_yaml("test.yaml", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(
            content, path, ["name", "records{record_id=r1}.content"]
        )
        assert result.success
        assert result.output["name"] == "test-session"
        assert result.output["records{record_id=r1}.content"] == "hello"

    @pytest.mark.asyncio
    async def test_json_filter_no_match_skipped(self):
        """JSON 文件：筛选无匹配时不包含该字段"""
        data = {"records": [{"id": "r1"}]}
        path = self._write_json("test.json", data)
        content = path.read_text(encoding="utf-8")
        result = self.tool._extract_fields(
            content, path, ["records{id=nonexistent}"]
        )
        assert result.success
        assert "records{id=nonexistent}" not in result.output
