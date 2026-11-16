# @feature: B9 卡字段词汇单源化 | @ci: python-coverage
"""卡字段词汇属主单源机械断言（card_io 属主，server 派生）。

锁定行为：
- 覆盖律（server 表 ⊇ card_io 表，唯二登记例外）：card_io 三词汇表全部字段
  在 server 三表中有着落——默认落字符串归一表；creator 登记落透传表（ST V3
  作者署名载体，有意设计）；character_version 登记不入任何 server 表（归属
  分歧按现状行为保持）；
- 派生律（序保持）：server 三表逐位置等于 card_io 属主表 + 模式局部扩展的
  派生式——card_io 新增标准字段自动随属主流转，server 侧零改动；server 侧
  直书新增（绕过属主）立即红；
- 局部扩展律：模式扩展字段（lorebook_ids/pack_ids/avatar/voice_ref/
  state_schema/cover/portrait）不得倒灌 card_io 属主表；
- 正交律：server 三表两两无交（一字段跨表 = 归一与透传双语义，必是漂移）；
- 归一行为锚：_card_from_data 对含 creator/character_version 的源数据——
  creator 原样透传、character_version 不落键；两组有区分度输入（全字段
  携带 / 全缺省）钉默认空值与 name 回落卡 id。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_MODES_DIR = Path(__file__).resolve().parents[2]
_CARD_DIR = _MODES_DIR / "mode_roleplay"
# card_io 在 mode_roleplay 属主包内；包内模块自 shared 根裸名取 SDK 引导，
# 两目录都须在 path 上（tests/test_card_io.py 同款装载形态）。
for _p in (str(_MODES_DIR), str(_CARD_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import card_io  # noqa: E402  (需先推 sys.path 再导入；平铺裸模块先例)

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(str(_MODES_DIR), "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_card_vocab_roleplay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _server_tables() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return _load_server()._card_vocab()


def test_server_tables_cover_card_io_vocab() -> None:
    """覆盖律：server 三表并集 ⊇ card_io 三表并集，减登记例外 character_version。"""
    str_fields, list_fields, passthrough = _server_tables()
    io_union = {*card_io.STR_FIELDS, *card_io.LIST_FIELDS, *card_io.DICT_FIELDS}
    server_union = {*str_fields, *list_fields, *passthrough}
    assert io_union - {"character_version"} <= server_union
    assert "character_version" not in server_union
    # creator 处置：不在字符串归一表、在透传表（署名载体，缺席卡 None 不落盘）
    assert "creator" not in str_fields
    assert "creator" in passthrough


def test_server_tables_derive_from_card_io_in_order() -> None:
    """派生律：三表逐位置等于属主表派生式（序保持，禁 server 侧直书副本）。"""
    str_fields, list_fields, passthrough = _server_tables()
    assert str_fields == tuple(f for f in card_io.STR_FIELDS if f not in ("creator", "character_version"))
    assert list_fields == (*card_io.LIST_FIELDS, "lorebook_ids", "pack_ids")
    assert passthrough == ("creator", "avatar", "voice_ref", "state_schema", "cover", "portrait", *card_io.DICT_FIELDS)


def test_mode_extensions_stay_local() -> None:
    """局部扩展律：模式扩展字段只随 server 表存在，不倒灌 card_io 属主表。"""
    io_union = {*card_io.STR_FIELDS, *card_io.LIST_FIELDS, *card_io.DICT_FIELDS}
    for ext in ("lorebook_ids", "pack_ids", "avatar", "voice_ref", "state_schema", "cover", "portrait"):
        assert ext not in io_union


def test_server_tables_pairwise_disjoint() -> None:
    """正交律：字符串归一/列表/透传三表两两无交（跨表双语义必是漂移）。"""
    str_fields, list_fields, passthrough = _server_tables()
    assert not set(str_fields) & set(list_fields)
    assert not set(str_fields) & set(passthrough)
    assert not set(list_fields) & set(passthrough)


def test_card_from_data_full_fields() -> None:
    """归一行为锚（全字段携带）：str 归一/列表归一/透传真值/分歧字段不落键。"""
    module = _load_server()
    data: dict[str, Any] = {
        "name": "  月影  ",
        "description": "介绍",
        "tags": ["奇幻", 7],
        "alternate_greetings": ["线A", "线B"],
        "lorebook_ids": ["lb1"],
        "pack_ids": ["pk1", 2],
        "creator": "洪流",
        "character_version": "1.2.3",
        "avatar": "🎬",
        "extensions": {"depth_prompt": "x"},
        "unknown_future_field": {"keep": True},
    }
    card = module._card_from_data(data, "card_x", "builtin")
    assert card["name"] == "  月影  "
    assert card["description"] == "介绍"
    assert card["tags"] == ["奇幻", "7"]
    assert card["alternate_greetings"] == ["线A", "线B"]
    assert card["pack_ids"] == ["pk1", "2"]
    assert card["lorebook_ids"] == ["lb1"]
    assert card["creator"] == "洪流"
    assert card["avatar"] == "🎬"
    assert card["extensions"] == {"depth_prompt": "x"}
    assert "character_version" not in card
    # 词表外未知键不落输出卡（归一只认三表 + theme；宽容透传指表内字段原样）
    assert "unknown_future_field" not in card


def test_card_from_data_missing_fields_defaults() -> None:
    """归一行为锚（全缺省）：默认空值 + name 回落卡 id + creator 缺席透传 None。"""
    module = _load_server()
    card = module._card_from_data({}, "card_y", "user")
    assert card["id"] == "card_y"
    assert card["origin"] == "user"
    assert card["name"] == "card_y"
    assert card["description"] == ""
    assert card["tags"] == []
    assert card["alternate_greetings"] == []
    assert card["lorebook_ids"] == []
    assert card["pack_ids"] == []
    assert card["creator"] is None
    assert card["extensions"] is None
    assert card["theme"] is None
