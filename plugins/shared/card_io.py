"""SillyTavern V2/V3 角色卡读写库 —— PNG（tEXt chunk 嵌元数据）/ JSON 双格式。

角色卡 = 带声明信封的字典：V2 为 ``spec="chara_card_v2"`` + ``spec_version="2.0"``，
V3 为 ``spec="chara_card_v3"`` + ``spec_version="3.0"``，角色数据在 ``data`` 键下
（name / description / personality / scenario / first_mes / mes_example /
alternate_greetings / system_prompt / post_history_instructions / creator_notes /
tags / creator / character_version / extensions）。

PNG 载体按 SillyTavern 生态约定：tEXt chunk，keyword V2 用 ``chara``、V3 用
``ccv3``，值 = base64(utf-8 JSON)；双 chunk 并存时 ``ccv3`` 优先（与 SillyTavern
导入行为一致，读侧另对 keyword 做大小写兜底）。

容忍策略（round-trip 不丢数据）：
- 信封完整（spec 为已知/未知字符串且 data 为 dict）的输入原样返回——未知字段
  （含 extensions 内未知键、顶层未知键）不增不删不改；
- 缺信封的裸字典按 V2 字段子集宽容解析：只认标准字段名（缺的补默认空值：
  字符串字段 ""、alternate_greetings/tags []、extensions {}），未知键原样带进
  data；裸字典顶层的 ``spec``/``spec_version`` 键归位到信封层（不落 data）。

导入形态与 ``atomic_io`` 先例一致：消费方把 ``plugins/shared`` 推上 sys.path
后裸名导入（``from card_io import load_card``）。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
from pathlib import Path

from atomic_io import atomic_write_text
from PIL import Image
from PIL.PngImagePlugin import PngInfo

logger = logging.getLogger(__name__)

__all__ = [
    "PNG_KEYWORD_V2",
    "PNG_KEYWORD_V3",
    "SPEC_V2",
    "SPEC_V3",
    "load_card",
    "save_card_json",
    "save_card_png",
    "validate_card",
]

SPEC_V2 = "chara_card_v2"
SPEC_V3 = "chara_card_v3"
# spec → 对应 spec_version（读写与校验同源）
SPEC_VERSIONS: dict[str, str] = {SPEC_V2: "2.0", SPEC_V3: "3.0"}

# PNG tEXt chunk keyword：V2 生态惯用 `chara`，V3 新增 `ccv3`
PNG_KEYWORD_V2 = "chara"
PNG_KEYWORD_V3 = "ccv3"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# data 下标准字段按缺省空值类型分组（宽容解析补齐用；默认值每次现场构造，
# 不共享可变对象）
_STR_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
    "creator_notes",
    "creator",
    "character_version",
)
_LIST_FIELDS = ("alternate_greetings", "tags")
_DICT_FIELDS = ("extensions",)
# 信封层键：宽容解析包 data 时归位到顶层，不落入 data
_ENVELOPE_KEYS = ("spec", "spec_version")


def load_card(path_or_bytes: str | Path | bytes | bytearray) -> dict:
    """加载角色卡：自动识别 PNG（tEXt chunk）或 JSON，返回带信封的卡字典。

    PNG 输入取 ``ccv3``/``chara`` chunk 的 base64(utf-8 JSON)；JSON 输入
    （文件路径或字节）信封完整时原样返回，裸字典按 V2 宽容解析补默认空值。

    Args:
        path_or_bytes: 角色卡文件路径（str/Path）或文件字节（PNG 或 JSON 均可）

    Returns:
        带 ``spec``/``spec_version``/``data`` 信封的卡字典；未知字段原样保留

    Raises:
        FileNotFoundError: 路径不存在
        ValueError: PNG 解析失败 / 未找到角色卡 chunk / chunk 解码失败 /
            JSON 非法 / JSON 顶层不是对象（均带定位信息）
    """
    raw = _read_bytes(path_or_bytes)
    if raw.startswith(_PNG_MAGIC):
        return _load_from_png(raw)
    return _load_from_json_bytes(raw)


def save_card_png(card: dict, out_path: str | Path, cover_png_bytes: bytes | None = None) -> None:
    """角色卡写为 PNG：tEXt chunk 嵌 base64(utf-8 JSON)。

    V3 卡写 ``ccv3`` chunk，其余（V2/未声明 spec）写 ``chara`` chunk。

    Args:
        card: 带信封的卡字典（未知字段原样序列化，不裁剪）
        out_path: 输出 PNG 路径（父目录需已存在，调用方负责 mkdir）
        cover_png_bytes: 立绘底图字节；None 时生成 1x1 纯色占位图
            （任何 Pillow 可读图像字节均可作底图，统一重编码为 PNG）

    Raises:
        ValueError: cover 字节不是 Pillow 可读图像
        OSError: 写盘失败（调用方定语义）
    """
    payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8")).decode("ascii")
    meta = PngInfo()
    keyword = PNG_KEYWORD_V3 if card.get("spec") == SPEC_V3 else PNG_KEYWORD_V2
    meta.add_text(keyword, payload)
    if cover_png_bytes is None:
        img = Image.new("RGB", (1, 1))
    else:
        try:
            img = Image.open(io.BytesIO(cover_png_bytes))
        except Exception as exc:
            raise ValueError(f"cover 立绘解析失败（需为 PNG 等 Pillow 可读图像字节）: {exc}") from exc
    try:
        img.save(out_path, format="PNG", pnginfo=meta)
    finally:
        img.close()
    logger.debug("[card_io] PNG 角色卡已写出 | keyword=%s | path=%s", keyword, out_path)


def save_card_json(card: dict, out_path: str | Path) -> None:
    """角色卡写为 JSON 文件（utf-8、ensure_ascii=False、原子写防截断）。

    Args:
        card: 带信封的卡字典
        out_path: 输出 JSON 路径（父目录需已存在，调用方负责 mkdir）

    Raises:
        OSError: 写盘失败（目标保持旧内容，tmp 不残留）
    """
    atomic_write_text(out_path, json.dumps(card, ensure_ascii=False, indent=2) + "\n")


def validate_card(card: dict) -> list[str]:
    """校验角色卡结构合法性，返回问题列表（空列表 = 合法）。

    规则：顶层须为对象；``spec`` 须为 V2/V3；``spec_version`` 须与 spec 对应
    （2.0/3.0）；``data`` 须为对象；``data.name`` 须非空字符串；
    ``data.description`` 须为字符串；其余字符串字段 / 列表字段 / extensions
    存在且非 null 时须类型正确（V2/V3 允许可选字段为 null，不视为问题）。
    """
    if not isinstance(card, dict):
        return [f"顶层必须是 JSON 对象，实际 {type(card).__name__}"]
    problems: list[str] = []
    spec = card.get("spec")
    if spec not in SPEC_VERSIONS:
        problems.append(f"spec 必须是 {SPEC_V2!r}/{SPEC_V3!r}，实际 {spec!r}")
    else:
        expected = SPEC_VERSIONS[spec]
        actual = card.get("spec_version")
        if actual != expected:
            problems.append(f"spec_version 必须是 {expected!r}（{spec}），实际 {actual!r}")
    data = card.get("data")
    if not isinstance(data, dict):
        problems.append(f"data 必须是 JSON 对象，实际 {type(data).__name__ if data is not None else '缺失'}")
        return problems
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"data.name 必须是非空字符串，实际 {name!r}")
    description = data.get("description")
    if not isinstance(description, str):
        problems.append(f"data.description 必须是字符串，实际 {description!r}")
    problems.extend(_validate_field_types(data))
    return problems


def _validate_field_types(data: dict) -> list[str]:
    """data 内除 name/description 外的标准字段类型检查（存在且非 null 才查）。"""
    problems: list[str] = []
    for field in _STR_FIELDS:
        if field in ("name", "description"):
            continue
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            problems.append(f"data.{field} 必须是字符串或 null，实际 {type(value).__name__}")
    for field in _LIST_FIELDS:
        value = data.get(field)
        if value is not None and not isinstance(value, list):
            problems.append(f"data.{field} 必须是数组或 null，实际 {type(value).__name__}")
    extensions = data.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        problems.append(f"data.extensions 必须是对象或 null，实际 {type(extensions).__name__}")
    return problems


def _read_bytes(path_or_bytes: str | Path | bytes | bytearray) -> bytes:
    """入参归一为字节：bytes/bytearray 原样取用，str/Path 读文件字节。"""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return bytes(path_or_bytes)
    return Path(path_or_bytes).read_bytes()


def _load_from_png(raw: bytes) -> dict:
    """PNG 字节 → 卡字典：读 tEXt/iTXt 文本 chunk，ccv3 优先、chara 兜底。"""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            texts: dict[str, str] = {str(k): str(v) for k, v in (getattr(img, "text", None) or {}).items()}
    except Exception as exc:
        raise ValueError(f"PNG 图像解析失败（字节损坏或非 PNG）: {exc}") from exc
    for keyword in (PNG_KEYWORD_V3, PNG_KEYWORD_V2):
        if keyword in texts:
            return _load_from_chunk_value(keyword, texts[keyword])
    lowered = {k.lower(): v for k, v in texts.items()}
    for keyword in (PNG_KEYWORD_V3, PNG_KEYWORD_V2):
        if keyword in lowered:
            return _load_from_chunk_value(keyword, lowered[keyword])
    raise ValueError(
        f"PNG 中未找到角色卡元数据 chunk（keyword 须为 {PNG_KEYWORD_V2!r}/{PNG_KEYWORD_V3!r}），"
        f"实际现有 keyword={sorted(texts)}"
    )


def _load_from_chunk_value(keyword: str, value: str) -> dict:
    """单条 chunk 值（base64 utf-8 JSON）→ 卡字典，失败带 keyword 定位。"""
    try:
        card = json.loads(_b64_decode(value))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"角色卡 chunk 解码失败（keyword={keyword!r}，值须为 base64(utf-8 JSON)）: {exc}") from exc
    if not isinstance(card, dict):
        raise ValueError(f"角色卡 chunk 解码后顶层不是 JSON 对象（keyword={keyword!r}，实际 {type(card).__name__}）")
    return _normalize_card(card)


def _b64_decode(value: str) -> bytes:
    """base64 容忍解码：剔除空白、补齐 padding；标准字母表失败回落 URL 安全。"""
    compact = "".join(value.split())
    padded = compact + "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except binascii.Error:
        return base64.urlsafe_b64decode(padded)


def _load_from_json_bytes(raw: bytes) -> dict:
    """JSON 字节 → 卡字典：utf-8 解码 + 顶层对象校验后归一。"""
    try:
        card = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"输入既非 PNG 也非合法 JSON（utf-8 解析失败）: {exc}") from exc
    if not isinstance(card, dict):
        raise ValueError(f"JSON 顶层必须是对象，实际 {type(card).__name__}")
    return _normalize_card(card)


def _normalize_card(card: dict) -> dict:
    """信封归一：spec + data dict 齐备即原样返回；否则按 V2 宽容包裹。"""
    spec = card.get("spec")
    if isinstance(spec, str) and isinstance(card.get("data"), dict):
        return card
    return _wrap_loose_v2(card)


def _wrap_loose_v2(raw: dict) -> dict:
    """裸字典 → V2 信封卡：标准字段缺省补空值，未知键原样保留进 data。"""
    data: dict = {k: v for k, v in raw.items() if k not in _ENVELOPE_KEYS}
    for field in (*_STR_FIELDS, *_LIST_FIELDS, *_DICT_FIELDS):
        if field not in data:
            data[field] = _default_for(field)
    return {"spec": SPEC_V2, "spec_version": SPEC_VERSIONS[SPEC_V2], "data": data}


def _default_for(field: str) -> str | list | dict:
    """标准字段的默认空值（可变类型每次新造，不共享实例）。"""
    if field in _LIST_FIELDS:
        return []
    if field in _DICT_FIELDS:
        return {}
    return ""
