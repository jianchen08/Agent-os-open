# @feature: FP-0.2.二 None | @ci: python-coverage
# @feature: browser-live-workspace-tab | @ci: python-coverage
# 持久浏览器运行时测试——换票/活动页跟踪/MJPEG 分段/投屏循环/端点接线。
#
# 不依赖真浏览器：CDP WebSocket 用假连接替身，页面列表/启动过程 monkeypatch。

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import browser_runtime  # noqa: E402
from browser_runtime import (  # noqa: E402
    BrowserLiveError,
    BrowserRuntime,
    mjpeg_part,
    resolve_browser_executable,
)
from server import BridgeApp  # noqa: E402

TOKEN = "tok-abc"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_TEST_TOKEN", TOKEN)


# ── 纯函数与解析 ──────────────────────────────────────────────


class TestPurePieces:
    def test_mjpeg_part_encodes_headers_and_length(self):
        part = mjpeg_part(b"\xff\xd8jpegdata\xff\xd9")
        assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
        assert b"Content-Length: 12\r\n\r\n" in part
        assert part.endswith(b"\xff\xd8jpegdata\xff\xd9\r\n")

    def test_resolve_executable_explicit_hit_and_miss(self, tmp_path):
        real = tmp_path / "chrome.exe"
        real.write_bytes(b"MZ")
        assert resolve_browser_executable(str(real)) == str(real)
        assert resolve_browser_executable(str(tmp_path / "missing.exe")) == ""

    def test_resolve_executable_empty_falls_through(self):
        # 空配置走自动解析：本机（有 Chrome/playwright 缓存）非空即真，断言不炸即可。
        result = resolve_browser_executable("")
        assert isinstance(result, str)


# ── 换票 ─────────────────────────────────────────────────────


class TestTickets:
    def _runtime(self, tmp_path, **over):
        cfg = {
            "profile_dir": str(tmp_path / "profile"),
            "debug_port": 19999,
        }
        cfg.update(over)
        return BrowserRuntime(cfg, bridge_port=18765)

    def test_open_ticket_shape_and_single_use(self, tmp_path, monkeypatch):
        rt = self._runtime(tmp_path)
        monkeypatch.setattr(rt, "ensure_started", lambda: True)
        monkeypatch.setattr(rt, "resolve_active_page", lambda: {"id": "t1", "url": "https://a.example"})
        info = rt.open_ticket("sess-1")
        assert info["session"] == "sess-1"
        assert info["page_url"] == "https://a.example"
        assert "/browser/live.mjpeg?ticket=" in info["mjpeg_url"]
        assert rt.consume_ticket(info["ticket"]) is True
        # 单次消费：同票第二次校验失败。
        assert rt.consume_ticket(info["ticket"]) is False

    def test_consume_unknown_ticket_rejected(self, tmp_path):
        rt = self._runtime(tmp_path)
        assert rt.consume_ticket("no-such-ticket") is False

    def test_ticket_expiry(self, tmp_path, monkeypatch):
        rt = self._runtime(tmp_path)
        monkeypatch.setattr(rt, "ensure_started", lambda: True)
        monkeypatch.setattr(rt, "resolve_active_page", lambda: None)
        info = rt.open_ticket("s")
        # 时钟拨过 TTL。
        real_mono = browser_runtime.time.monotonic
        monkeypatch.setattr(browser_runtime.time, "monotonic", lambda: real_mono() + 3600.0)
        assert rt.consume_ticket(info["ticket"]) is False

    def test_open_ticket_without_websocket_lib(self, tmp_path, monkeypatch):
        rt = self._runtime(tmp_path)
        monkeypatch.setattr(browser_runtime, "_WS_AVAILABLE", False)
        with pytest.raises(BrowserLiveError, match="websocket-client"):
            rt.open_ticket("s")


# ── 活动页跟踪 ────────────────────────────────────────────────


