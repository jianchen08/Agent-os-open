# @feature: FP-0.2.〇 RP批1b PNG导出 | @ci: python-coverage
"""card_io.write_png_card 手写 PNG 写面契约测试（批 1b，ST 生态往返收口）。

锁定行为：
- 往返语义等价：write_png_card 字节经读侧 load_card 解析回等价卡（V2 完整
  含 extensions 未知键/顶层未知键、V3、ASCII 极简三组区分度输入）；
- PNG 结构合法性：签名 + IHDR(1x1 RGBA 8bit) + IDAT + tEXt + IEND chunk
  序列，逐 chunk CRC32 校验通过、字节流无尾随冗余（手工 chunk 走查，不依赖
  Pillow 解析）；
- tEXt 载荷严格互逆：chunk 值 = base64(utf-8 JSON) 且与规范序列化逐字节
  相等（删值实验锚点：keyword 规则/序列化口径改动必红）；
- keyword 规则与 save_card_png 同源：V2 卡 chara、V3 卡 ccv3。

零依赖契约：实现只许 struct/zlib 标准库（测试同样只用标准库走查 chunk）；
load_card/Pillow 是外部读方，走真实解码不 mock。
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
# card_io 在 mode_roleplay 属主包内（R1 拍平迁移回归），包内模块自 shared 根
# 裸名取 atomic_io——两目录都须在 path 上（test_card_lint.py 同款装载形态）。
_CARD_IO_DIR = _SHARED_DIR / "mode_roleplay"
for _p in (str(_SHARED_DIR), str(_CARD_IO_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import card_io  # noqa: E402, I001  (需先推 sys.path 再导入；平铺裸模块 per-file-ignores 先例)
from card_io import load_card, write_png_card  # noqa: E402, I001

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _v2_card_full() -> dict:
    """完整 V2 卡：CJK 内容 + extensions 未知厂商键 + 顶层未知键。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "灵汐",
            "description": "可进化的智能体操作系统",
            "first_mes": "你好，我是灵汐。",
            "alternate_greetings": ["备用开场"],
            "tags": ["测试", "agent"],
            "extensions": {
                "depth_prompt": {"prompt": "深度提示词", "depth": 4, "role": "system"},
                "unknown_vendor_key": {"nested": [1, 2.5, True, None]},
            },
        },
        "unknown_top_level": "顶层未知字段也应保留",
    }


def _v3_card() -> dict:
    """V3 卡：spec 3.0 + extensions。"""
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


