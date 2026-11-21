# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""Bridge 网关生命周期（自动拉起/健康检查/凭据自给）行为单测。

被测契约（用户视角）：MCP 工具开箱即用——用户不起进程、不造密钥，
第一次调用就应成功；网关崩了下次调用自愈。断言只钉这些外部可见行为，
不钉内部实现（是否真的 fork、锁怎么加，属实现细节）。
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos_plugin_sdk import bridge_client as bc
from agentos_plugin_sdk import bridge_gateway as gw
from agentos_plugin_sdk.bridge_client import BridgeClient, BridgeClientError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 契约
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # 静音测试日志
        return


def _fake_root(tmp_path, with_script: bool = True):
    root = tmp_path / "proj"
    script = root / "mcp-servers" / "mcp-bridge" / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    if with_script:
        script.write_text("# stub gateway\n", encoding="utf-8")
    return root


# ── 地址自判（决定该不该拉起）─────────────────────────────────


@pytest.mark.parametrize(
    "base,expected",
    [
        ("http://127.0.0.1:8765", True),
        ("http://localhost:8765", True),
        ("http://[::1]:8765", True),
        ("http://0.0.0.0:8765", True),
        ("http://192.168.1.5:8765", False),
        ("http://host.docker.internal:8765", False),
        ("http://bridge.example.com:8765", False),
    ],
)
def test_is_local_base(base: str, expected: bool) -> None:
    """本机地址才自动拉起；远端地址不自作主张（避免替别人的网关兜底）。"""
    assert gw.is_local_base(base) is expected


@pytest.mark.parametrize(
    "raw,enabled",
    [("", True), ("1", True), ("0", False), ("false", False), ("OFF", False), ("no", False), ("yes", True)],
)
def test_autostart_flag(raw: str, enabled: bool, monkeypatch) -> None:
    monkeypatch.setenv(gw.AUTOSTART_ENV, raw)
    assert gw.autostart_enabled() is enabled


# ── 健康探测（走真实 HTTP，不 mock urllib）────────────────────


def test_health_true_against_live_endpoint() -> None:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert gw.health(f"http://127.0.0.1:{port}") is True
    finally:
        server.shutdown()
        server.server_close()


def test_health_false_when_nothing_listening() -> None:
    assert gw.health(f"http://127.0.0.1:{_free_port()}", timeout=1.0) is False


# ── 自动拉起 ────────────────────────────────────────────────


def test_ensure_gateway_returns_up_without_spawn_when_already_healthy(monkeypatch, tmp_path) -> None:
    """已健康即返回 up：不重复起进程（幂等）。"""
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(_fake_root(tmp_path)))
    monkeypatch.setattr(gw, "probe", lambda *a, **k: {"reachable": True, "auth_ok": True, "sessions": 0})

    def _boom(*a, **k):  # pragma: no cover - 命中即失败
        raise AssertionError("已健康时不应 spawn")

    monkeypatch.setattr(gw, "_spawn", _boom)
    assert gw.ensure_gateway("http://127.0.0.1:8765") == "up"


def test_ensure_gateway_spawns_and_reports_started(monkeypatch, tmp_path) -> None:
    """不可达 → 拉起 → 就绪后返回 started（用户看到的是一次成功调用）。"""
    root = _fake_root(tmp_path)
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    states = {"spawned": False}
    calls: list[tuple] = []

    monkeypatch.setattr(
        gw,
        "probe",
        lambda *a, **k: {"reachable": states["spawned"], "auth_ok": states["spawned"], "sessions": 0},
    )

    def _fake_spawn(r, token, log_path):
        calls.append((r, token, log_path))
        states["spawned"] = True

        class _P:
            def poll(self):
                return None

        return _P()

    monkeypatch.setattr(gw, "_spawn", _fake_spawn)
    assert gw.ensure_gateway("http://127.0.0.1:8765", token="tok-1") == "started"  # noqa: S106 — 测试占位凭据
    assert len(calls) == 1
    assert calls[0][1] == "tok-1"  # 凭据下传给子进程（网关 fail-closed 需要）
    assert calls[0][2].endswith("gateway.log")


