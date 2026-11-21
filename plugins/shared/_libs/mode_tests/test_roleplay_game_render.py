# @feature: FP-0.2.〇 RP批4-Ⅲ 模板渲染+choices/ui 渲染消费 | @vision: V1 可进化 | @ci: python-coverage
"""游戏宿主表现层两件真实断言（RP 批 4-Ⅲ：4d template_pack 模板渲染 + 4e
choices/ui 渲染消费，宿主页 webview JS 单源块，node 抽函数直测）。

- 4d rpRenderTemplate/rpRenderTemplates = game_host.html 引擎单源块内纯函数：
  {{...}} 插值子集（state 点路径/getvar 双形态/user·char 宏/其余走 4a 求值器，
  禁 eval——白名单外宏原样保留）+ 产物整体过 rpSanitizeHtml 消毒（单源 serve-time
  装配自 roleplay_panel.html 的 rp-sanitize:single-source 块——游戏页磁盘不持
  第三份手抄副本，装配测试执法）；
- 4e rpCollectChoices 声明式 choices 归一化（text/action 短写与对象形态）；点击
  =文本入输入区+action 交 4c；自动发送随批 5 接通（契约测试在
  test_roleplay_game_turn.py）；
- 批 4 联调验收：五声明齐（rules+graph+template+choices+ui）玩法包 fixture 在
  无 LLM 输入（narration 恒空）下走通「节点进入→模板渲染→choices 展示→点击→
  handler→快照变更」全链闭环；无宿主/无声明=零变化（渲染件空转返回空集）；
- 装配面：下发游戏页含面板单源块逐字节内容、装配标记消失；磁盘页无第三副本；
  装配标记/单源块缺失 = fail-closed（不静默降级为未消毒渲染面）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEBVIEW_DIR = Path(MODES_DIR) / "mode_roleplay" / "webview"
_GAME_HTML = WEBVIEW_DIR / "game_host.html"
_PANEL_HTML = WEBVIEW_DIR / "roleplay_panel.html"

_ENGINE_MARK = r"rp-engine:single-source-begin \*/([\s\S]*?)/\* rp-engine:single-source-end"
_SANITIZE_MARK = r"rp-sanitize:single-source-begin \*/([\s\S]*?)/\* rp-sanitize:single-source-end"
_INCLUDE_MARK = "/*@agentos-include:rp-sanitize*/"

pytestmark = pytest.mark.unit


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract(mark: str, src: str, tag: str) -> str:
    match = re.search(mark, src)
    assert match is not None, f"{tag} 单源块标记缺失"
    return match.group(1)


def _node() -> str | None:
    return shutil.which("node")


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    node = _node()
    assert node is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        return subprocess.run([node, path], capture_output=True, text=True, check=False)
    finally:
        Path(path).unlink(missing_ok=True)


def _render_sandbox(script_body: str) -> str:
    """消毒单源（面板）+ 引擎单源块（游戏页）+ 断言体 → 单文件 node 脚本。

    消毒块在前：rpRenderTemplate 产物消毒依赖 rpSanitizeHtml（浏览器内为
    serve-time 装配同序；node 侧按同源拼接，与下发页脚本序一致）。
    """
    return (
        _extract(_SANITIZE_MARK, _source(_PANEL_HTML), "rp-sanitize")
        + "\n"
        + _extract(_ENGINE_MARK, _source(_GAME_HTML), "rp-engine")
        + "\n"
        + script_body
    )


def _js_assert(cond_expr: str, fail_tag: str, value_expr: str = "JSON.stringify(out)") -> str:
    return (
        f"if (!({cond_expr})) {{ console.error('{fail_tag}: ' + {value_expr}); process.exit(1); }}\n"
    )


def _run_case(body: str) -> None:
    if _node() is None:
        pytest.skip("node 不可用（webview 无构建线）")
    proc = _run_node(_render_sandbox(body))
    assert proc.returncode == 0, f"渲染断言失败: {proc.stderr or proc.stdout}"


# ── 4d 模板插值子集（宏外文本原样；拒绝/缺席宏原样保留）─────────────────────────

_TPL_CASES = [
    # (text, state, macros, expect_in, expect_not_in)
    ("金币 {{gold}}", {"gold": 50}, {}, ["金币 50"], []),
    ("{{state:hp}}", {"hp": 80}, {}, ["80"], []),
    ("{{state:npc.苏清婉.好感度}}", {"npc": {"苏清婉": {"好感度": 95}}}, {}, ["95"], []),
    ("{{state:deep.a.b}}", {"deep": {"a": {"b": "底"}}}, {}, ["底"], []),
    ("{{gold * 2 + 1}}", {"gold": 50}, {}, ["101"], []),
    ("{{min(gold, 99)}}", {"gold": 50}, {}, ["50"], []),
    ("{{getvar::gold}}", {"gold": 50}, {}, ["50"], []),
    ("{{getvar gold}}", {"gold": 50}, {}, ["50"], []),
    ("{{char}}相遇", {}, {"char": "侠客"}, ["侠客相遇"], []),
    ("{{user}} vs {{char}}", {}, {"user": "旅人", "char": "侠客"}, ["旅人 vs 侠客"], []),
    ("缺失 {{state.nokey}} 尾", {"hp": 1}, {}, ["缺失  尾"], []),
    ("缺失 {{getvar::nope}} 尾", {}, {}, ["缺失  尾"], []),
    ("布尔 {{state.flag}}", {"flag": True}, {}, ["布尔 true"], []),
    ("对象 {{state.obj}}", {"obj": {"k": 1}}, {}, ['对象 {"k":1}'], []),
    ("空值 {{state.nul}}", {"nul": None}, {}, ["空值 "], []),
    ("宏值缺席 {{user}} 占位", {}, {}, ["宏值缺席 {{user}} 占位"], []),
    ("拒绝 {{foo(1)}} 原样", {"hp": 1}, {}, ["拒绝 {{foo(1)}} 原样"], []),
    ("逃逸 {{state.constructor}} 原样", {}, {}, ["{{state.constructor}}"], []),
    ("未闭合 {{state.hp 原", {"hp": 1}, {}, ["未闭合 {{state.hp 原"], []),
    ("纯文本无宏", {"hp": 1}, {}, ["纯文本无宏"], []),
]


@pytest.mark.parametrize(
    ("text", "state", "macros", "expect_in", "expect_not_in"),
    _TPL_CASES,
    ids=[c[0][:16] for c in _TPL_CASES],
)
def test_template_interpolation_forms(
    text: str, state: dict[str, Any], macros: dict[str, str],
    expect_in: list[str], expect_not_in: list[str],
) -> None:
    body = (
        f"var out = rpRenderTemplate({json.dumps(text, ensure_ascii=False)}, "
        f"{json.dumps(state, ensure_ascii=False)}, null, "
        f"{json.dumps(macros, ensure_ascii=False)});\n"
        + _js_assert("out.ok === true", "TPL_FAIL")
        + _js_assert("typeof out.html === 'string'", "TPL_HTML_TYPE")
        + "".join(
            _js_assert(f"out.html.indexOf({json.dumps(s)}) !== -1", "TPL_EXPECT")
            for s in expect_in
        )
        + "".join(
            _js_assert(f"out.html.indexOf({json.dumps(s)}) === -1", "TPL_FORBID")
            for s in expect_not_in
        )
    )
    _run_case(body)


def test_template_output_sanitized() -> None:
    """模板产物过 rpSanitizeHtml：危险标签/属性剥除，白名单标签保留（卡内模板
    注入 HTML 防线）；插值值携带的 HTML 同样消毒。"""
    body = (
        "var out = rpRenderTemplate("
        "'<b>粗</b><script>alert(1)</script><img src=x onerror=alert(1)>{{state.evi}}', "
        "{'evi': '<i>斜</i>'}, null, {});\n"
        + _js_assert("out.ok === true", "SANITIZE_FAIL")
        + _js_assert("out.html.indexOf('<b>粗</b>') !== -1", "WHITELIST_KEPT")
        + _js_assert("out.html.indexOf('<i>斜</i>') !== -1", "VALUE_WHITELIST_KEPT")
        + _js_assert("out.html.toLowerCase().indexOf('script') === -1", "SCRIPT_STRIPPED")
        + _js_assert("out.html.toLowerCase().indexOf('onerror') === -1", "ONERROR_STRIPPED")
        + _js_assert("out.html.toLowerCase().indexOf('<img') === -1", "IMG_STRIPPED")
    )
    _run_case(body)


def test_template_non_string_text_rejected() -> None:
    """text 非字符串 = ok:false（声明面契约，不静默转字符串）。"""
    body = (
        "var out = rpRenderTemplate(42, {}, null, {});\n"
        + _js_assert("out.ok === false", "NON_STRING_OK")
        + _js_assert("typeof out.error === 'string'", "NON_STRING_ERROR")
    )
    _run_case(body)


# ── 4e 声明式 choices 归一化 ──────────────────────────────────────────────────

_CHOICES_EXPECTED = [
    ([{"text": "进山", "action": "go"}], [{"text": "进山", "action": {"id": "go", "params": {}}}]),
    (
        [{"text": "开箱", "action": {"id": "open", "params": {"item": "钥匙"}}}],
        [{"text": "开箱", "action": {"id": "open", "params": {"item": "钥匙"}}}],
    ),
    ([{"text": "离开"}], [{"text": "离开", "action": None}]),
    ([{"text": "坏", "action": 42}], [{"text": "坏", "action": None}]),
    ([{"text": "坏", "action": {}}], [{"text": "坏", "action": None}]),
    ([{"nope": 1}, {"text": ""}, {"text": 3}, "x", None], []),
    ("not-array", []),
    ([], []),
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    _CHOICES_EXPECTED,
    ids=[str(c[0])[:24] for c in _CHOICES_EXPECTED],
)
def test_collect_choices_normalization(raw: Any, expected: list[dict[str, Any]]) -> None:
    body = (
        f"var out = rpCollectChoices({json.dumps(raw, ensure_ascii=False)});\n"
        + _js_assert(
            # JSON.stringify 紧凑序化（无空格分隔），期望值须同口径
            "JSON.stringify(out) === "
            + json.dumps(json.dumps(expected, ensure_ascii=False, separators=(",", ":"))),
            "CHOICES_NORM",
        )
    )
    _run_case(body)


# ── 4d 渲染集：节点进入 + trigger 真值两消费时机 ─────────────────────────────────


def test_templates_node_entry_and_trigger_truthy() -> None:
    """渲染集=当前节点同名模板（优先）+ trigger 真值模板（声明序）；无 trigger 且
    非当前节点=不渲染（模板库条目不随轮全量重放）。"""
    templates = {
        "village": {"text": "村口 {{gold}}"},
        "well": {"trigger": "state.hp < 30", "text": "井边疗伤"},
        "feast": {"trigger": "state.hp > 90", "text": "宴席"},
        "lib": {"text": "从未被引用的库条目"},
    }
    body = (
        f"var tpl = {json.dumps(templates, ensure_ascii=False)};\n"
        + "var out = rpRenderTemplates(tpl, "
        + json.dumps({"node": "village", "state": {"gold": 7, "hp": 20}, "rng": None}, ensure_ascii=False)
        + ");\n"
        + _js_assert("out.ok === true", "TEMPLATES_FAIL")
        + _js_assert("out.items.length === 2", "ITEMS_COUNT")
        + _js_assert("out.items[0].name === 'village' && out.items[0].html.indexOf('7') !== -1", "NODE_FIRST")
        + _js_assert("out.items[1].name === 'well'", "TRIGGER_TRUTHY")
        + "var none = rpRenderTemplates(tpl, "
        + json.dumps({"node": "elsewhere", "state": {"gold": 7, "hp": 50}, "rng": None}, ensure_ascii=False)
        + ");\n"
        + _js_assert("none.items.length === 0", "NO_NODE_NO_TRIGGER_IDLE")
    )
    _run_case(body)


def test_templates_node_template_not_duplicated_and_fail_soft() -> None:
    """节点同名模板不因 trigger 循环重复；text 缺失/非字符串条目跳过（fail-soft）。"""
    templates = {
        "cave": {"trigger": "true", "text": "洞穴"},
        "broken": {"trigger": "true"},
        "badtext": {"trigger": "true", "text": 42},
        "skip": "非映射条目",
    }
    body = (
        f"var tpl = {json.dumps(templates, ensure_ascii=False)};\n"
        + "var out = rpRenderTemplates(tpl, "
        + json.dumps({"node": "cave", "state": {}, "rng": None}, ensure_ascii=False)
        + ");\n"
        + _js_assert("out.ok === true && out.items.length === 1", "NO_DUPLICATE_SKIP")
        + _js_assert("out.items[0].name === 'cave'", "ONLY_CAVE")
        + "var idle = rpRenderTemplates(null, "
        + json.dumps({"node": "cave", "state": {}, "rng": None}, ensure_ascii=False)
        + ");\n"
        + _js_assert("idle.ok === true && idle.items.length === 0", "TIER0_IDLE")
    )
    _run_case(body)


def test_templates_entries_carry_choices() -> None:
    """模板条目可携带 choices（choices 来源=模板/节点声明——渲染消费面透出）；
    非法 action 归一为 null 但条目保留。"""
    templates = {
        "village": {
            "text": "村口",
            "choices": [{"text": "进山", "action": "go"}, {"text": "坏", "action": 1}, {"nope": 1}],
        },
    }
    body = (
        f"var tpl = {json.dumps(templates, ensure_ascii=False)};\n"
        + "var out = rpRenderTemplates(tpl, "
        + json.dumps({"node": "village", "state": {}, "rng": None}, ensure_ascii=False)
        + ");\n"
        + _js_assert("out.items[0].choices.length === 2", "TPL_CHOICES")
        + _js_assert("out.items[0].choices[0].action.id === 'go'", "TPL_CHOICES_ACTION")
        + _js_assert("out.items[0].choices[1].action === null", "TPL_CHOICES_BAD_ACTION_NULL")
    )
    _run_case(body)


# ── 媒体 URL 白名单（4c show_image 与 4e ui 同源同口径）─────────────────────────

_URL_CASES = [
    # (url, allowed)
    ("https://example.com/bg.png", True),
    ("http://example.com/bg.png", True),
    ("/local/portrait.png", True),
    ("data:image/png;base64,iVBORw0=", True),
    ("javascript:alert(1)", False),
    ("data:text/html,<script>", False),
    ("ftp://example.com/f", False),
    ("", False),
]


@pytest.mark.parametrize(("url", "allowed"), _URL_CASES, ids=[c[0][:20] for c in _URL_CASES])
def test_url_allowed_whitelist(url: str, allowed: bool) -> None:
    body = (
        f"var out = rpUrlAllowed({json.dumps(url)});\n"
        + _js_assert(f"out === {json.dumps(allowed)}", "URL_WHITELIST")
    )
    _run_case(body)


# ── 批 4 联调验收：五声明齐玩法包 fixture 全链闭环（无 LLM 输入）──────────────────

_E2E_FIXTURE = {
    "card_id": "card_treasure",
    "rules": {"combat": {"formula": "atk - def"}},
    "narrative_graph": {
        "nodes": [
            {"id": "camp"},
            {
                "id": "cave",
                "actions": [{"id": "sfx_echo", "params": {"name": "echo"}}],
                "choices": [
                    {"text": "开宝箱", "action": "open_chest"},
                    {"text": "原路离开"},
                ],
            },
            {"id": "home", "ending": "满载而归"},
        ],
        "triggers": [{"type": "event", "when": "进洞", "jump_to": "cave"}],
    },
    "actions": [
        {"id": "sfx_echo", "target": "engine", "handler": "play_sfx", "params": {"name": "echo"}},
        {"id": "open_chest", "target": "engine", "handler": "give", "params": {"item": "宝藏"}},
    ],
    "template": {
        "camp": {"text": "营地。金币 {{gold}}。<b>安全</b>"},
        "cave": {"text": "洞窟深处…金币 {{gold}}"},
        "treasure_got": {"trigger": "state.inventory.宝藏 >= 1", "text": "宝藏到手！"},
    },
    "ui": {
        "status_bar_template": "金币 {{gold}} · 位置 {{location}}",
        "bg_image": "https://example.com/cave.png",
        "bgm": "/media/cave.mp3",
    },
}


def test_e2e_full_declaration_pack_loop_without_llm() -> None:
    """五声明齐 fixture：无 LLM 输入（narration 恒空串）走通
    「节点进入→模板渲染→choices 展示→点击→handler→快照变更」全链。"""
    cfg = json.dumps(_E2E_FIXTURE, ensure_ascii=False)
    body = (
        f"var cfg = {cfg};\n"
        + "// 消费 1：事件触发节点进入（无 LLM——narration 恒空，仅事件/状态驱动）\n"
        + "var s0 = {gold: 5, location: '营地'};\n"
        + "var out = rpEvalGraph(cfg.narrative_graph, {state: s0, narration: '', player_text: '', "
        "events: ['进洞'], rng: rpCreateRng(7)});\n"
        + _js_assert("out.ok === true && out.node === 'cave'", "E2E_ENTER")
        + _js_assert("out.actions.length === 1 && out.actions[0].id === 'sfx_echo'", "E2E_NODE_ACTIONS")
        + "// 消费 2：节点 actions 交 4c（进入即执行）\n"
        + "var appliedEntry = rpApplyHandlers(out.actions, cfg.actions, s0, rpCreateRng(7));\n"
        + _js_assert("appliedEntry.ok && appliedEntry.events[0].type === 'sfx'", "E2E_ENTRY_HANDLER")
        + "// 消费 3：模板渲染（节点同名模板；插值+消毒；开箱前宝藏触发器未真）\n"
        + "var tpl = rpRenderTemplates(cfg.template, {node: out.node, state: s0, rng: null, macros: {char: cfg.card_id}});\n"
        + _js_assert("tpl.ok && tpl.items.length === 1 && tpl.items[0].name === 'cave'", "E2E_TPL")
        + _js_assert("tpl.items[0].html.indexOf('金币 5') !== -1", "E2E_TPL_INTERP")
        + "// 消费 4：choices 展示（节点声明归一化）\n"
        + "var choices = rpCollectChoices(cfg.narrative_graph.nodes[1].choices);\n"
        + _js_assert("choices.length === 2 && choices[0].text === '开宝箱'", "E2E_CHOICES")
        + _js_assert("choices[1].action === null", "E2E_CHOICES_NO_ACTION")
        + "// 消费 5：点击开宝箱 → action 交 4c → 快照变更（未声明 id 会被白名单拒绝）\n"
        + "var applied = rpApplyHandlers([{id: choices[0].action.id, params: choices[0].action.params}], "
        "cfg.actions, s0, rpCreateRng(7));\n"
        + _js_assert("applied.ok && applied.results[0].ok === true", "E2E_CLICK_HANDLER")
        + _js_assert("applied.state.inventory['宝藏'] === 1", "E2E_SNAPSHOT_CHANGED")
        + _js_assert("applied.state.gold === 5", "E2E_UNRELATED_UNTOUCHED")
        + "// 消费 6：状态条（4d 同款渲染）+ ui 媒体白名单\n"
        + "var bar = rpRenderTemplate(cfg.ui.status_bar_template, applied.state, null, {});\n"
        + _js_assert("bar.ok && bar.html.indexOf('金币 5') !== -1", "E2E_STATUSBAR")
        + _js_assert("rpUrlAllowed(cfg.ui.bg_image) && rpUrlAllowed(cfg.ui.bgm)", "E2E_UI_URLS")
        + "// 消费 7：状态更新后再求值——trigger 模板随快照翻面（无 LLM 降级轮；\n"
        + "//   __graph_node 随快照回推=宿主权威节点记忆，此处按页面回推形态补写）\n"
        + "applied.state.__graph_node = out.node;\n"
        + "var out2 = rpEvalGraph(cfg.narrative_graph, {state: applied.state, narration: '', "
        "player_text: '', events: [], rng: rpCreateRng(7)});\n"
        + _js_assert("out2.ok && out2.node === 'cave' && out2.jump === null", "E2E_STAY")
        + "var tpl2 = rpRenderTemplates(cfg.template, {node: out2.node, state: applied.state, rng: null});\n"
        + _js_assert("tpl2.items.length === 2 && tpl2.items[1].name === 'treasure_got'", "E2E_TRIGGER_TPL")
        + _js_assert("tpl2.items[1].html.indexOf('宝藏到手') !== -1", "E2E_TPL_RERENDER")
    )
    _run_case(body)


def test_e2e_no_declarations_zero_change() -> None:
    """无宿主/无声明=零变化：渲染件对空声明全空转（Tier 0 兼容态锁定）。"""
    body = (
        "var ctx = {node: 'any', state: {hp: 1}, rng: null};\n"
        + "var tpl = rpRenderTemplates(undefined, ctx);\n"
        + _js_assert("tpl.ok === true && tpl.items.length === 0", "NO_TPL_IDLE")
        + _js_assert("rpCollectChoices(undefined).length === 0", "NO_CHOICES_IDLE")
        + _js_assert("rpEvalGraph(undefined, {state: {}, narration: '', player_text: '', events: []}) === null", "NO_GRAPH_IDLE")
        + "var s = {hp: 1};\n"
        + "var applied = rpApplyHandlers([], [], s, null);\n"
        + _js_assert("applied.ok === true && applied.state.hp === 1 && applied.events.length === 0", "NO_ACTIONS_IDLE")
        + _js_assert("rpUrlAllowed(undefined) === false", "NO_URL")
    )
    _run_case(body)


# ── 装配面：消毒单源 serve-time include（磁盘无第三副本）─────────────────────────


def _load_server() -> Any:
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_game_render_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    module = _load_server()
    module.reset_providers()
    return module


def test_game_page_disk_has_no_third_sanitize_copy() -> None:
    """磁盘游戏页持装配标记、不持手抄消毒函数副本（禁第三份——单源复用硬约束）。"""
    src = _source(_GAME_HTML)
    assert _INCLUDE_MARK in src, "游戏页缺消毒装配标记"
    assert "function rpSanitizeHtml" not in src, "游戏页磁盘出现第三份手抄消毒副本"


_BRIDGE_INCLUDE_MARK = "/*@agentos-include:rp-bridge*/"
_BRIDGE_MARK = r"rp-bridge:single-source-begin \*/([\s\S]*?)/\* rp-bridge:single-source-end"


def test_game_page_disk_has_no_bridge_copy() -> None:
    """宿主桥函数（agentosFetch/unwrap）同走 serve-time include（评审修复刀）：
    磁盘游戏页持装配标记、不持手抄副本——禁第二份手抄（复制必漂移红线）。"""
    src = _source(_GAME_HTML)
    assert _BRIDGE_INCLUDE_MARK in src, "游戏页缺桥装配标记"
    assert "function agentosFetch" not in src, "游戏页磁盘出现手抄桥函数副本"
    assert "function unwrap" not in src, "游戏页磁盘出现手抄 unwrap 副本"


def test_game_page_served_assembles_bridge_single_source(rp: Any) -> None:
    """下发游戏页含面板桥单源块逐字节内容、装配标记消失（与消毒装配同机制）。"""
    result = asyncio.run(rp.http_handle(path="/ext/mode_roleplay/page/game-host", method="GET"))
    assert result["success"] is True
    html = base64.b64decode(result["data"]["body"]).decode("utf-8")
    panel_block = _extract(_BRIDGE_MARK, _source(_PANEL_HTML), "rp-bridge")
    assert _BRIDGE_INCLUDE_MARK not in html, "桥装配标记未替换（下发页带裸标记）"
    assert panel_block in html, "下发页未装配面板桥单源块"
    assert "function agentosFetch" in html


def test_game_page_served_assembles_sanitize_single_source(rp: Any) -> None:
    """下发游戏页含面板单源块逐字节内容、装配标记消失；页内脚本语法成立。"""
    result = asyncio.run(rp.http_handle(path="/ext/mode_roleplay/page/game-host", method="GET"))
    assert result["success"] is True
    html = base64.b64decode(result["data"]["body"]).decode("utf-8")
    panel_block = _extract(_SANITIZE_MARK, _source(_PANEL_HTML), "rp-sanitize")
    assert _INCLUDE_MARK not in html, "装配标记未替换（下发页带裸标记）"
    assert panel_block in html, "下发页未装配面板消毒单源块"
    assert "function rpSanitizeHtml" in html
    # 装配后页内脚本语法成立（node --check；node 缺失 = 择机补跑）
    node = _node()
    if node is None:
        pytest.skip("node 不可用（webview 无构建线）")
    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(match.group(1))
        path = fh.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True, check=False)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, f"装配后脚本 node --check 失败: {proc.stderr}"


def test_game_page_assembly_failclosed_missing_block(
    rp: Any, tmp_path: Any, patch_shared: Any
) -> None:
    """面板单源块缺失 = RuntimeError 上抛（包事实不静默降级为未消毒渲染面）。"""
    broken_panel = tmp_path / "broken_panel.html"
    broken_panel.write_text("<html>无消毒块</html>", encoding="utf-8")
    patch_shared("_PANEL_HTML_PATH", str(broken_panel))  # 消费点在 rp_game 自持绑定
    with pytest.raises(RuntimeError):
        rp._game_page_html_response()


def test_game_page_assembly_failclosed_missing_bridge_block(
    rp: Any, tmp_path: Any, patch_shared: Any
) -> None:
    """面板桥单源块缺失（消毒块在场）= RuntimeError 上抛（装配清单逐项 fail-closed）。"""
    panel_src = _source(_PANEL_HTML)
    sanitize_block = _extract(_SANITIZE_MARK, panel_src, "rp-sanitize")
    broken_panel = tmp_path / "broken_panel.html"
    broken_panel.write_text(
        "<html>/* rp-sanitize:single-source-begin */" + sanitize_block
        + "/* rp-sanitize:single-source-end */无桥块</html>",
        encoding="utf-8",
    )
    patch_shared("_PANEL_HTML_PATH", str(broken_panel))  # 消费点在 rp_game 自持绑定
    with pytest.raises(RuntimeError, match="rp-bridge"):
        rp._game_page_html_response()


def test_game_page_assembly_failclosed_missing_marker(
    rp: Any, tmp_path: Any, patch_shared: Any
) -> None:
    """游戏页装配标记缺失 = RuntimeError 上抛（装配契约 fail-closed）。"""
    broken_page = tmp_path / "broken_game.html"
    broken_page.write_text("<html>无装配标记</html>", encoding="utf-8")
    patch_shared("_GAME_PAGE_HTML_PATH", str(broken_page))  # 消费点在 rp_game 自持绑定
    with pytest.raises(RuntimeError):
        rp._game_page_html_response()


def test_game_page_http_error_shape_on_assembly_failure(
    rp: Any, tmp_path: Any, patch_shared: Any
) -> None:
    """装配失败经 http_handle 面返回 success:False + 500 可读错误（错误传播收口）。"""
    broken_page = tmp_path / "broken_game.html"
    broken_page.write_text("<html>无装配标记</html>", encoding="utf-8")
    patch_shared("_GAME_PAGE_HTML_PATH", str(broken_page))  # 消费点在 rp_game 自持绑定
    result = asyncio.run(rp.http_handle(path="/ext/mode_roleplay/page/game-host", method="GET"))
    assert result["success"] is False
    assert "game page html unavailable" in result["error"]


# ── 页面接线结构断言（4d/4e 消费时机；turn 红线批 5 已解除改锁接线面）─────────────


def test_game_page_4d_4e_wiring_structure() -> None:
    """4d/4e 接线在场：状态条/choices 卡/渲染函数/自推回声防重入；turn 已接通（批 5）。"""
    src = _source(_GAME_HTML)
    for token in (
        "data-testid=\"gh-statusbar\"",
        "data-testid=\"gh-choices\"",
        "gh-flow",
        "renderFlow",
        "renderStatusBar",
        "applyUiStatics",
        "rpRenderTemplate",
        "rpRenderTemplates",
        "rpCollectChoices",
        "rpUrlAllowed",
        "lastSelfPush",  # 自推回声防重入（无 LLM 降级轮闭环）
        "bgmStarted",    # bgm 仅用户交互后起播
    ):
        assert token in src, f"游戏页缺 4d/4e 接线: {token}"
    assert "/game/turn" in src, "turn 通道已接通（批 5 解除批 4 占位红线）"
    assert "game_bridge/turn" not in src, "外部桥 turn 亦不接"
