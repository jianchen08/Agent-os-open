# @feature: FP-0.2.一 第三方插件协议·python_packager 装载 | @ci: python-coverage
"""python_packager 缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（行号语义经源码逐行确认）：
- server.py:54-55（subprocess.run 的 FileNotFoundError → 找不到可执行文件）、
  59-60（subprocess.TimeoutExpired → 命令超时）、62（非零退出码 → 带 rc 的
  失败结果）、82（AGENTOS_PROJECT_ROOT 显式覆盖 _project_root）、
  106（package_dir 不存在 → 目录不存在拒绝）、160（install 的 uv sync 失败
  透传 fail-closed）、235（resolve_dependencies 的 uv sync 失败：venv/lock
  已在位但仍诚实标 resolved=false + error）

外部依赖边界：uv 子进程调用（subprocess.run）用替身脚本化，其余（路径边界
判定、pyproject 解析、目录存在性、venv/lock 探测）走真实 tmp_path 文件系统。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))


def _load_server() -> Any:
    mod_name = "python_packager_gaps_server"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture(autouse=True)
def _whitelist_pytest_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """包目录边界白名单：本批用例的包目录都在 pytest 临时根下。"""
    monkeypatch.setenv("PYTHON_PACKAGER_ALLOWED_DIRS", str(tmp_path.parent))


def _make_uv_package(root: Path, deps: list[str] | None = None) -> Path:
    d = root / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    dep_lines = "".join(f'        "{x}",\n' for x in (deps or []))
    (d / "pyproject.toml").write_text(
        f"[project]\n"
        f'name = "pkg"\n'
        f'version = "1.0.0"\n'
        f'requires-python = ">=3.11"\n'
        f"dependencies = [\n{dep_lines}]\n",
        encoding="utf-8",
    )
    return d


class _SubprocessStub:
    """subprocess 模块替身（uv 子进程边界）：run 回放预设结果或抛预设异常。"""

    def __init__(self, *, result: Any = None, raises: Exception | None = None) -> None:
        self.TimeoutExpired = subprocess.TimeoutExpired
        self.CompletedProcess = subprocess.CompletedProcess
        self._result = result
        self._raises = raises
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(cmd))
        if self._raises is not None:
            raise self._raises
        return self._result


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["uv"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestRunSubprocessFailures:
    """_run 的三条失败翻译（FileNotFoundError / TimeoutExpired / 非零退出）。"""

    def test_missing_executable_translated(self, server: Any) -> None:
        """FileNotFoundError → 找不到可执行文件（54-55），不向上抛。"""
        stub = _SubprocessStub(raises=FileNotFoundError("no such exe"))
        server.subprocess = stub
        result = server._run(["ghost-uv", "sync"], Path.cwd())
        assert result["ok"] is False
        assert "找不到可执行文件" in result["error"]
        assert "ghost-uv" in result["error"]
        assert stub.calls, "替身应被真实调用（未走空转）"

    def test_timeout_translated(self, server: Any) -> None:
        """TimeoutExpired → 命令超时，错误文案带超时秒数与命令行（59-60）。"""
        stub = _SubprocessStub(raises=subprocess.TimeoutExpired(cmd=["uv", "sync"], timeout=300))
        server.subprocess = stub
        result = server._run(["uv", "sync", "--project", "/x"], Path.cwd(), timeout=300)
        assert result["ok"] is False
        assert "命令超时(300s)" in result["error"]
        assert "--project" in result["error"]

    @pytest.mark.parametrize(
        ("stdout", "stderr", "expected_snippet"),
        [
            ("", "fatal: resolution failed", "resolution failed"),
            ("only stdout text", "", "only stdout text"),
        ],
    )
    def test_nonzero_returncode_reports_rc_and_output(
        self, server: Any, stdout: str, stderr: str, expected_snippet: str
    ) -> None:
        """非零退出 → ok=False + rc + stderr/stdout 兜底（62）。"""
        server.subprocess = _SubprocessStub(result=_completed(2, stdout, stderr))
        result = server._run(["uv", "sync"], Path.cwd())
        assert result["ok"] is False
        assert result["rc"] == 2
        assert expected_snippet in result["error"]

    def test_zero_returncode_success_shape(self, server: Any) -> None:
        """对照组：0 退出 → ok=True + rc=0 + stdout 供调用方消费。"""
        server.subprocess = _SubprocessStub(result=_completed(0, "resolved 3 packages\n"))
        result = server._run(["uv", "sync"], Path.cwd())
        assert result["ok"] is True
        assert result["rc"] == 0
        assert "resolved 3 packages" in result["stdout"]


class TestProjectRootOverride:
    """_project_root：AGENTOS_PROJECT_ROOT 显式覆盖（82）。"""

    def test_env_override_shifts_plugins_boundary(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 指向自定义根 → 其下 plugins/** 放行、原根之外仍拒绝。"""
        fake_root = tmp_path / "custom-root"
        pkg = fake_root / "plugins" / "my_plugin"
        pkg.mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "my_plugin"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(fake_root))
        monkeypatch.delenv("PYTHON_PACKAGER_ALLOWED_DIRS", raising=False)

        resolved, err = server._check_package_dir(str(pkg))
        assert err == "" and resolved == pkg.resolve()
        assert server._project_root() == fake_root.resolve()

    def test_outside_env_root_denied(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照组：不在 env 根 plugins/** 下 → 越界拒绝（证明边界随 env 移动）。"""
        fake_root = tmp_path / "custom-root"
        fake_root.mkdir()
        other = _make_uv_package(tmp_path / "elsewhere")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(fake_root))
        monkeypatch.delenv("PYTHON_PACKAGER_ALLOWED_DIRS", raising=False)

        resolved, err = server._check_package_dir(str(other))
        assert resolved is None
        assert "不在允许范围" in err

    def test_without_env_falls_back_to_file_location(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 env 时按本文件位置推导（对照组，非 env 分支）。"""
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        assert server._project_root() == Path(server.__file__).resolve().parents[4]


class TestPackageDirExistence:
    """_check_package_dir：目录不存在 → 明确拒绝（106）。"""

    @pytest.mark.parametrize(
        "name", ["never-created", "ghost/nested/deep"]
    )
    def test_missing_directory_rejected(
        self, server: Any, tmp_path: Path, name: str
    ) -> None:
        missing = tmp_path / name
        resolved, err = server._check_package_dir(str(missing))
        assert resolved is None
        assert "目录不存在" in err
        assert not missing.exists()

    def test_file_instead_of_directory_rejected(self, server: Any, tmp_path: Path) -> None:
        """路径是普通文件（非目录）→ 同样按目录不存在处理（有区分度输入）。"""
        f = tmp_path / "not-a-dir.txt"
        f.write_text("x", encoding="utf-8")
        resolved, err = server._check_package_dir(str(f))
        assert resolved is None
        assert "目录不存在" in err


class TestUvActionFailurePropagation:
    """uv 动作失败在 install / resolve_dependencies 的透传（160 / 235）。"""

    async def test_install_propagates_uv_sync_failure(
        self, server: Any, tmp_path: Path
    ) -> None:
        """uv sync 失败 → install 原样返回失败（160），不伪造 venv 路径。"""
        d = _make_uv_package(tmp_path)
        server.subprocess = _SubprocessStub(result=_completed(1, "", "uv: network unreachable"))

        result = await server.install(str(d))

        assert result["ok"] is False
        assert result["rc"] == 1
        assert "network unreachable" in result["error"]
        assert "venv_exists" not in result, "失败结果不得携带成功字段"

    async def test_resolve_dependencies_reports_unresolved_on_sync_failure(
        self, server: Any, tmp_path: Path
    ) -> None:
        """venv+lock 在位但 uv sync 失败 → resolved=false + error（235），不假装绿。"""
        d = _make_uv_package(tmp_path, deps=["requests>=2.0"])
        (d / ".venv").mkdir()
        (d / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        server.subprocess = _SubprocessStub(result=_completed(1, "", "lock out of date"))

        result = await server.resolve_dependencies(str(d))

        assert result["ok"] is True, "解析失败是环境未满足而非调用错误"
        assert result["resolved"] is False
        assert result["missing"] == ["requests>=2.0"]
        assert result["satisfied"] == []
        assert "lock out of date" in (result["error"] or "")

    async def test_resolve_dependencies_without_lock_skips_uv(
        self, server: Any, tmp_path: Path
    ) -> None:
        """对照组：缺 lock/venv → 直接标未解析，不触发任何 uv 调用（230）。"""
        d = _make_uv_package(tmp_path, deps=["requests>=2.0"])
        stub = _SubprocessStub(result=_completed(0))
        server.subprocess = stub

        result = await server.resolve_dependencies(str(d))

        assert result["resolved"] is False
        assert result["missing"] == ["requests>=2.0"]
        assert stub.calls == [], "未安装过不应执行 uv sync"

    async def test_status_reports_unavailable_uv(
        self, server: Any, tmp_path: Path
    ) -> None:
        """uv --version 失败 → uv_available=False 且 uv_version=None（不崩）。"""
        d = _make_uv_package(tmp_path)
        server.subprocess = _SubprocessStub(result=_completed(127, "", "command not found"))

        result = await server.status(str(d))

        assert result["ok"] is True
        assert result["uv_available"] is False
        assert result["uv_version"] is None
        assert result["venv_exists"] is False and result["lock_exists"] is False
