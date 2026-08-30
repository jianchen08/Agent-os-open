"""
WSL 容器自愈逻辑

从 start_web_cn.bat 提取的自动恢复逻辑，
当 WSL 出现 D-state 死锁或 cgroup 残留时，
通过 wsl --shutdown 重启 WSL 并重试。

暴露接口：
- auto_shutdown_and_retry: WSL 自动关闭重试（对标 start_web_cn.bat 的 auto_shutdown 逻辑）
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)

# 最大重试次数
_MAX_RETRIES = 3
# wsl --shutdown 后等待秒数
_SHUTDOWN_WAIT = 10


def wsl_shutdown(timeout: int = 15) -> bool:
    """执行 wsl --shutdown，带超时保护。

    通过 wsl_shutdown.ps1 执行，防止 wsl --shutdown 本身挂起。

    Args:
        timeout: 超时秒数

    Returns:
        True 表示 shutdown 执行成功
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    shutdown_script = os.path.join(script_dir, "scripts", "wsl_shutdown.ps1")

    if not os.path.exists(shutdown_script):
        logger.warning("[self_heal] wsl_shutdown.ps1 not found, skip shutdown (treat as no-op success)")
        return True

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                shutdown_script,
                "-Timeout",
                str(timeout),
            ],
            capture_output=True,
                check=False,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning("[self_heal] wsl_shutdown exception: %s", e)
        return False


def disable_known_dstate_culprits() -> None:
    """禁用已知的 D-state 死锁罪魁祸首服务。

    禁用 landscape-client、unattended-upgrades 等服务，
    防止 WSL 重启后再次被污染。
    """
    wsl_distro = os.environ.get("WSL_DISTRO", "Ubuntu")
    try:
        subprocess.run(
            [
                "wsl",
                "-d",
                wsl_distro,
                "-u",
                "root",
                "--",
                "bash",
                "-c",
                "systemctl disable landscape-client landscape-client.service "
                "unattended-upgrades 2>/dev/null; "
                "systemctl mask landscape-client landscape-client.service 2>/dev/null; true",
            ],
            capture_output=True,
                check=False,
        )
        logger.info("[self_heal] Disabled known D-state services")
    except Exception as e:
        logger.warning("[self_heal] disable_known_dstate_culprits exception: %s", e)


def auto_shutdown_and_retry(
    reason: str,
    retry_count: int,
    probe_fn=None,
) -> tuple[bool, int]:
    """WSL 自动关闭并重试。

    对标 start_web_cn.bat 第 167-186 行的 auto_shutdown 逻辑。
    当 WSL 出现问题时，执行 wsl --shutdown 然后重新探测。

    Args:
        reason: 触发自动关闭的原因
        retry_count: 当前重试次数（从 0 开始）
        probe_fn: 重试时调用的探测函数，无参数，返回 ProbeResult

    Returns:
        (是否应该继续重试, 更新后的重试次数)
    """
    retry_count += 1
    if retry_count > _MAX_RETRIES:
        logger.error(
            "[self_heal] auto wsl --shutdown retried %d times still failed, giving up. reason: %s",
            retry_count,
            reason,
        )
        return False, retry_count

    logger.warning(
        "[self_heal] %s, auto wsl --shutdown then retry (%d/%d)...",
        reason,
        retry_count,
        _MAX_RETRIES,
    )

    # 执行 wsl --shutdown
    if not wsl_shutdown():
        logger.error("[self_heal] wsl --shutdown failed")

    # 等待 WSL 内核退出
    logger.info("[self_heal] Waiting for WSL kernel to exit (~%ds)...", _SHUTDOWN_WAIT)
    time.sleep(_SHUTDOWN_WAIT)

    # 禁用 D-state 罪魁祸首
    disable_known_dstate_culprits()

    logger.info("[self_heal] Re-probing WSL response...")

    if probe_fn is not None:
        result = probe_fn()
        if result.rc == 0:
            return True, retry_count
        return True, retry_count  # 返回 True 让调用者决定是否继续

    return True, retry_count
