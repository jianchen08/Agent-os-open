# @feature: FP-0.2.〇 RP批4-Ⅱ-核 游戏宿主引擎件 | @vision: V1 可进化 | @ci: python-coverage
"""游戏宿主引擎三件真实断言（RP 批 4-Ⅱ-核：4a 表达式求值 + 4b narrative_graph
求值 + 4c 通用 handler 库，宿主页 webview JS 单源块，node 抽函数直测）。

- 引擎件 = game_host.html 内 rp-engine:single-source 标记块（纯函数无 DOM 依
  赖），node 侧真实执行——不允许"渲染时自然安全"的不可测说法（面板刀-Ⅱ
  rpSanitizeHtml 先例）；node 缺失 = 跳过（webview 无构建线）；
- 核心验收：两套有区分度玩法 fixture（仙侠伤害公式 vs 星露谷好感事件）跑同
  一求值器/graph 求值器/handler 库——「一引擎多玩法全声明」；
- 沙箱负例：白名单外一切（require/process/constructor 链/eval/白名单外函数/
  字符串拼接/bracket 下标/赋值）静态拒绝返回 error 不执行；eval/Function 禁用
  以源码扫描执法（硬约束）；
- rand seed 注入：同 seed 序列确定（性质断言）；
- 后端声明聚合通道在 test_roleplay_game_config.py。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

WEBVIEW_DIR = (
    Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    / "mode_roleplay" / "webview"
)
_GAME_HTML = WEBVIEW_DIR / "game_host.html"

_MARK = r"rp-engine:single-source-begin \*/([\s\S]*?)/\* rp-engine:single-source-end"

pytestmark = pytest.mark.unit


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_engine(src: str) -> str:
    match = re.search(_MARK, src)
    assert match is not None, "引擎单源块标记缺失（rp-engine:single-source）"
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


def _engine_sandbox(script_body: str) -> str:
    """引擎单源块 + 断言体 → 单文件 node 脚本（用后即删，无工作区残留）。"""
    return _extract_engine(_source(_GAME_HTML)) + "\n" + script_body


def _js_assert(cond_expr: str, fail_tag: str, value_expr: str = "JSON.stringify(out)") -> str:
    """生成一条 JS 断言（条件不满足 → exit(1) 并打印现场）。"""
    return (
        f"if (!({cond_expr})) {{ console.error('{fail_tag}: ' + {value_expr}); process.exit(1); }}\n"
    )


def _run_case(body: str) -> None:
    if _node() is None:
        pytest.skip("node 不可用（webview 无构建线）")
    proc = _run_node(_engine_sandbox(body))
    assert proc.returncode == 0, f"引擎断言失败: {proc.stderr or proc.stdout}"


# ── 硬约束：禁 eval/Function（源码扫描执法）+ 单源块在场 ────────────────────────


def test_engine_block_present_and_free_of_eval_function() -> None:
    """引擎单源块在场；剥除注释后无 eval(/Function( 调用形态（禁 eval/Function 硬约束）。

    行注释（//）同剥（评审修复刀）：注释字样里的 eval(/Function( 不得假阳性——
    剥除只减文本不增文本，行前缀内不可能新造出 eval( 相邻对。"""
    src = _extract_engine(_source(_GAME_HTML))
    code = re.sub(r"/\*[\s\S]*?\*/", "", src)
    code = re.sub(r"//[^\n]*", "", code)
    assert "eval(" not in code, "引擎件禁 eval"
    assert "Function(" not in code, "引擎件禁 Function 构造"
    for fn in ("rpEvalExpr", "rpEvalGraph", "rpApplyHandlers", "rpCreateRng"):
        assert f"function {fn}(" in src, f"引擎件缺 {fn}"


# ── 4a 表达式求值：白名单内正例 ───────────────────────────────────────────────

_EXPR_POSITIVE = [
    # (expr, state, expected)
    ("1 + 2 * 3", {}, 7),
    ("(1 + 2) * 3", {}, 9),
    ("10 / 4", {}, 2.5),
    ("2 - 3 - 4", {}, -5),
    ("-3 + 5", {}, 2),
    ("2 > 1", {}, True),
    ("1 >= 2", {}, False),
    ("3 < 2", {}, False),
    ("2 <= 2", {}, True),
    ("1 == 1", {}, True),
    ("1 != 2", {}, True),
    ("true and false", {}, False),
    ("true or false", {}, True),
    ("not false", {}, True),
    ("not true and true", {}, False),
    ("state.hp > 50", {"hp": 80}, True),
    ("state.hp + state.bonus", {"hp": 80, "bonus": 20}, 100),
    ("atk * 2 + def", {"atk": 3, "def": 4}, 10),
    ("state.npc.苏清婉.好感度 >= 90", {"npc": {"苏清婉": {"好感度": 90}}}, True),
    ("state.world.location == '山门'", {"world": {"location": "山门"}}, True),
    ("state.world.location != '山门'", {"world": {"location": "森林"}}, True),
    ("min(3, 2) + max(1, 5)", {}, 7),
    ("abs(0 - 5)", {}, 5),
    ("round(2.6)", {}, 3),
    ("state.missing == 'x'", {}, False),
    ("not state.missing", {}, True),
    ("state.flag and state.hp > 0", {"flag": True, "hp": 1}, True),
    ("1 + 1 == 2 and 2 > 1", {}, True),
]


@pytest.mark.parametrize(
    ("expr", "state", "expected"),
    _EXPR_POSITIVE,
    ids=[c[0] for c in _EXPR_POSITIVE],
)
def test_expr_positive(expr: str, state: dict[str, Any], expected: Any) -> None:
    body = (
        f"var out = rpEvalExpr({json.dumps(expr)}, {json.dumps(state, ensure_ascii=False)}, null);\n"
        + _js_assert("out.ok === true", "EXPR_FAIL")
        + _js_assert(f"out.value === {json.dumps(expected)}", "EXPR_VALUE")
    )
    _run_case(body)


def test_expr_rand_with_injected_rng_in_bounds() -> None:
    """rand(a,b)：注入 rng 后取值域 [a,b)，seed 同则序列确定（性质断言）。"""
    body = (
        "var rng = rpCreateRng(42);\n"
        "var out = rpEvalExpr('rand(-0.1, 0.1)', {}, rng);\n"
        + _js_assert("out.ok === true", "RAND_FAIL")
        + _js_assert("out.value >= -0.1 && out.value < 0.1", "RAND_BOUNDS")
        + "var seq1 = [];\n"
        + "var rngA = rpCreateRng(7);\n"
        + "for (var i = 0; i < 5; i++) seq1.push(rpEvalExpr('rand(0, 100)', {}, rngA).value);\n"
        + "var seq2 = [];\n"
        + "var rngB = rpCreateRng(7);\n"
        + "for (var i = 0; i < 5; i++) seq2.push(rpEvalExpr('rand(0, 100)', {}, rngB).value);\n"
        + "var seq3 = [];\n"
        + "var rngC = rpCreateRng(8);\n"
        + "for (var i = 0; i < 5; i++) seq3.push(rpEvalExpr('rand(0, 100)', {}, rngC).value);\n"
        + _js_assert("JSON.stringify(seq1) === JSON.stringify(seq2)", "RAND_DETERMINISM")
        + _js_assert("JSON.stringify(seq1) !== JSON.stringify(seq3)", "RAND_SEED_SENSITIVE")
        + _js_assert("seq1.every(function (v) { return v >= 0 && v < 100; })", "RAND_SEQ_BOUNDS")
    )
    _run_case(body)


def test_expr_rejects_rng_unavailable_for_rand() -> None:
    """rand 无 rng（未注入/非函数）= error（不隐式取 Math.random，保可测性）。"""
    body = (
        "var out = rpEvalExpr('rand(0, 1)', {}, null);\n"
        + _js_assert("out.ok === false", "RAND_RNG_FAIL")
        + "var out2 = rpEvalExpr('rand(0, 1)', {}, 'not-a-function');\n"
        + _js_assert("out2.ok === false", "RAND_RNG_TYPE_FAIL")
    )
    _run_case(body)


# ── 4a 沙箱逃逸负例（全部静态拒绝返回 error，不执行不抛异常）──────────────────

_EXPR_SANDBOX_NEGATIVES = [
    "require('fs')",                                # 白名单外函数（Node 注入面）
    "require('child_process')",
    "process.exit(1)",                              # 裸标识符白名单外
    "globalThis",
    "eval('1 + 1')",                                # 禁用函数直呼
    "Function('return 1')()",
    "state.constructor",                            # 原型链属性访问（非自有键）
    "state.__proto__",
    "state.constructor.constructor('return process')()",
    "state['hp']",                                  # bracket 下标（语法面拒绝）
    "1..toString",                                  # 字面量属性访问
    "'a' + 'b'",                                    # 字符串拼接出值域
    "'abc'.length",
    "x = 1",                                        # 赋值（= 非法字符）
    "{}",                                           # 对象字面量
    "while(1){}",                                   # 语句形态
    "state.hp; state.gold",                         # 多语句（; 非法字符）
    "state.hp +",                                   # 畸形表达式（尾随算子）
    "state.hp > '5'",                               # 比较非数值（值域拒绝）
    "'5' + 1",                                      # 字符串参与算术
    "foo(1)",                                       # 白名单外函数调用
    "state.hp.toString()",
    "10 / 0",                                       # 除零（不产出 Infinity）
    # 语法面负例六形态（评审修复刀补钉：白名单文法外语法全拒防回归）
    "state.hp?.x",                                  # 可选链（? 非法字符）
    "'hp' in state",                                # in 运算符（表达式后尾随 token）
    "`abc${1}`",                                    # 模板字符串（反引号非法字符）
    "state.hp, state.gold",                         # 逗号表达式（尾随 token）
    "/abc/.test('x')",                              # 正则字面量（/ 意外 token）
    "state.\\u0068p",                               # Unicode 转义标识符（\ 非法字符）
]


@pytest.mark.parametrize("expr", _EXPR_SANDBOX_NEGATIVES, ids=[c[:28] for c in _EXPR_SANDBOX_NEGATIVES])
def test_expr_sandbox_negatives_static_reject(expr: str) -> None:
    body = (
        f"var out = rpEvalExpr({json.dumps(expr)}, {{'hp': 80}}, null);\n"
        + _js_assert("out.ok === false", "SANDBOX_LEAK")
        + _js_assert("typeof out.error === 'string' && out.error.length > 0", "SANDBOX_NO_ERROR")
    )
    _run_case(body)


def test_expr_deep_nesting_rejected() -> None:
    """深度嵌套括号超上限 = 拒绝（解析深度护栏，不爆栈）。"""
    body = (
        "var deep = '';\n"
        + "for (var i = 0; i < 200; i++) deep += '(';\n"
        + "deep += '1';\n"
        + "for (var i = 0; i < 200; i++) deep += ')';\n"
        + "var out = rpEvalExpr(deep, {}, null);\n"
        + _js_assert("out.ok === false", "DEPTH_LEAK")
        + "var ok = rpEvalExpr('(((1 + 2)))', {}, null);\n"
        + _js_assert("ok.ok === true && ok.value === 3", "DEPTH_NORMAL_BROKEN")
    )
    _run_case(body)


# ── 4b narrative_graph 求值 ──────────────────────────────────────────────────

_XIANXIA_GRAPH = {
    "nodes": [
        {"id": "intro", "decisions": [{"when": "narration matches /你拜入山门/", "next": "sect_life"}]},
        {"id": "sect_life", "decisions": [{"when": "state.hp < 30", "next": "danger"}]},
        {"id": "danger", "ending": "重伤归隐"},
        {"id": "love_confession", "ending": True},
    ],
    "triggers": [
        {"type": "threshold", "when": "state.npc.苏清婉.好感度 >= 90", "jump_to": "love_confession"},
        {"type": "location", "when": "state.world.location == '山门'", "jump_to": "intro"},
    ],
}


def _graph_ctx(**overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "state": {"hp": 80, "npc": {"苏清婉": {"好感度": 10}}, "world": {"location": "森林"}},
        "narration": "",
        "player_text": "",
        "events": [],
    }
    ctx.update(overrides)
    return ctx


def test_graph_decision_regex_hit_and_miss() -> None:
    body = (
        f"var graph = {json.dumps(_XIANXIA_GRAPH, ensure_ascii=False)};\n"
        + "var hit = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(narration="你拜入山门，成为外门弟子"), ensure_ascii=False)
        + ");\n"
        + _js_assert("hit && hit.ok === true", "GRAPH_FAIL")
        + _js_assert("hit.node === 'sect_life'", "REGEX_HIT")
        + _js_assert("hit.jump && hit.jump.by === 'decision' && hit.jump.from === 'intro'", "REGEX_JUMP_META")
        + "var miss = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(narration="山风拂面"), ensure_ascii=False)
        + ");\n"
        + _js_assert("miss.node === 'intro' && miss.jump === null", "REGEX_MISS")
    )
    _run_case(body)


def test_graph_decision_player_text_subject_and_state_condition() -> None:
    graph = {
        "nodes": [
            {"id": "start", "decisions": [
                {"when": "player.text matches /攻击/", "next": "combat"},
                {"when": "state.hp < 30", "next": "flee"},
            ]},
            {"id": "combat"},
            {"id": "flee"},
        ]
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var hit = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(player_text="我要攻击他"), ensure_ascii=False)
        + ");\n"
        + _js_assert("hit.node === 'combat'", "PLAYER_TEXT_HIT")
        + "var cond = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state={"hp": 20}), ensure_ascii=False)
        + ");\n"
        + _js_assert("cond.node === 'flee'", "STATE_COND_HIT")
    )
    _run_case(body)


def test_graph_decision_matches_pattern_length_cap_fail_soft() -> None:
    """graph 决策 matches 正则 pattern 超长（>512 字符）= 该条跳过不编译（ReDoS
    长度护栏，缓解非根治——升级触发见引擎块 docstring）。pattern 若真被编译会
    命中（player_text 同形）而跳转，护栏在场 = 跳过该条 decision 不命中。"""
    long_pattern = "a" * 600
    graph = {
        "nodes": [
            {"id": "start", "decisions": [
                {"when": f"player.text matches /{long_pattern}/", "next": "end"},
            ]},
            {"id": "end"},
        ]
    }
    ctx = _graph_ctx(player_text="a" * 600)
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var out = rpEvalGraph(graph, "
        + json.dumps(ctx, ensure_ascii=False)
        + ");\n"
        + _js_assert("out && out.ok === true", "GRAPH_FAIL")
        + _js_assert("out.jump === null", "PATTERN_CAP_LEAK")
        + _js_assert("out.node === 'start'", "PATTERN_CAP_NODE")
    )
    _run_case(body)


def test_expr_source_length_cap_rejected_before_tokenization() -> None:
    """源串超长（>1024 字符）= 求值前拒绝。长标识符形态（3 token）钉死这是
    源串长度护栏而非 token 数护栏——资源消耗在 tokenize 之前截断（ReDoS
    缓解非根治）；短标识符（906 字符）不误伤。"""
    body = (
        "var longIdent = 'state.' + 'a'.repeat(1100);\n"
        + "var out = rpEvalExpr(longIdent, {}, null);\n"
        + _js_assert("out.ok === false", "SRC_LEN_LEAK")
        + "var shortIdent = 'state.' + 'a'.repeat(900);\n"
        + "var out2 = rpEvalExpr(shortIdent, {}, null);\n"
        + _js_assert("out2.ok === true", "SRC_LEN_OVERBLOCK")
    )
    _run_case(body)


def test_graph_trigger_threshold_exact_and_below() -> None:
    """threshold 恰等（>= 90 于 90）触发；越界下（89）不触发。"""
    at_state = {"hp": 80, "npc": {"苏清婉": {"好感度": 90}}, "world": {"location": "森林"}}
    body = (
        f"var graph = {json.dumps(_XIANXIA_GRAPH, ensure_ascii=False)};\n"
        + "var at = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state=at_state), ensure_ascii=False)
        + ");\n"
        + _js_assert("at.jump && at.jump.by === 'trigger:threshold' && at.jump.to === 'love_confession'", "THRESHOLD_AT")
        + _js_assert("at.node === 'love_confession'", "THRESHOLD_NODE")
        + "var below = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(), ensure_ascii=False)
        + ");\n"
        + _js_assert("below.jump === null", "THRESHOLD_BELOW")
    )
    _run_case(body)


def test_graph_trigger_location_time_event() -> None:
    graph = {
        "nodes": [{"id": "start"}, {"id": "night"}, {"id": "gate"}, {"id": "storm"}],
        "triggers": [
            {"type": "time", "when": "state.world.time_of_day == '子时'", "jump_to": "night"},
            {"type": "location", "when": "state.world.location == '山门'", "jump_to": "gate"},
            {"type": "event", "when": "暴雨", "jump_to": "storm"},
        ],
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var t = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state={"world": {"time_of_day": "子时", "location": "森林"}}), ensure_ascii=False)
        + ");\n"
        + _js_assert("t.jump && t.jump.to === 'night' && t.jump.by === 'trigger:time'", "TIME_TRIGGER")
        + "var l = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state={"world": {"time_of_day": "午时", "location": "山门"}}), ensure_ascii=False)
        + ");\n"
        + _js_assert("l.jump && l.jump.to === 'gate' && l.jump.by === 'trigger:location'", "LOCATION_TRIGGER")
        + "var e = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(events=["暴雨"]), ensure_ascii=False)
        + ");\n"
        + _js_assert("e.jump && e.jump.to === 'storm' && e.jump.by === 'trigger:event'", "EVENT_TRIGGER")
        + "var none = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(), ensure_ascii=False)
        + ");\n"
        + _js_assert("none.jump === null", "TRIGGER_NONE")
    )
    _run_case(body)


def test_graph_trigger_priority_over_decision() -> None:
    """触发器（全局）优先于节点决策（局部）：两者同轮命中 = 触发器胜。"""
    graph = {
        "nodes": [
            {"id": "start", "decisions": [{"when": "narration matches /山门/", "next": "by_decision"}]},
            {"id": "by_decision"},
            {"id": "by_trigger"},
        ],
        "triggers": [
            {"type": "location", "when": "state.world.location == '山门'", "jump_to": "by_trigger"},
        ],
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var out = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(narration="抵达山门", state={"world": {"location": "山门"}}), ensure_ascii=False)
        + ");\n"
        + _js_assert("out.jump && out.jump.by === 'trigger:location' && out.node === 'by_trigger'", "TRIGGER_PRIORITY")
    )
    _run_case(body)


def test_graph_jump_chain_across_rounds() -> None:
    """跳转链：前轮跳转结果（__graph_node 记入 state）驱动后轮继续跳。"""
    graph = {
        "nodes": [
            {"id": "a", "decisions": [{"when": "state.step >= 1", "next": "b"}]},
            {"id": "b", "decisions": [{"when": "state.step >= 2", "next": "c"}]},
            {"id": "c", "ending": "终局"},
        ]
    }
    ctx2 = _graph_ctx(state={"step": 2})
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var r1 = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state={"step": 1}), ensure_ascii=False)
        + ");\n"
        + _js_assert("r1.node === 'b' && r1.ending === null", "CHAIN_R1")
        + "var s2 = "
        + json.dumps(ctx2, ensure_ascii=False)
        + ";\n"
        + "s2.state.__graph_node = r1.node;\n"
        + "var r2 = rpEvalGraph(graph, s2);\n"
        + _js_assert("r2.node === 'c' && r2.ending === '终局'", "CHAIN_R2")
    )
    _run_case(body)


def test_graph_ending_string_and_boolean_label() -> None:
    graph = {
        "nodes": [{"id": "win", "ending": "大团圆"}, {"id": "dead", "ending": True}],
        "triggers": [
            {"type": "event", "when": "胜利", "jump_to": "win"},
            {"type": "event", "when": "阵亡", "jump_to": "dead"},
        ],
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var w = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(events=["胜利"]), ensure_ascii=False)
        + ");\n"
        + _js_assert("w.ending === '大团圆'", "ENDING_LABEL")
        + "var d = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(events=["阵亡"]), ensure_ascii=False)
        + ");\n"
        + _js_assert("d.ending === 'dead'", "ENDING_BOOL_FALLS_TO_NODE_ID")
    )
    _run_case(body)


def test_graph_missing_idle_and_malformed_error() -> None:
    """graph 缺失 = 引擎件空转（返回 null，Tier 0 兼容）；nodes 形态非法 = ok:false。"""
    ctx_json = json.dumps(_graph_ctx(), ensure_ascii=False)
    body = (
        _js_assert(f"rpEvalGraph(null, {ctx_json}) === null", "MISSING_NULL")
        + _js_assert(f"rpEvalGraph(undefined, {ctx_json}) === null", "UNDEF_NULL")
        + f"var bad = rpEvalGraph({{'nodes': 'x'}}, {ctx_json});\n"
        + _js_assert("bad && bad.ok === false", "MALFORMED_OK")
        + f"var empty = rpEvalGraph({{'nodes': []}}, {ctx_json});\n"
        + _js_assert("empty && empty.ok === false", "EMPTY_NODES_OK")
    )
    _run_case(body)


def test_graph_node_actions_surfaced_on_entry_node() -> None:
    """入口节点声明的 actions 随求值产出（handler 消费面，4c 执行）。"""
    graph = {
        "nodes": [
            {"id": "start"},
            {"id": "gift", "actions": [{"id": "give_gift", "params": {"item": "向日葵"}}]},
        ],
        "triggers": [{"type": "event", "when": "送礼", "jump_to": "gift"}],
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var out = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(events=["送礼"]), ensure_ascii=False)
        + ");\n"
        + _js_assert("out.node === 'gift' && out.actions.length === 1", "NODE_ACTIONS")
        + _js_assert("out.actions[0].id === 'give_gift' && out.actions[0].params.item === '向日葵'", "NODE_ACTION_SHAPE")
        + "var stay = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(), ensure_ascii=False)
        + ");\n"
        + _js_assert("stay.actions.length === 0", "STAY_NO_ACTIONS")
    )
    _run_case(body)


def test_graph_unknown_jump_target_and_bad_regex_fail_soft() -> None:
    """jump_to 未知节点/决策正则畸形 = 该条跳过不崩（fail-soft 读面）。"""
    graph = {
        "nodes": [
            {"id": "start", "decisions": [
                {"when": "narration matches /[未闭合/", "next": "nowhere"},
                {"when": "narration matches /命中/", "next": "also_missing"},
            ]},
        ],
        "triggers": [{"type": "event", "when": "e", "jump_to": "ghost"}],
    }
    body = (
        f"var graph = {json.dumps(graph, ensure_ascii=False)};\n"
        + "var out = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(narration="命中"), ensure_ascii=False)
        + ");\n"
        + _js_assert("out && out.ok === true && out.node === 'start' && out.jump === null", "FAIL_SOFT")
    )
    _run_case(body)


# ── 4c 通用 handler 库 ────────────────────────────────────────────────────────

_STARDEW_DECLS = [
    {"id": "give_gift", "target": "engine", "handler": "give", "params": {"item": "向日葵"}},
    {"id": "take_item", "target": "engine", "handler": "take"},
    {"id": "buy_seed", "target": "engine", "handler": "buy"},
    {"id": "go_field", "target": "engine", "handler": "move"},
    {"id": "set_weather", "target": "engine", "handler": "set_flag"},
    {"id": "sfx_water", "target": "engine", "handler": "play_sfx"},
    {"id": "show_portrait", "target": "engine", "handler": "show_image"},
    {"id": "heal_hp", "target": "engine", "handler": "heal"},
    {"id": "hit_npc", "target": "engine", "handler": "damage"},
    {"id": "send_to_game", "target": "game"},
]

_DAMAGE_FORMULA = "atk * (1 + rand(-0.1, 0.1)) - def * 0.5"


def _run_handlers(calls: list[dict[str, Any]], state: dict[str, Any], tail: str) -> None:
    body = (
        f"var out = rpApplyHandlers({json.dumps(calls, ensure_ascii=False)}, "
        f"{json.dumps(_STARDEW_DECLS, ensure_ascii=False)}, "
        f"{json.dumps(state, ensure_ascii=False)}, rpCreateRng(42));\n"
        + tail
    )
    _run_case(body)


def test_handler_give_and_take() -> None:
    _run_handlers(
        [
            {"id": "give_gift"},
            {"id": "give_gift", "params": {"item": "锄头", "count": 2}},
            {"id": "take_item", "params": {"item": "向日葵", "count": 1}},
        ],
        {},
        _js_assert("out.ok === true", "HANDLERS_FAIL")
        + _js_assert("out.state.inventory['向日葵'] === 0", "GIVE_TAKE_TO_ZERO")
        + _js_assert("out.state.inventory['锄头'] === 2", "GIVE_COUNT")
        + _js_assert("out.results.every(function (r) { return r.ok === true; })", "TAKE_OK"),
    )


def test_handler_proto_param_rejected_with_receipt() -> None:
    """inventory/flags 键 __proto__ = 拒绝+回执（不再静默 no-op 回 ok）。

    JS 赋值 obj['__proto__'] = 数值是静默无操作（原型 Setter 吞掉）——回 ok
    却零落账是假成功；原型链键写面拒绝并给可读 error 回执。give/take/buy
    （inventory 键面）与 set_flag（flags 键面）同守卫。"""
    calls = [
        {"id": "give_gift", "params": {"item": "__proto__", "count": 2}},
        {"id": "take_item", "params": {"item": "__proto__", "count": 1}},
        {"id": "buy_seed", "params": {"item": "__proto__", "price": 1}},
        {"id": "set_weather", "params": {"name": "__proto__"}},
    ]
    _run_handlers(
        calls,
        {"gold": 10},
        _js_assert("out.ok === true", "APPLY_FAIL")
        + "for (var i = 0; i < out.results.length; i++) {\n"
        + "  if (out.results[i].ok !== false) { console.error('PROTO_LEAK: ' + JSON.stringify(out.results)); process.exit(1); }\n"
        + "  if (String(out.results[i].error || '').indexOf('__proto__') === -1) { console.error('PROTO_MSG: ' + out.results[i].error); process.exit(1); }\n"
        + "}\n"
        + "if (out.state.inventory !== undefined) { console.error('INV_CREATED'); process.exit(1); }\n"
        + "if (out.state.flags !== undefined) { console.error('FLAG_CREATED'); process.exit(1); }\n"
        + _js_assert("out.state.gold === 10", "GOLD_CHANGED"),
    )


def test_handler_take_insufficient_rejected_no_negative() -> None:
    """take 超持有 = 拒绝且库存不变（无负库存不变量）。"""
    _run_handlers(
        [{"id": "take_item", "params": {"item": "向日葵", "count": 5}}],
        {"inventory": {"向日葵": 3}},
        _js_assert("out.ok === true", "OUTER_FAIL")
        + _js_assert("out.results[0].ok === false", "TAKE_REJECT")
        + _js_assert("out.state.inventory['向日葵'] === 3", "TAKE_UNCHANGED")
        + _js_assert(
            "Object.keys(out.state.inventory).every(function (k) { return out.state.inventory[k] >= 0; })",
            "NO_NEGATIVE",
        ),
    )


def test_handler_buy_success_and_insufficient() -> None:
    _run_handlers(
        [{"id": "buy_seed", "params": {"item": "防风草种子", "price": 10, "count": 2}}],
        {"gold": 25},
        _js_assert("out.ok === true", "BUY_FAIL")
        + _js_assert("out.state.gold === 5", "BUY_DEDUCT")
        + _js_assert("out.state.inventory['防风草种子'] === 2", "BUY_ITEM"),
    )
    _run_handlers(
        [{"id": "buy_seed", "params": {"item": "防风草种子", "price": 10, "count": 3}}],
        {"gold": 25},
        _js_assert("out.results[0].ok === false", "BUY_INSUFF_NOT_REJECTED")
        + _js_assert("out.state.gold === 25", "BUY_GOLD_UNCHANGED")
        + _js_assert("out.state.inventory === undefined", "BUY_NO_ITEM"),
    )


def test_handler_move_and_set_flag() -> None:
    _run_handlers(
        [
            {"id": "go_field", "params": {"to": "牧场"}},
            {"id": "set_weather", "params": {"name": "下雨"}},
        ],
        {"location": "小镇"},
        _js_assert("out.state.location === '牧场'", "MOVE")
        + _js_assert("out.state.flags['下雨'] === true", "FLAG_DEFAULT"),
    )
    _run_handlers(
        [{"id": "set_weather", "params": {"name": "节日", "value": False}}],
        {},
        _js_assert("out.state.flags['节日'] === false", "FLAG_VALUE"),
    )


def test_handler_play_sfx_and_show_image_url_gate() -> None:
    """表现类本刀仅事件占位/URL 校验（媒体播放后置）：合法 URL 产事件，危险 URL 拒绝。"""
    _run_handlers(
        [
            {"id": "sfx_water", "params": {"name": "watering"}},
            {"id": "show_portrait", "params": {"url": "https://example.com/a.png"}},
        ],
        {},
        _js_assert("out.events.length === 2", "PERF_EVENTS")
        + _js_assert("out.events[0].type === 'sfx' && out.events[0].name === 'watering'", "SFX_EVENT")
        + _js_assert("out.events[1].type === 'image' && out.events[1].url === 'https://example.com/a.png'", "IMAGE_EVENT"),
    )
    _run_handlers(
        [{"id": "show_portrait", "params": {"url": "javascript:alert(1)"}}],
        {},
        _js_assert("out.results[0].ok === false", "URL_GATE")
        + _js_assert("out.events.length === 0", "URL_GATE_NO_EVENT"),
    )
    _run_handlers(
        [{"id": "show_portrait", "params": {"url": "/local/portrait.png"}}],
        {},
        _js_assert("out.results[0].ok === true && out.events[0].url === '/local/portrait.png'", "URL_RELATIVE"),
    )


def test_handler_heal_damage_with_expr_amounts() -> None:
    """heal/damage 数值走 4a 表达式（amount 可为表达式字符串，rand 走注入 rng）。"""
    _run_handlers(
        [{"id": "heal_hp", "params": {"path": "player.hp", "amount": "10 + round(2.4)"}}],
        {"player": {"hp": 50}},
        _js_assert("out.state.player.hp === 62", "HEAL_EXPR"),
    )
    _run_handlers(
        [{"id": "hit_npc", "params": {"path": "player.hp", "amount": _DAMAGE_FORMULA}}],
        {"player": {"hp": 100}, "atk": 100, "def": 20},
        _js_assert("out.ok === true", "DAMAGE_FAIL")
        + _js_assert(
            "out.state.player.hp <= 100 - (100 * 0.9 - 10) + 0.001"
            " && out.state.player.hp >= 100 - (100 * 1.1 - 10) - 0.001",
            "DAMAGE_BOUNDS",
        )
        + "var again = rpApplyHandlers([{id: 'hit_npc', params: {path: 'player.hp', amount: '"
        + _DAMAGE_FORMULA
        + "'}}], "
        + json.dumps(_STARDEW_DECLS, ensure_ascii=False)
        + ", {'player': {'hp': 100}, 'atk': 100, 'def': 20}, rpCreateRng(42));\n"
        + _js_assert("again.state.player.hp === out.state.player.hp", "DAMAGE_SEEDED"),
    )


def test_handler_damage_on_missing_path_rejected() -> None:
    _run_handlers(
        [{"id": "hit_npc", "params": {"path": "player.hp", "amount": 10}}],
        {"atk": 1},
        _js_assert("out.results[0].ok === false", "MISSING_PATH")
        + _js_assert("out.state.player === undefined", "MISSING_PATH_NO_WRITE"),
    )


def test_handler_undeclared_id_rejected() -> None:
    """未声明 id 一律拒绝+记录（白名单制）。"""
    _run_handlers(
        [{"id": "nuke_everything"}, {"id": "give_gift"}],
        {},
        _js_assert("out.results[0].ok === false", "UNDECLARED_NOT_REJECTED")
        + _js_assert("out.results[0].error.indexOf('未声明') !== -1", "UNDECLARED_REASON")
        + _js_assert("out.results[1].ok === true", "DECLARED_BROKEN")
        + _js_assert("out.state.inventory['向日葵'] === 1", "UNDECLARED_NO_SIDE_EFFECT"),
    )


def test_handler_target_game_deferred() -> None:
    """target=game 的声明 id = 本刀拒绝执行（game 下发后置，占位）。"""
    _run_handlers(
        [{"id": "send_to_game", "params": {"payload": 1}}],
        {},
        _js_assert("out.results[0].ok === false", "GAME_TARGET_RUN")
        + _js_assert("out.results[0].error.indexOf('game') !== -1", "GAME_TARGET_REASON"),
    )


def test_handler_input_state_not_mutated_and_malformed_calls() -> None:
    """入参 state 不被突变（克隆语义）；calls 非法形态 = ok:false。"""
    decls_json = json.dumps(_STARDEW_DECLS, ensure_ascii=False)
    body = (
        "var input = {gold: 10};\n"
        + "var out = rpApplyHandlers([{id: 'buy_seed', params: {item: '种子', price: 5}}], "
        + decls_json
        + ", input, rpCreateRng(1));\n"
        + _js_assert("out.ok === true && input.gold === 10", "INPUT_MUTATED")
        + f"var bad = rpApplyHandlers('not-array', {decls_json}, {{}}, null);\n"
        + _js_assert("bad.ok === false", "BAD_CALLS_OK")
        + f"var badEntry = rpApplyHandlers([{{nope: 1}}], {decls_json}, {{}}, null);\n"
        + _js_assert("badEntry.ok === true && badEntry.results[0].ok === false", "BAD_ENTRY")
    )
    _run_case(body)


# ── 核心验收：两套有区分度玩法 fixture 跑同一引擎（一引擎多玩法全声明）───────────


def test_fixture_xianxia_combat_formula_same_engine() -> None:
    """仙侠玩法：rules.combat 公式（4a）+ 山门 location 触发器（4b）+ damage handler（4c）。"""
    state = {
        "player": {"气血": 100},
        "atk": 100,
        "def": 20,
        "world": {"location": "森林"},
    }
    body = (
        f"var formula = {json.dumps(_DAMAGE_FORMULA)};\n"
        + "var rng = rpCreateRng(2026);\n"
        + "var dmg = rpEvalExpr(formula, {atk: 100, def: 20}, rng);\n"
        + _js_assert("dmg.ok === true", "XIANXIA_DMG_FAIL")
        + _js_assert(
            "dmg.value >= 100 * 0.9 - 10 - 0.001 && dmg.value <= 100 * 1.1 - 10 + 0.001",
            "XIANXIA_DMG_BOUNDS",
        )
        + "var graph = "
        + json.dumps(_XIANXIA_GRAPH, ensure_ascii=False)
        + ";\n"
        + "var jump = rpEvalGraph(graph, "
        + json.dumps(
            _graph_ctx(state={"hp": 80, "npc": {"苏清婉": {"好感度": 10}}, "world": {"location": "山门"}}),
            ensure_ascii=False,
        )
        + ");\n"
        + _js_assert("jump.node === 'intro' && jump.jump.by === 'trigger:location'", "XIANXIA_TRIGGER")
        + "var combat = rpApplyHandlers([{id: 'hit_npc', params: {path: 'player.气血', amount: formula}}], "
        + json.dumps(_STARDEW_DECLS, ensure_ascii=False)
        + ", "
        + json.dumps(state, ensure_ascii=False)
        + ", rpCreateRng(2026));\n"
        + _js_assert("combat.ok === true", "XIANXIA_HANDLER_FAIL")
        + _js_assert("combat.state.player.气血 === 100 - dmg.value", "XIANXIA_DAMAGE_APPLIED")
    )
    _run_case(body)


def test_fixture_stardew_affection_same_engine() -> None:
    """星露谷玩法：好感 threshold 触发（4b）+ give handler（4c）——同一求值器/graph/handler 库。"""
    stardew_graph = {
        "nodes": [
            {"id": "farm_life"},
            {"id": "love_confession", "ending": "心动告白", "actions": [
                {"id": "give_gift", "params": {"item": "向日葵"}},
            ]},
        ],
        "triggers": [
            {"type": "threshold", "when": "state.npc.阿比.好感度 >= 90", "jump_to": "love_confession"},
        ],
    }
    stardew_state = {"gold": 500, "npc": {"阿比": {"好感度": 92}}}
    body = (
        f"var graph = {json.dumps(stardew_graph, ensure_ascii=False)};\n"
        + "var out = rpEvalGraph(graph, "
        + json.dumps(_graph_ctx(state=stardew_state), ensure_ascii=False)
        + ");\n"
        + _js_assert("out.ok === true && out.node === 'love_confession'", "STARDEW_TRIGGER")
        + _js_assert("out.ending === '心动告白'", "STARDEW_ENDING")
        + _js_assert("out.actions.length === 1 && out.actions[0].id === 'give_gift'", "STARDEW_ACTIONS")
        + "var applied = rpApplyHandlers(out.actions, "
        + json.dumps(_STARDEW_DECLS, ensure_ascii=False)
        + ", "
        + json.dumps(stardew_state, ensure_ascii=False)
        + ", rpCreateRng(1));\n"
        + _js_assert("applied.ok === true", "STARDEW_HANDLER_FAIL")
        + _js_assert("applied.state.inventory['向日葵'] === 1", "STARDEW_GIVE")
        + _js_assert("applied.state.gold === 500", "STARDEW_GOLD_UNTOUCHED")
    )
    _run_case(body)


def test_engine_block_extractable_pure_source() -> None:
    """单源块无 DOM/window/document 依赖（node 直测前提的结构性执法）。"""
    src = _extract_engine(_source(_GAME_HTML))
    for token in ("document.", "window.", "getElementById"):
        assert token not in src, f"引擎单源块含 DOM 依赖 {token}"
