# @feature: FP-0.2.二 角色扮演成熟化 Wave A（card_io 共享库） | @ci: python-coverage
"""card_io 共享角色卡读写库契约测试。

锁定行为：
- PNG/JSON round-trip：含 extensions 未知字段与顶层未知字段的卡逐字段相等
  （CJK + ASCII + 非字符串值两组有区分度输入，未知字段不丢）；
- PNG chunk 约定：V2 卡写 chara、V3 卡写 ccv3；第三方工具手造 ccv3 chunk 可读；
  双 chunk 并存 ccv3 优先；keyword 大小写兜底；
- 宽容解析：缺信封裸字典按 V2 包裹补默认空值（默认值不共享可变实例）；
- validate：合法卡空列表；缺 name / 空 name / spec 或 spec_version 错 /
  data 缺失 / 字段类型错各报问题。

文件系统为外部边界（tmp_path 走真实盘）；PNG 底图用 Pillow 现生成。
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
for _p in (str(_SHARED_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import card_io  # noqa: E402, I001  (需先推 sys.path 再导入；平铺裸模块 per-file-ignores 先例)
from card_io import (  # noqa: E402, I001
    load_card,
    save_card_json,
    save_card_png,
    validate_card,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _v2_card_full() -> dict:
    """完整 V2 卡：CJK 内容 + null 可选字段 + extensions 未知键 + 顶层未知键。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "灵汐",
            "description": "可进化的智能体操作系统",
            "personality": "严谨",
            "scenario": "仓库协作",
            "first_mes": "你好，我是灵汐。",
            "mes_example": "<START>{{user}}: 你好\n{{char}}: 你好！",
            "alternate_greetings": ["第一份备用开场", "second greeting"],
            "system_prompt": "你是灵汐。",
            "post_history_instructions": None,
            "creator_notes": "测试用卡",
            "tags": ["测试", "agent"],
            "creator": "lingxi",
            "character_version": "0.2",
            "extensions": {
                "depth_prompt": {"prompt": "深度提示词", "depth": 4, "role": "system"},
                "unknown_vendor_key": {"nested": [1, 2.5, True, None]},
            },
        },
        "unknown_top_level": "顶层未知字段也应保留",
    }


