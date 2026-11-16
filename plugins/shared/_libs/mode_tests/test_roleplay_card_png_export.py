# @feature: FP-0.2.〇 RP批1b PNG导出 | @ci: python-coverage
"""export 端点 format 参数测试（批 1b：ST 生态 PNG 往返闭环收口）。

锁定行为：
- 三段往返语义等价：import → export(format=png) → import，V2/V3 各一组
  fixture，往返后落盘模式卡逐字段相等（extensions 扩展字段不丢不增）；
- 契约：缺省 format = json 行为与现状 byte-identical（响应键集/文件名/
  信封字节零变化），显式 format=json 同值；
- fail-closed：非法 format（未知串/大小写不符/非字符串）400 参数错误，
  错误信息可读且不带载荷键。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（同 test_roleplay_server_maturity）；
PNG 字节走真实编解码（card_io 手写写面 + load_card 真读），零 mock 内部依赖。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay/data"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_png_export_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离（AGENTOS_USER_CONFIG_DIR → tmp 解析链）。"""
    user_config = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_config))
    module = _load_server()
    module.reset_providers()
    module.user_config = user_config  # 测试回读用户层文件的锚点
    return module


def _call(module: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return asyncio.run(module.http_handle(path=path, method=method, raw_body=raw_body, query={}))


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _envelope_status(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _post(module: Any, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
    return _call(module, f"{_EP}{suffix}", method="POST", raw_body=json.dumps(body))


def _card_by_id(module: Any, card_id: str) -> dict[str, Any]:
    cards = _body_json(_call(module, f"{_EP}/cards"))["cards"]
    return next(c for c in cards if c["id"] == card_id)


def _st_envelope(spec_key: str) -> dict[str, Any]:
    """ST 信封 fixture 两组（V2/V3）：CJK 内容 + extensions 扩展字段（含未知
    厂商键与嵌套标量混合值）——保真断言锚点。"""
    if spec_key == "v2":
        return {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "月语·导出",
                "description": "批1b PNG 往返 V2",
                "first_mes": "你好，旅人。",
                "personality": "温柔",
                "tags": ["奇幻", "测试"],
                "alternate_greetings": ["备用开场"],
                "extensions": {
                    "talkativeness": "0.5",
                    "depth_prompt": {"prompt": "深度", "depth": 4, "role": "system"},
                    "unknown_vendor": {"nested": [1, 2.5, True, None]},
                },
            },
        }
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "苍羽·导出",
            "description": "批1b PNG 往返 V3",
            "first_mes": "V3 开场",
            "creator_notes": "v3 备注随平铺保真",
            "extensions": {"talkativeness": "0.7", "v3_only": {"x": [1, 2]}},
        },
    }


class TestExportPngRoundTrip:
    @pytest.mark.parametrize("spec_key", ["v2", "v3"], ids=["v2", "v3"])
    def test_import_export_png_import_flatten_equal(self, rp: Any, spec_key: str) -> None:
        """三段往返：import → export(png) → import，落盘模式卡逐字段相等，
        extensions 不丢不增。export 信封恒为 V2（既有 JSON 导出契约，本批
        不改），故语义等价以落盘扁平卡为准。"""
        module = rp
        envelope = _st_envelope(spec_key)
        payload = base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode("utf-8")).decode("ascii")
        body = _body_json(_post(module, "/cards/import", {"file_b64": payload, "filename": "png_round.json"}))
        assert body["card_id"] == "png_round"
        assert body["issues"] == []
        card_before = _card_by_id(module, "png_round")

        exported = _body_json(_post(module, "/cards/export", {"card_id": "png_round", "format": "png"}))
        assert exported["filename"] == "png_round.png"
        assert exported["content_base64"] == exported["file_b64"]
        png_bytes = base64.b64decode(exported["file_b64"])
        assert png_bytes.startswith(_PNG_MAGIC)

        body = _body_json(
            _post(module, "/cards/import", {"file_b64": exported["file_b64"], "filename": "png_round.png"})
        )
        assert body["card_id"] == "png_round"
        card_after = _card_by_id(module, "png_round")
        assert card_after == card_before
        assert card_after["extensions"] == envelope["data"]["extensions"]

    def test_png_payload_readable_by_card_io(self, rp: Any) -> None:
        """export(png) 字节经读侧 load_card 解析：data.name/spec 保真（写读
        同构的真依赖验证，不经端点内环）。"""
        from card_io import load_card

        module = rp
        payload = base64.b64encode(
            json.dumps(_st_envelope("v2"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        _body_json(_post(module, "/cards/import", {"file_b64": payload, "filename": "reader.json"}))
        exported = _body_json(_post(module, "/cards/export", {"card_id": "reader", "format": "png"}))
        envelope = load_card(base64.b64decode(exported["file_b64"]))
        assert envelope["spec"] == "chara_card_v2"
        assert envelope["data"]["name"] == "月语·导出"
        assert envelope["data"]["extensions"] == _st_envelope("v2")["data"]["extensions"]


class TestExportFormatContract:
    def test_default_and_explicit_json_byte_identical(self, rp: Any) -> None:
        """缺省（无 format 键）与显式 json：响应整体相等且键集/文件名/信封
        与现状契约一致（byte-identical，既有消费方零感知）。"""
        module = rp
        default = _body_json(_post(module, "/cards/export", {"card_id": "card_luna"}))
        explicit = _body_json(_post(module, "/cards/export", {"card_id": "card_luna", "format": "json"}))
        assert default == explicit
        assert set(default) == {"filename", "file_b64", "content_base64"}
        assert default["filename"] == "card_luna.json"
        envelope = json.loads(base64.b64decode(default["file_b64"]).decode("utf-8"))
        assert envelope["spec"] == "chara_card_v2"
        assert envelope["spec_version"] == "2.0"

    @pytest.mark.parametrize("bad_format", ["xml", "PNG", 123], ids=["unknown", "case-sensitive", "non-string"])
    def test_invalid_format_rejected(self, rp: Any, bad_format: Any) -> None:
        """非法 format 400 参数错误（fail-closed）：错误可读、不带载荷键。"""
        module = rp
        result = _post(module, "/cards/export", {"card_id": "card_luna", "format": bad_format})
        assert _envelope_status(result) == 400
        body = _body_json(result)
        assert "非法 format" in body["error"]
        assert "file_b64" not in body
        assert "content_base64" not in body
