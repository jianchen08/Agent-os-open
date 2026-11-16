# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""tests/ci_packs conftest——临时 CI 项目构造器与工具替身（不依赖真实 godot 环境）。

工具探测两路（skip / 真跑）由可执行替身驱动：win 写 .bat、posix 写 sh 脚本，
经 PATH 或 --godot/GODOT_BIN 注入，行为（退出码/输出）由测试指定。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS_ROOT = REPO_ROOT / "packs" / "ci"
BASE_PACK = PACKS_ROOT / "base"
GODOT_PACK = PACKS_ROOT / "godot"
RUN_PY = BASE_PACK / "run.py"

# 供 runner 语义测试用的假 checker 源码（退出码契约 0/1/2 的三态 + 崩溃态）
CHECKER_OK = "import sys\nsys.exit(0)\n"
CHECKER_FAIL = "import sys\nprint('发现违规X')\nsys.exit(1)\n"
CHECKER_SKIP = "import sys\nprint('工具缺席')\nsys.exit(2)\n"
CHECKER_CRASH = "import sys\nsys.exit(3)\n"
CHECKER_STDIN = (
    "import sys\ndata = sys.stdin.read()\n"
    "print('STAGED:' + data.replace(chr(0), ',').strip(','))\nsys.exit(1)\n"
)


def run_runner(
    project: Path,
    *args: str,
    stdin: bytes | str = b"",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """以项目内组装的 run.py 跑一次 runner（cwd=项目根，python 经绝对路径注入 PATH）。"""
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(project / ".agentos" / "ci" / "run.py"), *args],
        cwd=str(project),
        input=stdin.decode("utf-8") if isinstance(stdin, bytes) else stdin,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def run_checker(
    project: Path,
    check_name: str,
    *args: str,
    stdin: bytes | str = b"",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """跑项目内 checks/<name>（cwd=项目根，与 runner 子进程同构）。"""
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(project / ".agentos" / "ci" / "checks" / check_name), *args],
        cwd=str(project),
        input=stdin.decode("utf-8") if isinstance(stdin, bytes) else stdin,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def make_ci_project(
    root: Path,
    factory_gates: list[dict[str, Any]] | dict[str, Any],
    checks: dict[str, str],
    local_gates: list[dict[str, Any]] | dict[str, Any] | None = None,
    local_checks: dict[str, str] | None = None,
) -> Path:
    """搭一个带 .agentos/ci 的临时项目（runner 用真件，checkers 用测试假件）。"""
    (root / ".git").mkdir()
    ci = root / ".agentos" / "ci"
    (ci / "checks").mkdir(parents=True)
    shutil.copyfile(RUN_PY, ci / "run.py")
    (ci / "gates.json").write_text(json.dumps(factory_gates), encoding="utf-8")
    for name, body in checks.items():
        (ci / "checks" / (name if name.endswith(".py") else f"{name}.py")).write_text(
            body, encoding="utf-8"
        )
    if local_gates is not None:
        local = ci / "local"
        local.mkdir(exist_ok=True)
        (local / "gates.json").write_text(json.dumps(local_gates), encoding="utf-8")
    for name, body in (local_checks or {}).items():
        target = ci / "local" / (name if name.endswith(".py") else f"{name}.py")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def load_runner_module() -> Any:
    """按路径装载真实 run.py（校验函数的进程内直测面）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("agentos_ci_runner", RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_tool_shim(dir_: Path, name: str, rc: int, stdout: str = "") -> str:
    """可执行替身：返回完整路径（win=.bat / posix=sh），rc 与 stdout 由测试指定。"""
    dir_.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = dir_ / f"{name}.bat"
        body = "@echo off\r\n" + (f"echo {stdout}\r\n" if stdout else "") + f"exit /b {rc}\r\n"
        path.write_text(body, encoding="utf-8")
    else:
        path = dir_ / name
        body = "#!/bin/sh\n" + (f"echo '{stdout}'\n" if stdout else "") + f"exit {rc}\n"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return str(path)


def tool_path(*extra: Path) -> str:
    """受控 PATH：仅 python + git + System32 + 指定替身目录（工具缺席判定与环境解耦）。

    System32 必带：win 上 .bat 经 cmd 运行，AutoRun（如 chcp 65001）需要系统目录。
    """
    import shutil as _shutil

    parts = [str(p) for p in extra]
    git_dir = os.path.dirname(_shutil.which("git") or "")
    if git_dir:
        parts.append(git_dir)
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if os.path.isdir(system32):
        parts.append(system32)
    parts.append(os.path.dirname(sys.executable))
    return os.pathsep.join(parts)


def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """项目内 git 操作（内联身份，不触全局配置）。"""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """干净临时 git 项目根。"""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=str(root), capture_output=True, timeout=60)
    return root
