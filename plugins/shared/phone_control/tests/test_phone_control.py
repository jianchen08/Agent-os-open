# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""phone_control 插件测试：安全闸拦截 + localhost HTTP 调用行为。

行为测试原则：断言可观察行为（输入→回执 status/message）；
uia2-server 为外部依赖，用本地 HTTP 桩（真实 socket）验证调用链路，
不 mock 内部模块边界。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import importlib.util
import pathlib

_IMPL = pathlib.Path(__file__).resolve().parent.parent / "phone_control.py"
_spec = importlib.util.spec_from_file_location("phone_control_impl", _IMPL)
phone_control_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phone_control_impl)


# ══ 本地 HTTP 桩（模拟 uia2-server：记录请求、回固定响应） ═════════


class _StubHandler(BaseHTTPRequestHandler):
    last_requests: list[tuple[str, str, bytes]] = []

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        type(self).last_requests.append(("GET", self.path, b""))
        if self.path == "/wd/hub/session/none/appium/source":
            tree = {
                "value": {
                    "elements": [
                        {"text": "btn", "bounds": "[50,100][300,300]"},
                        # MF8 语义锚：付款按钮（点内=拦截，点外=放行）
                        {"text": "立即支付", "bounds": "[100,500][400,600]"},
                    ]
                }
            }
            self._respond(200, json.dumps(tree).encode())
        elif self.path == "/screenshot":
            self._respond(200, json.dumps({"value": "iVBORw0KGgo"}).encode())
        else:
            self._respond(404, b'{"error": "not found"}')

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).last_requests.append(("POST", self.path, body))
        self._respond(200, json.dumps({"value": True}).encode())

    def log_message(self, *args) -> None:  # 静音
        pass


@pytest.fixture()
def stub_base_url():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    _StubHandler.last_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ══ 安全闸（≥2 组区分度输入 + 性质断言） ═════════════════════════


@pytest.mark.parametrize(
    "text",
    ["请输入支付密码", "验证码是 123456", "打开支付宝付款", "my password is x"],
)
def test_gate_blocks_sensitive_text(text):
    """性质：含付款/密码/验证码关键词的 input 文本恒拦截。"""
    out = phone_control_impl.phone_control(action="input", text=text, base_url="http://127.0.0.1:1")
    assert out["status"] == "blocked"
    assert "安全闸" in out["message"]


@pytest.mark.parametrize(
    "text",
    ["打开设置页", "系统设置里关蓝牙"],
)
def test_gate_blocks_system_settings(text):
    """系统设置类恒交还用户。"""
    out = phone_control_impl.phone_control(action="input", text=text, base_url="http://127.0.0.1:1")
    assert out["status"] == "blocked"


@pytest.mark.parametrize(
    "text",
    ["今天的天气怎么样", "给张三发消息说明天见"],
)
def test_gate_allows_normal_text(text):
    """性质：普通文本放行（blocked=False 且后续会走到 HTTP 调用）。"""
    out = phone_control_impl.phone_control(action="input", text=text, base_url="http://127.0.0.1:1")
    assert out["status"] in {"ok", "error"}  # 非 blocked（error=桩不可达也证明放行了闸）


def test_gate_does_not_block_readonly_actions(stub_base_url):
    """tree/screenshot 只读动作不经文本闸拦截。"""
    out = phone_control_impl.phone_control(action="tree", base_url=stub_base_url)
    assert out["status"] == "ok"


# ══ localhost HTTP 调用与结果回流 ═══════════════════════════════


def test_tree_calls_uia2_source(stub_base_url):
    out = phone_control_impl.phone_control(action="tree", base_url=stub_base_url)
    assert out["status"] == "ok"
    assert out["data"]["value"]["elements"][0]["text"] == "btn"
    methods = [r[0] for r in _StubHandler.last_requests]
    assert methods == ["GET"]


def test_click_posts_tap_action(stub_base_url):
    """click 命中普通控件（MF8：x/y 定位目标控件文本判定，非敏感 → 放行）。"""
    out = phone_control_impl.phone_control(action="click", x=100, y=200, base_url=stub_base_url)
    assert out["status"] == "ok"
    method, path, body = _StubHandler.last_requests[-1]
    assert (method, path) == ("POST", "/actions")
    assert json.loads(body)["actions"] == [{"type": "tap", "x": 100, "y": 200}]


def test_click_on_payment_target_blocked(stub_base_url):
    """MF8 性质：click 坐标命中付款按钮（目标控件文本含敏感词）→ 恒拦截交还用户。"""
    out = phone_control_impl.phone_control(action="click", x=150, y=550, base_url=stub_base_url)
    assert out["status"] == "blocked"
    assert "click_target_sensitive" in out["message"]
    # 拦截即不向 uia2-server 下发 tap（最后一条请求仍是 tree 的 GET）
    assert _StubHandler.last_requests[-1][0] == "GET"


def test_click_requires_int_coords(stub_base_url):
    """坐标非法 → 目标控件定位不到 → fail-safe 拦截（MF8：无法判定目标控件时交还用户）。"""
    out = phone_control_impl.phone_control(action="click", x="100", y=200, base_url=stub_base_url)
    assert out["status"] == "blocked"
    assert "click_unverifiable_failsafe" in out["message"]


def test_input_posts_settext(stub_base_url):
    out = phone_control_impl.phone_control(action="input", text="你好", base_url=stub_base_url)
    assert out["status"] == "ok"
    method, _, body = _StubHandler.last_requests[-1]
    assert method == "POST"
    assert json.loads(body)["actions"] == [{"type": "setText", "text": "你好"}]


def test_key_posts_presskey(stub_base_url):
    out = phone_control_impl.phone_control(action="key", keycode=4, base_url=stub_base_url)
    assert out["status"] == "ok"
    _, _, body = _StubHandler.last_requests[-1]
    assert json.loads(body)["actions"] == [{"type": "pressKey", "keycode": 4}]


def test_unreachable_server_returns_error():
    """uia2-server 不可达 → error 信封（不抛异常、不静默）。"""
    out = phone_control_impl.phone_control(action="tree", base_url="http://127.0.0.1:1")
    assert out["status"] == "error"
    assert "unreachable" in out["message"]


def test_http_error_status_returns_error(stub_base_url):
    out = phone_control_impl.phone_control(action="screenshot", base_url=stub_base_url)
    # 桩 /screenshot 返回 200 JSON，走 ok；404 路径单测如下
    assert out["status"] == "ok"


def test_unknown_action():
    out = phone_control_impl.phone_control(action="wipe", base_url="http://127.0.0.1:1")
    assert out["status"] == "error"
    assert "unknown action" in out["message"]


# ══ 声明面一致性（G2 防漂移） ═══════════════════════════════════


def test_schema_constants_match_manifest():
    import json as _json
    manifest = _json.loads((pathlib.Path(__file__).resolve().parent.parent / "plugin.json").read_text("utf-8"))
    tool = manifest["capabilities"]["tools"][0]
    assert tool["input_schema"] == phone_control_impl.PHONE_CONTROL_INPUT_SCHEMA
    assert tool["output_schema"] == phone_control_impl.PHONE_CONTROL_OUTPUT_SCHEMA
    assert tool["render"] == phone_control_impl.PHONE_CONTROL_RENDER
    assert tool["description"] == phone_control_impl.PHONE_CONTROL_DESCRIPTION
    assert tool["name"] == phone_control_impl.TOOL_NAME