@pytest.mark.parametrize(
    "base,env,expect_frag",
    [
        ("http://10.20.30.40:8765", "", "非本机"),
        ("http://127.0.0.1:8765", "0", "自动拉起已关闭"),
    ],
)
def test_ensure_gateway_raises_with_actionable_reason(base, env, expect_frag, monkeypatch, tmp_path) -> None:
    """拉不起来时必须讲清"为什么 + 怎么修"，而不是丢一个连接错误。"""
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(_fake_root(tmp_path)))
    monkeypatch.setenv(gw.AUTOSTART_ENV, env)
    monkeypatch.setattr(gw, "probe", lambda *a, **k: {"reachable": False, "auth_ok": False, "sessions": 0})
    monkeypatch.setattr(gw, "_spawn", lambda *a, **k: pytest.fail("不应 spawn"))
    with pytest.raises(gw.BridgeGatewayError) as exc:
        gw.ensure_gateway(base)
    assert expect_frag in str(exc.value)


def test_ensure_gateway_reports_script_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(_fake_root(tmp_path, with_script=False)))
    monkeypatch.setattr(gw, "probe", lambda *a, **k: {"reachable": False, "auth_ok": False, "sessions": 0})
    monkeypatch.chdir(tmp_path)  # 根目录推导也不能命中脚本
    with pytest.raises(gw.BridgeGatewayError) as exc:
        gw.ensure_gateway("http://127.0.0.1:8765")
    assert "找不到网关脚本" in str(exc.value)


def test_ensure_gateway_surfaces_log_tail_on_failure(monkeypatch, tmp_path) -> None:
    """拉起后仍不可达：错误里带日志尾部（排障入口，不用用户翻目录）。"""
    root = _fake_root(tmp_path)
    log = root / "logs" / "mcp-bridge" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("Traceback: ModuleNotFoundError: yaml\n", encoding="utf-8")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    monkeypatch.setenv(gw.GATEWAY_LOG_ENV, str(log))
    monkeypatch.setattr(gw, "probe", lambda *a, **k: {"reachable": False, "auth_ok": False, "sessions": 0})

    class _P:
        def poll(self):
            return 3

    monkeypatch.setattr(gw, "_spawn", lambda *a, **k: _P())
    with pytest.raises(gw.BridgeGatewayError) as exc:
        gw.ensure_gateway("http://127.0.0.1:8765", wait_secs=0.3)
    assert "退出码 3" in str(exc.value)
    assert "ModuleNotFoundError" in str(exc.value)


# ── 凭据自给 ────────────────────────────────────────────────


def test_resolve_token_prefers_explicit_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(_fake_root(tmp_path)))
    monkeypatch.setenv(gw.TOKEN_KEY, "from-env")
    assert gw.resolve_token("from-config") == "from-config"


def test_resolve_token_reads_env_then_file(monkeypatch, tmp_path) -> None:
    root = _fake_root(tmp_path)
    (root / "user_root").mkdir(parents=True, exist_ok=True)
    (root / "user_root" / ".env").write_text("AGENTOS_BRIDGE_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    assert gw.resolve_token() == "from-file"
    monkeypatch.setenv(gw.TOKEN_KEY, "from-env")
    assert gw.resolve_token() == "from-env"


def test_resolve_token_autogenerates_and_persists(monkeypatch, tmp_path) -> None:
    """凭据缺失时自生成并落用户空间 .env：网关与客户端同源，用户零操作。"""
    root = _fake_root(tmp_path)
    (root / "user_root").mkdir(parents=True, exist_ok=True)
    env_file = root / "user_root" / ".env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)

    first = gw.resolve_token()
    assert len(first) >= 32
    assert f"AGENTOS_BRIDGE_TOKEN={first}" in env_file.read_text(encoding="utf-8")
    # 幂等：同一部署再次解析拿到同一凭据（不每次换密钥导致 401）
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    assert gw.resolve_token() == first
    # 区分度：另起一份部署生成的凭据不同
    other_root = _fake_root(tmp_path / "b")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(other_root))
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    assert gw.resolve_token() != first


# ── 客户端：失败即自愈重试 ──────────────────────────────────


def _client(monkeypatch, base: str = "http://127.0.0.1:8765") -> BridgeClient:
    monkeypatch.setenv(gw.TOKEN_KEY, "tok-x")
    monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-x"))
    return BridgeClient({"bridge_base": base}, session_id="s1")


