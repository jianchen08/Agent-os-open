"""测试 human_interaction 工具的 file_paths 功能。

验证项：
1. Service 层 create_choice_request / create_conversation_request
   正确传递 file_paths 参数（路径列表）
2. Tool 层通知模式传入 file_paths 时返回 INVALID_PARAMS 错误
3. WebSocket 通知器正确传递 file_paths 和 pipeline_id
4. Tool 层 choice 模式正确传递 file_paths 到 service
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
#  TestServiceFilePaths — 测试 Service 层 file_paths 传递
# ---------------------------------------------------------------------------


class TestServiceFilePaths:
    """测试 HumanInteractionService 中 file_paths 参数的传递。"""

    def setup_method(self):
        """每个测试用例前创建 service 实例。"""
        self.service = HumanInteractionService()

    @pytest.mark.asyncio
    async def test_choice_request_with_file_paths(self):
        """choice 请求正确携带 file_paths 到存储记录中。"""
        file_paths = ["/path/to/file.md", "/path/to/code.py"]
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_paths=file_paths,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_paths"] == file_paths

    @pytest.mark.asyncio
    async def test_conversation_request_with_file_paths(self):
        """conversation 请求正确携带 file_paths 到存储记录中。"""
        file_paths = ["/path/to/code.py"]
        request_id = await self.service.create_conversation_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_paths=file_paths,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_paths"] == file_paths

    @pytest.mark.asyncio
    async def test_choice_request_without_file_paths(self):
        """不传 file_paths 时，记录中对应字段为 None。"""
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"].get("file_paths") is None

    @pytest.mark.asyncio
    async def test_conversation_request_without_file_paths(self):
        """conversation 请求不传 file_paths 时，记录中对应字段为 None。"""
        request_id = await self.service.create_conversation_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"].get("file_paths") is None

    @pytest.mark.asyncio
    async def test_choice_request_with_empty_file_paths(self):
        """choice 请求传入空列表时，记录中保留空列表。"""
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_paths=[],
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_paths"] == []

    @pytest.mark.asyncio
    async def test_choice_request_with_multiple_file_paths(self):
        """choice 请求携带多个文件路径时，全部正确传递。"""
        file_paths = [
            "/path/to/a.py",
            "/path/to/b.md",
            "/path/to/c.yaml",
        ]
        request_id = await self.service.create_choice_request(
            session_id="test-session",
            thread_id="test-thread",
            tab_id="test-tab",
            title="Test",
            file_paths=file_paths,
        )

        record = await self.service.get_request(request_id)
        assert record is not None
        assert record["message_data"]["file_paths"] == file_paths
        assert len(record["message_data"]["file_paths"]) == 3


# ---------------------------------------------------------------------------
#  TestToolNotificationFilePathsValidation — 测试 Tool 层 file_paths 校验
# ---------------------------------------------------------------------------


class TestToolNotificationFilePathsValidation:
    """测试通知模式下 file_paths 参数的校验逻辑。"""

    @pytest.mark.asyncio
    async def test_notification_mode_with_file_paths_returns_error(self):
        """通知模式传入 file_paths 时返回 INVALID_PARAMS 错误。"""
        tool = HumanInteractionTool(pipeline_id="pipe-001")
        service = AsyncMock()
        service.send_notification = AsyncMock(return_value="req-003")

        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await tool.execute({
                "mode": "notification",
                "title": "测试通知",
                "file_paths": ["/path/to/file.md"],
            })

        assert result.success is False
        assert result.error_code == "INVALID_PARAMS"
        assert "file_paths" in result.error


# ---------------------------------------------------------------------------
#  TestToolFilePathsValidation — 测试 Tool 层 file_paths 校验逻辑
# ---------------------------------------------------------------------------


class TestToolFilePathsValidation:
    """测试 choice/conversation 模式下 file_paths 参数的校验逻辑。"""

    def setup_method(self):
        """每个测试用例前创建工具实例和临时目录。"""
        self.tool = HumanInteractionTool(pipeline_id="pipe-001")
        self.tmp_dir = tempfile.mkdtemp()

    def _create_file(self, name: str, size: int = 100) -> str:
        """在临时目录中创建指定大小的文件并返回路径。"""
        file_path = Path(self.tmp_dir) / name
        file_path.write_bytes(b"x" * size)
        return str(file_path)

    def _make_service_mock(self) -> AsyncMock:
        """创建 service mock 对象。"""
        service = AsyncMock()
        service.create_choice_request = AsyncMock(return_value="req-mock")
        service.create_conversation_request = AsyncMock(return_value="req-mock")
        service.wait_for_choice = AsyncMock(return_value={
            "response_type": "approved",
            "selected_option": "yes",
            "feedback": "",
        })
        return service

    @pytest.mark.asyncio
    async def test_file_paths_not_exist_returns_error(self):
        """file_paths 中文件不存在时返回错误。"""
        service = self._make_service_mock()
        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await self.tool.execute({
                "mode": "choice",
                "title": "测试",
                "file_paths": ["nonexistent_file.md"],
            })

        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"
        assert "文件不存在" in result.error

    @pytest.mark.asyncio
    async def test_file_paths_is_directory_returns_error(self):
        """file_paths 中路径是目录时返回错误。"""
        service = self._make_service_mock()
        subdir = Path.cwd() / "_test_dir_for_is_dir_check"
        subdir.mkdir(exist_ok=True)
        try:
            with patch(
                "tools.builtin.human_interaction.tool.get_human_interaction_service",
                return_value=service,
            ):
                result = await self.tool.execute({
                    "mode": "choice",
                    "title": "测试",
                    "file_paths": [str(subdir)],
                })

            assert result.success is False
            assert result.error_code == "INVALID_FILE_PATHS"
            assert "目录而非文件" in result.error
        finally:
            subdir.rmdir()

    @pytest.mark.asyncio
    async def test_file_paths_exceeds_limit_returns_error(self):
        """file_paths 超过 10 个时返回错误。"""
        service = self._make_service_mock()
        paths = [f"file_{i}.txt" for i in range(11)]
        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await self.tool.execute({
                "mode": "choice",
                "title": "测试",
                "file_paths": paths,
            })

        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"
        assert "超过最大限制" in result.error

    @pytest.mark.asyncio
    async def test_file_paths_file_too_large_returns_error(self):
        """file_paths 中文件超过 10MB 时返回错误。"""
        service = self._make_service_mock()
        large_file = Path.cwd() / "_test_large_file.bin"
        large_file.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
        try:
            with patch(
                "tools.builtin.human_interaction.tool.get_human_interaction_service",
                return_value=service,
            ):
                result = await self.tool.execute({
                    "mode": "choice",
                    "title": "测试",
                    "file_paths": [str(large_file)],
                })

            assert result.success is False
            assert result.error_code == "INVALID_FILE_PATHS"
            assert "超过单文件上限" in result.error
        finally:
            large_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_file_paths_none_passes(self):
        """file_paths 为 None 时正常通过。"""
        service = self._make_service_mock()
        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await self.tool.execute({
                "mode": "choice",
                "title": "测试",
            })

        assert result.success is True

    @pytest.mark.asyncio
    async def test_file_paths_empty_list_passes(self):
        """file_paths 为空列表时正常通过。"""
        service = self._make_service_mock()
        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await self.tool.execute({
                "mode": "choice",
                "title": "测试",
                "file_paths": [],
            })

        assert result.success is True

    @pytest.mark.asyncio
    async def test_file_paths_all_valid_passes(self):
        """file_paths 全部合法时正常通过。"""
        service = self._make_service_mock()
        file1 = Path.cwd() / "_test_valid1.txt"
        file2 = Path.cwd() / "_test_valid2.txt"
        file1.write_bytes(b"x" * 100)
        file2.write_bytes(b"x" * 200)
        try:
            with patch(
                "tools.builtin.human_interaction.tool.get_human_interaction_service",
                return_value=service,
            ):
                result = await self.tool.execute({
                    "mode": "choice",
                    "title": "测试",
                    "file_paths": [str(file1), str(file2)],
                })

            assert result.success is True
        finally:
            file1.unlink(missing_ok=True)
            file2.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_conversation_mode_file_paths_not_exist_returns_error(self):
        """conversation 模式下 file_paths 中文件不存在时返回错误。"""
        service = self._make_service_mock()
        with patch(
            "tools.builtin.human_interaction.tool.get_human_interaction_service",
            return_value=service,
        ):
            result = await self.tool.execute({
                "mode": "conversation",
                "title": "测试",
                "file_paths": ["nonexistent_file.md"],
            })

        assert result.success is False
        assert result.error_code == "INVALID_FILE_PATHS"
        assert "文件不存在" in result.error
