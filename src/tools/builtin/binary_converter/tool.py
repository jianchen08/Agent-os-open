"""二进制文件转 Markdown 模块

将 PDF、DOCX、XLSX、PPTX、图片等二进制文件转换为 Markdown 文本，
供 file_read 工具统一消费。依赖 markitdown 库（可选依赖，懒加载）。

暴露接口：
- get_file_category(path) -> str: 返回文件分类
- is_convertible_binary(path) -> bool: 是否可转换的二进制文件
- convert_binary_to_markdown(path) -> ToolResult: 执行转换
"""

from pathlib import Path

from tools.types import (
    ToolResult,
    create_failure_result,
    create_success_result,
)

# 文档类扩展名 — markitdown 可转换
DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".xls",
        ".csv",
        ".pptx",
        ".ppt",
    }
)

# 图片类扩展名 — markitdown 可转换（含 OCR/描述）
IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".tif",
        ".svg",
    }
)

# 拒绝的扩展名 — 音视频/压缩包/可执行文件
REJECTED_EXTENSIONS = frozenset(
    {
        # 音视频
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mkv",
        ".mov",
        ".flv",
        ".wmv",
        ".webm",
        ".m4a",
        ".aac",
        ".ogg",
        ".flac",
        # 压缩包
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".zst",
        ".cab",
        ".iso",
        # 可执行文件
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".obj",
        ".a",
        ".lib",
        # 数据/编译产物
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pyc",
        ".pyd",
        ".pyo",
        ".class",
        ".jar",
        ".war",
        # 字体
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        # 其他二进制
        ".node",
        ".wasm",
    }
)

MAX_BINARY_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def get_file_category(path: Path) -> str:
    """根据扩展名判断文件类别。

    Returns:
        "document" | "image" | "rejected" | "text"
    """
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in REJECTED_EXTENSIONS:
        return "rejected"
    return "text"


def is_convertible_binary(path: Path) -> bool:
    """判断文件是否为可转换的二进制文件（文档或图片）。"""
    category = get_file_category(path)
    return category in ("document", "image")


def convert_binary_to_markdown(path: Path) -> ToolResult:  # noqa: PLR0911
    """将二进制文件转换为 Markdown 文本。

    使用 markitdown 库进行转换。如果 markitdown 未安装，
    返回安装提示。

    Args:
        path: 文件的绝对路径（已 resolve）

    Returns:
        成功：包含 file、content、format、size 的 success result
        失败：包含 error_code 的 failure result
    """
    if not path.exists():
        return create_failure_result(
            error=f"文件不存在: {path}",
            error_code="FILE_NOT_FOUND",
        )

    category = get_file_category(path)
    if category not in ("document", "image"):
        return create_failure_result(
            error=f"不支持转换此类型文件: {path.name}。支持：PDF、DOCX、XLSX、PPTX、PNG、JPG 等图片。",
            error_code="BINARY_FILE_NOT_SUPPORTED",
        )

    file_size = path.stat().st_size
    if file_size > MAX_BINARY_FILE_SIZE:
        return create_failure_result(
            error=f"文件过大 ({_format_size(file_size)})，超过二进制文件限制 ({_format_size(MAX_BINARY_FILE_SIZE)}): {path.name}",
            error_code="FILE_TOO_LARGE",
        )

    # 检查 markitdown 是否可用
    try:
        from markitdown import MarkItDown  # noqa: PLC0415
    except ImportError:
        return create_failure_result(
            error=f"无法转换文件 {path.name}：需要安装 markitdown 库。\n安装命令：pip install markitdown",
            error_code="MARKITDOWN_NOT_INSTALLED",
        )

    # 执行转换
    try:
        md = MarkItDown()
        result = md.convert(str(path))
        content = result.text_content

        if content and content.strip():
            return create_success_result(
                data={
                    "file": str(path),
                    "content": content,
                    "format": category,
                    "size": _format_size(file_size),
                },
                metadata={"action": f"read_binary_{category}"},
            )

        # 转换结果为空：文件存在且可读，只是提取不出文本，返回基本元数据
        return create_success_result(
            data={
                "file": str(path),
                "format": category,
                "size": _format_size(file_size),
            },
            metadata={"action": f"read_binary_{category}_no_text"},
        )
    except Exception as e:
        return create_failure_result(
            error=f"转换文件失败 ({path.name}): {str(e)}",
            error_code="CONVERSION_FAILED",
        )


def _format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"
