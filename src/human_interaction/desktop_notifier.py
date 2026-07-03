"""
桌面 OS 通知器 — 监听人机交互信号，自动触发系统桌面通知。

设计思路：
    通知信号发出（notify_request / notify_timeout_reminder）
        → 本模块作为旁路监听器自动收到信号
        → 调用 OS 桌面通知 API

使用方式：
    import human_interaction.desktop_notifier  # 一行导入即生效

配置通过环境变量控制：
    AGENT_OS_DESKTOP_NOTIFY=0|1        总开关（默认 1）
    AGENT_OS_DESKTOP_NOTIFY_SOUND=0|1  声音（默认 1）

工作原理：
    模块被导入时自动 hook HumanInteractionService.set_notifier，
    每次 set_notifier 被调用时，自动将桌面通知器作为旁路接入，
    无需修改任何现有代码。
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from human_interaction.interfaces import IInteractionNotifier
from human_interaction.os_notification import play_alert_sound, send_notification

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class DesktopNotifierConfig:
    """桌面通知配置。"""

    enabled: bool = True
    sound: bool = True
    app_name: str = "Agent OS"
    max_message_length: int = 200

    notify_request: bool = True
    notify_timeout_reminder: bool = True
    notify_cancel: bool = False
    notify_timeout: bool = False
    notify_conversation_start: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "DesktopNotifierConfig":
        """从环境变量读取配置，overrides 优先级更高。"""
        defaults: dict[str, Any] = {}
        if "AGENT_OS_DESKTOP_NOTIFY" in os.environ:
            defaults["enabled"] = os.environ["AGENT_OS_DESKTOP_NOTIFY"] == "1"
        if "AGENT_OS_DESKTOP_NOTIFY_SOUND" in os.environ:
            defaults["sound"] = os.environ["AGENT_OS_DESKTOP_NOTIFY_SOUND"] == "1"
        defaults.update(overrides)
        return cls(**defaults)


# ---------------------------------------------------------------------------
# 旁路监听器：收到信号 → 调 OS 通知
# ---------------------------------------------------------------------------


class DesktopInteractionNotifier(IInteractionNotifier):
    """监听交互信号，触发 OS 桌面通知。"""

    def __init__(self, config: DesktopNotifierConfig | None = None):
        self._config = config or DesktopNotifierConfig.from_env()

    async def notify_request(self, request: Any) -> bool:
        if not self._config.enabled or not self._config.notify_request:
            return False
        title = _extract_title(request)
        body = _build_request_body(_extract_mode(request), _extract_description(request))
        # 独立播放系统提示音，不依赖桌面通知自身的声音机制
        if self._config.sound:
            asyncio.ensure_future(play_alert_sound())
        return await send_notification(
            title=title,
            message=body,
            sound=self._config.sound,
            app_name=self._config.app_name,
        )

    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        if not self._config.enabled or not self._config.notify_timeout_reminder:
            return False
        mins, secs = divmod(remaining_seconds, 60)
        time_str = f"{mins} 分 {secs} 秒" if mins else f"{secs} 秒"
        display_title = title or "交互请求"
        # 独立播放系统提示音，不依赖桌面通知自身的声音机制
        if self._config.sound:
            asyncio.ensure_future(play_alert_sound())
        return await send_notification(
            title="超时提醒",
            message=f"「{display_title}」将在 {time_str} 后超时",
            sound=self._config.sound,
            app_name=self._config.app_name,
        )

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        if not self._config.enabled or not self._config.notify_cancel:
            return False
        body = f"请求 {request_id[:8]} 已取消"
        if reason:
            body += f"：{reason}"
        # 独立播放系统提示音，不依赖桌面通知自身的声音机制
        if self._config.sound:
            asyncio.ensure_future(play_alert_sound())
        return await send_notification(
            title="请求已取消",
            message=body,
            sound=self._config.sound,
            app_name=self._config.app_name,
        )

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        if not self._config.enabled or not self._config.notify_timeout:
            return False
        # 独立播放系统提示音，不依赖桌面通知自身的声音机制
        if self._config.sound:
            asyncio.ensure_future(play_alert_sound())
        return await send_notification(
            title="请求已超时",
            message=f"请求 {request_id[:8]} 已超时",
            sound=self._config.sound,
            app_name=self._config.app_name,
        )

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        if not self._config.enabled or not self._config.notify_conversation_start:
            return False
        body = title
        if initial_message:
            body += f"：{initial_message[: self._config.max_message_length]}"
        # 独立播放系统提示音，不依赖桌面通知自身的声音机制
        if self._config.sound:
            asyncio.ensure_future(play_alert_sound())
        return await send_notification(
            title="对话已开启",
            message=body,
            sound=self._config.sound,
            app_name=self._config.app_name,
        )


# ---------------------------------------------------------------------------
# 自动 Hook：导入即生效
# ---------------------------------------------------------------------------

_hooked = False


def install_hook() -> None:
    """
    自动 hook HumanInteractionService.set_notifier。

    每次 set_notifier 被调用时，自动将桌面通知器作为旁路接入：
    原始通知器正常工作，桌面通知器独立接收同样的信号。
    """
    global _hooked  # noqa: PLW0603
    if _hooked:
        return

    try:
        from human_interaction.composite_notifier import CompositeNotifier  # noqa: PLC0415
        from human_interaction.service import HumanInteractionService  # noqa: PLC0415

        _original_set_notifier = HumanInteractionService.set_notifier

        def _patched_set_notifier(self: Any, notifier: IInteractionNotifier) -> None:
            # 先执行原始注册
            _original_set_notifier(self, notifier)

            config = DesktopNotifierConfig.from_env()
            if not config.enabled:
                logger.debug("Desktop notification disabled by config")
                return

            # 解包已有 CompositeNotifier，防止嵌套包装
            existing_notifiers = getattr(notifier, "_notifiers", [notifier])
            non_desktop = [n for n in existing_notifiers if not isinstance(n, DesktopInteractionNotifier)]
            desktop = DesktopInteractionNotifier(config)
            composite = CompositeNotifier(*non_desktop, desktop)
            _original_set_notifier(self, composite)
            logger.info("Desktop notification hook installed (signal → OS notifier)")

        HumanInteractionService.set_notifier = _patched_set_notifier  # type: ignore[assignment]
        _hooked = True
        logger.debug("Desktop notification hook registered on HumanInteractionService")

    except Exception:
        logger.warning("Failed to install desktop notification hook", exc_info=True)


# 模块被导入时自动安装 hook
install_hook()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(request: Any) -> str:
    if isinstance(request, dict):
        return request.get("message_data", {}).get("title") or request.get("title") or "人机交互请求"
    return getattr(request, "title", None) or "人机交互请求"


def _extract_description(request: Any) -> str:
    if isinstance(request, dict):
        return request.get("message_data", {}).get("description") or request.get("description") or ""
    return getattr(request, "description", "")


def _extract_mode(request: Any) -> str:
    if isinstance(request, dict):
        mode = request.get("message_data", {}).get("interaction_mode") or request.get("mode", "")
    else:
        mode = getattr(request, "mode", "")
    return str(mode)


def _build_request_body(mode: str, desc: str) -> str:
    parts: list[str] = []
    if mode == "conversation":
        parts.append("[对话模式]")
    elif mode == "choice":
        parts.append("[选择模式]")
    if desc:
        parts.append(desc[:160])
    return " ".join(parts) if parts else "请前往应用查看"
