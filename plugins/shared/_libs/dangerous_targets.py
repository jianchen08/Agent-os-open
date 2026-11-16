"""危险目标空间校验单源（isolation / task_submit 共用）。

历史为两消费方各自的逐字双份（``_DANGEROUS_WINDOWS_DIRS`` +
``_DANGEROUS_UNIX_DIRS`` 两清单 + normpath/lower 装配循环），复制必漂移；
2026-10-20 多端平台差异普查（⚠️-4）收敛于此，同步义务随复制件消亡。
盘符根判定以 isolation 版为准——**按路径形态而非运行平台**：sidecar 在
Linux 容器消费 Windows 项目路径时 ``os.name`` 是 posix，但 "C:\\" 形态的
盘符根同样必须拒（task_submit 原按 ``os.name`` 判定，Linux 侧存在漏判洞，
随单源化顺带修复）。
"""

from __future__ import annotations

import os

# ── 危险目标空间目录列表 ──
# 这些目录是操作系统关键目录，绝不允许作为任务/会话的目标工作空间。
DANGEROUS_WINDOWS_DIRS: list[str] = [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
]

DANGEROUS_UNIX_DIRS: list[str] = [
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/tmp",
    "/home",
    "/opt",
]

# 装配（normpath + lower 归一，原两消费方循环装配同语义）
DANGEROUS_DIRS: set[str] = {
    os.path.normpath(_d).lower() for _d in DANGEROUS_WINDOWS_DIRS + DANGEROUS_UNIX_DIRS
}


def check_root_and_system_dirs(normalized: str, original: str) -> str | None:
    """磁盘根目录 + 系统危险目录检查（形态驱动，无平台分支）。

    Args:
        normalized: ``os.path.normpath`` 归一后的路径
        original: 原始输入（错误消息回显用）

    Returns:
        拒绝返回错误信息字符串；放行返回 None。
    """
    # ── 磁盘根目录检查 ──
    # 判定基准 = 路径形态而非运行平台：任务 workspace 可能来自 Windows 项目
    # （sidecar 在 Linux 容器消费），此时 os.name 是 posix 但 "C:\" 形态的
    # 盘符根目录同样必须拒绝；反之亦然。
    if len(normalized) == 3 and normalized[1] == ":" and normalized[2] in ("\\", "/"):
        return f"目标空间不能设置为磁盘根目录: {original}。请指定具体的项目子目录。"
    if normalized == "/" or (len(normalized) == 2 and normalized[1] == ":"):
        return f"目标空间不能设置为根目录: {original}。请指定具体的项目子目录。"

    # ── 系统危险目录检查 ──
    if normalized.lower() in DANGEROUS_DIRS:
        return f"目标空间不能设置为系统目录: {original}。系统关键目录不允许作为任务的工作空间。"

    return None
