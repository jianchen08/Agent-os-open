"""
IDE 检测器

暴露接口：
- detect() -> IDEInfo | None：detect功能
- detect_all() -> list[IDEInfo]：detect_all功能
- get_ide_type(name: str) -> IDEType：get_ide_type功能
- IDEDetector：IDEDetector类
"""

import os
from pathlib import Path

import psutil

from src.lsp.types import IDEInfo, IDEType


class IDEDetector:
    """
    IDE 检测器

    通过进程、文件、端口等方式检测当前使用的 IDE
    """

    # IDE 进程名称映射
    PROCESS_NAMES = {
        IDEType.VSCODE: [
            "Code.exe",
            "code",
            "Code",
        ],
        IDEType.JETBRAINS: [
            "idea64.exe",
            "pycharm64.exe",
            "clion64.exe",
            "rider64.exe",
            "goland64.exe",
            "webstorm64.exe",
            "datagrip64.exe",
            "idea",
            "pycharm",
        ],
        IDEType.NVIM: [
            "nvim",
            "neovim",
        ],
        IDEType.EMACS: [
            "emacs",
            "emacs.exe",
        ],
        IDEType.VS: [
            "devenv.exe",
            "VisualStudio",
        ],
    }

    # IDE 端口映射（部分 IDE 会开放 LSP 端口）
    DEFAULT_PORTS = {
        IDEType.VSCODE: None,  # VSCode 使用内置 LSP
        IDEType.NVIM: None,  # Nvim 内置 LSP
    }

    @staticmethod
    def detect() -> IDEInfo | None:
        """检测当前运行的 IDE"""
        # 1. 通过进程检测
        ide_info = IDEDetector._detect_by_process()
        if ide_info:
            return ide_info

        # 2. 通过文件检测（配置文件）
        ide_info = IDEDetector._detect_by_files()
        if ide_info:
            return ide_info

        # 3. 通过环境变量检测
        ide_info = IDEDetector._detect_by_env()
        if ide_info:
            return ide_info

        return None

    @staticmethod
    def _detect_by_process() -> IDEInfo | None:
        """通过运行进程检测 IDE"""
        try:
            for proc in psutil.process_iter(["name", "exe", "cwd"]):
                try:
                    proc_name = proc.info["name"]
                    if not proc_name:
                        continue

                    # 检查是否匹配已知 IDE
                    for ide_type, names in IDEDetector.PROCESS_NAMES.items():
                        if proc_name in names:
                            return IDEInfo(
                                type=ide_type,
                                name=proc_name,
                                workspace=proc.info.get("cwd"),
                            )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        return None

    @staticmethod
    def _detect_by_files() -> IDEInfo | None:
        """通过配置文件检测 IDE"""
        cwd = Path.cwd()

        # VSCode: .vscode 目录
        vscode_dir = cwd / ".vscode"
        if vscode_dir.exists():
            return IDEInfo(
                type=IDEType.VSCODE,
                name="Visual Studio Code",
                workspace=str(cwd),
            )

        # JetBrains: .idea 目录
        idea_dir = cwd / ".idea"
        if idea_dir.exists():
            return IDEInfo(
                type=IDEType.JETBRAINS,
                name="JetBrains IDE",
                workspace=str(cwd),
            )

        # Nvim: init.lua 或 init.vim
        nvim_config = Path.home() / ".config" / "nvim"
        if nvim_config.exists():
            return IDEInfo(
                type=IDEType.NVIM,
                name="Neovim",
            )

        return None

    @staticmethod
    def _detect_by_env() -> IDEInfo | None:
        """通过环境变量检测 IDE"""
        # VSCODE_PID 环境变量（从 VSCode 扩展运行时）
        if "VSCODE_PID" in os.environ:
            return IDEInfo(
                type=IDEType.VSCODE,
                name="Visual Studio Code",
            )

        # TERM_PROGRAM 环境变量（macOS/iTerm）
        term_program = os.environ.get("TERM_PROGRAM", "")
        if "vscode" in term_program.lower():
            return IDEInfo(
                type=IDEType.VSCODE,
                name="Visual Studio Code",
            )

        return None

    @staticmethod
    def detect_all() -> list[IDEInfo]:
        """检测所有运行的 IDE"""
        results = []

        try:
            seen_types = set()
            for proc in psutil.process_iter(["name", "exe", "cwd"]):
                try:
                    proc_name = proc.info["name"]
                    if not proc_name:
                        continue

                    for ide_type, names in IDEDetector.PROCESS_NAMES.items():
                        if proc_name in names and ide_type not in seen_types:
                            results.append(
                                IDEInfo(
                                    type=ide_type,
                                    name=proc_name,
                                    workspace=proc.info.get("cwd"),
                                )
                            )
                            seen_types.add(ide_type)
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        return results

    @staticmethod
    def get_ide_type(name: str) -> IDEType:
        """根据 IDE 名称获取类型"""
        name_lower = name.lower()

        # VSCode: 检查 "vscode", "visual studio code", 或 "code.exe"
        if "vscode" in name_lower or "visual studio code" in name_lower or name_lower in {"code.exe", "code"}:
            return IDEType.VSCODE
        if "idea" in name_lower or "pycharm" in name_lower or "jetbrains" in name_lower:
            return IDEType.JETBRAINS
        if "nvim" in name_lower or "neovim" in name_lower:
            return IDEType.NVIM
        if "emacs" in name_lower:
            return IDEType.EMACS
        if "visual studio" in name_lower:
            return IDEType.VS
        return IDEType.UNKNOWN