def _v2_card_ascii_minimal() -> dict:
    """第二组输入：ASCII 内容、可选字段缺失（信封完整，不补默认值）。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "tester-01",
            "description": "plain ascii card",
            "extensions": {"flag": False, "count": 0},
        },
    }


def _v3_card() -> dict:
    """V3 卡：spec/spec_version 3.0 + extensions.depth_prompt。"""
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "苍羽",
            "description": "V3 卡描述",
            "first_mes": "V3 首条消息",
            "extensions": {"depth_prompt": {"prompt": "V3 深度", "depth": 2, "role": "assistant"}},
        },
    }


def _png_bytes(size: tuple[int, int] = (1, 1)) -> bytes:
    """Pillow 现生成纯色 PNG 字节（作 cover 与无 chunk 反例）。"""
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 96)).save(buf, format="PNG")
    return buf.getvalue()


def _png_with_chunks(chunks: dict[str, str]) -> bytes:
    """手造带若干 tEXt chunk 的 PNG（模拟第三方写方，不经过 save_card_png）。"""
    meta = PngInfo()
    for keyword, value in chunks.items():
        meta.add_text(keyword, value)
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()


def _card_chunk_payload(card: dict) -> str:
    """卡字典 → chunk 值（base64(utf-8 JSON)），与 PNG 约定同口径。"""
    return base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8")).decode("ascii")


class TestPngRoundTrip:
    def test_v2_full_roundtrip_by_path(self, tmp_path: Path) -> None:
        """完整 V2 卡（含未知字段）：save_png → load(path) 逐字段相等。"""
        card = _v2_card_full()
        target = tmp_path / "灵汐卡.png"
        save_card_png(card, target)
        assert target.read_bytes().startswith(_PNG_MAGIC)
        assert load_card(target) == card

    def test_v2_ascii_minimal_roundtrip_by_bytes(self, tmp_path: Path) -> None:
        """第二组输入（ASCII/缺可选字段）：load(bytes) 与 load(path) 同结果。"""
        card = _v2_card_ascii_minimal()
        target = tmp_path / "card.png"
        save_card_png(card, target)
        raw = target.read_bytes()
        assert load_card(raw) == card
        assert load_card(bytearray(raw)) == card

    def test_v3_card_roundtrip_writes_ccv3(self, tmp_path: Path) -> None:
        """V3 卡 round-trip 相等，且落盘 PNG 的 chunk keyword 为 ccv3。"""
        card = _v3_card()
        target = tmp_path / "v3.png"
        save_card_png(card, target)
        assert load_card(target) == card
        with Image.open(target) as img:
            assert card_io.PNG_KEYWORD_V3 in img.text

    def test_v2_card_writes_chara_keyword(self, tmp_path: Path) -> None:
        """V2 卡落盘 chunk keyword 为 chara（生态约定，读方按此寻址）。"""
        target = tmp_path / "v2.png"
        save_card_png(_v2_card_ascii_minimal(), target)
        with Image.open(target) as img:
            assert card_io.PNG_KEYWORD_V2 in img.text

    def test_cover_image_preserved_with_card(self, tmp_path: Path) -> None:
        """指定 cover：卡元数据嵌入成功且底图像素尺寸不变。"""
        card = _v2_card_ascii_minimal()
        target = tmp_path / "covered.png"
        save_card_png(card, target, cover_png_bytes=_png_bytes((3, 2)))
        assert load_card(target) == card
        with Image.open(target) as img:
            assert img.size == (3, 2)

    def test_cover_non_image_bytes_raises(self, tmp_path: Path) -> None:
        """cover 不是图像字节：ValueError 带定位，不产出半成品文件。"""
        target = tmp_path / "bad_cover.png"
        with pytest.raises(ValueError, match="cover"):
            save_card_png(_v2_card_ascii_minimal(), target, cover_png_bytes=b"not-an-image")
        assert not target.exists()


class TestPngLoadTolerance:
    def test_foreign_ccv3_chunk_loads(self) -> None:
        """第三方写方手造的 ccv3 chunk PNG 可正确读出 V3 卡。"""
        card = _v3_card()
        raw = _png_with_chunks({card_io.PNG_KEYWORD_V3: _card_chunk_payload(card)})
        assert load_card(raw) == card

    def test_dual_chunk_prefers_ccv3(self) -> None:
        """chara(V2) 与 ccv3(V3) 并存：ccv3 赢（对齐 SillyTavern 导入行为）。"""
        raw = _png_with_chunks(
            {
                card_io.PNG_KEYWORD_V2: _card_chunk_payload(_v2_card_ascii_minimal()),
                card_io.PNG_KEYWORD_V3: _card_chunk_payload(_v3_card()),
            }
        )
        loaded = load_card(raw)
        assert loaded["spec"] == "chara_card_v3"
        assert loaded == _v3_card()

    def test_keyword_case_insensitive_fallback(self) -> None:
        """keyword 大小写变体（如 `Chara`）兜底可读。"""
        card = _v2_card_ascii_minimal()
        raw = _png_with_chunks({"Chara": _card_chunk_payload(card)})
        assert load_card(raw) == card

    def test_chunk_value_with_whitespace_and_short_padding(self) -> None:
        """chunk 值含换行空白、base64 长度非 4 倍数缺 padding：仍可解码。"""
        card = _v2_card_ascii_minimal()
        payload = _card_chunk_payload(card)
        packed = "\n".join(payload[i : i + 7].strip("=") for i in range(0, len(payload), 7))
        raw = _png_with_chunks({card_io.PNG_KEYWORD_V2: packed})
        assert load_card(raw) == card

    def test_png_without_card_chunk_raises(self) -> None:
        """无角色卡 chunk 的纯 PNG：ValueError 带现有 keyword 定位。"""
        with pytest.raises(ValueError, match="chunk"):
            load_card(_png_bytes())

    def test_corrupt_png_raises(self) -> None:
        """PNG 魔数后即损坏：ValueError 而非底层异常透传。"""
        with pytest.raises(ValueError, match="PNG"):
            load_card(_PNG_MAGIC + b"broken-not-a-png")

    def test_chunk_value_not_base64_json_raises(self) -> None:
        """chunk 值不是合法 base64/JSON：ValueError 带 keyword 定位。"""
        raw = _png_with_chunks({card_io.PNG_KEYWORD_V2: "%%%not-base64%%%"})
        with pytest.raises(ValueError, match="chara"):
            load_card(raw)

    def test_chunk_value_decodes_to_non_object_raises(self) -> None:
        """chunk 值解码为非对象 JSON（数组）：ValueError。"""
        raw = _png_with_chunks({card_io.PNG_KEYWORD_V3: _card_chunk_payload([1, 2])})
        with pytest.raises(ValueError, match="ccv3"):
            load_card(raw)


class TestJsonRoundTrip:
    def test_v2_full_json_roundtrip(self, tmp_path: Path) -> None:
        """JSON round-trip：含未知字段的完整 V2 卡逐字段相等。"""
        card = _v2_card_full()
        target = tmp_path / "card.json"
        save_card_json(card, target)
        assert load_card(target) == card

    def test_json_bytes_and_path_equivalent(self, tmp_path: Path) -> None:
        """第二组输入（V3 卡）：路径与字节两种入参读出一致。"""
        card = _v3_card()
        target = tmp_path / "v3.json"
        save_card_json(card, target)
        raw = target.read_bytes()
        assert load_card(target) == card
        assert load_card(raw) == card

    def test_json_invalid_raises(self) -> None:
        """非法 JSON 字节 / 顶层非对象：ValueError 带定位。"""
        with pytest.raises(ValueError, match="JSON"):
            load_card(b"\xff\xfe not utf-8 json")
        with pytest.raises(ValueError, match="JSON"):
            load_card(b"[1, 2, 3]")


class TestLooseParse:
    def test_bare_dict_wraps_v2_with_defaults(self) -> None:
        """裸字典（无信封）：按 V2 包裹，标准字段补默认空值，未知键保留。"""
        bare = {"name": "裸卡", "description": "无信封输入", "custom_field": "未知键"}
        loaded = load_card(json.dumps(bare, ensure_ascii=False).encode("utf-8"))
        assert loaded["spec"] == "chara_card_v2"
        assert loaded["spec_version"] == "2.0"
        data = loaded["data"]
        assert data["name"] == "裸卡"
        assert data["custom_field"] == "未知键"
        assert data["personality"] == ""
        assert data["alternate_greetings"] == []
        assert data["tags"] == []
        assert data["extensions"] == {}

    def test_second_bare_dict_ascii(self) -> None:
        """第二组裸字典输入（ASCII、带 tags 子集）：同规则包裹。"""
        bare = {"name": "bare-en", "tags": ["a"]}
        loaded = load_card(json.dumps(bare).encode("utf-8"))
        data = loaded["data"]
        assert data["name"] == "bare-en"
        assert data["tags"] == ["a"]
        assert data["description"] == ""
        assert data["mes_example"] == ""

    def test_default_containers_not_shared_between_loads(self) -> None:
        """性质断言：两次解析的可变默认值互为独立实例（改动不串扰）。"""
        first = load_card(json.dumps({"name": "a"}).encode("utf-8"))
        second = load_card(json.dumps({"name": "b"}).encode("utf-8"))
        assert first["data"]["tags"] is not second["data"]["tags"]
        assert first["data"]["extensions"] is not second["data"]["extensions"]
        first["data"]["tags"].append("mutated")
        first["data"]["extensions"]["k"] = 1
        assert second["data"]["tags"] == []
        assert second["data"]["extensions"] == {}

    def test_envelope_keys_of_bare_dict_rehomed(self) -> None:
        """裸字典带 spec 键但无 data：spec/spec_version 归位信封层，不落 data。"""
        loaded = load_card(json.dumps({"spec": "chara_card_v2", "spec_version": "2.0", "name": "半信封"}).encode("utf-8"))
        assert loaded["spec"] == "chara_card_v2"
        assert loaded["data"]["name"] == "半信封"
        assert "spec" not in loaded["data"]

    def test_unknown_spec_with_data_dict_kept_as_is(self) -> None:
        """spec 为未知版本但 data 为对象：信封完整，原样返回（由 validate 定责）。"""
        exotic = {"spec": "chara_card_v9", "spec_version": "9.9", "data": {"name": "未来卡"}}
        loaded = load_card(json.dumps(exotic).encode("utf-8"))
        assert loaded == exotic


class TestValidate:
    def test_valid_cards_pass(self) -> None:
        """合法 V2 完整卡与 V3 卡：问题列表为空。"""
        assert validate_card(_v2_card_full()) == []
        assert validate_card(_v3_card()) == []

    def test_missing_or_empty_name(self) -> None:
        """缺 name / 空 name / 非 name 字符串：各报 name 问题。"""
        missing = _v2_card_ascii_minimal()
        del missing["data"]["name"]
        empty = _v2_card_ascii_minimal()
        empty["data"]["name"] = "  "
        for card in (missing, empty):
            problems = validate_card(card)
            assert len(problems) == 1
            assert "name" in problems[0]

    def test_bad_spec_and_version(self) -> None:
        """spec 未知 / spec_version 与 spec 不匹配 / spec 缺失：各报问题。"""
        bad_spec = _v2_card_ascii_minimal()
        bad_spec["spec"] = "v1"
        mismatch = _v2_card_ascii_minimal()
        mismatch["spec"] = "chara_card_v3"
        no_spec = {"spec_version": "2.0", "data": _v2_card_ascii_minimal()["data"]}
        for card in (bad_spec, mismatch, no_spec):
            problems = validate_card(card)
            assert problems
            assert any("spec" in p for p in problems)

    def test_missing_data_reports_single_problem(self) -> None:
        """data 缺失：只报 data 一条，不再对 data 内字段误报。"""
        problems = validate_card({"spec": "chara_card_v2", "spec_version": "2.0"})
        assert problems == ["data 必须是 JSON 对象，实际 缺失"]

    def test_field_type_violations(self) -> None:
        """tags 为字符串 / extensions 为数组 / description 缺失：各报对应问题。"""
        card = _v2_card_ascii_minimal()
        card["data"]["tags"] = "not-a-list"
        card["data"]["extensions"] = ["not-a-dict"]
        card["data"]["personality"] = ["not-a-string"]
        del card["data"]["description"]
        problems = validate_card(card)
        assert any("tags" in p for p in problems)
        assert any("extensions" in p for p in problems)
        assert any("personality" in p for p in problems)
        assert any("description" in p for p in problems)
        assert not any("name" in p for p in problems)

    def test_null_optional_fields_allowed(self) -> None:
        """可选字段为 null（V2/V3 允许）：不算问题。"""
        card = _v2_card_ascii_minimal()
        card["data"]["personality"] = None
        card["data"]["tags"] = None
        assert validate_card(card) == []

    def test_non_dict_input_returns_problem(self) -> None:
        """入参不是对象：返回问题列表而非抛错（校验器对非法形状负责）。"""
        assert validate_card("not a card")  # type: ignore[arg-type]