def test_client_retries_once_after_autostart(monkeypatch) -> None:
    """连接层失败 → 拉起网关 → 重试一次即成功（用户无感）。"""
    client = _client(monkeypatch)
    responses = [
        {"ok": False, "status": 0, "body": {"error": "Connection refused", "kind": "connect"}},
        {"ok": True, "status": 200, "body": {"result": {"content": [{"type": "text", "text": "hi"}]}}},
    ]
    monkeypatch.setattr(client, "_dispatch", lambda payload: responses.pop(0))
    ensured: list[tuple] = []
    monkeypatch.setattr(
        gw, "ensure_gateway", lambda base, token="": ensured.append((base, token)) or "started"
    )
    assert client.call("browser_navigate", {"url": "http://x"}) == {"content": [{"type": "text", "text": "hi"}]}
    assert ensured == [("http://127.0.0.1:8765", "tok-x")]


def test_client_reports_autostart_failure_with_cause(monkeypatch) -> None:
    """拉不起来：错误含原始连接错误 + 拉起失败原因（一次调用讲清两层）。"""
    client = _client(monkeypatch)
    monkeypatch.setattr(
        client,
        "_dispatch",
        lambda payload: {"ok": False, "status": 0, "body": {"error": "Connection refused", "kind": "connect"}},
    )

    def _fail(*a, **k):
        raise gw.BridgeGatewayError("找不到网关脚本")

    monkeypatch.setattr(gw, "ensure_gateway", _fail)
    with pytest.raises(BridgeClientError) as exc:
        client.call("browser_navigate", {"url": "http://x"})
    msg = str(exc.value)
    assert "Connection refused" in msg
    assert "自动拉起未成功" in msg
    assert "找不到网关脚本" in msg


@pytest.mark.parametrize("status", [401, 403, 502])
def test_client_does_not_autostart_on_http_errors(status: int, monkeypatch) -> None:
    """治理/上游类错误不再拉起网关（确定性结果，重试无意义）。"""
    client = _client(monkeypatch)
    monkeypatch.setattr(
        client, "_dispatch", lambda payload: {"ok": False, "status": status, "body": {"error": "denied"}}
    )
    monkeypatch.setattr(gw, "ensure_gateway", lambda *a, **k: pytest.fail("不应自动拉起"))
    with pytest.raises(BridgeClientError):
        client.call("browser_navigate", {"url": "http://x"})


