"""持久浏览器运行时——宿主 Chromium 生命周期 + 活动页跟踪 + 直连投屏。

设计（ADR 2026-10-21-browser-live-workspace-tab）：
- Bridge 拥有浏览器进程（subprocess 直管，Chromium/Chrome headless=new +
  ``--remote-debugging-port``），playwright-mcp 上游经 ``--cdp-endpoint`` 挂接
  同一实例——工作区直播页看到的页 = 工具操作的页（严格同一实例）。
- 持久 profile（``--user-data-dir``）：登录态/Cookie/缓存跨会话记住；既有
  session_idle 回收只杀 MCP 上游进程，浏览器本体常驻（空闲不杀）。
- 直连投屏：``/browser/live.mjpeg`` 由 HTTP worker 线程裸连 CDP WebSocket
  （websocket-client）转发 ``Page.startScreencast`` 帧——不经内核中转。

浏览器进程管理走 CDP HTTP 端点（/json/version、/json/list）+ subprocess，
与 upstream.py 的上游进程管理同范式；不依赖 playwright Python 包（规避其
sync API 线程亲和约束——ThreadingHTTPServer 每请求一线程）。
"""

from __future__ import annotations

import base64
import glob
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlencode

# CDP WebSocket 客户端为可选依赖：缺失时投屏端点 503，MCP 代理面不受影响。
try:
    import websocket  # type: ignore[import-untyped]  # websocket-client

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境差异兜底
    websocket = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

# 浏览器启动等待上限（秒）：轮询 /json/version 就绪。
_START_TIMEOUT_SECS = 15.0
# 换票 TTL（秒）：一次性使用，过期作废；前端断流重连须重新换票。
_TICKET_TTL_SECS = 60.0
# 投屏无帧等待上限（秒）：静态页面 CDP 不推帧，超时重发 startScreencast 强制
# 出帧（兼当客户端断连探针——写失败即结束流）。
_FRAME_IDLE_SECS = 2.0
# 投屏总时长上限（秒）：到点结束流，前端自动换票重连（防僵尸连接累积）。
_STREAM_MAX_SECS = 300.0


class BrowserLiveError(Exception):
    """浏览器运行时不可用/请求不合法。http_status 语义同 PolicyError。"""

    def __init__(self, message: str, http_status: int = 503):
        super().__init__(message)
        self.http_status = http_status


