"""
0.2 版本端到端测试 - HTTP 工具函数

提供 http_get / http_post_json 等工具函数，供所有测试模块导入。
独立模块避免与 tests/conftest.py 的命名冲突。
"""
import json
import os
import urllib.error
import urllib.request

# ============================================================
# 服务地址配置
# ============================================================
KERNEL_HOST = os.environ.get("KERNEL_HOST", "localhost")
KERNEL_PORT = int(os.environ.get("KERNEL_PORT", "9100"))
KERNEL_URL = f"http://{KERNEL_HOST}:{KERNEL_PORT}"

FRONTEND_HOST = os.environ.get("FRONTEND_HOST", "localhost")
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "5290"))
FRONTEND_URL = f"http://{FRONTEND_HOST}:{FRONTEND_PORT}"

CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")

# WebSocket URL
WS_URL = f"ws://{KERNEL_HOST}:{KERNEL_PORT}/ws"


def http_get(url, timeout=5):
    """发起 HTTP GET 请求，返回 (status_code, body_dict_or_str, headers)。

    使用 urllib 标准库，无第三方依赖。
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            headers = dict(resp.headers)
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_json = body
            return status, body_json, headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            body_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body_json = body
        return e.code, body_json, dict(e.headers)
    except urllib.error.URLError as e:
        raise ConnectionError(f"无法连接 {url}: {e}")


def http_post_json(url, data, timeout=5):
    """发起 HTTP POST JSON 请求，返回 (status_code, body_dict_or_str, headers)。"""
    try:
        body_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            headers = dict(resp.headers)
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_json = body
            return status, body_json, headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            body_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body_json = body
        return e.code, body_json, dict(e.headers)
    except urllib.error.URLError as e:
        raise ConnectionError(f"无法连接 {url}: {e}")


def http_get_with_auth(url, token=None, timeout=10):
    """发起带可选 Bearer Token 的 GET 请求，返回 (status_code, body_dict_or_str)。

    供需要登录态的新增 e2e 测试（审批/管道 chat/WS 流式）复用。
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_json = body
            return status, body_json, {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            body_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body_json = body
        return e.code, body_json, {}
    except urllib.error.URLError as e:
        raise ConnectionError(f"无法连接 {url}: {e}")


def http_post_json_auth(url, data, token=None, timeout=10):
    """发起带可选 Bearer Token 的 HTTP POST JSON 请求，返回 (status_code, body, headers)。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        body_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body_bytes, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_json = body
            return status, body_json, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            body_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            body_json = body
        return e.code, body_json, dict(e.headers)
    except urllib.error.URLError as e:
        raise ConnectionError(f"无法连接 {url}: {e}")


# ============================================================
# 登录 / 会话 / WS 地址 工具（新增 e2e 测试复用）
# ============================================================
def login_admin(username="admin", password="admin12345", timeout=10):
    """登录默认管理员，返回 access_token；登录失败抛 RuntimeError。"""
    status, body, _ = http_post_json(
        f"{KERNEL_URL}/api/v1/auth/login",
        {"username": username, "password": password},
        timeout=timeout,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise RuntimeError(f"登录失败: status={status}, body={body}")
    return body["access_token"]


def create_session(token, title="e2e-session", timeout=10):
    """创建会话，返回响应 dict（含 thread_id / active_pipeline_id）。"""
    status, body, _ = http_post_json_auth(
        f"{KERNEL_URL}/api/v1/sessions", {"title": title}, token=token, timeout=timeout
    )
    if status != 200 or not isinstance(body, dict) or not body.get("thread_id"):
        raise RuntimeError(f"创建会话失败: status={status}, body={body}")
    return body


def ws_chat_url(token, version="1"):
    """构建带认证 token 的 /ws/chat WebSocket URL（与前端 buildGlobalWebSocketUrl 同构）。"""
    from urllib.parse import quote

    return f"{WS_URL}/chat?token={quote(token)}&version={version}"
