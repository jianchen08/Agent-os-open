"""
文件跳转协议

暴露接口：
- parse_uri(uri: str) -> tuple[str, Position | None]：parse_uri功能
- generate_uri(file_path: str, position: Position | None, ide_type: IDEType | None) -> str：generate_uri功能
- FileJumpProtocol：FileJumpProtocol类
"""

import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote

from src.lsp.detector import IDEDetector, IDEType
from src.lsp.types import IDEInfo, Position

logger = logging.getLogger(__name__)


class FileJumpProtocol:
    """
    文件跳转协议

    支持的 IDE：
    - VSCode: vscode://file/path/to/file:line:col
    - JetBrains: idea://file/path/to/file:line:col
    - Nvim: nvim://file/path/to/file:line:col
    """

    # IDE 命令行参数格式
    COMMAND_FORMATS = {
        IDEType.VSCODE: {
            "windows": "code",
            "linux": "code",
            "darwin": "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            "args": ["--goto", "{file}:{line}:{col}"],
        },
        IDEType.JETBRAINS: {
            "windows": "idea64.exe",
            "linux": "idea.sh",
            "darwin": "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea",
            "args": ["--line", "{line}", "--column", "{col}", "{file}"],
        },
        IDEType.NVIM: {
            "windows": "nvim",
            "linux": "nvim",
            "darwin": "nvim",
            "args": ["+call cursor({line}, {col})", "{file}"],
        },
    }

    @staticmethod
    async def jump_to_file(
        file_path: str,
        position: Position | None = None,
        ide_info: IDEInfo | None = None,
    ) -> bool:
        """跳转到文件指定位置"""
        if not os.path.exists(file_path):  # noqa: PTH110
            logger.error(f"文件不存在: {file_path}")
            return False

        # 检测 IDE
        if ide_info is None:
            ide_info = IDEDetector.detect()

        if ide_info is None:
            logger.warning("未检测到 IDE，尝试使用默认方式打开")
            return FileJumpProtocol._open_with_default(file_path, position)

        # 根据 IDE 类型跳转
        try:
            return await FileJumpProtocol._jump_by_ide_type(ide_info.type, file_path, position)
        except Exception as e:
            logger.error(f"跳转失败: {e}")
            return False

    @staticmethod
    async def _jump_by_ide_type(
        ide_type: IDEType,
        file_path: str,
        position: Position | None = None,
    ) -> bool:
        """根据 IDE 类型跳转"""
        if ide_type not in FileJumpProtocol.COMMAND_FORMATS:
            logger.warning(f"不支持的 IDE 类型: {ide_type}")
            return False

        import platform  # noqa: PLC0415

        system = platform.system().lower()

        # 获取命令格式
        format_info = FileJumpProtocol.COMMAND_FORMATS[ide_type]
        command = format_info.get(system, format_info.get("linux"))

        if not command:
            logger.warning(f"未找到 {ide_type} 在 {system} 上的命令")
            return False

        # 构建参数
        line = position.line if position else 1
        col = position.character if position else 1

        args = []
        for arg in format_info["args"]:
            arg = arg.replace("{file}", file_path)  # noqa: PLW2901
            arg = arg.replace("{line}", str(line + 1))  # IDE 通常从 1 开始  # noqa: PLW2901
            arg = arg.replace("{col}", str(col + 1))  # noqa: PLW2901
            args.append(arg)

        # 执行命令 - 避免使用shell=True以提高安全性
        try:
            # 在Windows上，某些命令可能需要通过cmd执行
            if system == "windows":
                # 使用完整的cmd路径并避免shell注入
                cmd_args = ["cmd", "/c"] + [command] + args
                subprocess.Popen(cmd_args, shell=False)
            else:
                subprocess.Popen([command] + args, shell=False)
            logger.info(f"已打开 {file_path}:{line}:{col} in {command}")
            return True
        except Exception as e:
            logger.error(f"执行命令失败: {e}")
            return False

    @staticmethod
    def _open_with_default(
        file_path: str,
        position: Position | None = None,  # noqa: ARG004
    ) -> bool:
        """使用系统默认方式打开文件"""
        import platform  # noqa: PLC0415

        system = platform.system()

        try:
            if system == "Windows":
                os.startfile(file_path)  # type: ignore
            elif system == "Darwin":
                subprocess.Popen(["open", file_path])
            else:  # Linux
                subprocess.Popen(["xdg-open", file_path])

            logger.info(f"已使用默认方式打开 {file_path}")
            return True
        except Exception as e:
            logger.error(f"打开文件失败: {e}")
            return False

    @staticmethod
    def parse_uri(uri: str) -> tuple[str, Position | None]:
        """解析文件 URI"""
        # 移除协议前缀
        if "://" in uri:
            protocol, path = uri.split("://", 1)

            if protocol == "file":
                # file:///path/to/file
                path = unquote(path)
            elif protocol in ["vscode", "idea", "nvim"]:
                # vscode://file/path/to/file:line:col
                # 去掉 file/ 前缀
                if path.startswith("file/"):
                    path = path[5:]
                path = unquote(path)
            else:
                # 未知协议，当作路径处理
                path = unquote(uri)
        else:
            path = uri

        # 解析位置
        file_path = path
        position = None

        if ":" in path:
            parts = path.rsplit(":", 2)
            if len(parts) == 3:
                file_path, line_str, col_str = parts
                try:
                    line = int(line_str) - 1  # 转换为从 0 开始
                    col = int(col_str) - 1
                    position = Position(line=line, character=col)
                except ValueError:
                    file_path = path
            elif len(parts) == 2:
                file_path, line_str = parts
                try:
                    line = int(line_str) - 1
                    position = Position(line=line, character=0)
                except ValueError:
                    file_path = path

        return file_path, position

    @staticmethod
    async def jump_from_uri(uri: str) -> bool:
        """从 URI 跳转到文件"""
        file_path, position = FileJumpProtocol.parse_uri(uri)
        return await FileJumpProtocol.jump_to_file(file_path, position)

    @staticmethod
    def generate_uri(
        file_path: str,
        position: Position | None = None,
        ide_type: IDEType | None = None,
    ) -> str:
        """生成文件 URI"""
        # 标准化路径
        file_path = os.path.abspath(file_path)  # noqa: PTH100

        if ide_type == IDEType.VSCODE:
            uri = f"vscode://file/{file_path}"
        elif ide_type == IDEType.JETBRAINS:
            uri = f"idea://file/{file_path}"
        elif ide_type == IDEType.NVIM:
            uri = f"nvim://file/{file_path}"
        else:
            uri = Path(file_path).as_uri()

        # 添加位置信息
        if position:
            uri += f":{position.line + 1}:{position.character + 1}"

        return uri