def _v2_card_ascii_minimal() -> dict:
    """第三组输入：ASCII 内容、可选字段缺失（信封完整，不补默认值）。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {"name": "tester-01", "description": "plain ascii card", "extensions": {"flag": False}},
    }


def _iter_chunks(raw: bytes) -> list[tuple[bytes, bytes, int]]:
    """手工走查 PNG chunk 序列：[(type, data, crc)]；签名/尾随冗余在此断言。"""
    assert raw.startswith(_PNG_MAGIC), "输出须以 PNG 签名开头"
    chunks: list[tuple[bytes, bytes, int]] = []
    pos = len(_PNG_MAGIC)
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos : pos + 4])
        chunk_type = raw[pos + 4 : pos + 8]
        data = raw[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", raw[pos + 8 + length : pos + 12 + length])
        chunks.append((chunk_type, data, crc))
        pos += 12 + length
    assert pos == len(raw), "chunk 序列后不得有尾随冗余字节"
    return chunks


class TestWritePngRoundTrip:
    @pytest.mark.parametrize(
        ("name", "card"),
        [
            ("v2_full_cjk_with_unknown_fields", _v2_card_full()),
            ("v3_with_extensions", _v3_card()),
            ("v2_ascii_minimal", _v2_card_ascii_minimal()),
        ],
        ids=["v2-full", "v3-ext", "v2-ascii"],
    )
    def test_load_card_of_written_bytes_equals_card(self, name: str, card: dict) -> None:
        """写出的字节经读侧 load_card（bytes/path 双入参）解析回等价卡。"""
        raw = write_png_card(card)
        assert load_card(raw) == card
        assert load_card(bytearray(raw)) == card

    def test_written_bytes_loadable_from_file(self, tmp_path: Path) -> None:
        """落盘路径入参同结果（export 落盘消费形态）。"""
        card = _v2_card_full()
        target = tmp_path / "exported.png"
        target.write_bytes(write_png_card(card))
        assert load_card(target) == card

    def test_pillow_reads_text_chunk(self) -> None:
        """外部读方（Pillow，与 ST 同类消费路径）能取回 tEXt 文本。"""
        from PIL import Image

        card = _v2_card_ascii_minimal()
        import io

        with Image.open(io.BytesIO(write_png_card(card))) as img:
            assert card_io.PNG_KEYWORD_V2 in img.text


class TestWritePngStructure:
    def test_chunk_sequence_and_crc(self) -> None:
        """chunk 序列 = IHDR/IDAT/tEXt/IEND，逐 chunk CRC32(类型+数据) 校验通过。"""
        raw = write_png_card(_v2_card_ascii_minimal())
        chunks = _iter_chunks(raw)
        assert [c[0] for c in chunks] == [b"IHDR", b"IDAT", b"tEXt", b"IEND"]
        for chunk_type, data, crc in chunks:
            assert zlib.crc32(chunk_type + data) == crc, f"{chunk_type!r} CRC 不符"

    def test_ihdr_minimal_legal_1x1_rgba(self) -> None:
        """IHDR = 1x1、8bit、RGBA(6)、无压缩/滤波/隔行偏移——最小合法载体。"""
        chunks = _iter_chunks(write_png_card(_v2_card_ascii_minimal()))
        ihdr = chunks[0][1]
        width, height, depth, color_type, compression, filt, interlace = struct.unpack(
            ">IIBBBBB", ihdr
        )
        assert (width, height) == (1, 1)
        assert (depth, color_type) == (8, 6)
        assert (compression, filt, interlace) == (0, 0, 0)

    def test_idat_inflates_to_single_scanline(self) -> None:
        """IDAT 解压 = 单扫描行（filter 0 + 1 个 RGBA 像素）：图像数据合法最小。"""
        chunks = _iter_chunks(write_png_card(_v2_card_ascii_minimal()))
        idat = next(d for t, d, _ in chunks if t == b"IDAT")
        scanline = zlib.decompress(idat)
        assert scanline == b"\x00\x00\x00\x00\x00"

    def test_iend_empty_and_last(self) -> None:
        """IEND 空载荷且居末（PNG 终止 chunk 契约）。"""
        chunks = _iter_chunks(write_png_card(_v2_card_ascii_minimal()))
        assert chunks[-1][:2] == (b"IEND", b"")


class TestTextChunkPayload:
    def test_payload_is_canonical_base64_json(self) -> None:
        """tEXt 值与规范序列化逐字节相等（load_card 解码的严格逆，删值锚点）。"""
        card = _v2_card_full()
        chunks = _iter_chunks(write_png_card(card))
        _, text_data, _ = next(c for c in chunks if c[0] == b"tEXt")
        keyword, _, value = text_data.partition(b"\x00")
        assert keyword == b"chara"
        expected = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8")).decode("ascii")
        assert value.decode("latin-1") == expected

    @pytest.mark.parametrize(
        ("card", "expected_keyword"),
        [
            (_v2_card_full(), "chara"),
            (_v3_card(), "ccv3"),
            (_v2_card_ascii_minimal(), "chara"),
        ],
        ids=["v2-chara", "v3-ccv3", "v2-minimal-chara"],
    )
    def test_keyword_matches_spec(self, card: dict, expected_keyword: str) -> None:
        """keyword 规则与 save_card_png 同源：V3 卡 ccv3、其余 chara。"""
        chunks = _iter_chunks(write_png_card(card))
        _, text_data, _ = next(c for c in chunks if c[0] == b"tEXt")
        keyword = text_data.partition(b"\x00")[0]
        assert keyword.decode("latin-1") == expected_keyword
