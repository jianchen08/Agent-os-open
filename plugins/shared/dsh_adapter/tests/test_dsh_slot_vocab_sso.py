# @feature: FP-0.2.可观测性 dsh_adapter 插件（槽位词汇属主单源） | @ci: python-coverage
"""DSH 槽位名词汇一致性断言 + 两层诚实回退行为测试。

词汇属主 = translator.DSH_KNOWN_SLOT_NAMES（单源宇宙）；各层只挂目标侧映射：
translator.DSH_SLOT_LINGXI_MAP → 灵汐 UI 槽位（语义侧），server._DSH_SLOT_DOM_ANCHORS
→ 我方 DOM 锚点（位置侧；sidebar.settings 复合形规则留主表保序）。

锁定行为：
- 一致性律：词汇属主 = 语义侧键 ∪ 位置侧锚点键 ∪ 复合形 sidebar.settings
  （登记不挂空转、挂映射不登记均红）；主表 data-slot 规则触达的槽位名 ⊆ 词汇；
- 派生律：主表纯 data-slot 规则逐条等于锚点表派生式（键序=规则序，复合名
  先于其前缀——顺序敏感铁律）；语义/位置两层对同槽位各挂各的目标侧；
- 回退保持：translator 未收录槽位 → direct（诚实边界，不强行归并）；
  server 未收录位置词汇 → 原样透传（惰性，零副作用），单双引号两形都测。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))  # server.py 顶层裸名导入（bridge/translator）可达

# 纯 data-slot 规则的 pattern 前后缀（与主表字面量同形；复合形规则前缀引号序
# 相异——(['\"])——不会误入此筛）
_RULE_PREFIX = r'\[data-slot=(["\'])'
_RULE_SUFFIX = r'\1\]'


def _load(name: str, filename: str) -> Any:
    """按唯一模块名装载平铺裸模块（先弹同名缓存防其它插件目录劫持）。"""
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def translator() -> Any:
    return _load("dsh_translator_vocab_sso", "translator.py")


@pytest.fixture(scope="module")
def server() -> Any:
    return _load("dsh_server_vocab_sso", "server.py")


def _data_slot_rules(server: Any) -> list[tuple[str, str]]:
    """主表中纯 data-slot 规则的（pattern, 替换串）序列（按规则序，字符串手术）。"""
    return [
        (pat.pattern, repl)
        for pat, repl in server._DSH_POSITION_MAP
        if pat.pattern.startswith(_RULE_PREFIX)
    ]


def test_vocabulary_consistency(translator: Any, server: Any) -> None:
    """词汇属主与两侧挂载全等（无孤儿登记、无词汇外挂载）。"""
    vocab = translator.DSH_KNOWN_SLOT_NAMES
    expected_union = (
        set(translator.DSH_SLOT_LINGXI_MAP)
        | set(server._DSH_SLOT_DOM_ANCHORS)
        | {"sidebar.settings"}  # 复合形规则载体，挂在主表不进锚点表
    )
    assert vocab == expected_union
    # 主表 data-slot 规则触达的槽位名全部在词汇内（含复合形 sidebar.settings）
    for pattern, _ in _data_slot_rules(server):
        slot = pattern.removeprefix(_RULE_PREFIX).removesuffix(_RULE_SUFFIX).replace("\\.", ".")
        assert slot in vocab, f"主表 data-slot 规则引用了词汇外槽位: {slot}"


def test_data_slot_rules_derive_from_anchor_map_in_order(server: Any) -> None:
    """派生律：纯 data-slot 规则逐条等于派生式（re.escape 字面量 + 键序=规则序）。"""
    expected = [
        (_RULE_PREFIX + re.escape("sidebar.settings") + _RULE_SUFFIX, r"[data-testid=\1sidebar-user-area\1]"),
        *(
            (_RULE_PREFIX + re.escape(slot) + _RULE_SUFFIX, f"[data-testid=\\1{anchor}\\1]")
            for slot, anchor in server._DSH_SLOT_DOM_ANCHORS.items()
        ),
    ]
    assert _data_slot_rules(server) == expected
    # 顺序敏感铁律：复合名规则先于其前缀规则（前缀先命中会残留错位结构）
    names = [pattern for pattern, _ in _data_slot_rules(server)]
    assert names.index(_RULE_PREFIX + re.escape("conversation.session.header.actions") + _RULE_SUFFIX) < names.index(
        _RULE_PREFIX + re.escape("conversation.session.header") + _RULE_SUFFIX
    )
    assert names.index(_RULE_PREFIX + re.escape("conversation.composer.dock") + _RULE_SUFFIX) < names.index(
        _RULE_PREFIX + re.escape("conversation.composer") + _RULE_SUFFIX
    )


def test_translation_targets_stay_in_own_layer(translator: Any, server: Any) -> None:
    """两层对同槽位各挂各的目标侧：语义层出灵汐槽位名、位置层出我方 testid，互不串写。"""
    for slot in ("conversation.composer", "conversation.composer.dock", "conversation.chat.node"):
        assert translator.map_dsh_slot(slot)["lingxi_slot"] in {
            "chatMessages",
            "chatInteractions",
            "dockItems",
        }
    slot, anchor = "conversation.composer", "chat-composer"
    pat, repl = next(
        (p, r) for p, r in server._DSH_POSITION_MAP if p.pattern == _RULE_PREFIX + re.escape(slot) + _RULE_SUFFIX
    )
    assert pat.sub(repl, f'[data-slot="{slot}"]') == f'[data-testid="{anchor}"]'
    assert pat.sub(repl, f"[data-slot='{slot}']") == f"[data-testid='{anchor}']"


def test_translator_fallback_direct(translator: Any) -> None:
    """回退保持（语义侧）：未收录槽位 → direct（≥2 组有区分度输入）。"""
    for unknown in ("conversation.trajectory", "brand.new.slot"):
        assert translator.map_dsh_slot(unknown)["lingxi_slot"] == "direct"
    # 回退副本隔离：改返回值不污染表（map_dsh_slot 返回 dict 拷贝）
    result = translator.map_dsh_slot("conversation.trajectory")
    result["lingxi_slot"] = "hacked"
    assert translator.map_dsh_slot("conversation.trajectory")["lingxi_slot"] == "direct"


def test_server_fallback_lazy_passthrough(server: Any) -> None:
    """回退保持（位置侧）：词汇外/未收录位置词汇原样透传（单双引号两形）。"""
    for selector in (
        '[data-slot="never.registered.slot"]',
        "[data-slot='never.registered.slot']",
        '[data-pane="unknown-pane"]',
    ):
        assert server._sub_position(selector) == selector
