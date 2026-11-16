"""MCP Bridge 网关——HTTP 入口与路由。

POST /mcp/{upstream}          转发 JSON-RPC（initialize / tools/list / tools/call）
GET  /browser/live-ticket     直播换票（Bearer 鉴权；一次性短时票）
GET  /browser/live.mjpeg      直播投屏（票鉴权；CDP screencast → MJPEG 长流）
GET  /health                  存活探针（无鉴权）
认证/白名单/审计见 policy.py；上游子进程管理见 upstream.py；
持久浏览器运行时（直播画面来源）见 browser_runtime.py。

配置来源：AGENTOS_BRIDGE_CONFIG 或 config/bridge/bridge.yaml（仓库布局推导）。
会话 key 来自请求头 X-Bridge-Session（沙箱内客户端用 session_id，跨会话
浏览器实例隔离）；caller 来自 X-Bridge-Caller（host|sandbox，仅审计字段）。

MCP 握手：网关对 client 侧 initialize 自答（回 protocolVersion/capabilities），
转发 tools/* 前 lazily 与上游完成真实 initialize 握手。initialize 是会话级
状态（同 key 的后续请求复用已握手进程）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from browser_runtime import BrowserLiveError, BrowserRuntime
from policy import BridgePolicy, PolicyError
from upstream import UpstreamError, UpstreamManager

CONFIG_PATHS = (
    os.environ.get("AGENTOS_BRIDGE_CONFIG"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "bridge", "bridge.yaml"),
)

# JSON-RPC 请求体的最大字节数。
MAX_BODY_BYTES = 512 * 1024


def load_config() -> dict:
    """按候选路径加载 bridge.yaml；找不到/解析失败抛异常（fail-closed 启动）。"""
    import yaml

    for path in CONFIG_PATHS:
        if not path:
            continue
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data
    tried = [p for p in CONFIG_PATHS if p]
    raise RuntimeError(f"bridge 配置未找到: {tried}")


class BridgeApp:
    """网关应用逻辑（HTTP handler 薄壳，便于测试直驱）。"""

    def __init__(self, config: dict, policy: BridgePolicy | None = None, manager: UpstreamManager | None = None):
        self.config = config
        self.policy = policy or BridgePolicy.from_config(config)
        idle = float(config.get("session_idle_timeout_secs", 300))
        self.manager = manager or UpstreamManager(idle_timeout_secs=idle)
        # 持久浏览器运行时（config browser 段缺省 = 关闭，MCP 代理面不受影响）。
        self.live = BrowserRuntime(
            config.get("browser") or {}, bridge_port=int(config.get("port", 8765))
        )
        # (upstream, session) → 已与上游完成 initialize 握手。
        self._handshaken: set[tuple[str, str]] = set()
        self._handshake_lock = threading.Lock()

    # ── 请求处理 ──────────────────────────────────────────────

    def handle_mcp(
        self, upstream: str, body: bytes, auth_header: str | None, session_key: str, caller: str
    ) -> tuple[int, dict]:
        """处理 /mcp/{upstream}。返回 (http_status, json_body)。"""
        started = time.monotonic()
        try:
            self.policy.check_auth(auth_header)
        except PolicyError as e:
            self.policy.audit(upstream, caller, session_key, "auth", "", "deny", str(e), 0.0)
            return e.http_status, {"error": str(e)}

        try:
            request = json.loads(body.decode("utf-8"))
            method = str(request.get("method", ""))
            params = request.get("params") or {}
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.policy.audit(upstream, caller, session_key, "parse", "", "deny", str(e), 0.0)
            return 400, {"error": f"请求体不是合法 JSON: {e}"}

        tool = ""
        code = 200
        body_out: dict
        decision = "allow"
        reason = ""
        try:
            spec = self.policy.upstream_policy(upstream)  # 未知上游 404
            if not method:
                raise PolicyError("缺少 method", http_status=400)
            if method == "initialize":
                # client 侧握手：网关自答（上游握手延迟到首个转发请求前）。
                body_out = {
                    "result": {
                        "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": f"mcp-bridge:{upstream}", "version": "0.1.0"},
                    }
                }
                self.policy.audit(upstream, caller, session_key, method, "", "allow", "", 0.0)
                return 200, body_out
            session = self.manager.get_or_create(upstream, session_key, [spec.command] + spec.args, spec.cwd)
            key = (upstream, session_key)
            handshaken = key in self._handshaken
            if not handshaken:
                with self._handshake_lock:
                    if key not in self._handshaken:
                        self._upstream_handshake(session)
                        self._handshaken.add(key)
            if method == "tools/call":
                tool = str(params.get("name", ""))
                self.policy.check_tool(upstream, params)
            result = session.request(method, params if params else None)
            if method == "tools/call":
                args = (params.get("arguments") or {}) if isinstance(params.get("arguments"), dict) else {}
                self.live.note_activity(upstream, session_key, tool, args)
            body_out = {"result": result}
        except PolicyError as e:
            code, body_out, decision, reason = e.http_status, {"error": str(e)}, "deny", str(e)
        except UpstreamError as e:
            # 上游失败：丢弃该会话进程（下次调用重建），返回 502。
            self.manager.discard(upstream, session_key)
            self._handshaken.discard((upstream, session_key))
            code, body_out, decision, reason = 502, {"error": f"上游不可用: {e}"}, "deny", str(e)

        self.policy.audit(
            upstream, caller, session_key, method, tool, decision, reason,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
        return code, body_out

    def _upstream_handshake(self, session) -> None:
        """与上游 MCP server 完成 initialize 握手。"""
        session.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-bridge", "version": "0.1.0"},
        })
        session.notify("notifications/initialized")

    def handle_live_ticket(self, auth_header: str | None, session_key: str) -> tuple[int, dict]:
        """处理 GET /browser/live-ticket（Bearer 鉴权，签发一次性投屏票）。"""
        try:
            self.policy.check_auth(auth_header)
        except PolicyError as e:
            return e.http_status, {"error": str(e)}
        if not self.live.enabled:
            return 404, {"error": "浏览器运行时未启用（配置 browser 段）"}
        try:
            return 200, self.live.open_ticket(session_key)
        except BrowserLiveError as e:
            return e.http_status, {"error": str(e)}

    def consume_live_ticket(self, path: str) -> tuple[bool, str]:
        """校验 /browser/live.mjpeg?ticket= 的票。返回 (ok, 错误信息)。"""
        if not self.live.enabled:
            return False, "浏览器运行时未启用（配置 browser 段）"
        ticket = (parse_qs(urlparse(path).query).get("ticket") or [""])[0]
        if not ticket or not self.live.consume_ticket(ticket):
            return False, "直播票无效或已过期（请重新换票）"
        return True, ""

    def start_browser_background(self) -> None:
        """后台拉起持久浏览器（fail-soft：失败仅打日志，MCP 面自会 502 可见）。"""

        def _start() -> None:
            try:
                self.live.ensure_started()
            except BrowserLiveError as e:
                print(f"[mcp-bridge] 浏览器运行时启动失败（直播与工具面将不可用）: {e}", flush=True)

        threading.Thread(target=_start, daemon=True).start()

    def sweep(self) -> list[tuple[str, str]]:
        """回收空闲会话并清握手簿记（sweep 线程周期调用）。"""
        reclaimed = self.manager.sweep_idle()
        for key in reclaimed:
            self._handshaken.discard(key)
        return reclaimed

    def discard(self, upstream: str, session_key: str) -> None:
        self.manager.discard(upstream, session_key)
        self._handshaken.discard((upstream, session_key))


# ── HTTP 层 ──────────────────────────────────────────────────

def make_handler(app: BridgeApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mcp-bridge/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默默认访问日志（审计面已覆盖）
            pass

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok", "sessions": app.manager.session_count()})
                return
            if self.path.split("?")[0] == "/browser/live-ticket":
                session_key = (self.headers.get("X-Bridge-Session") or "").strip() or "default"
                code, payload = app.handle_live_ticket(self.headers.get("Authorization"), session_key)
                self._send(code, payload)
                return
            if self.path.split("?")[0] == "/browser/live.mjpeg":
                self._handle_live_mjpeg()
                return
            self._send(404, {"error": "not found"})

        def _handle_live_mjpeg(self) -> None:
            """MJPEG 长流：票校验后阻塞转发 CDP screencast（票一次性，流断重换）。"""
            ok, reason = app.consume_live_ticket(self.path)
            if not ok:
                self._send(403, {"error": reason})
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            try:
                app.live.stream_mjpeg(self.wfile)
            except BrowserLiveError as e:
                # 流中途失败：补一段错误 JSON 收尾（客户端 img onerror 触发重连换票）。
                try:
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                except OSError:
                    pass
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # 客户端断开（关页签/刷新），正常结束

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "mcp":
                self._send(404, {"error": "not found"})
                return
            upstream = parts[1]
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"error": f"请求体超限 > {MAX_BODY_BYTES}"})
                return
            body = self.rfile.read(length) if length else b""
            session_key = (self.headers.get("X-Bridge-Session") or "").strip() or "default"
            caller = (self.headers.get("X-Bridge-Caller") or "").strip() or "host"
            code, payload = app.handle_mcp(
                upstream, body, self.headers.get("Authorization"), session_key, caller
            )
            self._send(code, payload)

    return Handler


def make_server(config: dict | None = None, config_path: str | None = None) -> ThreadingHTTPServer:
    """构建 HTTP server（测试注入 config 直建；生产走 serve()）。"""
    cfg = config if config is not None else load_config()
    app = BridgeApp(cfg)
    return ThreadingHTTPServer(
        (str(cfg.get("bind", "127.0.0.1")), int(cfg.get("port", 8765))),
        make_handler(app),
    )


def serve() -> None:
    """启动网关（阻塞）。sweep 线程周期回收空闲会话；浏览器后台拉起。"""
    config = load_config()
    app = BridgeApp(config)
    sweep_secs = max(15.0, float(config.get("session_idle_timeout_secs", 300)) / 4)

    def _sweep() -> None:
        while True:
            time.sleep(sweep_secs)
            app.sweep()

    threading.Thread(target=_sweep, daemon=True).start()
    if app.live.enabled:
        app.start_browser_background()
    bind = str(config.get("bind", "127.0.0.1"))
    port = int(config.get("port", 8765))
    server = ThreadingHTTPServer((bind, port), make_handler(app))
    print(f"[mcp-bridge] listening on {bind}:{port}, audit={app.policy.audit_dir}", flush=True)
    try:
        server.serve_forever()
    finally:
        app.manager.shutdown_all()
        app.live.shutdown()


if __name__ == "__main__":
    serve()
