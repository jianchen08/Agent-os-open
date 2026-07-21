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