def test_client_and_gateway_share_token_source(monkeypatch, tmp_path) -> None:
    """客户端解析出的凭据 == 写入 .env 的凭据（网关子进程读同源，避免 401）。"""
    root = _fake_root(tmp_path)
    (root / "user_root").mkdir(parents=True, exist_ok=True)
    (root / "user_root" / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    token = gw.resolve_token()
    assert f"AGENTOS_BRIDGE_TOKEN={token}" in (root / "user_root" / ".env").read_text(encoding="utf-8")
    assert gw.read_token_from_files(str(root)) == token


def test_client_token_comes_from_gateway_resolution(monkeypatch, tmp_path) -> None:
    """未配置 token 时客户端取自生成凭据（不再抛"未配置"，即开箱即用）。"""
    root = _fake_root(tmp_path)
    (root / "user_root").mkdir(parents=True, exist_ok=True)
    (root / "user_root" / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv(gw.PROJECT_ROOT_ENV, str(root))
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    client = BridgeClient({"bridge_base": "http://127.0.0.1:8765"}, session_id="s1")
    assert client._token == gw.read_token_from_files(str(root))
    # 配置显式给药优先（装机侧用户空间配置是合规供给面）
    assert BridgeClient._resolve_token({"token": "cfg-tok"}) == "cfg-tok"


# ── 回归面：审查 Must Fix 1（装机 resources 布局可定位网关）────────────


def test_project_root_finds_packaged_resources_layout(monkeypatch, tmp_path) -> None:
    """装机布局 <root>/resources/mcp-servers/... 也要定位到（extraResources 落位）。"""
    install = tmp_path / "install"
    script = install / "resources" / "mcp-servers" / "mcp-bridge" / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.chdir(install / "resources")
    monkeypatch.delenv(gw.PROJECT_ROOT_ENV, raising=False)
    assert gw.project_root() == str(install / "resources")
    assert os.path.isfile(gw.gateway_script(gw.project_root()))


def test_ensure_gateway_works_on_packaged_layout(monkeypatch, tmp_path) -> None:
    install = tmp_path / "install"
    script = install / "resources" / "mcp-servers" / "mcp-bridge" / "server.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.chdir(install / "resources")
    monkeypatch.delenv(gw.PROJECT_ROOT_ENV, raising=False)
    started = {"v": False}
    monkeypatch.setattr(
        gw, "probe", lambda *a, **k: {"reachable": started["v"], "auth_ok": started["v"], "sessions": 0}
    )

    class _P:
        def poll(self):
            return None

    def _spawn(root, token, log_path):
        started["v"] = True
        assert root.endswith("resources")
        return _P()

    monkeypatch.setattr(gw, "_spawn", _spawn)
    assert gw.ensure_gateway("http://127.0.0.1:8765", token="t") == "started"  # noqa: S106 — 测试占位凭据


# ── 回归面：Must Fix 2（读超时不得触发重试，防非幂等双发）────────────


class _FakeResp:
    """urlopen 返回值最小替身（真实 Request 走 _send_direct 才会用它）。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_client_does_not_retry_on_timeout(monkeypatch) -> None:
    """超时（连接已建立）→ 不拉起、不重试：browser_click 之类重发会执行两遍。

    在 urllib.request.urlopen 层注入真实超时异常，走完整 _open_with_errors →
    _error_kind → _send 链路（不 mock 内部返回值）。
    """
    client = _client(monkeypatch)
    attempts = {"n": 0}

    def _urlopen(*a, **k):
        attempts["n"] += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(bc.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(gw, "ensure_gateway", lambda *a, **k: pytest.fail("超时不应拉起网关"))
    with pytest.raises(BridgeClientError) as exc:
        client.call("browser_click", {"target": "e1"})
    assert "未自动重试" in str(exc.value)
    assert attempts["n"] == 1, "超时不得重发（应为 1 次尝试）"


def test_client_retries_on_connection_refused(monkeypatch) -> None:
    """连接未建立（请求根本没送达）→ 拉起网关后重试一次，第二次成功。"""
    client = _client(monkeypatch)
    state = {"n": 0}

    def _urlopen(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return _FakeResp(b'{"result": {"content": []}}')

    monkeypatch.setattr(bc.urllib.request, "urlopen", _urlopen)
    ensured: list[str] = []
    monkeypatch.setattr(gw, "ensure_gateway", lambda *a, **k: ensured.append("x") or "started")
    assert client.call("browser_click", {"target": "e1"}) == {"content": []}
    assert ensured == ["x"], "连接拒绝应触发一次拉起"
    assert state["n"] == 2, "连接拒绝应重试一次"


def test_open_with_errors_writes_kind_for_real_exception(monkeypatch) -> None:
    """接线证据：_open_with_errors 把真实异常分类写进 body.kind（不是文本嗅探）。"""
    client = _client(monkeypatch)
    req = urllib.request.Request("http://127.0.0.1:8765/mcp/browser", method="POST")

    def _urlopen(*a, **k):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(bc.urllib.request, "urlopen", _urlopen)
    out = client._open_with_errors(req, timeout=1.0)
    assert out["ok"] is False and out["status"] == 0
    assert out["body"]["kind"] == "timeout"
    assert "_failure_kind(out)" == "_failure_kind(out)"  # 表征用同一分类器
    assert bc._failure_kind(out) == "timeout"


def test_ambiguous_connection_error_is_not_retried(monkeypatch) -> None:
    """连接复位等不可判形态：保守不重试（请求可能已送达）。"""
    client = _client(monkeypatch)
    attempts = {"n": 0}

    def _urlopen(*a, **k):
        attempts["n"] += 1
        raise urllib.error.URLError(ConnectionResetError(104, "reset"))

    monkeypatch.setattr(bc.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(gw, "ensure_gateway", lambda *a, **k: pytest.fail("不可判形态不应拉起"))
    with pytest.raises(BridgeClientError) as exc:
        client.call("browser_click", {"target": "e1"})
    assert "不可判" in str(exc.value)
    assert attempts["n"] == 1


@pytest.mark.parametrize(
    "exc,expect",
    [
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (ConnectionRefusedError(111, "refused"), "connect"),
        (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "connect"),
        (socket.gaierror(-2, "Name or service not known"), "connect"),
        (ConnectionResetError(104, "reset"), "ambiguous"),
        (BrokenPipeError(32, "broken pipe"), "ambiguous"),
    ],
)
def test_error_kind_classification(exc, expect) -> None:
    """错误分类是"能否重试"的唯一判据，必须对超时族与连接族给出不同答案。"""
    assert bc._error_kind(exc) == expect


# ── 回归面：Should Fix（并发锁 / 解释器解析 / 凭据落盘权限）──────────


def test_spawn_lock_is_mutually_exclusive() -> None:
    """同进程并发抢锁：同一时刻只允许一个持有者（跨进程机制的单机可验部分）。"""
    import threading
    import time as _t

    with tempfile.TemporaryDirectory() as d:
        lock_path = os.path.join(d, "gateway.spawn.lock")
        held = {"max": 0, "cur": 0}
        guard = threading.Lock()

        def worker():
            with gw._spawn_lock(lock_path) as acquired:
                if not acquired:
                    return
                with guard:
                    held["cur"] += 1
                    held["max"] = max(held["max"], held["cur"])
                _t.sleep(0.15)
                with guard:
                    held["cur"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert held["max"] == 1, f"并发持有者应恒为 1，实测 {held['max']}"


def test_resolve_interpreter_reports_candidates_when_none_works(monkeypatch) -> None:
    monkeypatch.setattr(gw, "_interpreter_candidates", lambda root: ["/nonexistent/python-x"])
    with pytest.raises(gw.BridgeGatewayError) as exc:
        gw.resolve_interpreter("/tmp")
    assert "解释器" in str(exc.value)


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位（st_mode 不表达 0600），依赖目录 ACL")
def test_persisted_token_file_is_owner_only(tmp_path) -> None:
    """凭据文件首建即 0600（不留明文可读窗口）。"""
    env_dir = tmp_path / "user_root"
    env_dir.mkdir()
    path = gw.persist_token(str(tmp_path), "tok-perm")
    assert path
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


def test_resolve_token_without_root_raises_actionable_error(monkeypatch, tmp_path) -> None:
    """定位不到根时不许静默生成进程内凭据（那会一路 401 无法排障）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(gw.PROJECT_ROOT_ENV, raising=False)
    monkeypatch.delenv(gw.TOKEN_KEY, raising=False)
    with pytest.raises(gw.BridgeGatewayError) as exc:
        gw.resolve_token()
    assert gw.PROJECT_ROOT_ENV in str(exc.value)


# ── 回归面（第 3 轮审查 Must Fix）：跨进程 sandbox 路径也带 kind 并自愈 ──


def _free_port_2() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_inner_script_reports_connect_kind_on_refused(tmp_path) -> None:
    """容器内客户端脚本对"连接拒绝"必须自报 kind=connect（真实子进程执行）。

    这条链路是真跑内嵌脚本连一个没人监听的端口：不 mock 任何返回值，
    证明跨进程路径的分类是脚本自身产出的，不是宿主文本嗅探出来的。
    """
    script = tmp_path / "inner.py"
    script.write_text(bc._INNER_SCRIPT, encoding="utf-8")
    spec = {
        "base_candidates": [f"http://127.0.0.1:{_free_port_2()}"],
        "token": "t",
        "upstream": "browser",
        "session": "s",
        "caller": "sandbox",
        "payload": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        "timeout": 3,
    }
    proc = subprocess.run(  # noqa: S603 — 固定解释器 + 本测试自写的临时脚本，无外部输入
        [sys.executable, str(script)],
        input=json.dumps(spec).encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    out = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    assert out["ok"] is False and out["status"] == 0
    assert out["body"]["kind"] == "connect", out


def test_client_retries_when_sandbox_path_reports_connect(monkeypatch) -> None:
    """隔离任务（docker exec 路径）首跳 connect → 拉起网关并重试成功。

    回归第 3 轮 Must Fix：三档判据不得让隔离路径永久失去自愈能力。
    """
    monkeypatch.setenv(gw.TOKEN_KEY, "tok-x")
    monkeypatch.setattr(BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok-x"))
    client = BridgeClient(
        {"bridge_base": "http://127.0.0.1:8765"}, session_id="s", caller="sandbox", container_id="cid"
    )
    inner_fail = json.dumps(
        {"ok": False, "status": 0, "body": {"error": "bridge unreachable: refused", "kind": "connect"}}
    )
    inner_ok = json.dumps({"ok": True, "status": 200, "body": {"result": {"content": []}}})
    outputs = [inner_fail, inner_ok]
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        # 首次 subprocess 是 _docker_gateway_ip 的 network inspect（候选地址探测），
        # 只有 docker exec 那次才是容器内 MCP 调用。
        if "exec" not in argv:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0).encode("utf-8"), stderr=b"")

    monkeypatch.setattr(bc.subprocess, "run", _run)
    ensured: list[str] = []
    monkeypatch.setattr(gw, "ensure_gateway", lambda *a, **k: ensured.append("x") or "started")
    assert client.call("browser_navigate", {"url": "http://localhost:6390/"}) == {"content": []}
    assert ensured == ["x"], "sandbox 路径 connect 必须触发拉起（否则隔离任务永不自愈）"
    assert len(calls) == 2, "应经 docker exec 重试一次"
    assert "docker" in calls[0][0] and "exec" in calls[0], calls[0][:3]


# ── sandbox 认证回归（用户实测：网关在跑但 401）─────────────────


def _fake_gateway(port: int, auth_field: bool | None) -> None:
    """起一个最小 /health 服务：auth_field=None 表示旧版网关（不返回 auth）。"""

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body: dict = {"status": "ok", "sessions": 0}
            if auth_field is not None:
                body["auth"] = auth_field
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize(
    "auth_field,expect_reachable,expect_auth,expect_health",
    [
        (True, True, True, True),
        (False, True, False, False),   # 活着但没凭据：存活≠可用
        (None, True, True, True),       # 旧版网关无该字段 → 向后兼容按可用
    ],
)
def test_probe_reports_auth_state(auth_field, expect_reachable, expect_auth, expect_health) -> None:
    """/health 的 auth 字段决定"能否用它"，避免把 401 误报成凭据配错。"""
    port = _free_port()
    srv = _fake_gateway(port, auth_field)
    try:
        state = gw.probe(f"http://127.0.0.1:{port}")
        assert state["reachable"] is expect_reachable
        assert state["auth_ok"] is expect_auth
        assert gw.health(f"http://127.0.0.1:{port}") is expect_health
    finally:
        srv.shutdown()
        srv.server_close()


def test_ensure_gateway_reports_stale_gateway_without_credential(monkeypatch) -> None:
    """端口被"活着但无凭据"的旧网关占着：给可行动结论，不假装已就绪。"""
    port = _free_port()
    srv = _fake_gateway(port, False)
    try:
        with pytest.raises(gw.BridgeGatewayError) as exc:
            gw.ensure_gateway(f"http://127.0.0.1:{port}")
        msg = str(exc.value)
        assert "未配置凭据" in msg
        assert gw.TOKEN_KEY in msg
    finally:
        srv.shutdown()
        srv.server_close()


def test_policy_reads_token_from_file_when_env_missing(monkeypatch, tmp_path) -> None:
    """网关凭据与客户端同源：env 缺失时必须读凭据文件（用户实测 401 的根因）。"""
    import importlib.util

    bridge_dir = Path(__file__).resolve().parents[3] / "mcp-servers" / "mcp-bridge"
    import sys as _sys

    spec = importlib.util.spec_from_file_location("bridge_policy_auth", bridge_dir / "policy.py")
    pol = importlib.util.module_from_spec(spec)
    # from __future__ import annotations 下 dataclass 依赖模块在 sys.modules 中解析注解
    _sys.modules["bridge_policy_auth"] = pol
    spec.loader.exec_module(pol)  # type: ignore[union-attr]

    root = tmp_path / "proj"
    (root / "user_root").mkdir(parents=True)
    (root / "mcp-servers" / "mcp-bridge").mkdir(parents=True)
    (root / "user_root" / ".env").write_text("AGENTOS_BRIDGE_TOKEN=file-tok\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN_FILE", str(root / "user_root" / ".env"))
    monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN", raising=False)

    policy = pol.BridgePolicy(upstreams={})
    assert policy.bearer_token() == "file-tok"
    assert policy.credential_configured() is True
    # env 优先（与客户端同序）
    monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "env-tok")
    policy2 = pol.BridgePolicy(upstreams={})
    assert policy2.bearer_token() == "env-tok"
    # 两处都没有 → fail-closed（另起不含凭据文件的目录树，否则 cwd 向上仍会命中）
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN")
    monkeypatch.delenv("AGENTOS_BRIDGE_TOKEN_FILE")
    policy3 = pol.BridgePolicy(upstreams={})
    assert policy3.bearer_token() is None
    assert policy3.credential_configured() is False
    with pytest.raises(pol.PolicyError):
        policy3.check_auth("Bearer file-tok")
