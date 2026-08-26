# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件文件跳转协议） | @ci: python-coverage
"""file_jump 文件跳转协议测试。

覆盖（对齐 plugins/shared/tools/lsp/file_jump.py）：
1. generate_uri：VSCode/JetBrains/Nvim/file 四类 scheme，带/不带位置
2. parse_uri：file/vscode/idea/nvim/未知协议/无协议，位置解析（3 段/2 段/非法值）
3. jump_to_file：文件不存在、显式 ide_info、自动检测、默认打开、异常翻译
4. _jump_by_ide_type：不支持的 IDE、Windows cmd 包装、非 Windows 直启、Popen 失败
5. _open_with_default：Windows/Darwin/Linux 三分支与失败
6. jump_from_uri：URI → 跳转闭环
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_MOD_NAME = "lsp_file_jump_under_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _HERE / "file_jump.py")
    assert spec is not None and spec.loader is not None, "cannot load file_jump.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fj() -> Any:
    return _load_module()


@pytest.fixture(scope="module")
def lsp_types_mod() -> Any:
    import lsp_types  # noqa: PLC0415

    return lsp_types


class TestGenerateUri:
    def test_vscode(self, fj: Any) -> None:
        uri = fj.FileJumpProtocol.generate_uri("a.py", ide_type=fj.IDEType.VSCODE)
        assert uri.startswith("vscode://file/")
        assert uri.endswith("a.py")

    def test_jetbrains(self, fj: Any) -> None:
        uri = fj.FileJumpProtocol.generate_uri("a.py", ide_type=fj.IDEType.JETBRAINS)
        assert uri.startswith("idea://file/")
        assert uri.endswith("a.py")

    def test_nvim(self, fj: Any) -> None:
        uri = fj.FileJumpProtocol.generate_uri("a.py", ide_type=fj.IDEType.NVIM)
        assert uri.startswith("nvim://file/")
        assert uri.endswith("a.py")

    def test_default_file_scheme(self, fj: Any) -> None:
        uri = fj.FileJumpProtocol.generate_uri("a.py")
        assert uri.startswith("file://")

    def test_with_position(self, fj: Any, lsp_types_mod: Any) -> None:
        pos = lsp_types_mod.Position(line=4, character=2)
        uri = fj.FileJumpProtocol.generate_uri("a.py", position=pos, ide_type=fj.IDEType.VSCODE)
        assert uri.endswith(":5:3")

    def test_position_zero_based_offset(self, fj: Any, lsp_types_mod: Any) -> None:
        # 性质断言：0 基位置 → URI 中 +1（IDE 从 1 开始）
        pos = lsp_types_mod.Position(line=0, character=0)
        uri = fj.FileJumpProtocol.generate_uri("a.py", position=pos, ide_type=fj.IDEType.NVIM)
        assert uri.endswith(":1:1")

    def test_abspath_normalization(self, fj: Any, tmp_path: Path) -> None:
        uri = fj.FileJumpProtocol.generate_uri(str(tmp_path / "b.py"), ide_type=fj.IDEType.VSCODE)
        assert str(tmp_path) in uri


class TestParseUri:
    def test_file_scheme(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("file:///C:/ws/a.py")
        assert file_path == "/C:/ws/a.py"
        assert position is None

    def test_file_scheme_with_windows_drive_colon(self, fj: Any) -> None:
        # "C:" 中的冒号不应被误解析为位置
        file_path, position = fj.FileJumpProtocol.parse_uri("file:///C:/ws/a.py")
        assert position is None

    def test_vscode_with_position(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("vscode://file/C:/ws/a.py:10:5")
        assert file_path == "C:/ws/a.py"
        assert position is not None
        assert position.line == 9
        assert position.character == 4

    def test_idea_with_line_only(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("idea://file/a.py:3")
        assert file_path == "a.py"
        assert position is not None
        assert position.line == 2
        assert position.character == 0

    def test_nvim_no_position(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("nvim://file/a.py")
        assert file_path == "a.py"
        assert position is None

    def test_unknown_protocol(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("custom://x/y.py")
        assert file_path == "custom://x/y.py"
        assert position is None

    def test_plain_path(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("a.py")
        assert file_path == "a.py"
        assert position is None

    def test_plain_path_with_position(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("a.py:5:2")
        assert file_path == "a.py"
        assert position is not None
        assert position.line == 4
        assert position.character == 1

    def test_invalid_line_value(self, fj: Any) -> None:
        # 非法数字 → 整体回退为路径
        file_path, position = fj.FileJumpProtocol.parse_uri("a.py:abc")
        assert file_path == "a.py:abc"
        assert position is None

    def test_invalid_col_value(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("a.py:2:xyz")
        assert file_path == "a.py:2:xyz"
        assert position is None

    def test_percent_encoding_decoded(self, fj: Any) -> None:
        file_path, position = fj.FileJumpProtocol.parse_uri("vscode://file/C:/ws/my%20file.py:1:1")
        assert file_path == "C:/ws/my file.py"
        assert position is not None
        assert position.line == 0
        assert position.character == 0

    def test_roundtrip(self, fj: Any, lsp_types_mod: Any) -> None:
        # 性质断言：generate → parse 还原（Windows 盘符路径，路径比较归一化分隔符）
        pos = lsp_types_mod.Position(line=2, character=3)
        uri = fj.FileJumpProtocol.generate_uri("C:/ws/a.py", position=pos, ide_type=fj.IDEType.VSCODE)
        file_path, parsed = fj.FileJumpProtocol.parse_uri(uri)
        assert os.path.normpath(file_path) == os.path.normpath("C:/ws/a.py")
        assert parsed is not None
        assert parsed.line == 2
        assert parsed.character == 3


class TestJumpToFile:
    def test_file_not_exists(self, fj: Any) -> None:
        assert asyncio.run(fj.FileJumpProtocol.jump_to_file("C:/no/such/file_xyz.py")) is False

    def test_with_ide_info_vscode(self, fj: Any, lsp_types_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        ide = lsp_types_mod.IDEInfo(type=fj.IDEType.VSCODE, name="Code")
        result = asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target), ide_info=ide))
        assert result is True
        assert len(calls) == 1
        assert calls[0][:2] == ["cmd", "/c"]
        assert calls[0][2] == "code"
        assert "--goto" in calls[0]

    def test_with_ide_info_jetbrains_args(self, fj: Any, lsp_types_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        pos = lsp_types_mod.Position(line=0, character=0)
        ide = lsp_types_mod.IDEInfo(type=fj.IDEType.JETBRAINS, name="IDEA")
        result = asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target), position=pos, ide_info=ide))
        assert result is True
        assert calls[0][2] == "idea64.exe"
        assert calls[0][3:7] == ["--line", "1", "--column", "1"]

    def test_unsupported_ide_type(self, fj: Any, lsp_types_mod: Any, tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        ide = lsp_types_mod.IDEInfo(type=fj.IDEType.EMACS, name="emacs")
        assert asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target), ide_info=ide)) is False

    def test_detect_none_uses_default(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        started: list[str] = []
        monkeypatch.setattr(fj.os, "startfile", lambda p: started.append(p))
        monkeypatch.setattr(fj.IDEDetector, "detect", staticmethod(lambda: None))
        result = asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target)))
        assert result is True
        assert started == [str(target)]

    def test_detect_finds_ide(self, fj: Any, lsp_types_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        monkeypatch.setattr(
            fj.IDEDetector,
            "detect",
            staticmethod(lambda: lsp_types_mod.IDEInfo(type=fj.IDEType.VSCODE, name="Code")),
        )
        result = asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target)))
        assert result is True
        assert calls and calls[0][2] == "code"

    def test_jump_error_translated_to_false(self, fj: Any, lsp_types_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        monkeypatch.setattr(platform, "system", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        ide = lsp_types_mod.IDEInfo(type=fj.IDEType.VSCODE, name="Code")
        assert asyncio.run(fj.FileJumpProtocol.jump_to_file(str(target), ide_info=ide)) is False


class TestJumpByIdeType:
    def test_unsupported_type(self, fj: Any) -> None:
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.EMACS, "a.py")) is False

    def test_windows_cmd_wrap(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(platform, "system", lambda: "windows")
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.VSCODE, str(target))) is True
        assert calls[0][0] == "cmd"
        assert calls[0][2] == "code"

    def test_linux_direct(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(platform, "system", lambda: "linux")
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.NVIM, str(target))) is True
        assert calls[0][0] == "nvim"
        # 无 position 时默认 1 基再 +1（现状契约：默认位置 1,1 → cursor(2, 2)）
        assert calls[0][1] == "+call cursor(2, 2)"

    def test_darwin_command(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        calls: list[list[str]] = []
        monkeypatch.setattr(platform, "system", lambda: "darwin")
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.JETBRAINS, str(target))) is True
        assert calls[0][0] == "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"

    def test_popen_failure(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        monkeypatch.setattr(platform, "system", lambda: "linux")

        def raiser(args, shell=False):
            raise OSError("no such binary")

        monkeypatch.setattr(fj.subprocess, "Popen", raiser)
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.NVIM, str(target))) is False

    def test_unknown_system_no_command(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 未知系统名且无 linux 兜底模板 → 无命令 → False（现状契约：不静默降级）
        target = tmp_path / "a.py"
        target.write_text("x")
        monkeypatch.setattr(platform, "system", lambda: "beos")
        formats = {fj.IDEType.VSCODE: {"windows": "code", "args": ["--goto", "{file}:{line}:{col}"]}}
        monkeypatch.setattr(fj.FileJumpProtocol, "COMMAND_FORMATS", formats)
        assert asyncio.run(fj.FileJumpProtocol._jump_by_ide_type(fj.IDEType.VSCODE, str(target))) is False


class TestOpenWithDefault:
    def test_windows_startfile(self, fj: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[str] = []
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(fj.os, "startfile", lambda p: started.append(p))
        assert fj.FileJumpProtocol._open_with_default("C:/a.py") is True
        assert started == ["C:/a.py"]

    def test_darwin_open(self, fj: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        assert fj.FileJumpProtocol._open_with_default("a.py") is True
        assert calls == [["open", "a.py"]]

    def test_linux_xdg_open(self, fj: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(fj.subprocess, "Popen", lambda args, shell=False: calls.append(args))
        assert fj.FileJumpProtocol._open_with_default("a.py") is True
        assert calls == [["xdg-open", "a.py"]]

    def test_failure(self, fj: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        def raiser(p):
            raise OSError("denied")

        monkeypatch.setattr(fj.os, "startfile", raiser)
        assert fj.FileJumpProtocol._open_with_default("a.py") is False


class TestJumpFromUri:
    def test_roundtrip(self, fj: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "a.py"
        target.write_text("x")
        started: list[str] = []
        monkeypatch.setattr(fj.os, "startfile", lambda p: started.append(p))
        monkeypatch.setattr(fj.IDEDetector, "detect", staticmethod(lambda: None))
        uri = fj.FileJumpProtocol.generate_uri(str(target), ide_type=fj.IDEType.VSCODE)
        assert asyncio.run(fj.FileJumpProtocol.jump_from_uri(uri)) is True
        assert started == [str(target)]

    def test_missing_file(self, fj: Any) -> None:
        assert asyncio.run(fj.FileJumpProtocol.jump_from_uri("vscode://file/C:/no/such_xyz.py:1:1")) is False
