"""
Desktop notification unit tests.

Tests the OS notification dispatcher, desktop notifier, composite notifier,
and the auto-hook mechanism using mocked subprocess calls.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from human_interaction.composite_notifier import CompositeNotifier
from human_interaction.desktop_notifier import (
    DesktopInteractionNotifier,
    DesktopNotifierConfig,
)
from human_interaction.os_notification import is_supported, send_notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    title: str = "测试请求",
    description: str = "请确认",
    mode: str = "choice",
) -> dict:
    return {
        "message_data": {
            "title": title,
            "description": description,
            "interaction_mode": mode,
        },
        "request_id": "req-123",
    }


class _FakeNotifier:
    """用于测试 composite 的 mock notifier。"""

    def __init__(self, return_value: bool = True, *, raise_error: bool = False):
        self.return_value = return_value
        self.raise_error = raise_error
        self.calls: list[str] = []

    async def notify_request(self, request):
        self.calls.append("notify_request")
        if self.raise_error:
            raise RuntimeError("boom")
        return self.return_value

    async def notify_cancel(self, request_id, reason=None, thread_id=""):
        self.calls.append("notify_cancel")
        if self.raise_error:
            raise RuntimeError("boom")
        return self.return_value

    async def notify_timeout(self, request_id, thread_id=""):
        self.calls.append("notify_timeout")
        if self.raise_error:
            raise RuntimeError("boom")
        return self.return_value

    async def notify_timeout_reminder(
        self, request_id, remaining_seconds, thread_id="", **kw
    ):
        self.calls.append("notify_timeout_reminder")
        if self.raise_error:
            raise RuntimeError("boom")
        return self.return_value

    async def notify_conversation_start(
        self, thread_id, tab_id, title, **kw
    ):
        self.calls.append("notify_conversation_start")
        if self.raise_error:
            raise RuntimeError("boom")
        return self.return_value


# ---------------------------------------------------------------------------
# os_notification tests
# ---------------------------------------------------------------------------


class TestOsNotification:
    """os_notification.send_notification tests."""

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_false(self):
        with patch("human_interaction.os_notification._PLATFORM", "unsupported"):
            assert await send_notification("t", "m") is False

    @pytest.mark.asyncio
    async def test_windows_toast_called(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        with patch("human_interaction.os_notification._PLATFORM", "win32"):
            with patch(
                "human_interaction.os_notification.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec:
                result = await send_notification("Hello", "World")
                assert result is True
                mock_exec.assert_called_once()
                cmd_args = mock_exec.call_args[0]
                assert cmd_args[0] == "powershell"

    @pytest.mark.asyncio
    async def test_macos_osascript_called(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        with patch("human_interaction.os_notification._PLATFORM", "darwin"):
            with patch(
                "human_interaction.os_notification.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec:
                result = await send_notification("Title", "Msg")
                assert result is True
                cmd_args = mock_exec.call_args[0]
                assert cmd_args[0] == "osascript"

    @pytest.mark.asyncio
    async def test_linux_notify_send_called(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.returncode = 0

        with patch("human_interaction.os_notification._PLATFORM", "linux"):
            with patch(
                "human_interaction.os_notification.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec:
                result = await send_notification("Title", "Msg")
                assert result is True
                cmd_args = mock_exec.call_args[0]
                assert cmd_args[0] == "notify-send"

    @pytest.mark.asyncio
    async def test_subprocess_failure_returns_false(self):
        with patch("human_interaction.os_notification._PLATFORM", "linux"):
            with patch(
                "human_interaction.os_notification.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("no cmd"),
            ):
                result = await send_notification("T", "M")
                assert result is False

    def test_is_supported_win32(self):
        with patch("human_interaction.os_notification._PLATFORM", "win32"):
            assert is_supported() is True

    def test_is_supported_unknown(self):
        with patch("human_interaction.os_notification._PLATFORM", "freebsd"):
            assert is_supported() is False


# ---------------------------------------------------------------------------
# DesktopNotifierConfig tests
# ---------------------------------------------------------------------------


class TestDesktopNotifierConfig:
    def test_defaults(self):
        cfg = DesktopNotifierConfig()
        assert cfg.enabled is True
        assert cfg.notify_request is True
        assert cfg.notify_timeout_reminder is True
        assert cfg.notify_cancel is False

    def test_from_env(self):
        with patch.dict(os.environ, {"AGENT_OS_DESKTOP_NOTIFY": "0"}):
            cfg = DesktopNotifierConfig.from_env()
            assert cfg.enabled is False

    def test_from_env_overrides(self):
        with patch.dict(os.environ, {"AGENT_OS_DESKTOP_NOTIFY": "1"}):
            cfg = DesktopNotifierConfig.from_env(enabled=False)
            assert cfg.enabled is False


# ---------------------------------------------------------------------------
# DesktopInteractionNotifier tests
# ---------------------------------------------------------------------------


class TestDesktopNotifier:
    @pytest.mark.asyncio
    async def test_notify_request_sends_notification(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
            return_value=True,
        ):
            notifier = DesktopInteractionNotifier()
            result = await notifier.notify_request(_make_request())
            assert result is True
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["title"] == "测试请求"

    @pytest.mark.asyncio
    async def test_notify_request_disabled(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
        ):
            cfg = DesktopNotifierConfig(enabled=False)
            notifier = DesktopInteractionNotifier(cfg)
            result = await notifier.notify_request(_make_request())
            assert result is False
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_timeout_reminder_sends(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
            return_value=True,
        ):
            notifier = DesktopInteractionNotifier()
            result = await notifier.notify_timeout_reminder(
                "req-1", 120, title="确认操作"
            )
            assert result is True
            msg = mock_send.call_args[1]["message"]
            assert "2 分 0 秒" in msg
            assert "确认操作" in msg

    @pytest.mark.asyncio
    async def test_notify_cancel_disabled_by_default(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
        ):
            notifier = DesktopInteractionNotifier()
            await notifier.notify_cancel("req-1")
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_timeout_disabled_by_default(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
        ):
            notifier = DesktopInteractionNotifier()
            await notifier.notify_timeout("req-1")
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_mode_label(self):
        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send, patch(
            "human_interaction.desktop_notifier.play_alert_sound",
            new_callable=AsyncMock,
            return_value=True,
        ):
            notifier = DesktopInteractionNotifier()
            await notifier.notify_request(_make_request(mode="conversation"))
            msg = mock_send.call_args[1]["message"]
            assert "[对话模式]" in msg


# ---------------------------------------------------------------------------
# CompositeNotifier tests
# ---------------------------------------------------------------------------


class TestCompositeNotifier:
    @pytest.mark.asyncio
    async def test_delegates_to_all(self):
        a = _FakeNotifier(return_value=True)
        b = _FakeNotifier(return_value=False)
        composite = CompositeNotifier(a, b)
        result = await composite.notify_request(_make_request())
        assert result is True
        assert "notify_request" in a.calls
        assert "notify_request" in b.calls

    @pytest.mark.asyncio
    async def test_continues_on_error(self):
        a = _FakeNotifier(raise_error=True)
        b = _FakeNotifier(return_value=True)
        composite = CompositeNotifier(a, b)
        result = await composite.notify_request(_make_request())
        assert result is True
        assert "notify_request" in b.calls

    @pytest.mark.asyncio
    async def test_all_fail_returns_false(self):
        a = _FakeNotifier(return_value=False)
        b = _FakeNotifier(return_value=False)
        composite = CompositeNotifier(a, b)
        result = await composite.notify_request(_make_request())
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_reminder_delegates(self):
        a = _FakeNotifier(return_value=True)
        composite = CompositeNotifier(a)
        await composite.notify_timeout_reminder("req-1", 60, title="t")
        assert "notify_timeout_reminder" in a.calls


# ---------------------------------------------------------------------------
# Auto-hook tests
# ---------------------------------------------------------------------------


class TestAutoHook:
    def test_install_hook_patches_set_notifier(self):
        from human_interaction.service import HumanInteractionService

        # hook 在模块导入时已执行，验证 set_notifier 已被 patch
        svc = HumanInteractionService()
        fake = _FakeNotifier()

        with patch(
            "human_interaction.desktop_notifier.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ):
            svc.set_notifier(fake)
            # 内部 _notifier 应该是 CompositeNotifier（包含 fake + desktop）
            assert isinstance(svc._notifier, CompositeNotifier)

    def test_hook_disabled_by_env(self):
        from human_interaction.service import HumanInteractionService

        with patch.dict(os.environ, {"AGENT_OS_DESKTOP_NOTIFY": "0"}):
            svc = HumanInteractionService()
            fake = _FakeNotifier()
            svc.set_notifier(fake)
            # disabled 时 _notifier 应该仍是原始 fake，不是 composite
            assert svc._notifier is fake