class TestActivityTracking:
    def _runtime(self):
        return BrowserRuntime({"debug_port": 19999}, bridge_port=18765)

    def test_disabled_runtime_ignores_activity(self):
        rt = BrowserRuntime({}, bridge_port=18765)
        rt.note_activity("browser", "s1", "browser_navigate", {"url": "https://x"})
        assert rt._activity == {}

    def test_non_browser_upstream_ignored(self):
        rt = self._runtime()
        rt.note_activity("other", "s1", "browser_navigate", {"url": "https://x"})
        assert rt._activity == {}

    def test_navigate_records_url_and_activity(self):
        rt = self._runtime()
        rt.note_activity("browser", "s1", "browser_navigate", {"url": "https://a.example"})
        rt.note_activity("browser", "s1", "browser_click", {})
        assert rt._activity["s1"]["url"] == "https://a.example"

    def test_resolve_prefers_latest_session_url_match(self, monkeypatch):
        rt = self._runtime()
        monkeypatch.setattr(rt, "list_pages", lambda: [
            {"id": "p1", "url": "https://old.example", "type": "page"},
            {"id": "p2", "url": "https://new.example", "type": "page"},
        ])
        rt.note_activity("browser", "s1", "browser_navigate", {"url": "https://old.example"})
        assert rt.resolve_active_page()["id"] == "p1"
        rt.note_activity("browser", "s2", "browser_navigate", {"url": "https://new.example"})
        assert rt.resolve_active_page()["id"] == "p2"

    def test_resolve_falls_back_to_first_non_blank(self, monkeypatch):
        rt = self._runtime()
        monkeypatch.setattr(rt, "list_pages", lambda: [
            {"id": "p1", "url": "about:blank", "type": "page"},
            {"id": "p2", "url": "https://fallback.example", "type": "page"},
        ])
        assert rt.resolve_active_page()["id"] == "p2"

    def test_resolve_no_pages_returns_none(self, monkeypatch):
        rt = self._runtime()
        monkeypatch.setattr(rt, "list_pages", lambda: [])
        assert rt.resolve_active_page() is None


# ── 投屏循环（假 WebSocket）──────────────────────────────────


class _FakeWS:
    """CDP 连接替身：startScreencast 出一帧，ack 静默，空闲抛超时。"""

    def __init__(self):
        self.sent: list[dict] = []
        self._pending: list[str] = []

    def send(self, msg: str) -> None:
        payload = json.loads(msg)
        self.sent.append(payload)
        if payload.get("method") == "Page.startScreencast":
            frame = {
                "method": "Page.screencastFrame",
                "params": {
                    "sessionId": "s1",
                    "data": base64.b64encode(b"jpeg-bytes-%d" % len(self.sent)).decode("ascii"),
                },
            }
            self._pending.append(json.dumps(frame))

    def recv(self) -> str:
        if self._pending:
            return self._pending.pop(0)
        raise TimeoutError("idle")

    def close(self) -> None:
        pass


class _BrokenAfter:
    """写 N 段后模拟客户端断连。"""

    def __init__(self, survive: int):
        self._survive = survive
        self.buf = io.BytesIO()

    def write(self, data: bytes) -> int:
        if self._survive <= 0:
            raise BrokenPipeError("client gone")
        self._survive -= 1
        return self.buf.write(data)

    def flush(self) -> None:
        pass


