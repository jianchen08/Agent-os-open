"""WebSocket 连接修复回归测试。

BUG-FIX-fix_20260624_ws_connection_reliability:
覆盖四个修复点：
1. register_global 用 running loop 而不是 get_event_loop（避免老连接 close
   被丢到不运行的 loop 里）。
2. send_text 超时从硬编码 5s 改为模块常量 _SEND_TIMEOUT_SECONDS，默认 30s
   且可被环境变量 WS_SEND_TIMEOUT_SECONDS 覆盖。
3. _resolve_send_timeout 对非法值有兜底（非数字 / 负值 → 30s）。
4. 同 user 重复 register_global 同一连接对象时不应触发 close（self-replace
   保护）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


def _reload_ws_handler():
    """重新加载 ws_handler，使 _SEND_TIMEOUT_SECONDS 重新读取环境变量。"""
    import channels.websocket.ws_handler as mod
    return importlib.reload(mod)


class TestSendTimeoutResolution:
    """覆盖 _resolve_send_timeout 的所有分支。"""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WS_SEND_TIMEOUT_SECONDS", raising=False)
        mod = _reload_ws_handler()
        assert mod._resolve_send_timeout() == 30.0
        assert mod._SEND_TIMEOUT_SECONDS == 30.0

    def test_env_override_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "60")
        mod = _reload_ws_handler()
        assert mod._SEND_TIMEOUT_SECONDS == 60.0

    def test_env_invalid_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "not-a-number")
        mod = _reload_ws_handler()
        assert mod._SEND_TIMEOUT_SECONDS == 30.0

    def test_env_non_positive_falls_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "-1")
        mod = _reload_ws_handler()
        assert mod._SEND_TIMEOUT_SECONDS == 30.0

    def test_env_zero_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "0")
        mod = _reload_ws_handler()
        assert mod._SEND_TIMEOUT_SECONDS == 30.0


class TestRegisterGlobalSafeClose:
    """覆盖修复 1：register_global 调度老连接 close 的健壮性。"""

    @pytest.mark.asyncio
    async def test_old_connection_close_scheduled_on_running_loop(self) -> None:
        """同 user 出现新连接时，老连接的 close 应被调度到当前 running loop。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        old = MagicMock()
        old.close = AsyncMock()
        new = MagicMock()

        notifier.register_global("user_x", old)
        notifier.register_global("user_x", new)

        # 让事件循环跑一拍，让被调度的 close 任务真正执行
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        old.close.assert_awaited_once()
        kwargs = old.close.await_args.kwargs
        assert kwargs.get("code") == 4000
        assert "替换" in kwargs.get("reason", "")
        assert notifier._global_connections["user_x"] is new

    def test_no_running_loop_does_not_raise(self) -> None:
        """同步上下文（没有 running loop）注册不应抛异常。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        old = MagicMock()
        old.close = MagicMock(return_value=None)
        # 把它设置成可 await 的，但同步路径下不应被真的 await 起来
        new = MagicMock()
        notifier._global_connections["user_x"] = old
        # 此调用必须不抛异常
        notifier.register_global("user_x", new)
        assert notifier._global_connections["user_x"] is new

    @pytest.mark.asyncio
    async def test_same_ws_reregister_no_close(self) -> None:
        """重复 register 同一个 ws 实例不应触发 close（避免自杀）。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.close = AsyncMock()

        notifier.register_global("user_x", ws)
        notifier.register_global("user_x", ws)
        await asyncio.sleep(0)

        ws.close.assert_not_called()
        assert notifier._global_connections["user_x"] is ws


class TestSendTimeoutHonoured:
    """覆盖修复 2：发送路径使用模块常量超时。"""

    @pytest.mark.asyncio
    async def test_send_to_user_uses_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "12")
        mod = _reload_ws_handler()

        notifier = mod.WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(return_value=None)
        notifier._global_connections["u"] = ws

        captured: dict = {}
        real_wait_for = asyncio.wait_for

        async def _spy_wait_for(coro, timeout):
            captured["timeout"] = timeout
            return await real_wait_for(coro, timeout)

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy_wait_for)

        ok = await notifier.send_to_user("u", {"type": "test"})

        assert ok is True
        assert captured["timeout"] == 12.0

    @pytest.mark.asyncio
    async def test_send_to_thread_uses_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "17")
        mod = _reload_ws_handler()

        notifier = mod.WebSocketInteractionNotifier()
        ws = MagicMock()
        ws.send_text = AsyncMock(return_value=None)
        notifier.register_global("u1", ws)
        notifier.register_thread_user("t1", "u1")

        captured: list[float] = []
        real_wait_for = asyncio.wait_for

        async def _spy_wait_for(coro, timeout):
            captured.append(timeout)
            return await real_wait_for(coro, timeout)

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy_wait_for)

        ok = await notifier.send_to_thread("t1", {"type": "test"})

        assert ok is True
        assert captured and captured[0] == 17.0
