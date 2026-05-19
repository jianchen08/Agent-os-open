"""测试 human_interaction 工具的 file_paths 功能。

验证项：
1. _read_file_contents 方法：单文件、多文件、空列表、不存在文件、
   数量限制(10)、大小限制(2MB)、编码回退(GBK)
2. Service 层 create_choice_request / create_conversation_request
   正确传递 file_contents 参数
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 将 src 目录添加到 Python 路径最前面
# 注意：tests/tools/ 下存在同名 tools 包，必须确保 src/tools 优先加载
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_src_path = str(_PROJECT_ROOT / "src")
if _src_path in sys.path:
    sys.path.remove(_src_path)
sys.path.insert(0, _src_path)

# 清除已被 tests/tools/ 抢占的 tools 模块缓存，强制重新从 src/tools 加载
_tools_related = [k for k in sys.modules if k == "tools" or k.startswith("tools.")]
for _key in _tools_related:
    del sys.modules[_key]

import pytest

from tools.builtin.human_interaction.tool import HumanInteractionTool
from human_interaction.service import HumanInteractionService


# ---------------------------------------------------------------------------
#  TestReadFileContents — 测试 _read_file_contents 静态方法
# ---------------------------------------------------------------------------


class TestReadFileContents:
    """测试 HumanInteractionTool._read_file_contents 方法。"""

    def setup_method(self):
        """每个测试用例前创建工具实例和临时目录。"""
        self.tool = HumanInteractionTool()
        self.tmpdir = tempfile.mkdtemp()

    def test_read_single_file(self):
        """成功读取单个文件内容。"""
        path = os.path.join(self.tmpdir, "test.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello world")

        result = self.tool._read_file_contents([path])
        assert result[path] == "hello world"

    def test_read_multiple_files(self):
        """成功读取多个文件，返回完整的路径-内容映射。"""
        path1 = os.path.join(self.tmpdir, "a.txt")
        path2 = os.path.join(self.tmpdir, "b.py")
        with open(path1, "w", encoding="utf-8") as f:
            f.write("content a")
        with open(path2, "w", encoding="utf-8") as f:
            f.write("content b")

        result = self.tool._read_file_contents([path1, path2])
        assert len(result) == 2
        assert result[path1] == "content a"
        assert result[path2] == "content b"

    def test_read_empty_list(self):
        """空路径列表返回空字典。"""
        result = self.tool._read_file_contents([])
        assert result == {}

    def test_read_nonexistent_file(self):
        """不存在的文件返回包含错误信息的结果，不中断处理。"""
        result = self.tool._read_file_contents(["/nonexistent/file.txt"])
        assert "/nonexistent/file.txt" in result
        assert "读取失败" in result["/nonexistent/file.txt"]

    def test_read_max_10_files(self):
        """超过 10 个文件时只读取前 10 个。"""
        paths = []
        for i in range(15):
            path = os.path.join(self.tmpdir, f"file{i}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"content {i}")
            paths.append(path)

        result = self.tool._read_file_contents(paths)
        assert len(result) == 10

    def test_read_large_file_skipped(self):
        """超过 2MB 的文件标记为"文件过大"并跳过。"""
        path = os.path.join(self.tmpdir, "large.txt")
        # 创建一个略大于 2MB 的文件
        with open(path, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024 + 1))

        result = self.tool._read_file_contents([path])
        assert "文件过大" in result[path]

    def test_read_gbk_file(self):
        """GBK 编码文件自动回退读取，内容正确。"""
        path = os.path.join(self.tmpdir, "gbk.txt")
        with open(path, "w", encoding="gbk") as f:
            f.write("中文内容测试")

        result = self.tool._read_file_contents([path])
        assert result[path] == "中文内容测试"


# ---------------------------------------------------------------------------
#  TestServiceFileContents — 测试 Service 层 file_contents 传递
# ---------------------------------------------------------------------------


class TestServiceFileContents:
    """测试 HumanInteractionService 中 file_contents 参数的传递。"""

    def setup_method(self):
        """每个测试用例前创建 service 实例。"""
        self.service = HumanInteractionService()

    @pytest.mark.asyncio
    async def test_choice_request_with_file_contents(self):
        """choice 请求正确携带 file_contents 到存储记录中。"""
        file_contents = {"/path/to/file.md": "# Title\nContent"}
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_contents=file_contents,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_contents"] == file_contents

    @pytest.mark.asyncio
    async def test_conversation_request_with_file_contents(self):
        """conversation 请求正确携带 file_contents 到存储记录中。"""
        file_contents = {"/path/to/code.py": "print('hello')"}
        request_id = await self.service.create_conversation_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_contents=file_contents,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_contents"] == file_contents

    @pytest.mark.asyncio
    async def test_choice_request_without_file_contents(self):
        """不传 file_contents 时，记录中对应字段为 None。"""
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"].get("file_contents") is None

    @pytest.mark.asyncio
    async def test_conversation_request_without_file_contents(self):
        """conversation 请求不传 file_contents 时，记录中对应字段为 None。"""
        request_id = await self.service.create_conversation_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"].get("file_contents") is None

    @pytest.mark.asyncio
    async def test_choice_request_with_empty_file_contents(self):
        """choice 请求传入空字典时，记录中保留空字典。"""
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_contents={},
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_contents"] == {}

    @pytest.mark.asyncio
    async def test_choice_request_with_multiple_file_contents(self):
        """choice 请求携带多个文件内容时，全部正确传递。"""
        file_contents = {
            "/path/to/a.py": "print('a')",
            "/path/to/b.md": "# B",
            "/path/to/c.yaml": "key: value",
        }
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_contents=file_contents,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_contents"] == file_contents
        assert len(record["message_data"]["file_contents"]) == 3
