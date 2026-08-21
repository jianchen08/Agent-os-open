#!/usr/bin/env python3
"""语言域装载插件（P5 第一匹）：Python 插件安装/依赖管理，uv 后端。

职责边界（内核只留契约层与编排，本插件回答"包怎么到位、语言依赖怎么解析、
产物/入口怎么交"）：

- ``packaging.python.install(package_dir)``：``uv sync`` 装进 sidecar venv +
  生成 ``uv.lock``（哈希完整锁定，uninstall 可整删）；
- ``packaging.python.uninstall(package_dir)``：删 ``.venv`` + ``uv.lock``
  （uv remove 语义收敛为环境整删，包目录本身由调用方管理）；
- ``packaging.python.resolve_dependencies(package_dir)``：对比 pyproject 声明依赖
  是否已被 uv 解析安装（.venv+uv.lock 就绪且 uv sync 幂等通过 = satisfied），
  未安装过诚实标"resolved:false + missing 全量"，不假装绿；
- ``packaging.python.status(package_dir)``：uv 版本 / .venv / uv.lock / 声明依赖。

后端唯一依赖 = 外部成熟包管理器 **uv**（不重复造安装器/版本解析/锁文件）；
异常全部 fail-closed 返回 ``{ok:false, error}``，不向调用方抛链上崩溃。

调用方经 capability 服务面 ``packaging.python.*`` 委托（``requires_services``
可声明 ``packaging.python`` 角色或具体端点）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("python_packager")


def _uv() -> str:
    """uv 可执行文件（PATH 查找，可被 PYTHON_PACKAGER_UV 环境变量显式覆盖）。"""
    return os.environ.get("PYTHON_PACKAGER_UV", "uv")


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> dict[str, Any]:
    """跑 uv 子进程；异常/非零退出 → 带 error 返回（fail-closed，不抛链上崩溃）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": f"找不到可执行文件: {cmd[0]}（uv 未安装或 PYTHON_PACKAGER_UV 无效）",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"命令超时({timeout}s): {' '.join(cmd)}"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "rc": proc.returncode,
            "error": (proc.stderr or proc.stdout or "").strip()[:2000],
        }
    return {"ok": True, "rc": 0, "stdout": (proc.stdout or "").strip()[:4000]}


# ── 包目录边界（安全审查 2026-08-19 A-5）──────────────────────────────
# packaging.python.* 四个工具都能让 uv 按任意目录下的 pyproject.toml 安装
# 依赖到该目录（等价于执行任意声明依赖的安装脚本）。包目录只允许：
#   1. 本项目 plugins/**（语言域装载插件的语义边界——本插件即"Python 包装载"）；
#   2. 环境变量 PYTHON_PACKAGER_ALLOWED_DIRS（os.pathsep 分隔）显式列出（逃逸舱）。
# 其余一律 fail-closed 拒绝。定位优先级：AGENTOS_PROJECT_ROOT（sidecar 若注入）
# → 本文件位置推导（始终成立）。


def _project_root() -> Path:
    env = os.environ.get("AGENTOS_PROJECT_ROOT", "")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[4]


def _allowed_extra_dirs() -> list[Path]:
    raw = os.environ.get("PYTHON_PACKAGER_ALLOWED_DIRS", "")
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]