def _http_get_json(url: str, timeout: float = 3.0) -> dict | list | None:
    """GET JSON（CDP HTTP 端点）。失败返回 None（调用方按不可用处理）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — 地址来自本机配置的调试端口
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def resolve_browser_executable(explicit: str) -> str:
    """解析浏览器可执行文件：配置显式路径 → playwright 缓存 → 系统 Chrome → Edge。

    找不到返回空串（ensure_started 报错，fail-soft——MCP 代理面不受影响）。
    """
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    candidates: list[str] = []
    # playwright 浏览器缓存（playwright-mcp npx 安装的 chromium；取版本最高者）
    for pattern in (
        os.path.join(local, "ms-playwright", "chromium-*", "chrome-win*", "chrome.exe"),
        os.path.join(home, ".cache", "ms-playwright", "chromium-*", "chrome-linux", "chrome"),
        # Playwright ≥1.44 的 Linux 产物目录名带 64 后缀（chrome-linux64）：
        # 只匹配 chrome-linux 会让 Linux 宿主解析不到浏览器。
        os.path.join(home, ".cache", "ms-playwright", "chromium-*", "chrome-linux64", "chrome"),
        os.path.join(
            home, "Library", "Caches", "ms-playwright", "chromium-*",
            "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium",
        ),
    ):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    # 系统 Chrome / Edge（Windows 常见安装位）
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        candidates.append(path)
    return next((c for c in candidates if os.path.isfile(c)), "")


def mjpeg_part(jpeg: bytes, boundary: str = "frame") -> bytes:
    """一帧 JPEG → MJPEG multipart 分段字节（纯函数，测试直驱）。"""
    return (
        f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
        + jpeg
        + b"\r\n"
    )


class BrowserRuntime:
    """持久浏览器进程 + 活动页跟踪 + 换票/投屏。

    线程模型：所有方法可从任意 HTTP worker 线程调用（状态由锁保护；进程管理
    走 subprocess + CDP HTTP，投屏每请求一线程独占一条 CDP WebSocket）。
    """

    def __init__(self, cfg: dict, bridge_port: int = 8765):
        self._enabled = bool(cfg)
        cfg = cfg or {}
        self._executable_cfg = str(cfg.get("executable", ""))
        self._profile_dir = str(cfg.get("profile_dir", "user_root/browser_profile"))
        self._debug_port = int(cfg.get("debug_port", 9223))
        self._headless = bool(cfg.get("headless", True))
        self._width = int(cfg.get("width", 1280))
        self._height = int(cfg.get("height", 800))
        live = cfg.get("live") or {}
        self._quality = int(live.get("quality", 60))
        self._max_width = int(live.get("max_width", 1280))
        self._max_height = int(live.get("max_height", 800))
        # 投屏对外基地址：远程部署（nginx 反代）时配置覆盖；空 = 本机直连。
        self._public_base_url = str(live.get("public_base_url", ""))
        self._bridge_port = int(bridge_port)
        self._upstream = str(cfg.get("upstream", "browser"))
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        # (session_key → {"last_activity": monotonic, "url": 导航目标或 ""})
        self._activity: dict[str, dict] = {}
        # ticket → 过期 monotonic（单次消费即删）
        self._tickets: dict[str, float] = {}

    # ── 就绪与进程 ────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def upstream(self) -> str:
        """本运行时挂接的上游名（浏览器型上游首转发前须等它就绪）。"""
        return self._upstream

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._debug_port}"

    def is_up(self) -> bool:
        return _http_get_json(f"{self._base_url()}/json/version") is not None

    def ensure_started(self) -> bool:
        """确保浏览器在跑：已在跑（本进程或遗留实例）即复用；否则拉起。"""
        if not self._enabled:
            return False
        if self.is_up():
            return True
        with self._lock:
            if self.is_up():
                return True
            executable = resolve_browser_executable(self._executable_cfg)
            if not executable:
                raise BrowserLiveError("未找到浏览器可执行文件（配置 browser.executable 或安装 Chrome/Edge/Chromium）")
            profile = os.path.abspath(self._profile_dir)
            os.makedirs(profile, exist_ok=True)
            args = [
                executable,
                *( ["--headless=new"] if self._headless else [] ),
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={self._debug_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-extensions",
                "--mute-audio",
                f"--window-size={self._width},{self._height}",
                "about:blank",
            ]
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            try:
                self._proc = subprocess.Popen(  # noqa: S603 — 可执行文件来自配置/固定安装位探测
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    **({} if os.name == "nt" else {"preexec_fn": os.setsid}),
                )
            except OSError as e:
                raise BrowserLiveError(f"浏览器进程启动失败: {e}") from e
            deadline = time.monotonic() + _START_TIMEOUT_SECS
            while time.monotonic() < deadline:
                if self.is_up():
                    return True
                if self._proc.poll() is not None:
                    raise BrowserLiveError(f"浏览器进程退出（code={self._proc.returncode}）")
                time.sleep(0.3)
            raise BrowserLiveError(f"浏览器调试端口 {self._debug_port} 就绪超时")

    def shutdown(self) -> None:
        """杀本进程拉起的浏览器（遗留实例不追杀——可能是复用的外部浏览器）。"""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(  # noqa: S603 — 固定参数杀自身子进程
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=5
                )
            else:
                os.killpg(os.getpgid(proc.pid), 15)
        except (OSError, subprocess.SubprocessError, ProcessLookupError):
            pass

    # ── 活动页跟踪（handle_mcp 回写）──────────────────────────

    def note_activity(self, upstream: str, session_key: str, tool: str, args: dict) -> None:
        """tools/call 回写：任意调用刷新会话活跃度；browser_navigate 记录导航 URL。

        直播页解析的会话归属与 URL 匹配依据（v1 启发式，ADR 记录边界）。
        """
        if not self._enabled or upstream != self._upstream:
            return
        with self._lock:
            entry = self._activity.setdefault(session_key, {"last_activity": 0.0, "url": ""})
            entry["last_activity"] = time.monotonic()
            if tool == "browser_navigate":
                url = args.get("url")
                entry["url"] = url if isinstance(url, str) else ""

    def list_pages(self) -> list[dict]:
        """CDP /json/list 的页面型目标（type==page）。不可用返回空表。"""
        data = _http_get_json(f"{self._base_url()}/json/list")
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict) and t.get("type") == "page" and t.get("id")]

    def resolve_active_page(self) -> dict | None:
        """活动页解析：最近活跃会话的导航 URL 精确匹配 → 首个非空白页（启发式兜底）。"""
        pages = self.list_pages()
        if not pages:
            return None
        with self._lock:
            latest = max(self._activity.items(), key=lambda kv: kv[1]["last_activity"], default=None)
            expected = latest[1]["url"] if latest else ""
        if expected:
            for page in pages:
                if page.get("url") == expected:
                    return page
        for page in pages:
            if page.get("url") and page.get("url") != "about:blank":
                return page
        return pages[0]

    # ── 换票与投屏 ────────────────────────────────────────────

    def open_ticket(self, session_key: str) -> dict:
        """签发一次性直播票（Bearer 面调用；票面仅供 /browser/live.mjpeg 消费）。"""
        if not _WS_AVAILABLE:
            raise BrowserLiveError("websocket-client 未安装（pip install websocket-client），投屏不可用")
        self.ensure_started()
        page = self.resolve_active_page()
        base = self._public_base_url.rstrip("/") or f"http://127.0.0.1:{self._bridge_port}"
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._tickets = {t: exp for t, exp in self._tickets.items() if exp > now}
            self._tickets[ticket] = now + _TICKET_TTL_SECS
        query = urlencode({"ticket": ticket})
        return {
            "ticket": ticket,
            "mjpeg_url": f"{base}/browser/live.mjpeg?{query}",
            "session": session_key,
            "page_url": (page or {}).get("url") or "",
        }

    def consume_ticket(self, ticket: str) -> bool:
        """校验并消费一次性票（过期/不存在/已用 → False）。"""
        with self._lock:
            expires = self._tickets.pop(ticket, None)
        return expires is not None and expires > time.monotonic()

    def stream_mjpeg(self, wfile, boundary: str = "frame") -> None:
        """阻塞转发 CDP screencast → MJPEG 到 wfile（HTTP worker 线程内运行）。

        结束条件：客户端断连（写失败）/ 页面目标关闭 / 总时长到点。
        帧间静默（静态页面）由 _FRAME_IDLE_SECS 超时重发 startScreencast 破除。
        """
        if not _WS_AVAILABLE:
            raise BrowserLiveError("websocket-client 未安装，投屏不可用")
        page = self.resolve_active_page()
        if page is None:
            raise BrowserLiveError("浏览器无可用页面（等待 agent 首次导航）", http_status=409)
        # 抑制 Origin 头：websocket-client 默认自动补 `Origin: http://{host}`，
        # 现代 Chrome/Edge 对携带 Origin 的 CDP WS 握手强制
        # --remote-allow-origins 白名单校验（403）；非浏览器客户端不带 Origin
        # 即允许——直连本机调试端口的合规形态。
        ws = websocket.create_connection(  # type: ignore[union-attr]
            f"ws://127.0.0.1:{self._debug_port}/devtools/page/{page['id']}",
            timeout=_FRAME_IDLE_SECS,
            suppress_origin=True,
        )
        start = time.monotonic()
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            start_params = json.dumps({
                "id": 2,
                "method": "Page.startScreencast",
                "params": {
                    "format": "jpeg",
                    "quality": self._quality,
                    "maxWidth": self._max_width,
                    "maxHeight": self._max_height,
                    "everyNthFrame": 1,
                },
            })
            ws.send(start_params)
            while time.monotonic() - start < _STREAM_MAX_SECS:
                try:
                    raw = ws.recv()
                except (websocket.WebSocketTimeoutException, TimeoutError):  # type: ignore[union-attr]
                    # 静默期强制出帧：重发 startScreencast 立即产一帧（兼探客户端存活——
                    # 客户端已断时下一帧写入失败退出）。
                    ws.send(start_params)
                    continue
                if not isinstance(raw, (str, bytes)):
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if msg.get("method") != "Page.screencastFrame":
                    continue
                params = msg.get("params") or {}
                session_id = params.get("sessionId")
                data = params.get("data")
                if session_id is not None:
                    ws.send(json.dumps({"method": "Page.screencastFrameAck", "params": {"sessionId": session_id}}))
                if not isinstance(data, str) or not data:
                    continue
                try:
                    jpeg = base64.b64decode(data)
                except (ValueError, TypeError):
                    continue
                wfile.write(mjpeg_part(jpeg, boundary))
                wfile.flush()
        finally:
            try:
                ws.close()
            except OSError:
                pass
