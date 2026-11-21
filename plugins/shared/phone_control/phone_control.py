"""phone_control 工具实现：安全闸 + localhost HTTP 调 uia2-server。

本文件 schema 常量与 plugin.json capabilities.tools[0] 声明逐字对应
（G2 声明↔实现严格比对，漂移即剔除）。

设计真值：docs/working/dsh-go移动端本地运行调研与接入工作量_20261112.md
§四B/§四C（uia2-server 拍板 + 分级安全闸自造项）。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any

TOOL_NAME = "phone_control"

# uia2-server 设备侧默认端口（调研报告 §四B：设备 6790 端口 REST）
UIA2_BASE_URL = "http://127.0.0.1:6790"

PHONE_CONTROL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["tree", "click", "input", "key", "screenshot"],
            "description": "操控动作：tree=读元素树(source)；click=点击坐标；input=输入文本(先聚焦输入框)；key=按键(keycode)；screenshot=截图(base64)",
        },
        "x": {"type": "integer", "description": "click 目标横坐标"},
        "y": {"type": "integer", "description": "click 目标纵坐标"},
        "text": {"type": "string", "description": "input 要输入的文本"},
        "keycode": {"type": "integer", "description": "key 动作的 Android keycode（如 4=返回）"},
    },
}

PHONE_CONTROL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["message", "status"],
    "properties": {
        "message": {"type": "string", "description": "执行结果摘要或拦截原因"},
        "status": {
            "type": "string",
            "enum": ["ok", "blocked", "error"],
            "description": "ok=执行成功；blocked=安全闸拦截（交还用户）；error=调用失败",
        },
        "data": {"type": "object", "description": "动作返回数据（tree 的元素 JSON / screenshot 的 base64 等）"},
    },
}

PHONE_CONTROL_RENDER = {"card": "form", "title": "PhoneControl"}

PHONE_CONTROL_DESCRIPTION = (
    "手机操控文本档：经 localhost HTTP 调 uia2-server 操控服务（元素树/点击/输入），"
    "带分级安全闸（付款/密码/验证码/系统设置恒交还用户）"
)

# ══ 分级安全闸 ═══════════════════════════════════════════════════
# 语义：命中黑名单的操控动作恒拦截、交还用户执行（dsh-Go 三档确认的产品
# 安全语义自持，调研报告 §四C）。拦截不抛错——以 status=blocked 结构化回执
# 返回，让 LLM 能向用户解释并请用户手动操作。

# 类别 → 关键词（付款/密码/验证码/系统设置恒交还用户）。
# 英文走词边界匹配（防 pin 命中 typing/shipping 等误拦），中文整词。
SENSITIVE_KEYWORDS: dict[str, list[str]] = {
    "payment": ["支付宝", "微信支付", "付款", "支付", "收银台", "checkout"],
    "credential": ["密码", "passcode", "password", "登录口令", "pin", "pin code", "pin码"],
    "verification": ["验证码", "短信校验", "captcha", "otp"],
    "system_settings": ["系统设置", "无障碍设置", "设备管理器", "设置页", "打开设置", "settings", "root"],
}

_SENSITIVE_RE = re.compile(
    "|".join(
        (r"\b" + re.escape(k) + r"\b") if k.isascii() else re.escape(k)
        for kws in SENSITIVE_KEYWORDS.values() for k in kws
    ),
    re.IGNORECASE,
)

# localhost 专用 opener：绕过 http_proxy/https_proxy 环境变量——uia2-server
# 恒在设备本机 127.0.0.1，走代理既不可达也不安全。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check_security_gate(action: str, params: dict[str, Any],
                        target_control: str | None = None) -> str | None:
    """分级安全闸：返回拦截类别（None=放行）。

    双拦截面：
    1. 文本面：input 文本含敏感词（付款/密码/验证码/系统设置）恒拦截；
    2. 动作面（MF4/MF8）：click 是唯一能完成付款的动作——调用方必须先读元素
       树（tree 动作），由 find_click_target(tree_data, x, y) 按 x/y 定位目标
       控件并取其文本传入 target_control，按目标控件语义判定（dsh-Go 三档
       确认形态）；定位不到目标控件（None）时 click 走 fail-safe 拦截——
       无法判定目标控件时交还用户，不放行；key 无敏感表。
    """
    text = params.get("text")
    if isinstance(text, str) and text and _SENSITIVE_RE.search(text):
        return "credential_or_verification_text"
    if action == "click":
        if target_control is None:
            return "click_unverifiable_failsafe"
        if _SENSITIVE_RE.search(target_control):
            return "click_target_sensitive"
    return None


def find_click_target(tree_data: Any, x: int | None, y: int | None) -> str | None:
    """按 x/y 坐标在元素树中定位目标控件，返回其可判定文本（未命中返回 None）。

    兼容两类树形态：uia2 JSON（{value:{elements|children|nodes:[...]}} 内嵌
    bounds/text/resource_id/content_desc）与元素列表直给。bounds 形态
    "[x1,y1][x2,y2]"，命中=点落在矩形内。只读坐标所在控件文本，不整树扫描
    （MF8：整树判定会把聊天屏他人消息/短信验证码误判为目标）。
    """
    if not isinstance(x, int) or not isinstance(y, int):
        return None
    root = tree_data.get("value") if isinstance(tree_data, dict) else tree_data
    candidates: list[Any] = []
    if isinstance(root, dict):
        for key in ("elements", "children", "nodes", "windows"):
            v = root.get(key)
            if isinstance(v, list):
                candidates = v
                break
        else:
            candidates = [root]
    elif isinstance(root, list):
        candidates = root
    else:
        return None

    stack = list(candidates)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        bounds = node.get("bounds")
        hit = False
        if isinstance(bounds, str) and bounds.startswith("["):
            try:
                parts = bounds.replace("][", "]|[").split("|")
                x1, y1 = (int(v) for v in parts[0].strip("[]").split(","))
                x2, y2 = (int(v) for v in parts[1].strip("[]").split(","))
                hit = x1 <= x <= x2 and y1 <= y <= y2
            except (ValueError, IndexError):
                hit = False
        if hit:
            for key in ("text", "resource_id", "resource-id", "content_desc", "content-desc"):
                v = node.get(key)
                if isinstance(v, str) and v.strip():
                    return v
        stack.extend(ch for ch in node.values() if isinstance(ch, list))
    return None


# ══ localhost HTTP 调用（本体只做调用与结果回流） ═══════════════


def _uia2_request(method: str, path: str, body: dict[str, Any] | None = None,
                  base_url: str = UIA2_BASE_URL, timeout: float = 10.0) -> dict[str, Any]:
    """单次 uia2-server REST 调用，返回 {status_code, body}。网络错误抛出。"""
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=timeout) as resp:
        raw_bytes = resp.read()
        try:
            payload: Any = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # 二进制载荷（如 screenshot 的 PNG）：base64 原样回流，不被 utf-8 replace 破坏（SF3）
            payload = base64.b64encode(raw_bytes).decode("ascii")
        return {"status_code": resp.status, "body": payload}


def _uia2_json(method: str, path: str, body: dict[str, Any] | None = None,
               base_url: str = UIA2_BASE_URL) -> dict[str, Any]:
    """调用并解析 JSON 响应；非 2xx 或非 JSON 归一为 error 信封。"""
    try:
        result = _uia2_request(method, path, body, base_url)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"status": "error", "message": f"uia2-server unreachable: {exc}"}
    if not 200 <= result["status_code"] < 300:
        return {"status": "error",
                "message": f"uia2-server HTTP {result['status_code']}: {result['body'][:200]}"}
    try:
        return {"status": "ok", "data": json.loads(result["body"]),
                "message": "ok"}
    except json.JSONDecodeError:
        # 非 JSON（如 screenshot 返回 PNG 字节）按原文回流
        return {"status": "ok", "data": {"raw": result["body"][:100000]},
                "message": "ok (non-json payload)"}


def _execute(action: str, params: dict[str, Any],
             base_url: str = UIA2_BASE_URL) -> dict[str, Any]:
    """按 action 分发到 uia2-server 端点（路由表见 uia2 API 文档）。"""
    if action == "tree":
        return _uia2_json("GET", "/wd/hub/session/none/appium/source", base_url=base_url)
    if action == "screenshot":
        return _uia2_json("GET", "/screenshot", base_url=base_url)
    if action == "click":
        x, y = params.get("x"), params.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            return {"status": "error", "message": "click 需要 x/y 整数坐标"}
        return _uia2_json("POST", "/actions",
                          {"actions": [{"type": "tap", "x": x, "y": y}]}, base_url)
    if action == "input":
        text = params.get("text")
        if not isinstance(text, str):
            return {"status": "error", "message": "input 需要 text 字符串"}
        return _uia2_json("POST", "/actions",
                          {"actions": [{"type": "setText", "text": text}]}, base_url)
    if action == "key":
        kc = params.get("keycode")
        if not isinstance(kc, int):
            return {"status": "error", "message": "key 需要 keycode 整数"}
        return _uia2_json("POST", "/actions",
                          {"actions": [{"type": "pressKey", "keycode": kc}]}, base_url)
    return {"status": "error", "message": f"unknown action: {action}"}


def phone_control(action: str = "", x: int | None = None, y: int | None = None,
                  text: str | None = None, keycode: int | None = None,
                  base_url: str = UIA2_BASE_URL) -> dict[str, Any]:
    """phone_control 工具入口：安全闸 → uia2-server 调用 → 结构化回执。"""
    if action not in {"tree", "click", "input", "key", "screenshot"}:
        return {"status": "error",
                "message": f"unknown action: {action!r}（允许: tree/click/input/key/screenshot）"}
    params: dict[str, Any] = {}
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    if text is not None:
        params["text"] = text
    if keycode is not None:
        params["keycode"] = keycode

    # MF4/MF8：click 动作面安全闸——先读元素树，按 x/y 定位目标控件文本判定；
    # 树不可读或定位不到控件时 target_control=None → fail-safe 拦截交还用户。
    target_control: str | None = None
    if action == "click":
        tree_res = _execute("tree", {}, base_url)
        if tree_res.get("status") == "ok":
            target_control = find_click_target(tree_res.get("data"), params.get("x"), params.get("y"))

    blocked = check_security_gate(action, params, target_control)
    if blocked is not None:
        return {
            "status": "blocked",
            "message": (
                f"安全闸拦截（{blocked}）：内容涉及付款/密码/验证码/系统设置等敏感操作，"
                "按分级安全策略交还用户手动执行；不通过自动化通道下发。"
            ),
        }

    result = _execute(action, params, base_url)
    return result