def _is_within(target: Path, base: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _check_package_dir(package_dir: str) -> tuple[Path | None, str]:
    """包目录边界校验；非法/越界 → (None, 原因)。"""
    try:
        d = Path(package_dir).resolve()
    except (OSError, ValueError):
        return None, f"非法路径: {package_dir!r}"
    if not d.is_dir():
        return None, f"目录不存在: {d}"
    plugins_root = _project_root() / "plugins"
    if _is_within(d, plugins_root):
        return d, ""
    for extra in _allowed_extra_dirs():
        if _is_within(d, extra):
            return d, ""
    return (
        None,
        f"package_dir 不在允许范围（仅项目 plugins/** 或 PYTHON_PACKAGER_ALLOWED_DIRS 白名单；实际: {d}）",
    )


def _declared_dependencies(package_dir: Path) -> list[str]:
    """pyproject [project].dependencies（缺失 pyproject/无该字段 → 空清单/报错）。"""
    pyproject = package_dir / "pyproject.toml"
    if not pyproject.exists():
        raise FileNotFoundError(f"不是 uv 包（缺 pyproject.toml）: {pyproject}")
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    project = data.get("project") or {}
    return [str(d) for d in (project.get("dependencies") or [])]


@plugin.tool(
    name="packaging.python.install",
    schema={
        "type": "object",
        "properties": {
            "package_dir": {
                "type": "string",
                "description": "含 pyproject.toml 的插件/包目录绝对路径",
            },
        },
        "required": ["package_dir"],
    },
    description="uv sync 安装 Python 包依赖到 sidecar venv，生成 uv.lock（哈希完整锁定）",
)
async def install(package_dir: str) -> dict[str, Any]:
    d, err = _check_package_dir(package_dir)
    if err:
        return {"ok": False, "error": err}
    assert d is not None  # _check_package_dir 约定：err 非空 ⟺ d 为 None
    try:
        declared = _declared_dependencies(d)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    if declared and shutil.which(_uv()) is None:
        return {
            "ok": False,
            "error": "pyproject 声明了依赖但 uv 不可用（PYTHON_PACKAGER_UV 指向无效）",
        }
    res = _run([_uv(), "sync", "--project", str(d)], d)
    if not res["ok"]:
        return res
    venv = d / ".venv"
    lock = d / "uv.lock"
    return {
        "ok": True,
        "package_dir": str(d),
        "venv": str(venv),
        "lock": str(lock),
        "venv_exists": venv.is_dir(),
        "lock_present": lock.is_file(),
        "declared_dependencies": declared,
    }


@plugin.tool(
    name="packaging.python.uninstall",
    schema={
        "type": "object",
        "properties": {
            "package_dir": {
                "type": "string",
                "description": "含 pyproject.toml 的插件/包目录绝对路径",
            },
        },
        "required": ["package_dir"],
    },
    description="卸载：删除插件 .venv 与 uv.lock（uv remove 语义收敛为环境整删）",
)
async def uninstall(package_dir: str) -> dict[str, Any]:
    d, err = _check_package_dir(package_dir)
    if err:
        return {"ok": False, "error": err}
    assert d is not None  # _check_package_dir 约定：err 非空 ⟺ d 为 None
    removed: list[str] = []
    for part in (d / ".venv", d / "uv.lock"):
        if part.exists() or part.is_symlink():
            shutil.rmtree(part) if part.is_dir() else part.unlink()
            removed.append(str(part))
    return {
        "ok": True,
        "package_dir": str(d),
        "removed": removed,
        "venv_exists": (d / ".venv").exists(),
        "lock_exists": (d / "uv.lock").exists(),
    }


@plugin.tool(
    name="packaging.python.resolve_dependencies",
    schema={
        "type": "object",
        "properties": {
            "package_dir": {
                "type": "string",
                "description": "含 pyproject.toml 的插件/包目录绝对路径",
            },
        },
        "required": ["package_dir"],
    },
    description="对比 pyproject 声明依赖是否已被 uv 解析安装",
)
async def resolve_dependencies(package_dir: str) -> dict[str, Any]:
    d, err = _check_package_dir(package_dir)
    if err:
        return {"ok": False, "error": err}
    assert d is not None  # _check_package_dir 约定：err 非空 ⟺ d 为 None
    try:
        declared = _declared_dependencies(d)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    if not d.joinpath("uv.lock").is_file() or not d.joinpath(".venv").is_dir():
        # 未安装过 → 诚实标"未解析"，声明依赖全部计为缺（不假装绿）。
        return {"ok": True, "satisfied": [], "missing": declared, "resolved": False}
    res = _run([_uv(), "sync", "--project", str(d)], d)  # 幂等：已满足则秒过
    if not res["ok"]:
        return {
            "ok": True,
            "satisfied": [],
            "missing": declared,
            "resolved": False,
            "error": res.get("error"),
        }
    return {"ok": True, "satisfied": declared, "missing": [], "resolved": True}


@plugin.tool(
    name="packaging.python.status",
    schema={
        "type": "object",
        "properties": {
            "package_dir": {
                "type": "string",
                "description": "含 pyproject.toml 的插件/包目录绝对路径",
            },
        },
        "required": ["package_dir"],
    },
    description="环境状态：uv 版本 / .venv / uv.lock / 声明依赖清单",
)
async def status(package_dir: str) -> dict[str, Any]:
    d, err = _check_package_dir(package_dir)
    if err:
        return {"ok": False, "error": err}
    assert d is not None  # _check_package_dir 约定：err 非空 ⟺ d 为 None
    try:
        declared = _declared_dependencies(d)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    ver = _run([_uv(), "--version"], d)
    return {
        "ok": True,
        "package_dir": str(d),
        "uv_version": (ver.get("stdout") or "").strip() if ver.get("ok") else None,
        "uv_available": bool(ver.get("ok")),
        "venv_exists": (d / ".venv").is_dir(),
        "lock_exists": (d / "uv.lock").is_file(),
        "declared_dependencies": declared,
    }


if __name__ == "__main__":
    plugin.run()
