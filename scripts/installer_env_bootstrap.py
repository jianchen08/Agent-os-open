#!/usr/bin/env python3
"""安装期环境引导：WSL 安装/升级（MXC wslc 隔离后端前置依赖）。

设计真值：``docs/decisions/2026-11-17-installer-optional-wsl-bootstrap.md``。

调用形态（安装器在文件落盘后调用）：

    python installer_env_bootstrap.py --check          # 只检测，退出码 0=满足 / 1=缺失或过旧
    python installer_env_bootstrap.py --install-wsl    # 检测 → 必要时提权安装/升级
    python installer_env_bootstrap.py --install-wsl --json

行为契约：
- 已满足（WSL ≥3.0.5）→ 不动系统，退出 0；
- 缺失 → 提权执行 ``wsl --install --no-distribution``（不支持该 flag 时回退
  ``wsl --install``）；过旧 → ``wsl --update --web-download``（失败回退
  ``wsl --update``）；
- 安装/升级后需重启 → 退出 3010（安装器据此提示用户）；
- 用户取消 UAC → 退出 4；安装失败 → 退出 2；仍低于要求且无法继续 → 退出 3；
- 非 Windows → 退出 5（安装器只在 Windows 调用）；
- 全程日志落 ``%LOCALAPPDATA%\\agentos\\logs\\installer-env-bootstrap.log``。

提权方式：本脚本自身非提权运行，检测到需要写入系统时用 PowerShell
``Start-Process -Verb RunAs`` 拉起自身（单次 UAC，等待子进程退出并透传退出码）。
执行器（MXC ``wxc-exec.exe``）不在此脚本下载：它不是包管理器可装项，且 533MB
未签名产物需单独钉版本 + 校验，归 P0 打包刀。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

MIN_WSL_VERSION: tuple[int, int, int] = (3, 0, 5)
MIN_WSL_TEXT = "3.0.5"

EXIT_OK = 0
EXIT_INSTALL_FAILED = 2
EXIT_VERSION_UNRESOLVED = 3
EXIT_ELEVATION_CANCELLED = 4
EXIT_UNSUPPORTED_PLATFORM = 5
EXIT_REBOOT_REQUIRED = 3010

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_WSL_EXE = "wsl.exe"

Runner = Callable[..., Any]


def parse_wsl_version(text: str) -> tuple[int, int, int] | None:
    """从 ``wsl --version`` 输出提取首个 x.y.z 版本号（中英文输出均可）。"""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def decode_wsl_output(data: bytes | None) -> str:
    """wsl.exe 输出解码：自身输出为 UTF-16-LE（含 NUL），其余按 UTF-8。"""
    if not data:
        return ""
    if b"\x00" in data:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def version_text(version: tuple[int, int, int] | None) -> str | None:
    return ".".join(str(part) for part in version) if version else None


def needs_wsl(version: tuple[int, int, int] | None) -> bool:
    """None（未安装/不可解析）或低于 3.0.5 都需要安装/升级。"""
    return version is None or version < MIN_WSL_VERSION


def plan_actions(version: tuple[int, int, int] | None) -> list[str]:
    """返回应执行的动作序列：``install``（缺失）/ ``update``（过旧）/ 空（满足）。"""
    if version is None:
        return ["install"]
    if version < MIN_WSL_VERSION:
        return ["update"]
    return []


def is_admin() -> bool:
    """当前进程是否已提权（非 Windows 恒 False）。"""
    if os.name != "nt":
        return False
    try:
        windll = getattr(ctypes, "windll", None)
        return bool(windll and windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 平台探测失败按非提权处理
        return False


def log_path() -> Path:
    """安装期日志路径；用户空间不可写时退到临时目录。"""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "agentos" / "logs" if base else Path(os.environ.get("TEMP", "."))
    return root / "installer-env-bootstrap.log"


def log(message: str) -> None:
    """追加一行带时间戳的日志（失败不阻断安装）。"""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def query_wsl_version(
    runner: Runner = subprocess.run,
    timeout: float = 30.0,
) -> tuple[tuple[int, int, int] | None, str]:
    """探测 WSL 版本；返回 (版本|None, 原始输出/错误)。"""
    try:
        proc = runner(
            [_WSL_EXE, "--version"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"wsl --version 调用失败: {exc}"
    text = "\n".join(
        [
            decode_wsl_output(getattr(proc, "stdout", b"")),
            decode_wsl_output(getattr(proc, "stderr", b"")),
        ]
    ).strip()
    return parse_wsl_version(text), text


def run_wsl(
    args: list[str],
    runner: Runner = subprocess.run,
    timeout: float = 900.0,
) -> tuple[int, str]:
    """执行 wsl 子命令并返回 (退出码, 输出)。"""
    logger = log
    logger(f"exec: {_WSL_EXE} {' '.join(args)}")
    try:
        proc = runner(
            [_WSL_EXE, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger(f"exec failed: {exc}")
        return 1, str(exc)
    output = "\n".join(
        [
            decode_wsl_output(getattr(proc, "stdout", b"")),
            decode_wsl_output(getattr(proc, "stderr", b"")),
        ]
    ).strip()
    logger(f"exit={proc.returncode} output={output[:2000]}")
    return int(proc.returncode), output


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字面量（内部单引号双写）。"""
    return "'" + value.replace("'", "''") + "'"