class TestStreamMjpeg:
    def _runtime_with_page(self, monkeypatch):
        rt = BrowserRuntime({"debug_port": 19999, "live": {"quality": 55}}, bridge_port=18765)
        monkeypatch.setattr(rt, "resolve_active_page", lambda: {"id": "target-9", "url": "https://x"})
        return rt

    def test_connect_suppresses_origin_header(self, monkeypatch):
        """CDP WS 连接必须抑制 Origin（websocket-client 默认自动补 Origin，
        现代 Chrome 对带 Origin 握手强制 --remote-allow-origins 校验即 403）。"""
        rt = self._runtime_with_page(monkeypatch)
        captured = {}

        def _conn(url, timeout=0, **kwargs):
            captured["suppress_origin"] = kwargs.get("suppress_origin")
            return _FakeWS()

        monkeypatch.setattr(browser_runtime.websocket, "create_connection", _conn)
        wfile = _BrokenAfter(survive=1)
        with pytest.raises(BrokenPipeError):
            rt.stream_mjpeg(wfile)
        assert captured["suppress_origin"] is True

    def test_streams_frames_and_acks(self, monkeypatch):
        rt = self._runtime_with_page(monkeypatch)
        fake = _FakeWS()

        def _conn(url, timeout=0, **kwargs):
            assert "/devtools/page/target-9" in url
            return fake

        monkeypatch.setattr(browser_runtime.websocket, "create_connection", _conn)
        # 写 2 段后断连 → BrokenPipeError 结束循环（server 层吞掉）。
        wfile = _BrokenAfter(survive=2)
        with pytest.raises(BrokenPipeError):
            rt.stream_mjpeg(wfile)
        out = wfile.buf.getvalue()
        assert out.count(b"--frame\r\n") == 2
        assert b"Content-Length: 12\r\n" in out  # b"jpeg-bytes-2" 长度
        # ack 已按 sessionId 回发。
        acks = [m for m in fake.sent if m.get("method") == "Page.screencastFrameAck"]
        assert acks and acks[0]["params"]["sessionId"] == "s1"
        # screencast 参数取配置（quality=55）。
        starts = [m for m in fake.sent if m.get("method") == "Page.startScreencast"]
        assert starts and starts[0]["params"]["quality"] == 55

    def test_no_page_conflict(self, monkeypatch):
        rt = BrowserRuntime({"debug_port": 19999}, bridge_port=18765)
        monkeypatch.setattr(rt, "resolve_active_page", lambda: None)
        with pytest.raises(BrowserLiveError, match="409|页面"):
            rt.stream_mjpeg(io.BytesIO())

    def test_idle_resends_screencast(self, monkeypatch):
        rt = self._runtime_with_page(monkeypatch)
        fake = _FakeWS()

        def _conn(url, timeout=0, **kwargs):
            return fake

        monkeypatch.setattr(browser_runtime.websocket, "create_connection", _conn)
        wfile = _BrokenAfter(survive=1)
        with pytest.raises(BrokenPipeError):
            rt.stream_mjpeg(wfile)
        # 首帧消费后空闲重发 startScreencast 强制出帧（sent 里 startScreencast ≥ 2）。
        starts = [m for m in fake.sent if m.get("method") == "Page.startScreencast"]
        assert len(starts) >= 2


# ── 端点接线（BridgeApp 层）──────────────────────────────────


def _app_config(tmp_path: Path, browser_cfg: dict | None) -> dict:
    return {
        "bind": "127.0.0.1",
        "port": 18765,
        "auth": {"token_env": "MCP_BRIDGE_TEST_TOKEN"},
        "audit_dir": str(tmp_path / "audit"),
        "session_idle_timeout_secs": 300,
        "browser": browser_cfg or {},
        "upstreams": {},
    }


class TestLiveEndpoints:
    def test_live_ticket_requires_auth(self, tmp_path):
        app = BridgeApp(_app_config(tmp_path, {"debug_port": 19999}))
        code, _ = app.handle_live_ticket(None, "s1")
        assert code == 401

    def test_live_ticket_disabled_404(self, tmp_path):
        app = BridgeApp(_app_config(tmp_path, None))
        code, body = app.handle_live_ticket(f"Bearer {TOKEN}", "s1")
        assert code == 404
        assert "未启用" in body["error"]

    def test_live_ticket_ok_and_consumable(self, tmp_path, monkeypatch):
        app = BridgeApp(_app_config(tmp_path, {"debug_port": 19999}))
        monkeypatch.setattr(app.live, "ensure_started", lambda: True)
        monkeypatch.setattr(app.live, "resolve_active_page", lambda: {"id": "t", "url": "https://x"})
        code, body = app.handle_live_ticket(f"Bearer {TOKEN}", "s1")
        assert code == 200
        assert body["page_url"] == "https://x"
        ok, _ = app.consume_live_ticket(f"/browser/live.mjpeg?ticket={body['ticket']}")
        assert ok is True
        ok2, reason = app.consume_live_ticket(f"/browser/live.mjpeg?ticket={body['ticket']}")
        assert ok2 is False and "重新换票" in reason

    def test_mjpeg_with_bad_ticket_denied(self, tmp_path):
        app = BridgeApp(_app_config(tmp_path, {"debug_port": 19999}))
        ok, reason = app.consume_live_ticket("/browser/live.mjpeg?ticket=bogus")
        assert ok is False

    def test_note_activity_wired_via_upstream_name(self, tmp_path):
        app = BridgeApp(_app_config(tmp_path, {"debug_port": 19999, "upstream": "browser"}))
        app.live.note_activity("browser", "s9", "browser_navigate", {"url": "https://w.example"})
        assert app.live._activity["s9"]["url"] == "https://w.example"
