"""插件 ``http.handle`` 面样板公共模块 — 内核 HttpHandleResponse / ToolExecutionResult 契约收口。

0.2 插件 HTTP 面（``http.handle`` 工具按 path 分发）的统一协议：

- 返回 **ToolExecutionResult** ``{success, data}``；``data`` 为内核期望的
  **HttpHandleResponse** ``{status, headers, body, body_encoding}``，
  body 为 base64 编码的 JSON（内核 HttpHandleResponse 约定）。

本模块提供各插件 server 的同构样板（曾以逐字拷贝散布于 task_form / evaluation /
review / agent_manager / scene / workspace / llm / multimodal / artifacts /
hindsight_memory / tasks 等 server，本模块为唯一实现）：

1. :func:`json_response` —— 任意 JSON 可序列化对象 → HttpHandleResponse。
2. :func:`ok` —— 成功 ToolExecutionResult ``{success: True, data}``。
3. :func:`error` —— 失败信封 ``{success: False, error, data}``，data 内嵌带
   HTTP 状态的错误响应（默认 503，sidecar 未就绪语义）。
4. :func:`protocol_error` —— 插件全权控制响应形态的协议级错误：
   ``{success: True, data: <带 HTTP status + 结构化错误体的 HttpHandleResponse>}``
   （review/multimodal 上传面契约：HTTP 400 + ``{"error": {"code", "message"}}``）。
5. :func:`decode_body` —— 解码 raw_body（base64 或明文 JSON）为 dict。
6. :func:`parse_multipart` —— 解析 multipart/form-data（email.parser，标准库）。

导入形态与 ``tenant_data`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入（如 ``from http_json import json_response as _json_response``），
保持各插件内部调用点零改动。

[来源: docs/working/规则驱动全仓扫描报告_20260827.md 辖区三 Must#9 /
 辖区四 Must#13；plugins/shared/tenant_data.py 导入先例]
"""

from __future__ import annotations

import base64
import json
from typing import Any


def json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把任意 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def error(message: str, status: int = 503) -> dict[str, Any]:
    """错误响应：{success:false, error, data}。data 携带 HTTP 状态给前端。"""
    return {"success": False, "error": message, "data": json_response({"error": message}, status)}


def protocol_error(message: str, status: int) -> dict[str, Any]:
    """协议级错误响应：{success:true, data:{status, body}}（插件全权控制响应形态）。

    body 为结构化错误体 ``{"error": {"code", "message"}}``，code 为字符串化的
    HTTP 状态——前端按 data.error.code/message 展示，与 :func:`error` 的扁平
    信封（success:false）是两条不同契约，不可混用。
    """
    return ok(json_response({"error": {"code": str(status), "message": message}}, status))


def decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。

    非 dict 顶层 JSON（数组/标量）不符合请求体契约 → 返回 ``{}``（交由
    调用方走缺参错误路径），不透传非 dict 值。
    """
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def parse_multipart(content_type: str, body_bytes: bytes) -> dict[str, Any]:
    """解析 multipart/form-data（内核透传的 raw_body base64 解码后的字节）。

    返回 {字段名: 值}；文件字段值为 {filename, content_type, data(bytes)}，
    普通字段为 str。用 email.parser 解析（标准库，无外部依赖）。

    Raises:
        Exception: 非法 multipart 体时由 email.parser 抛出，调用方转 400。
    """
    import email  # noqa: PLC0415
    from email.policy import default as default_policy  # noqa: PLC0415

    header = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body_bytes, policy=default_policy)
    fields: dict[str, Any] = {}
    if not msg.is_multipart():
        return fields
    parts = msg.get_payload()
    if not isinstance(parts, list):  # pragma: no cover —— 防御 typeshed
        return fields
    for part in parts:
        if not isinstance(part, email.message.Message):  # pragma: no cover
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        filename = part.get_filename()
        if filename is not None:
            data = part.get_payload(decode=True) or b""
            fields[name] = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            }
        else:
            payload = part.get_payload(decode=True)
            fields[name] = (
                payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
            )
    return fields
