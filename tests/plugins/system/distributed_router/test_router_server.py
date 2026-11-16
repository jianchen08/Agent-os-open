# @feature: FP-0.2.多端同步 2c 统一路径树 | @ci: python-coverage
"""distributed_router 内部路由面测试（R24 撤四工具后：channel_ops 链契约）。

断行为（内部函数可观察输出；LLM 工具面已撤除，解析纯函数契约归
test_tree_core.py，面零新增断言归 test_router_surface.py）；relay 拉取/签发
真 HTTP 边界走本地 ThreadingHTTPServer；挂载落盘走真实文件系统 tmp_path。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import channel_ops
import pytest
from channel_ops import RouterSettings, ensure_channel_mounted, fetch_routing_view, post_exec_token

pytestmark = pytest.mark.unit

_RELAY_URL = "http://127.0.0.1:19290/ext/sync_relay"
_TOKEN = "tok-initiator"


def _settings() -> RouterSettings:
    return RouterSettings(relay_url=_RELAY_URL, token=_TOKEN, self_device_id="initiator")


def _view(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"devices": devices, "online_window_secs": 90.0, "requested_by": "initiator"}


def _dev(
    device_id: str,
    *,
    cpu: int = 4,
    online: bool = True,
    roots: list[str] | None = None,
) -> dict[str, Any]:
    caps: dict[str, Any] = {"resource": {"cpu_cores": cpu, "mem_mb": 8192}}
    if roots is not None:
        caps["exec_channel"] = {"mcp_url": "http://10.0.0.9:9201/mcp", "allowed_roots": roots}
    return {
        "device_id": device_id,
        "tier": "desktop",
        "tier_rank": 1,
        "app_version": "1.0.0",
        "online": online,
        "last_heartbeat_at": "2026-10-17T08:00:00+00:00",
        "capabilities": caps,
    }


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


class TestEnsureChannelMounted:
    """首访通道建立链（非文件类能力工具通道面，D19；原 mount 工具内部逻辑，R24 下沉）。"""

    def test_mount_writes_manifest_with_env_ref_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", "real-secret")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["mounted"] is True
        assert out["exec_token_env"] == "AGENTOS_EXEC_TOKEN_DESKTOP_B"
        assert out["online"] is True
        assert out["mcp_url"] == "http://10.0.0.9:9201/mcp", "回执带能力工具挂载出站消费面（D19）"
        plugin_dir = Path(out["plugin_dir"])
        assert plugin_dir.name == "mcp_device_desktop-b"
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["entry"] == "mcp:external"
        assert manifest["mcp"]["endpoint"]["auth"]["value"] == "${AGENTOS_EXEC_TOKEN_DESKTOP_B}"
        assert "real-secret" not in (plugin_dir / "plugin.json").read_text(encoding="utf-8")

    def test_mount_offline_device_provisions_with_honest_note(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """掉盘挂载=通道备妥+诚实注记（恢复后自动可用），不拒配置。"""
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(
            ensure_channel_mounted(
                "desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"], online=False)])
            )
        )
        assert out["mounted"] is True
        assert out["online"] is False
        assert "不可用" in out["note"]

    def test_mount_unknown_device_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(ensure_channel_mounted("ghost", _settings(), _view([])))
        assert out["success"] is False
        assert "不在路由表" in out["error"]

    def test_mount_auto_issues_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """env 未设 → 自动签发：凭据登记 relay + 供给用户空间 .env（env 引用可解析）。"""
        monkeypatch.delenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", raising=False)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        issued_calls: list[dict[str, Any]] = []

        async def _fake_issue(settings: RouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
            issued_calls.append({"relay_url": settings.relay_url, "token": settings.token, **payload})
            return {
                "issued": True,
                "target_device": "desktop-b",
                "issued_by": "initiator",
                "expires_at": "2026-10-17T09:00:00+00:00",
            }

        monkeypatch.setattr(channel_ops, "post_exec_token", _fake_issue)
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["mounted"] is True
        assert out["exec_token_env"] == "AGENTOS_EXEC_TOKEN_DESKTOP_B"
        assert out["issued"]["expires_at"] == "2026-10-17T09:00:00+00:00"
        # 签发执法链实参：sync token 鉴权 + 目标绑定 + 凭据体发起端自带
        assert issued_calls[0]["target_device"] == "desktop-b"
        assert issued_calls[0]["exec_token"]
        assert issued_calls[0]["token"] == _TOKEN
        # env 供给：凭据体落用户空间 .env（内核 connect 时 ${ENV} overlay 解析）
        supplied = issued_calls[0]["exec_token"]
        env_text = (tmp_path / "user_root" / ".env").read_text(encoding="utf-8")
        assert f"AGENTOS_EXEC_TOKEN_DESKTOP_B={supplied}" in env_text
        # P6：凭据体不进回执输出（不落对话/上下文面）
        assert supplied not in json.dumps(out)

    def test_mount_env_preset_skips_issuance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """env 已外部供给 → 沿用既有凭据，不重复签发（零多余网络面）。"""
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", "external-secret")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        issued: list[dict[str, Any]] = []

        async def _fake_issue(settings: RouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
            issued.append(payload)
            return {}

        monkeypatch.setattr(channel_ops, "post_exec_token", _fake_issue)
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["mounted"] is True
        assert issued == []

    def test_mount_issuance_failure_honest_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """签发失败（relay 不可达/目标不在注册表）→ 诚实失败：无半挂载态。"""
        monkeypatch.delenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", raising=False)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))

        async def _fake_issue(settings: RouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("relay 404: 目标设备 desktop-b 不在注册表")

        monkeypatch.setattr(channel_ops, "post_exec_token", _fake_issue)
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["success"] is False
        assert "签发" in out["error"]
        assert not (tmp_path / "user_root" / "plugins" / "mcp_device_desktop-b").exists()
        assert not (tmp_path / "user_root" / ".env").exists()

    def test_mount_protocol_version_incompatible_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """协议版本低于最低版 → 明确拒（D10a' 挂载时执法，禁静默降级）。"""
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_OLD_B", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        dev = _dev("old-b", roots=["D:\\ws"])
        dev["capabilities"]["exec_channel"]["protocol_version"] = "0.9"
        out = _run(ensure_channel_mounted("old-b", _settings(), _view([dev])))
        assert out["success"] is False
        assert "protocol_version" in out["error"]

    def test_mount_protocol_version_malformed_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """声明但畸形 → 明确报错（禁静默降级）。"""
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_ODD_B", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        dev = _dev("odd-b", roots=["D:\\ws"])
        dev["capabilities"]["exec_channel"]["protocol_version"] = "abc"
        out = _run(ensure_channel_mounted("odd-b", _settings(), _view([dev])))
        assert out["success"] is False
        assert "protocol_version" in out["error"]

    def test_mount_protocol_version_undeclared_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """未声明 = 不假拒（未知 ≠ 不兼容，D10a'）。"""
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["mounted"] is True

    def test_mount_auto_issue_without_user_root_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自动签发路径用户空间根不可得 → 凭据无处供给，诚实拒（无半挂载态）。"""
        monkeypatch.delenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", raising=False)
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)

        async def _fake_issue(settings: RouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
            return {"issued": True, "expires_at": "2026-10-17T09:00:00+00:00"}

        monkeypatch.setattr(channel_ops, "post_exec_token", _fake_issue)
        monkeypatch.setattr(channel_ops, "user_root", lambda: None)
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["success"] is False
        assert "用户空间根" in out["error"]

    def test_mount_without_exec_channel_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_NO_CHANNEL", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(ensure_channel_mounted("no-channel", _settings(), _view([_dev("no-channel")])))
        assert out["success"] is False
        assert "exec_channel" in out["error"]

    def test_mount_empty_device_id_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        out = _run(ensure_channel_mounted("", _settings(), _view([])))
        assert out["success"] is False
        assert "device_id 必填" in out["error"]

    def test_mount_and_unmount_without_plugins_root_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_EXEC_TOKEN_DESKTOP_B", "tok")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        monkeypatch.setattr(channel_ops, "user_plugins_dir", lambda: None)
        out = _run(
            ensure_channel_mounted("desktop-b", _settings(), _view([_dev("desktop-b", roots=["D:\\ws"])]))
        )
        assert out["success"] is False
        assert "用户插件根" in out["error"]


class TestFetchRoutingView:
    """真 HTTP 边界（本地 ThreadingHTTPServer）：aiohttp 拉取 + 非 2xx 显式失败。"""

    def _start_server(self, payload: bytes | None, status: int = 200) -> tuple[Any, int]:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if payload is not None:
                    self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    def test_real_roundtrip_returns_view(self) -> None:
        import json as _json

        view = _view([_dev("desk-b", roots=["D:\\ws"])])
        server, port = self._start_server(_json.dumps(view).encode("utf-8"))
        try:
            settings = RouterSettings(
                relay_url=f"http://127.0.0.1:{port}", token=_TOKEN, self_device_id="initiator"
            )
            out = _run(fetch_routing_view(settings))
            assert out["requested_by"] == "initiator"
        finally:
            server.shutdown()

    def test_non_2xx_raises_runtime_error(self) -> None:
        server, port = self._start_server(b'{"error": "boom"}', status=500)
        try:
            settings = RouterSettings(
                relay_url=f"http://127.0.0.1:{port}", token=_TOKEN, self_device_id="initiator"
            )
            with pytest.raises(RuntimeError, match="500"):
                _run(fetch_routing_view(settings))
        finally:
            server.shutdown()

    def test_post_exec_token_real_roundtrip(self) -> None:
        """签发登记出站真 HTTP（ThreadingHTTPServer）：回执 dict 原样返回。"""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                received.append(
                    {
                        "body": _json.loads(self.rfile.read(length)),
                        "auth": self.headers.get("Authorization", ""),
                    }
                )
                body = _json.dumps({"issued": True, "expires_at": "2026-10-17T09:00:00+00:00"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            settings = RouterSettings(
                relay_url=f"http://127.0.0.1:{server.server_address[1]}",
                token=_TOKEN,
                self_device_id="initiator",
            )
            out = _run(
                post_exec_token(settings, {"target_device": "desktop-b", "exec_token": "tok-xyz"})
            )
            assert out["issued"] is True
            assert received[0]["body"]["target_device"] == "desktop-b"
            assert received[0]["body"]["exec_token"] == "tok-xyz"
            # 发起端 sync token 鉴权（Bearer 头随出站请求在场）
            assert received[0]["auth"] == f"Bearer {_TOKEN}"
        finally:
            server.shutdown()

    def test_post_exec_token_non_dict_body_yields_empty(self) -> None:
        """非 dict 回执 → 空 dict（防御面：不向挂载流回传畸形形态）。"""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = b'"just-a-string"'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            settings = RouterSettings(
                relay_url=f"http://127.0.0.1:{server.server_address[1]}",
                token=_TOKEN,
                self_device_id="initiator",
            )
            assert _run(post_exec_token(settings, {})) == {}
        finally:
            server.shutdown()

    def test_post_exec_token_non_2xx_raises(self) -> None:
        """非 2xx → 显式 RuntimeError（签发失败向上传播，挂载面诚实报错）。"""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = b'{"error": "target not in registry"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            settings = RouterSettings(
                relay_url=f"http://127.0.0.1:{server.server_address[1]}",
                token=_TOKEN,
                self_device_id="initiator",
            )
            with pytest.raises(RuntimeError, match="404"):
                _run(post_exec_token(settings, {}))
        finally:
            server.shutdown()


class TestServerAssembly:
    def test_on_load_unconfigured_warns_but_loads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """装载不受阻（warning 不 raise）：离线第一，本端属地干活不依赖 relay。"""
        import asyncio
        import importlib

        monkeypatch.delenv("AGENTOS_SYNC_RELAY_URL", raising=False)
        monkeypatch.delenv("AGENTOS_SYNC_TOKEN", raising=False)
        monkeypatch.setenv("AGENTOS_DEVICE_ID", "solo")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
        import server as router_server

        module = importlib.reload(router_server)
        asyncio.run(module._on_load({}))
        assert module._settings.relay_url == ""
        assert module._settings.self_device_id == "solo"
