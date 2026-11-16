"""进程运行时传输辅助（wsl_native / linux_native argv 构建单源）。

自 plugins/shared/bash/process_manager.py 迁入（工具隔离声明化 Slice 2，
ADR 2026-10-21-tool-isolation-capability-declaration）：bash 工具与 SDK
process_runtime 消费同一构建单源；与 isolation providers 侧同语义副本的
漂移由 tests/test_wsl_native_wiring.py::TestArgvDriftPin 钉死。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── wsl_native 传输辅助（与 WslNativeProvider._wsl_argv/_map_working_dir
#    同语义；跨插件不可 import，漂移由 tests/test_wsl_native_wiring.py
#    ::TestArgvDriftPin 钉死）────────────────────────────────────


def _wsl_transport(backend: dict[str, Any]) -> list[str]:
    """传输前缀：[wsl_exe, -d distro] + 可选 [-u user]（无 --，由调用方续接）。"""
    args = [str(backend.get("wsl_exe", "wsl")), "-d", str(backend.get("distro", "Ubuntu"))]
    user = backend.get("user")
    if user:
        args.extend(["-u", str(user)])
    return args


# `<X>:/mnt/<d>/Y`（污染变体，去伪盘符前缀）或 `/mnt/<d>/Y`（反斜杠已归一）。
_WS_MOUNT_ARGV_RE = re.compile(r"^(?:[A-Za-z]:)?/mnt/([A-Za-z])/(.*)$", re.DOTALL)

# 盘符绝对形态判据（`D:/x`、`D:x` 归一后前缀；UNC 归一后以 `/` 开头）。
_WIN_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_relative_working_dir(normalized: str) -> bool:
    """归一后工作目录是否相对形态（既非 POSIX 绝对也非盘符/UNC 绝对）。"""
    return not normalized.startswith("/") and not _WIN_DRIVE_PREFIX_RE.match(normalized)


def _map_wsl_working_dir(backend: dict[str, Any], working_dir: str | None) -> str:
    r"""/workspace 约定路径 → 环境 workspace 的 WSL 路径；相对路径按 workspace 根解析。

    反斜杠形态（`\workspace`，工具层 ntpath 规整产物）同樣映射——与
    WslNativeProvider._map_working_dir 同语义（漂移钉 TestArgvDriftPin）。

    WSL 挂载形态归一：`\mnt\d\X` / `D:\mnt\d\X` 类污染形态直透
    `wsl --cd` 会被 wsl.exe 按盘符相对路径解析为不存在的目录，随后 pwd/
    报错把脏形态回灌 LLM 上下文，读链写链全线挂污。归一为合法 POSIX
    `/mnt/d/X` 后透传（wsl --cd 原生接受）。

    相对路径（`.`/`frontend`）不得直透：`wsl --cd` 只接受绝对路径，相对
    形态实测报 Wsl/E_INVALIDARG、exit -1 整批失败（2026-10-17 装机实测）。
    容器语义下相对路径以 workspace 根为基准（docker -w 同源），按
    workspace_wsl 拼接；workspace_wsl 未知（异常形态）保持原样交 wsl 显式
    失败（不猜路径）。
    """
    workspace_wsl = str(backend.get("workspace_wsl", "") or "")
    normalized = working_dir.replace("\\", "/") if working_dir else ""
    if not normalized or normalized == "/workspace":
        mapped = workspace_wsl
    elif normalized.startswith("/workspace/"):
        mapped = workspace_wsl + normalized[len("/workspace") :]
    else:
        m = _WS_MOUNT_ARGV_RE.match(normalized)
        if m:
            mapped = f"/mnt/{m.group(1).lower()}/{m.group(2)}"
        elif _is_relative_working_dir(normalized) and workspace_wsl.rstrip("/"):
            base = workspace_wsl.rstrip("/")
            rel = normalized[2:] if normalized.startswith("./") else normalized
            mapped = base if rel in ("", ".") else f"{base}/{rel}"
        else:
            mapped = working_dir or ""
    logger.info("[WslNative] map wd: in=%r normalized=%r -> out=%r", working_dir, normalized, mapped)
    return mapped


def _wsl_bridge_env_pairs() -> list[str]:
    """bridge 通路 env k=v 对（与 DockerProvider 的 -e 注入同源环境变量）。"""
    pairs: list[str] = []
    url = os.environ.get("AGENTOS_BRIDGE_URL", "")
    if url:
        pairs.append(f"AGENTOS_BRIDGE_URL={url}")
    token = os.environ.get("AGENTOS_BRIDGE_TOKEN", "")
    if token:
        pairs.append(f"AGENTOS_BRIDGE_TOKEN={token}")
    return pairs


def _wsl_runtime_env_pairs(exec_backend: dict[str, Any]) -> list[str]:
    """任务运行时 env：副作用约束进 workspace（与 WslNativeProvider 同语义）。

    发行版 home 是持久面（VHDX），pip --user / XDG / npm 缓存写进去无清理。
    注入后主流工具副作用落 workspace/.task_runtime/（NTFS，随任务空间管理）。
    workspace_wsl 空（异常形态）不注入，防 env 值指向文件系统根。
    """
    workspace_wsl = str(exec_backend.get("workspace_wsl", "") or "")
    if not workspace_wsl:
        return []
    rt = f"{workspace_wsl.rstrip('/')}/.task_runtime"
    return [
        f"XDG_CACHE_HOME={rt}/cache",
        f"npm_config_cache={rt}/npm-cache",
        f"PYTHONUSERBASE={rt}/pyuser",
    ]


def _map_linux_native_working_dir(exec_backend: dict[str, Any], working_dir: str | None) -> str:
    """/workspace 约定路径 → 环境 workspace 真实路径（与 LinuxNativeProvider
    ._map_working_dir 同语义，漂移钉 tests/plugins/tools/bash/
    test_process_manager_linux_native.py）。同路径 rw-bind 下沙箱内可见可写；
    其余绝对路径原样（根 ro-bind，无跨机转换面）；相对/空落 workspace
    （cwd 必须存在）。workspace 未知（异常形态）透传 /workspace，沙箱内
    chdir 失败即显式报错（不猜路径）。"""
    workspace = str(exec_backend.get("workspace", "") or "")
    if not working_dir:
        return workspace or "/workspace"
    normalized = working_dir.replace("\\", "/")
    if normalized == "/workspace":
        return workspace or "/workspace"
    if normalized.startswith("/workspace/"):
        return (workspace or "/workspace") + normalized[len("/workspace") :]
    if normalized.startswith("/"):
        return normalized
    return workspace or "/workspace"