def elevate_self(argv: list[str]) -> int:
    """通过 PowerShell ``Start-Process -Verb RunAs`` 拉起自身并等待退出码。

    使用 ``-EncodedCommand``（UTF-16LE base64）规避路径/参数的引号转义问题。
    UAC 取消时 PowerShell 非零退出，按 EXIT_ELEVATION_CANCELLED 归一。
    """
    script = str(Path(__file__).resolve())
    argument_list = ",".join(_ps_quote(str(arg)) for arg in [script, *argv])
    command = (
        f"$p = Start-Process -FilePath {_ps_quote(sys.executable)} "
        f"-ArgumentList @({argument_list}) -Verb RunAs -Wait -PassThru; "
        "exit $p.ExitCode"
    )
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    log(f"elevate: {command}")
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"elevate failed: {exc}")
        return EXIT_INSTALL_FAILED
    if proc.returncode == 1:
        log("elevate cancelled or PowerShell error (exit=1)")
        return EXIT_ELEVATION_CANCELLED
    return int(proc.returncode)


def install_or_update_wsl(
    runner: Runner = subprocess.run,
    *,
    allow_elevation: bool = True,
) -> tuple[int, dict[str, Any]]:
    """检测并安装/升级 WSL；返回 (退出码, 结果 JSON)。

    子进程参数 ``--elevated-child`` 走实际安装分支；普通调用先检测，缺失/过旧
    且未提权时提权自身一次。
    """
    before, raw_before = query_wsl_version(runner=runner)
    result: dict[str, Any] = {
        "before": version_text(before),
        "min_required": MIN_WSL_TEXT,
        "actions": [],
        "status": "ok",
    }
    if not needs_wsl(before):
        result["after"] = version_text(before)
        log(f"WSL already ok: {version_text(before)}")
        return EXIT_OK, result

    if allow_elevation and not is_admin():
        log(f"WSL needs setup (detected={version_text(before)}); elevating once")
        code = elevate_self(["--install-wsl", "--elevated-child", "--json"])
        result["status"] = (
            "elevation_cancelled"
            if code == EXIT_ELEVATION_CANCELLED
            else ("needs_reboot" if code == EXIT_REBOOT_REQUIRED else ("ok" if code == 0 else "error"))
        )
        result["exit_code"] = code
        return code, result

    actions = plan_actions(before)
    result["actions"] = actions
    install_attempted = False
    if "install" in actions:
        # --no-distribution：wslc 只需要 WSL 运行时，不需要注册发行版；
        # 老版本 wsl.exe 不认该 flag 时回退默认安装。
        code, output = run_wsl(["--install", "--no-distribution"], runner=runner)
        install_attempted = True
        if code != 0 and code not in (EXIT_REBOOT_REQUIRED, 1641):
            code, output = run_wsl(["--install"], runner=runner)
        if code in (EXIT_REBOOT_REQUIRED, 1641):
            result.update(status="needs_reboot", exit_code=EXIT_REBOOT_REQUIRED)
            return EXIT_REBOOT_REQUIRED, result
        if code != 0:
            result.update(status="error", detail=output[:500], exit_code=EXIT_INSTALL_FAILED)
            return EXIT_INSTALL_FAILED, result
    elif "update" in actions:
        code, output = run_wsl(["--update", "--web-download"], runner=runner)
        if code != 0 and code not in (EXIT_REBOOT_REQUIRED, 1641):
            code, output = run_wsl(["--update"], runner=runner)
        if code in (EXIT_REBOOT_REQUIRED, 1641):
            result.update(status="needs_reboot", exit_code=EXIT_REBOOT_REQUIRED)
            return EXIT_REBOOT_REQUIRED, result
        if code != 0:
            result.update(status="error", detail=output[:500], exit_code=EXIT_INSTALL_FAILED)
            return EXIT_INSTALL_FAILED, result

    after, raw_after = query_wsl_version(runner=runner)
    result["after"] = version_text(after)
    if not needs_wsl(after):
        result.update(status="ok", exit_code=EXIT_OK)
        log(f"WSL ready: {version_text(after)}")
        return EXIT_OK, result
    if install_attempted:
        # 首次安装后版本查询仍不可用：绝大多数是「已装好、待重启」形态。
        result.update(status="needs_reboot", exit_code=EXIT_REBOOT_REQUIRED)
        log("WSL installed but not queryable yet; assuming reboot required")
        return EXIT_REBOOT_REQUIRED, result
    result.update(
        status="version_unresolved",
        detail=(raw_after or raw_before)[:500],
        exit_code=EXIT_VERSION_UNRESOLVED,
    )
    return EXIT_VERSION_UNRESOLVED, result


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            f"WSL: before={result.get('before')} after={result.get('after')} "
            f"status={result.get('status')} min={result.get('min_required')}"
        )
        if result.get("detail"):
            print(result["detail"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentOS 安装期 WSL 环境引导")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="只检测 WSL 是否满足要求")
    group.add_argument("--install-wsl", action="store_true", help="检测并按需提权安装/升级 WSL")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-elevate", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if os.name != "nt":
        result = {"status": "unsupported_platform", "exit_code": EXIT_UNSUPPORTED_PLATFORM}
        _emit(result, args.json)
        return EXIT_UNSUPPORTED_PLATFORM

    if args.check:
        version, raw = query_wsl_version()
        ok = not needs_wsl(version)
        result = {
            "present": version is not None,
            "version": version_text(version),
            "min_required": MIN_WSL_TEXT,
            "ok": ok,
            "detail": "" if ok else raw[:500],
            "exit_code": EXIT_OK if ok else 1,
        }
        _emit(result, args.json)
        return EXIT_OK if ok else 1

    allow_elevation = not args.no_elevate and not args.elevated_child
    code, result = install_or_update_wsl(allow_elevation=allow_elevation)
    result["exit_code"] = code
    _emit(result, args.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
