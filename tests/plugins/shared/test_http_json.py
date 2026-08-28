# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
# @feature: 插件 http.handle 面样板收口 | @ci: python-coverage
"""http_json 公共模块契约测试 — 内核 HttpHandleResponse / ToolExecutionResult 样板。

覆盖 ``plugins/shared/http_json.py`` 全部六个函数的行为契约：
- ``json_response``：形状（四键）+ base64 可逆性（解码回原 payload）+ 中文/日期等
  非 ASCII、非 JSON 类型经 default=str 仍可序列化；
- ``ok`` / ``error`` / ``protocol_error``：三条响应契约互不混淆（success 三态），
  error 默认 503、可指定状态，protocol_error body 结构化 {code,message}；
- ``decode_body``：明文/base64/空串/垃圾输入 × dict与非dict顶层 × 非法JSON 抛错；
- ``parse_multipart``：非 multipart → 空；文件字段三键结构 + 字节保真；普通字段 str。

意图：这些函数是全仓插件 HTTP 响应的唯一实现，编码内核 wire 契约不变量——
body 必须 base64 且 round-trip 一致、success 字段语义固定、multipart 文件字节无损。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

_SHARED_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import http_json  # noqa: E402, I001  (需先推 sys.path 再导入；isort 不识别平铺裸模块——per-file-ignores 先例)


# ── json_response：HttpHandleResponse 形状与 base64 round-trip ───────────


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"ok": True}, 200),
        ({"error": "boom"}, 400),
        ({"error": "unavailable"}, 503),
        ({"n": 1, "tags": ["a", "b"]}, 418),
    ],
)
def test_json_response_shape_and_roundtrip(payload: dict, status: int) -> None:
    """任意 status/payload → 四键 HttpHandleResponse，body base64 解码回原文。"""
    resp = http_json.json_response(payload, status)
    assert set(resp.keys()) == {"status", "headers", "body", "body_encoding"}
    assert resp["status"] == status
    assert resp["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    assert resp["body_encoding"] == "base64"
    # 性质：base64 可逆 —— 解码后与 json.dumps(payload) 等价（键序无关的比较）
    assert json.loads(base64.b64decode(resp["body"]).decode("utf-8")) == payload


def test_json_response_default_status_and_non_ascii() -> None:
    """默认 200；中文不转义地入 body（ensure_ascii=False），仍可逆。"""
    payload = {"名": "值", "note": "中文✓"}
    resp = http_json.json_response(payload)
    assert resp["status"] == 200
    decoded = base64.b64decode(resp["body"]).decode("utf-8")
    assert "中文✓" in decoded  # 非 ASCII 直存，非 \\uXXXX 转义
    assert json.loads(decoded) == payload


def test_json_response_non_serializable_falls_back_to_str() -> None:
    """非 JSON 原生类型经 default=str 降级为字符串，不抛错。"""
    class _Thing:
        def __str__(self) -> str:
            return "THING"

    resp = http_json.json_response({"obj": _Thing()})
    assert json.loads(base64.b64decode(resp["body"])) == {"obj": "THING"}


# ── ok / error / protocol_error：三条 ToolExecutionResult 契约 ───────────


def test_ok_wraps_data_unchanged() -> None:
    data = {"k": [1, 2]}
    result = http_json.ok(data)
    assert result == {"success": True, "data": data}


def test_error_default_503_and_flat_envelope() -> None:
    result = http_json.error("sidecar 未就绪")
    assert result["success"] is False
    assert result["error"] == "sidecar 未就绪"
    inner = result["data"]
    assert inner["status"] == 503
    assert json.loads(base64.b64decode(inner["body"])) == {"error": "sidecar 未就绪"}


def test_error_custom_status_propagates_into_body() -> None:
    result = http_json.error("bad request", 400)
    assert result["data"]["status"] == 400
    assert json.loads(base64.b64decode(result["data"]["body"]))["error"] == "bad request"


def test_protocol_error_structured_body() -> None:
    """协议级错误：success:true 包 HTTP 400 + 结构化 {code,message} 错误体。"""
    result = http_json.protocol_error("missing or empty 'file' field", 400)
    assert result["success"] is True  # 与 error 的 False 相区分
    inner = result["data"]
    assert inner["status"] == 400
    body = json.loads(base64.b64decode(inner["body"]))
    assert body == {"error": {"code": "400", "message": "missing or empty 'file' field"}}
    assert isinstance(body["error"]["code"], str)  # code 恒字符串化


def test_three_error_shapes_are_distinct_contracts() -> None:
    """同一 (message, status) 下 ok/error/protocol_error 不串形（防实现漂移回归）。"""
    msg, status = "x", 400
    flat = http_json.error(msg, status)
    proto = http_json.protocol_error(msg, status)
    assert flat["success"] != proto["success"]
    assert flat["data"]["body"] != proto["data"]["body"]


# ── decode_body：明文 / base64 / 空 / 垃圾 / 非dict顶层 ──────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),  # 空串 → 空 dict
        ('{"a": 1}', {"a": 1}),  # 明文 JSON object
        (base64.b64encode(b'{"b": [1, 2]}').decode(), {"b": [1, 2]}),  # base64 编码 object
        ("  {\"c\": 3}", {"c": 3}),  # 明文带前导空白仍识别为 object
    ],
)
def test_decode_body_accepts_dict_bodies(raw: str, expected: dict) -> None:
    assert http_json.decode_body(raw) == expected


@pytest.mark.parametrize("raw", ['"str"', "42", "true", "null"])
def test_decode_body_plain_scalar_top_level_returns_empty(raw: str) -> None:
    """明文标量/字面量顶层 JSON 不透传（契约=返回 dict）→ 空体走缺参路径。"""
    assert http_json.decode_body(raw) == {}


def test_decode_body_base64_array_returns_empty() -> None:
    """base64 编码的数组顶层：解码文以 '[' 开头被采纳解析 → 非 dict 归一为 {}。"""
    assert http_json.decode_body(base64.b64encode(b"[1, 2]").decode()) == {}


def test_decode_body_base64_of_scalar_falls_back_to_plain_parse_raises() -> None:
    """base64 解出文本不以对象/数组开头则不采纳（防误吞），回退按明文解析。

    此时明文即 b64 串本身、非法 JSON → ValueError（现状契约：只认
    明文 JSON 或 base64(JSON 对象/数组)，其余输入显式失败）。
    """
    encoded = base64.b64encode(b'"scalar"').decode()
    with pytest.raises(ValueError, match="invalid JSON body"):
        http_json.decode_body(encoded)


def test_decode_body_base64_of_non_json_text_treated_as_plain_garbage_raises() -> None:
    """base64 可解但解码文非 JSON：回退原文解析 → 非法 JSON 抛 ValueError。"""
    raw = base64.b64encode(b"hello").decode()
    with pytest.raises(ValueError, match="invalid JSON body"):
        http_json.decode_body(raw)


@pytest.mark.parametrize("raw", ["not-json", "{broken"])
def test_decode_body_invalid_json_raises_value_error(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid JSON body"):
        http_json.decode_body(raw)


def test_decode_body_plain_text_not_confused_with_base64() -> None:
    """明文中文文本（非法 base64 亦非法 JSON）→ ValueError，不静默成 {}。"""
    with pytest.raises(ValueError, match="invalid JSON body"):
        http_json.decode_body("这是一段明文")


# ── parse_multipart：文件字段 / 普通字段 / 非 multipart ──────────────────


def _multipart_body(boundary: str, parts: bytes) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="a.png"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode()
        + parts
        + f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="lang"\r\n\r\nzh-CN\r\n'
        f"--{boundary}--\r\n".encode()
    )


def test_parse_multipart_file_and_field() -> None:
    boundary = "XBOUND"
    blob = b"\x89PNG\r\n\x1a\n-binary\xff\x00bytes"
    fields = http_json.parse_multipart(f"multipart/form-data; boundary={boundary}", _multipart_body(boundary, blob))
    assert set(fields.keys()) == {"file", "lang"}
    # 文件字段：三键结构 + 字节无损（含 0xff/0x00 等二进制敏感值）
    assert fields["file"]["filename"] == "a.png"
    assert fields["file"]["content_type"] == "image/png"
    assert fields["file"]["data"] == blob
    # 普通字段：str 而非 bytes
    assert fields["lang"] == "zh-CN"
    assert isinstance(fields["lang"], str)


def test_parse_multipart_binary_idempotence_two_payloads() -> None:
    """两组不同负载各自无损（防固化单一字面值的伪测试）。"""
    boundary = "YBOUND"
    for blob in (b"", b"x" * 4096, bytes(range(256))):
        fields = http_json.parse_multipart(
            f"multipart/form-data; boundary={boundary}",
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="f"; filename="d.bin"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + blob
            + f"\r\n--{boundary}--\r\n".encode(),
        )
        assert fields["f"]["data"] == blob


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", b"hello"),
        ("application/json", b'{"a": 1}'),
        ("multipart/form-data; boundary=NOPE", b"garbage without valid delimiter"),
    ],
)
def test_parse_multipart_non_multipart_or_garbage_yields_empty(content_type: str, body: bytes) -> None:
    """非 multipart / 边界对不上 → 空 fields（调用方按缺 file 转 400）。"""
    assert http_json.parse_multipart(content_type, body) == {}
