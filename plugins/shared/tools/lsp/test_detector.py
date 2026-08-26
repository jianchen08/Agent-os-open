# @feature: FP-0.2.二 内部模块 manifest（lsp 工具插件 IDE 检测） | @ci: python-coverage
"""detector IDE 检测器测试。

覆盖（对齐 plugins/shared/tools/lsp/detector.py）：
1. get_ide_type：名称→类型映射（含大小写/子串/未知）
2. _detect_by_process：进程名匹配、无进程名跳过、异常吞掉
3. _detect_by_files：.vscode/.idea/nvim 配置目录
4. _detect_by_env：VSCODE_PID / TERM_PROGRAM
5. detect 优先级链：进程→文件→环境→None
6. detect_all：去重、多 IDE
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil
import pytest

from detector import IDEDetector
from lsp_types import IDEType

pytestmark = pytest.mark.unit


class TestGetIdeType:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Code.exe", IDEType.VSCODE),
            ("code", IDEType.VSCODE),
            ("Visual Studio Code", IDEType.VSCODE),
            ("vscode", IDEType.VSCODE),
            ("idea64.exe", IDEType.JETBRAINS),
            ("pycharm64.exe", IDEType.JETBRAINS),
            ("JetBrains Toolbox", IDEType.JETBRAINS),
            ("nvim", IDEType.NVIM),
            ("neovim", IDEType.NVIM),
            ("emacs", IDEType.EMACS),
            ("emacs.exe", IDEType.EMACS),
            ("devenv.exe", IDEType.UNKNOWN),
            ("Visual Studio", IDEType.VS),
            ("notepad", IDEType.UNKNOWN),
            ("", IDEType.UNKNOWN),
        ],
    )
    def test_mapping(self, name: str, expected: IDEType) -> None:
        assert IDEDetector.get_ide_type(name) is expected

    def test_case_insensitive(self) -> None:
        assert IDEDetector.get_ide_type("CODE.EXE") is IDEType.VSCODE
        assert IDEDetector.get_ide_type("Nvim") is IDEType.NVIM

    def test_priority_vscode_before_visual_studio(self) -> None:
        # "visual studio code" 含 "visual studio"，须命中 VSCODE 而非 VS
        assert IDEDetector.get_ide_type("Visual Studio Code") is IDEType.VSCODE


class TestDetectByProcess:
    @staticmethod
    def _fake_proc(name: str | None, cwd: str | None = None) -> Any:
        return type("FakeProc", (), {"info": {"name": name, "exe": None, "cwd": cwd}})()

    def test_match_vscode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([self._fake_proc("Code.exe", "C:/ws")]),
        )
        info = IDEDetector._detect_by_process()
        assert info is not None
        assert info.type is IDEType.VSCODE
        assert info.name == "Code.exe"
        assert info.workspace == "C:/ws"

    def test_match_jetbrains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([self._fake_proc("pycharm64.exe")]),
        )
        info = IDEDetector._detect_by_process()
        assert info is not None
        assert info.type is IDEType.JETBRAINS

    def test_no_match_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([self._fake_proc("explorer.exe"), self._fake_proc("python.exe")]),
        )
        assert IDEDetector._detect_by_process() is None

    def test_unnamed_process_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([self._fake_proc(None), self._fake_proc("Code.exe")]),
        )
        info = IDEDetector._detect_by_process()
        assert info is not None and info.type is IDEType.VSCODE

    def test_psutil_errors_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(attrs=None):
            yield self._fake_proc("Code.exe")
            raise psutil.NoSuchProcess(1)

        monkeypatch.setattr(psutil, "process_iter", raiser)
        info = IDEDetector._detect_by_process()
        assert info is not None and info.type is IDEType.VSCODE

    def test_info_access_denied_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class DeniedProc:
            @property
            def info(self) -> Any:
                raise psutil.AccessDenied(1)

        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([DeniedProc(), self._fake_proc("Code.exe")]),
        )
        info = IDEDetector._detect_by_process()
        assert info is not None and info.type is IDEType.VSCODE

    def test_iter_raises_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(attrs=None):
            raise OSError("boom")

        monkeypatch.setattr(psutil, "process_iter", raiser)
        assert IDEDetector._detect_by_process() is None


class TestDetectByFiles:
    def test_vscode_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".vscode").mkdir()
        monkeypatch.chdir(tmp_path)
        info = IDEDetector._detect_by_files()
        assert info is not None
        assert info.type is IDEType.VSCODE
        assert info.name == "Visual Studio Code"
        assert info.workspace == str(tmp_path)

    def test_idea_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".idea").mkdir()
        monkeypatch.chdir(tmp_path)
        info = IDEDetector._detect_by_files()
        assert info is not None
        assert info.type is IDEType.JETBRAINS
        assert info.name == "JetBrains IDE"

    def test_nvim_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        nvim_dir = tmp_path / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        info = IDEDetector._detect_by_files()
        assert info is not None
        assert info.type is IDEType.NVIM
        assert info.name == "Neovim"

    def test_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert IDEDetector._detect_by_files() is None

    def test_vscode_priority_over_idea(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / ".vscode").mkdir()
        (tmp_path / ".idea").mkdir()
        monkeypatch.chdir(tmp_path)
        info = IDEDetector._detect_by_files()
        assert info is not None and info.type is IDEType.VSCODE


class TestDetectByEnv:
    def test_vscode_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VSCODE_PID", "1234")
        info = IDEDetector._detect_by_env()
        assert info is not None
        assert info.type is IDEType.VSCODE
        assert info.name == "Visual Studio Code"

    def test_term_program_vscode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSCODE_PID", raising=False)
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        info = IDEDetector._detect_by_env()
        assert info is not None and info.type is IDEType.VSCODE

    def test_term_program_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSCODE_PID", raising=False)
        monkeypatch.setenv("TERM_PROGRAM", "VSCode")
        info = IDEDetector._detect_by_env()
        assert info is not None and info.type is IDEType.VSCODE

    def test_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VSCODE_PID", raising=False)
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        assert IDEDetector._detect_by_env() is None


class TestDetect:
    def test_process_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([type("P", (), {"info": {"name": "Code.exe", "exe": None, "cwd": None}})()]),
        )
        info = IDEDetector.detect()
        assert info is not None and info.type is IDEType.VSCODE

    def test_files_second(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([]))
        (tmp_path / ".idea").mkdir()
        monkeypatch.chdir(tmp_path)
        info = IDEDetector.detect()
        assert info is not None and info.type is IDEType.JETBRAINS

    def test_env_third(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([]))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("VSCODE_PID", "1")
        info = IDEDetector.detect()
        assert info is not None and info.type is IDEType.VSCODE

    def test_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([]))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("VSCODE_PID", raising=False)
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        assert IDEDetector.detect() is None


class TestDetectAll:
    @staticmethod
    def _fake_proc(name: str | None, cwd: str | None = None) -> Any:
        return type("FakeProc", (), {"info": {"name": name, "exe": None, "cwd": cwd}})()

    def test_multiple_ides_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter(
                [
                    self._fake_proc("Code.exe", "C:/a"),
                    self._fake_proc("code", "C:/b"),
                    self._fake_proc("nvim", "C:/c"),
                ]
            ),
        )
        results = IDEDetector.detect_all()
        types = [r.type for r in results]
        assert IDEType.VSCODE in types
        assert IDEType.NVIM in types
        # 同类型去重：VSCODE 只出现一次
        assert types.count(IDEType.VSCODE) == 1

    def test_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([]))
        assert IDEDetector.detect_all() == []

    def test_errors_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(attrs=None):
            yield self._fake_proc("Code.exe")
            raise psutil.AccessDenied(1)

        monkeypatch.setattr(psutil, "process_iter", raiser)
        results = IDEDetector.detect_all()
        assert [r.type for r in results] == [IDEType.VSCODE]

    def test_unnamed_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([self._fake_proc(None), self._fake_proc("nvim")]),
        )
        results = IDEDetector.detect_all()
        assert [r.type for r in results] == [IDEType.NVIM]

    def test_info_access_denied_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class DeniedProc:
            @property
            def info(self) -> Any:
                raise psutil.AccessDenied(1)

        monkeypatch.setattr(
            psutil,
            "process_iter",
            lambda attrs=None: iter([DeniedProc(), self._fake_proc("Code.exe")]),
        )
        results = IDEDetector.detect_all()
        assert [r.type for r in results] == [IDEType.VSCODE]

    def test_iter_raises_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(attrs=None):
            raise OSError("boom")

        monkeypatch.setattr(psutil, "process_iter", raiser)
        assert IDEDetector.detect_all() == []
