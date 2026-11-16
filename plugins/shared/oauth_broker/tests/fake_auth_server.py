"""进程内假授权服务器（真实 HTTP，127.0.0.1 随机端口）——OAuth 全链路测试的对面端。

按 MCP 授权规范（2026-07-28 修订）实现四层协议面，供测试走真 HTTP、真 PKCE、
真状态机：

1. 受保护资源元数据（RFC 9728）：同时提供子路径插入形态与根形态；
2. 授权服务器元数据（RFC 8414 / OpenID Connect Discovery 1.0）：
   `as_metadata_form` 选择本服务器暴露哪一种（其余路径一律 404）；
3. 动态客户端注册（RFC 7591）：`POST /register`；
4. 授权码 + PKCE(S256)（`GET /authorize` 302 + `POST /token`）与 refresh_token
   授权（含轮换）。

行为开关（构造后、`start()` 前设置）用于构造坏件与拒收场景：
`prm_document_override` / `issuer_override` / `omit_token_endpoint` /
`omit_iss_supported` / `iss_in_response` / `dcr_error` / `token_error` /
`include_expires_in` / `rotate_refresh`。全部调用记录（registrations /
authorize_requests / token_requests）供断言「是否真的注册/换 token」。

测试侧「用户代理」助手：[`drive_authorization`] 走完 授权 URL → 302 →
回调 query 三步（不跟随重定向，回调解码交给调用方）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PRM_SUFFIX = "/.well-known/oauth-protected-resource"
AS_SUFFIX = "/.well-known/oauth-authorization-server"
OIDC_SUFFIX = "/.well-known/openid-configuration"


def b64url(data: bytes) -> str:
    """RFC 7636 附录 A 的 base64url（无填充）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def fetch_redirect_target(url: str, timeout: float = 10.0) -> str:
    """GET 授权端点并返回 302 的 Location（不跟随重定向）。

    Raises:
        AssertionError: 响应不是 302（授权端点契约被破坏）。
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, N802, ARG002
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as resp:  # noqa: SIM117
            raise AssertionError(f"授权端点应返回 302，实得 HTTP {resp.status}")
    except urllib.error.HTTPError as exc:  # noqa: PT017 —— 302 在 urlopen 里就是 HTTPError，断言后取其 Location
        if exc.code != 302:
            raise AssertionError(f"授权端点应返回 302，实得 HTTP {exc.code}") from exc
        return str(exc.headers["Location"])


def parse_query(url: str) -> dict[str, str]:
    """从 URL（或裸 query 串）解析单值 query 字典（last-wins）。"""
    query = urllib.parse.urlsplit(url).query or url
    return dict(urllib.parse.parse_qsl(query, keep_blank_values=True))


def drive_authorization(authorization_url: str, callback: Callable[[dict[str, str]], Any]) -> dict[str, Any]:
    """模拟用户代理：授权 URL → 302 Location → 解析回调 query → 交回调处理。

    Returns:
        ``{"location": ..., "query": ..., "result": <callback 返回值>}``
    """
    location = fetch_redirect_target(authorization_url)
    query = parse_query(location)
    return {"location": location, "query": query, "result": callback(query)}


class FakeAuthServer:
    """真实 HTTP 假授权服务器（线程内起停，端口自动分配）。"""

    def __init__(self, *, resource_path: str = "/mcp", issuer_path: str = "") -> None:
        self.resource_path = resource_path
        self.issuer_path = issuer_path
        # 行为开关
        self.prm_document_override: dict[str, Any] | None = None
        self.prm_root_override: dict[str, Any] | None = None
        self.omit_prm_subpath = False
        self.omit_prm_root = False
        self.as_metadata_form = "rfc8414"  # rfc8414 | oidc | oidc_append | none
        self.issuer_override: str | None = None
        self.omit_token_endpoint = False
        self.omit_iss_supported = False
        self.code_challenge_methods: tuple[str, ...] = ("S256",)
        self.no_registration_endpoint = False
        self.iss_in_response: str | None = None  # None = 用真实 issuer；"" = 不发
        self.dcr_error: tuple[int, dict[str, Any]] | None = None
        self.dcr_client_secret: str | None = None
        self.dcr_response_override: dict[str, Any] | None = None  # 200 但响应缺字段的坏件
        self.require_client_secret: str | None = None  # 令牌端点校验 client_secret（预注册机密客户端）
        self.www_authenticate_pointer: str | None = None  # 无凭据 GET 资源路径返回 401 + 指针
        self.mcp_get_status: int | None = None  # 无凭据 GET 资源路径的直接应答码（非 401 面：无指针）
        self.custom_prm: dict[str, Any] | None = None  # /custom-prm 上的另一份 PRM（指针回指用）
        self.prm_scopes: list[str] = ["files:read", "files:write"]  # PRM 的 scopes_supported（引导面取它）
        self.token_error: tuple[int, dict[str, Any]] | None = None
        self.token_response_override: dict[str, Any] | None = None  # 200 但响应缺字段的坏件
        self.as_metadata_override: dict[str, Any] | None = None  # 整文档覆盖（坏件用）
        self.prm_raw_body: bytes | None = None  # 200 + 非 JSON 正文（解析失败面）
        self.include_expires_in = True
        self.expires_in = 3600
        self.rotate_refresh = True
        self.scopes_supported = "files:read files:write"
        # 观测面
        self.registrations: list[dict[str, Any]] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: dict[str, dict[str, str]] = {}
        self.errors: dict[int, dict[str, str]] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._httpd: Any = None
        self._thread: threading.Thread | None = None
        self.base_url = ""
        self.issuer = ""

    # ── 生命周期 ─────────────────────────────────────────────────────

    def start(self) -> FakeAuthServer:
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
                pass  # 静默：测试输出不被访问日志污染

            def do_GET(self):  # noqa: N802
                server._handle_get(self)

            def do_POST(self):  # noqa: N802
                server._handle_post(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.issuer = f"{self.base_url}{self.issuer_path}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── 端点数据 ─────────────────────────────────────────────────────

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}{self.resource_path}"

    def protected_resource_document(self) -> dict[str, Any]:
        if self.prm_document_override is not None:
            return self.prm_document_override
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(self.prm_scopes),
        }

    def authorization_server_document(self) -> dict[str, Any]:
        if self.as_metadata_override is not None:
            return self.as_metadata_override
        doc: dict[str, Any] = {
            "issuer": self.issuer_override if self.issuer_override is not None else self.issuer,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "code_challenge_methods_supported": list(self.code_challenge_methods),
        }
        if not self.omit_token_endpoint:
            doc["token_endpoint"] = f"{self.base_url}/token"
        if not self.no_registration_endpoint:
            doc["registration_endpoint"] = f"{self.base_url}/register"
        if not self.omit_iss_supported:
            doc["authorization_response_iss_parameter_supported"] = True
        doc["scopes_supported"] = self.scopes_supported.split()
        return doc

    # ── 请求处理 ─────────────────────────────────────────────────────

    def _json(self, handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _not_found(self, handler: BaseHTTPRequestHandler) -> None:
        self._json(handler, 404, {"error": "not_found"})

    def _raw(self, handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:  # noqa: C901 —— 路由表
        path = urllib.parse.urlsplit(handler.path).path
        query = parse_query(handler.path)

        if path == f"{PRM_SUFFIX}{self.resource_path}":
            if self.omit_prm_subpath:
                return self._not_found(handler)
            if self.prm_raw_body is not None:
                return self._raw(handler, 200, self.prm_raw_body, "application/json")
            return self._json(handler, 200, self.protected_resource_document())
        if path == PRM_SUFFIX:
            if self.omit_prm_root:
                return self._not_found(handler)
            if self.prm_raw_body is not None:
                return self._raw(handler, 200, self.prm_raw_body, "application/json")
            document = self.prm_root_override or self.protected_resource_document()
            return self._json(handler, 200, document)
        if path == "/custom-prm":
            if self.custom_prm is None:
                return self._not_found(handler)
            return self._json(handler, 200, self.custom_prm)
        if path == self.resource_path and self.www_authenticate_pointer is not None:
            body = b""
            handler.send_response(401)
            handler.send_header(
                "WWW-Authenticate",
                f'Bearer resource_metadata="{self.www_authenticate_pointer}", scope="files:read"',
            )
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return None

        if path == self.resource_path and self.mcp_get_status is not None:
            return self._json(handler, self.mcp_get_status, {"jsonrpc": "2.0", "result": {}})

        if self.as_metadata_form != "none":
            as_forms = {
                "rfc8414": f"{AS_SUFFIX}{self.issuer_path}" if self.issuer_path else AS_SUFFIX,
                "oidc": f"{OIDC_SUFFIX}{self.issuer_path}" if self.issuer_path else OIDC_SUFFIX,
                "oidc_append": f"{self.issuer_path}{OIDC_SUFFIX}" if self.issuer_path else OIDC_SUFFIX,
            }
            if path == as_forms[self.as_metadata_form]:
                return self._json(handler, 200, self.authorization_server_document())

        if path == "/authorize":
            return self._handle_authorize(handler, query)
        return self._not_found(handler)

    def _handle_authorize(self, handler: BaseHTTPRequestHandler, query: dict[str, str]) -> None:
        self.authorize_requests.append(query)
        with self._lock:
            self._seq += 1
            seq = self._seq
        code = f"code-{seq}"
        self.codes[code] = {
            "client_id": query.get("client_id", ""),
            "redirect_uri": query.get("redirect_uri", ""),
            "code_challenge": query.get("code_challenge", ""),
            "code_challenge_method": query.get("code_challenge_method", ""),
            "resource": query.get("resource", ""),
        }
        params = {"code": code, "state": query.get("state", "")}
        iss = self.issuer if self.iss_in_response is None else self.iss_in_response
        if iss:
            params["iss"] = iss
        target = f"{query.get('redirect_uri', '')}?{urllib.parse.urlencode(params)}"
        handler.send_response(302)
        handler.send_header("Location", target)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)
        path = urllib.parse.urlsplit(handler.path).path
        if path == "/register":
            return self._handle_register(handler, raw)
        if path == "/token":
            return self._handle_token(handler, raw)
        return self._not_found(handler)

    def _handle_register(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(handler, 400, {"error": "invalid_client_metadata"})
        if self.dcr_error is not None:
            status, body = self.dcr_error
            return self._json(handler, status, body)
        self.registrations.append(payload)
        if self.dcr_response_override is not None:
            return self._json(handler, 201, self.dcr_response_override)
        with self._lock:
            self._seq += 1
            client_id = f"client-{self._seq}"
        self.clients[client_id] = payload
        doc: dict[str, Any] = {"client_id": client_id, "redirect_uris": payload.get("redirect_uris", [])}
        if self.dcr_client_secret is not None:
            doc["client_secret"] = self.dcr_client_secret
        doc["token_endpoint_auth_method"] = payload.get("token_endpoint_auth_method", "none")
        return self._json(handler, 201, doc)

    def _handle_token(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:  # noqa: C901 —— 两授权类型分支
        form = dict(urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
        self.token_requests.append(form)
        if self.token_error is not None:
            status, body = self.token_error
            return self._json(handler, status, body)
        if self.token_response_override is not None:
            return self._json(handler, 200, self.token_response_override)

        grant = form.get("grant_type", "")
        if grant == "authorization_code":
            code = form.get("code", "")
            record = self.codes.pop(code, None)
            failure = self._check_code_exchange(form, record)
            if failure is not None:
                return self._json(handler, 400, failure)
        elif grant == "refresh_token":
            failure = self._check_refresh(form)
            if failure is not None:
                return self._json(handler, 400, failure)
        else:
            return self._json(handler, 400, {"error": "unsupported_grant_type"})

        with self._lock:
            self._seq += 1
            seq = self._seq
        access = f"AT-{seq}"
        doc: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "scope": form.get("scope", "")}
        if self.include_expires_in:
            doc["expires_in"] = self.expires_in
        if grant == "refresh_token":
            old = form.get("refresh_token", "")
            if self.rotate_refresh:
                with self._lock:
                    self._seq += 1
                    new_seq = self._seq
                new_refresh = f"RT-{new_seq}"
                self.refresh_tokens.pop(old, None)
                self.refresh_tokens[new_refresh] = {"client_id": form.get("client_id", "")}
                doc["refresh_token"] = new_refresh
            else:
                doc["refresh_token"] = old
        else:
            with self._lock:
                self._seq += 1
                new_seq = self._seq
            refresh = f"RT-{new_seq}"
            self.refresh_tokens[refresh] = {"client_id": form.get("client_id", "")}
            doc["refresh_token"] = refresh
        return self._json(handler, 200, doc)

    def _check_code_exchange(self, form: dict[str, str], record: dict[str, str] | None) -> dict[str, str] | None:
        if record is None:
            return {"error": "invalid_grant", "error_description": "unknown code"}
        if self.require_client_secret is not None and form.get("client_secret") != self.require_client_secret:
            return {"error": "invalid_client", "error_description": "client_secret required"}
        if record["client_id"] != form.get("client_id", ""):
            return {"error": "invalid_grant", "error_description": "client mismatch"}
        if record["redirect_uri"] != form.get("redirect_uri", ""):
            return {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
        if record["resource"] and record["resource"] != form.get("resource", ""):
            return {"error": "invalid_grant", "error_description": "resource mismatch"}
        verifier = form.get("code_verifier", "")
        if record["code_challenge_method"] != "S256" or pkce_challenge(verifier) != record["code_challenge"]:
            return {"error": "invalid_grant", "error_description": "pkce mismatch"}
        return None

    def _check_refresh(self, form: dict[str, str]) -> dict[str, str] | None:
        if self.require_client_secret is not None and form.get("client_secret") != self.require_client_secret:
            return {"error": "invalid_client", "error_description": "client_secret required"}
        token = form.get("refresh_token", "")
        record = self.refresh_tokens.get(token)
        if record is None:
            return {"error": "invalid_grant", "error_description": "unknown refresh token"}
        if record["client_id"] != form.get("client_id", ""):
            return {"error": "invalid_grant", "error_description": "client mismatch"}
        return None


__all__ = [
    "FakeAuthServer",
    "b64url",
    "drive_authorization",
    "fetch_redirect_target",
    "parse_query",
    "pkce_challenge",
]
