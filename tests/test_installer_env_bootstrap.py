# @feature: FP-0.2.PACK 安装器 WSL 环境准备 | @ci: python-coverage
"""安装期 WSL 引导脚本测试（scripts/installer_env_bootstrap.py）。

覆盖：
1. 版本解析（中英文/UTF-16-LE/不可解析）；
2. 需求判定与动作计划（缺失→install、过旧→update、达标→空）；
3. 探测（fake runner 返回 UTF-16 输出）；
4. 安装/升级流程：已达标 no-op、缺失安装成功、安装失败回退、安装后仍不可
   查询→待重启、过旧升级、升级后仍过旧→版本无法满足、更新失败→错误；
5. 提权入口：非管理员且允许提权时只调一次 elevate_self 并透传退出码；
6. CLI：--check 达标/不达标退出码、非 Windows 拒绝、--install-wsl 输出 JSON。

外部依赖（wsl.exe / PowerShell）全部用脚本化 runner 替身；不触真实系统。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "installer_env_bootstrap.py"


def _load_mod() -> Any:
    name = "installer_env_bootstrap_test_mod"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


class _Proc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _scripted_runner(responses: dict[tuple[str, ...], list[Any]]) -> Any:
    """按 argv 元组回放响应；每个键可给多个响应（顺序消费）。"""
    calls: list[list[str]] = []
    remaining = {key: list(value) for key, value in responses.items()}

    def runner(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        key = tuple(args)
        queue = remaining.get(key)
        if not queue:
            raise AssertionError(f"unexpected command: {args}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _version_out(version: str) -> _Proc:
    text = f"WSL 版本: {version}\n内核版本: 6.6.0\n"
    return _Proc(0, stdout=text.encode("utf-16-le"))


class TestVersionParsing:
    def test_parse_chinese_and_english(self) -> None:
        assert _MOD.parse_wsl_version("WSL 版本: 3.0.5\n内核版本: 6.6") == (3, 0, 5)
        assert _MOD.parse_wsl_version("WSL version: 2.7.8.0") == (2, 7, 8)

    def test_parse_none(self) -> None:
        assert _MOD.parse_wsl_version("") is None
        assert _MOD.parse_wsl_version("command not found") is None

    def test_decode_utf16_and_utf8(self) -> None:
        assert _MOD.decode_wsl_output("WSL 版本: 3.0.5".encode("utf-16-le")).startswith("WSL")
        assert _MOD.decode_wsl_output(b"plain") == "plain"
        assert _MOD.decode_wsl_output(b"") == ""

    def test_needs_wsl_matrix(self) -> None:
        assert _MOD.needs_wsl(None) is True
        assert _MOD.needs_wsl((3, 0, 4)) is True
        assert _MOD.needs_wsl((3, 0, 5)) is False
        assert _MOD.needs_wsl((3, 1, 0)) is False

    def test_plan_actions(self) -> None:
        assert _MOD.plan_actions(None) == ["install"]
        assert _MOD.plan_actions((2, 7, 8)) == ["update"]
        assert _MOD.plan_actions((3, 0, 5)) == []


class TestQuery:
    def test_query_utf16_output(self) -> None:
        runner = _scripted_runner({("wsl.exe", "--version"): [_version_out("3.0.6")]})
        version, raw = _MOD.query_wsl_version(runner=runner)
        assert version == (3, 0, 6)
        assert "3.0.6" in raw

    def test_query_oserror(self) -> None:
        runner = _scripted_runner({("wsl.exe", "--version"): [OSError("missing")]})
        version, raw = _MOD.query_wsl_version(runner=runner)
        assert version is None
        assert "调用失败" in raw


class TestInstallFlow:
    def _install(self, runner: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return _MOD.install_or_update_wsl(runner=runner, allow_elevation=False, **kwargs)

    def test_already_ok_no_action(self) -> None:
        runner = _scripted_runner({("wsl.exe", "--version"): [_version_out("3.1.0")]})
        code, result = self._install(runner)
        assert code == 0
        assert result["status"] == "ok"
        assert result["actions"] == []
        assert len(runner.calls) == 1  # type: ignore[attr-defined]

    def test_missing_installs_no_distribution_then_ok(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_Proc(1, stderr=b"not installed"), _version_out("3.0.6")],
                ("wsl.exe", "--install", "--no-distribution"): [_Proc(0, stdout=b"installing")],
            }
        )
        code, result = self._install(runner)
        assert code == 0
        assert result["status"] == "ok"
        assert result["actions"] == ["install"]
        assert ["wsl.exe", "--install", "--no-distribution"] in runner.calls  # type: ignore[attr-defined]

    def test_install_flag_unsupported_falls_back(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_Proc(1), _version_out("3.0.6")],
                ("wsl.exe", "--install", "--no-distribution"): [_Proc(1, stderr=b"unknown option")],
                ("wsl.exe", "--install"): [_Proc(0)],
            }
        )
        code, _ = self._install(runner)
        assert code == 0
        assert ["wsl.exe", "--install"] in runner.calls  # type: ignore[attr-defined]

    def test_installed_but_unqueryable_needs_reboot(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_Proc(1), _Proc(1, stderr=b"not ready")],
                ("wsl.exe", "--install", "--no-distribution"): [_Proc(0)],
            }
        )
        code, result = self._install(runner)
        assert code == _MOD.EXIT_REBOOT_REQUIRED
        assert result["status"] == "needs_reboot"

    def test_install_explicit_reboot_code(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_Proc(1)],
                ("wsl.exe", "--install", "--no-distribution"): [_Proc(3010)],
            }
        )
        code, result = self._install(runner)
        assert code == _MOD.EXIT_REBOOT_REQUIRED
        assert result["status"] == "needs_reboot"

    def test_install_failed(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_Proc(1)],
                ("wsl.exe", "--install", "--no-distribution"): [_Proc(1, stderr=b"failed")],
                ("wsl.exe", "--install"): [_Proc(1, stderr=b"failed")],
            }
        )
        code, result = self._install(runner)
        assert code == _MOD.EXIT_INSTALL_FAILED
        assert result["status"] == "error"

    def test_old_version_updates_with_web_download(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_version_out("2.7.8"), _version_out("3.0.6")],
                ("wsl.exe", "--update", "--web-download"): [_Proc(0)],
            }
        )
        code, result = self._install(runner)
        assert code == 0
        assert result["actions"] == ["update"]
        assert ["wsl.exe", "--update", "--web-download"] in runner.calls  # type: ignore[attr-defined]

    def test_update_fallback(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_version_out("2.7.8"), _version_out("3.0.6")],
                ("wsl.exe", "--update", "--web-download"): [_Proc(1)],
                ("wsl.exe", "--update"): [_Proc(0)],
            }
        )
        code, _ = self._install(runner)
        assert code == 0
        assert ["wsl.exe", "--update"] in runner.calls  # type: ignore[attr-defined]

    def test_update_but_still_old(self) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_version_out("2.7.8"), _version_out("2.7.8")],
                ("wsl.exe", "--update", "--web-download"): [_Proc(0)],
            }
        )
        code, result = self._install(runner)
        assert code == _MOD.EXIT_VERSION_UNRESOLVED
        assert result["status"] == "version_unresolved"


class TestElevation:
    def test_non_admin_elevates_once_and_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _scripted_runner({("wsl.exe", "--version"): [_version_out("2.7.8")]})
        calls: list[list[str]] = []

        def fake_elevate(argv: list[str]) -> int:
            calls.append(argv)
            return _MOD.EXIT_REBOOT_REQUIRED

        monkeypatch.setattr(_MOD, "is_admin", lambda: False)
        monkeypatch.setattr(_MOD, "elevate_self", fake_elevate)
        code, result = _MOD.install_or_update_wsl(runner=runner, allow_elevation=True)
        assert code == _MOD.EXIT_REBOOT_REQUIRED
        assert result["status"] == "needs_reboot"
        assert calls == [["--install-wsl", "--elevated-child", "--json"]]

    def test_admin_does_not_elevate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = _scripted_runner(
            {
                ("wsl.exe", "--version"): [_version_out("2.7.8"), _version_out("3.0.6")],
                ("wsl.exe", "--update", "--web-download"): [_Proc(0)],
            }
        )
        monkeypatch.setattr(_MOD, "is_admin", lambda: True)
        monkeypatch.setattr(
            _MOD,
            "elevate_self",
            lambda argv: (_ for _ in ()).throw(AssertionError("should not elevate")),  # noqa: ARG005
        )
        code, _ = _MOD.install_or_update_wsl(runner=runner, allow_elevation=True)
        assert code == 0


class TestCli:
    def test_non_windows_exit_5(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        monkeypatch.setattr(_MOD.os, "name", "posix")
        assert _MOD.main(["--check", "--json"]) == _MOD.EXIT_UNSUPPORTED_PLATFORM
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "unsupported_platform"

    def test_check_ok_and_not_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(_MOD.os, "name", "nt")
        monkeypatch.setattr(_MOD, "query_wsl_version", lambda **kwargs: ((3, 0, 6), "ok"))  # noqa: ARG005
        assert _MOD.main(["--check", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True

        monkeypatch.setattr(_MOD, "query_wsl_version", lambda **kwargs: ((2, 7, 8), "old"))  # noqa: ARG005
        assert _MOD.main(["--check", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False

    def test_install_wsl_json(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        monkeypatch.setattr(_MOD.os, "name", "nt")
        monkeypatch.setattr(
            _MOD,
            "install_or_update_wsl",
            lambda **kwargs: (0, {"status": "ok", "before": "3.0.6", "after": "3.0.6"}),  # noqa: ARG005
        )
        assert _MOD.main(["--install-wsl", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["exit_code"] == 0
