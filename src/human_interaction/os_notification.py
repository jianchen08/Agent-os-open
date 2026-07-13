"""
平台原生 OS 桌面通知调度器。

零外部依赖，使用系统命令发送通知：
  - Windows: PowerShell toast (Windows Runtime API) / NotifyIcon balloon fallback
  - macOS:   osascript display notification
  - Linux:   notify-send
"""

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_PLATFORM = sys.platform


def is_supported() -> bool:
    """当前平台是否支持桌面通知。"""
    return _PLATFORM in ("win32", "darwin", "linux")


async def send_notification(
    title: str,
    message: str,
    *,
    sound: bool = True,
    app_name: str = "Agent OS",
) -> bool:
    """
    发送 OS 桌面通知。

    Returns:
        True 发送成功或命令已执行，False 发送失败。
    """
    if not is_supported():
        logger.debug("Desktop notification not supported on %s", _PLATFORM)
        return False

    try:
        if _PLATFORM == "win32":
            return await _send_windows(title, message, sound=sound, app_name=app_name)
        if _PLATFORM == "darwin":
            return await _send_macos(title, message, sound=sound)
        if _PLATFORM == "linux":
            return await _send_linux(title, message, sound=sound)
    except Exception:
        logger.debug("Desktop notification failed", exc_info=True)
    return False


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

_WINDOWS_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null

$template = @"
<toast duration="short">
  <visual><binding template="ToastText02">
    <text id="1">{title}</text>
    <text id="2">{message}</text>
  </binding></visual>
  {audio}
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{app_id}").Show($toast)
"""

# Fallback: System.Windows.Forms.NotifyIcon balloon tip
_WINDOWS_BALLOON_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = "{title}"
$n.BalloonTipText = "{message}"
$n.Visible = $true
$n.ShowBalloonTip(5000)
Start-Sleep -Milliseconds 100
$n.Dispose()
"""


async def _send_windows(title: str, message: str, *, sound: bool = True, app_name: str = "Agent OS") -> bool:
    audio_tag = "" if sound else '<audio silent="true"/>'
    script = _WINDOWS_TOAST_PS.format(
        title=_ps_escape(title),
        message=_ps_escape(message),
        audio=audio_tag,
        app_id=_ps_escape(app_name),
    )
    ok = await _run_powershell(script)
    if ok:
        return True

    # Toast API 不可用时回退到 balloon tip
    fallback = _WINDOWS_BALLOON_PS.format(
        title=_ps_escape(title),
        message=_ps_escape(message),
    )
    return await _run_powershell(fallback)


async def _run_powershell(script: str) -> bool:
    """执行 PowerShell 脚本，返回是否成功。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=8)
        return proc.returncode == 0
    except Exception:
        logger.debug("PowerShell notification failed", exc_info=True)
        return False


def _ps_escape(s: str) -> str:
    """转义 PowerShell 字符串中的特殊字符。"""
    return s.replace('"', '`"').replace("'", "''").replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


async def _send_macos(title: str, message: str, *, sound: bool = True) -> bool:
    sound_part = ' sound name "default"' if sound else ""
    script = f'display notification "{_quote(message)}" with title "{_quote(title)}"{sound_part}'
    return await _run_cmd("osascript", "-e", script)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


async def _send_linux(title: str, message: str, *, sound: bool = True) -> bool:
    args: list[Any] = ["notify-send", title, message]
    if not sound:
        args.append("--hint")
        args.append("string:sound-name:")
    return await _run_cmd(*args)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


async def _run_cmd(*args: Any) -> bool:
    """执行外部命令，返回是否成功。"""
    try:
        str_args = [str(a) for a in args]
        proc = await asyncio.create_subprocess_exec(
            *str_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=8)
        return proc.returncode == 0
    except Exception:
        logger.debug("Notification command failed: %s", args[0], exc_info=True)
        return False


def _quote(s: str) -> str:
    """转义 shell 引号内的特殊字符。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# 独立提示音播放（不依赖桌面通知）
# ---------------------------------------------------------------------------


async def play_alert_sound() -> bool:  # noqa: PLR0911
    """
    播放系统提示音。

    使用平台原生方式播放提示音，与桌面通知解耦：
      - Windows: PowerShell [Console]::Beep(800, 300)
      - macOS:   afplay /System/Library/Sounds/Ping.aiff
      - Linux:   paplay /usr/share/sounds/freedesktop/stereo/bell.oga
                 回退到 printf '\a' (终端 BEL)

    Returns:
        True 播放成功，False 播放失败。
    """
    if not is_supported():
        return False

    try:
        if _PLATFORM == "win32":
            return await _run_powershell("[Console]::Beep(800, 300)")
        if _PLATFORM == "darwin":
            ok = await _run_cmd("afplay", "/System/Library/Sounds/Ping.aiff")
            if ok:
                return True
            # 回退到终端 BEL
            return await _run_cmd("printf", "\a")
        if _PLATFORM == "linux":
            ok = await _run_cmd(
                "paplay",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
            )
            if ok:
                return True
            # 回退到终端 BEL
            return await _run_cmd("printf", "\a")
    except Exception:
        logger.debug("Alert sound playback failed", exc_info=True)
    return False
