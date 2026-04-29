"""
binary_converter 模块测试
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.builtin.binary_converter import (
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_BINARY_FILE_SIZE,
    REJECTED_EXTENSIONS,
    convert_binary_to_markdown,
    get_file_category,
    is_convertible_binary,
)


class TestGetFileCategory:
    """get_file_category 函数测试"""

    @pytest.mark.parametrize(
        "suffix",
        [".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt"],
    )
    def test_document_extensions(self, suffix: str):
        """文档扩展名应返回 document"""
        path = Path(f"test{suffix}")
        assert get_file_category(path) == "document"

    @pytest.mark.parametrize(
        "suffix",
        [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg"],
    )
    def test_image_extensions(self, suffix: str):
        """图片扩展名应返回 image"""
        path = Path(f"test{suffix}")
        assert get_file_category(path) == "image"

    @pytest.mark.parametrize(
        "suffix",
        [".exe", ".dll", ".zip", ".rar", ".mp3", ".mp4", ".pyc", ".bin", ".db"],
    )
    def test_rejected_extensions(self, suffix: str):
        """拒绝的扩展名应返回 rejected"""
        path = Path(f"test{suffix}")
        assert get_file_category(path) == "rejected"

    @pytest.mark.parametrize("suffix", [".py", ".txt", ".md", ".json", ".yaml"])
    def test_text_extensions(self, suffix: str):
        """文本扩展名应返回 text"""
        path = Path(f"test{suffix}")
        assert get_file_category(path) == "text"

    def test_case_insensitive_pdf(self):
        """大写扩展名应不区分大小写"""
        assert get_file_category(Path("test.PDF")) == "document"

    def test_case_insensitive_jpg(self):
        """大写 JPG 应不区分大小写"""
        assert get_file_category(Path("photo.JPG")) == "image"

    def test_case_insensitive_mixed(self):
        """混合大小写扩展名应正确识别"""
        assert get_file_category(Path("report.Pdf")) == "document"

    def test_no_extension(self):
        """无扩展名应返回 text"""
        assert get_file_category(Path("Makefile")) == "text"


class TestIsConvertibleBinary:
    """is_convertible_binary 函数测试"""

    def test_document_is_convertible(self):
        """文档文件应可转换"""
        assert is_convertible_binary(Path("report.pdf")) is True

    def test_image_is_convertible(self):
        """图片文件应可转换"""
        assert is_convertible_binary(Path("photo.png")) is True

    def test_rejected_not_convertible(self):
        """拒绝的扩展名应不可转换"""
        assert is_convertible_binary(Path("setup.exe")) is False

    def test_text_not_convertible(self):
        """文本文件应不可转换"""
        assert is_convertible_binary(Path("script.py")) is False

    def test_archive_not_convertible(self):
        """压缩文件应不可转换"""
        assert is_convertible_binary(Path("archive.zip")) is False


class TestConvertBinaryToMarkdown:
    """convert_binary_to_markdown 函数测试"""

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_nonexistent_file(self, temp_dir: Path):
        """不存在的文件应返回 FILE_NOT_FOUND"""
        path = temp_dir / "nonexistent.pdf"
        result = convert_binary_to_markdown(path)

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    def test_rejected_extension(self, temp_dir: Path):
        """拒绝的扩展名应返回 BINARY_FILE_NOT_SUPPORTED"""
        path = temp_dir / "setup.exe"
        path.write_bytes(b"MZ\x00\x00")

        result = convert_binary_to_markdown(path)

        assert result.success is False
        assert result.error_code == "BINARY_FILE_NOT_SUPPORTED"

    def test_text_file_not_supported(self, temp_dir: Path):
        """文本文件应返回 BINARY_FILE_NOT_SUPPORTED"""
        path = temp_dir / "script.py"
        path.write_text("print('hello')")

        result = convert_binary_to_markdown(path)

        assert result.success is False
        assert result.error_code == "BINARY_FILE_NOT_SUPPORTED"

    def test_file_too_large(self, temp_dir: Path):
        """过大的文件应返回 FILE_TOO_LARGE"""
        path = temp_dir / "large.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        # Mock stat to return a file size exceeding the limit
        mock_stat = MagicMock()
        mock_stat.st_size = MAX_BINARY_FILE_SIZE + 1

        with patch.object(Path, "stat", return_value=mock_stat):
            result = convert_binary_to_markdown(path)

        assert result.success is False
        assert result.error_code == "FILE_TOO_LARGE"

    def test_markitdown_not_installed(self, temp_dir: Path):
        """markitdown 未安装时应返回 MARKITDOWN_NOT_INSTALLED"""
        path = temp_dir / "test.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        with patch.dict("sys.modules", {"markitdown": None}):
            # Also need to ensure the import inside the function raises ImportError
            with patch(
                "tools.builtin.binary_converter.MarkItDown",
                side_effect=ImportError("No module named 'markitdown'"),
                create=True,
            ):
                # Patch the import statement to raise ImportError
                import builtins

                real_import = builtins.__import__

                def mock_import(name, *args, **kwargs):
                    if name == "markitdown":
                        raise ImportError("No module named 'markitdown'")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=mock_import):
                    result = convert_binary_to_markdown(path)

        assert result.success is False
        assert result.error_code == "MARKITDOWN_NOT_INSTALLED"

    def test_successful_conversion(self, temp_dir: Path):
        """成功转换应返回包含 content/format/size 的成功结果"""
        path = temp_dir / "test.pdf"
        path.write_bytes(b"%PDF-1.4 fake content")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "# Test Document\n\nHello world"
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is True
        assert result.data["content"] == "# Test Document\n\nHello world"
        assert result.data["format"] == "document"
        assert "size" in result.data

    def test_successful_image_conversion(self, temp_dir: Path):
        """图片成功转换应返回 format=image"""
        path = temp_dir / "photo.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n fake image")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "An image showing a sunset"
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is True
        assert result.data["format"] == "image"

    def test_empty_conversion_result(self, temp_dir: Path):
        """转换结果为空应返回 CONVERSION_EMPTY"""
        path = temp_dir / "empty.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "   \n\n  "
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is False
        assert result.error_code == "CONVERSION_EMPTY"

    def test_none_conversion_result(self, temp_dir: Path):
        """转换结果为 None 应返回 CONVERSION_EMPTY"""
        path = temp_dir / "none.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = None
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is False
        assert result.error_code == "CONVERSION_EMPTY"

    def test_conversion_exception(self, temp_dir: Path):
        """转换抛出异常应返回 CONVERSION_FAILED"""
        path = temp_dir / "corrupt.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        mock_md_instance = MagicMock()
        mock_md_instance.convert.side_effect = RuntimeError("Corrupted file")

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is False
        assert result.error_code == "CONVERSION_FAILED"

    def test_success_result_has_file_field(self, temp_dir: Path):
        """成功结果应包含 file 字段"""
        path = temp_dir / "test.docx"
        path.write_bytes(b"PK fake docx")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "Document content"
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is True
        assert result.data["file"] == str(path)

    def test_success_result_has_metadata(self, temp_dir: Path):
        """成功结果应包含 action metadata"""
        path = temp_dir / "test.pptx"
        path.write_bytes(b"PK fake pptx")

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "Slide content"
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.dict(
            "sys.modules",
            {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
        ):
            result = original(path)

        assert result.success is True
        assert result.metadata["action"] == "read_binary_document"

    def test_file_exactly_at_size_limit(self, temp_dir: Path):
        """文件大小恰好等于限制时应成功"""
        path = temp_dir / "exact.pdf"
        path.write_bytes(b"%PDF-1.4 fake")

        mock_stat = MagicMock()
        mock_stat.st_size = MAX_BINARY_FILE_SIZE  # Exactly at limit

        mock_md_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "Content at limit"
        mock_md_instance.convert.return_value = mock_result

        import tools.builtin.binary_converter as mod

        original = mod.convert_binary_to_markdown

        with patch.object(Path, "stat", return_value=mock_stat):
            with patch.dict(
                "sys.modules",
                {"markitdown": MagicMock(MarkItDown=lambda: mock_md_instance)},
            ):
                result = original(path)

        assert result.success is True


class TestConstants:
    """模块常量测试"""

    def test_document_extensions_is_frozenset(self):
        """DOCUMENT_EXTENSIONS 应为 frozenset"""
        assert isinstance(DOCUMENT_EXTENSIONS, frozenset)

    def test_image_extensions_is_frozenset(self):
        """IMAGE_EXTENSIONS 应为 frozenset"""
        assert isinstance(IMAGE_EXTENSIONS, frozenset)

    def test_rejected_extensions_is_frozenset(self):
        """REJECTED_EXTENSIONS 应为 frozenset"""
        assert isinstance(REJECTED_EXTENSIONS, frozenset)

    def test_max_binary_file_size_is_10mb(self):
        """MAX_BINARY_FILE_SIZE 应为 10MB"""
        assert MAX_BINARY_FILE_SIZE == 10 * 1024 * 1024

    def test_document_image_no_overlap(self):
        """文档和图片扩展名不应重叠"""
        assert DOCUMENT_EXTENSIONS.isdisjoint(IMAGE_EXTENSIONS)

    def test_document_rejected_no_overlap(self):
        """文档和拒绝扩展名不应重叠"""
        assert DOCUMENT_EXTENSIONS.isdisjoint(REJECTED_EXTENSIONS)

    def test_image_rejected_no_overlap(self):
        """图片和拒绝扩展名不应重叠"""
        assert IMAGE_EXTENSIONS.isdisjoint(REJECTED_EXTENSIONS)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
