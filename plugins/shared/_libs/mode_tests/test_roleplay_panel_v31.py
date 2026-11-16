# @feature: FP-0.2.〇 RP面板v3.1刀2/刀4 | @vision: V1 可进化 | @ci: python-coverage
r"""RP 面板 v3.1 测试：刀2 四视图 IA 重构 + 刀4「工作区玩法页全 webview 化」
IA 收敛 + 刀8「首页记录显式化」。面板核心纯函数单源块 node 直测 + 新视图源码
锚面（大厅/玩法子页/组合 builder/我的▾）。

- rp-panel-core 单源块（roleplay_panel.html 内，无 DOM/state 依赖）：rp_init
  快照串组装（转义契约单源在 agent_config_load RP_INIT_KEY 注释：段内按第一
  个 = 切分故绝对 op 整串归 v；v 内 \,=逗号、\\=反斜杠、k 不转义）、谱系折叠
  （pure=仅①/world=①②/full=全部，非法或缺省档按 full）、会话按包归属过滤
  （历史记录区）、wizard_profile 拼接、自定义玩法命名与大厅副标——node 抽
  块直测（game_render 测试同机制），≥2 组区分度输入含转义例；刀4 起该块经服
  务端 rp-core 可选装配注入 webview 页（包页/game_host 页内 setup 同源消费）；
- 源码锚面：大厅载荷渲染（零硬编码包名铁律由 data-driven 测试另一面执法）、
  玩法子页 IA（刀8：记录落地页=历史记录+显式「新开一局」，has_page 包不再静默
  直落最新一局；游戏区在选中记录/新开后呈现）、combo save/delete 消费、我的▾
  三入口、自包含玩法页 document 注入机制（含刀4 pack 通道取页与 game_host 工作
  区签挂载、rp:session-bound 同步锚）；纯玩法 setup 段收敛出面板由收敛改钉执法。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PANEL_HTML = Path(MODES_DIR) / "mode_roleplay" / "webview" / "roleplay_panel.html"

_CORE_MARK = r"rp-panel-core:single-source-begin \*/([\s\S]*?)/\* rp-panel-core:single-source-end"

pytestmark = pytest.mark.unit


def _panel_source() -> str:
    return _PANEL_HTML.read_text(encoding="utf-8")


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None, "node 不可用（webview 无构建线，核心块直测无法执行）"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        return subprocess.run([node, path], capture_output=True, text=True, check=False)
    finally:
        Path(path).unlink(missing_ok=True)


def _core_case(body: str) -> None:
    """核心单源块 + 断言体 → node 单文件执行（断言失败以 stderr 判红）。"""
    match = re.search(_CORE_MARK, _panel_source())
    assert match is not None, "rp-panel-core 单源块标记缺失"
    proc = _run_node(match.group(1) + "\n" + body)
    assert proc.returncode == 0, f"核心块断言失败: {proc.stderr or proc.stdout}"


# ── rp_init 快照串组装（转义契约；消费端解析单源在 agent_config_load）──────────


def test_rp_init_snapshot_basic_and_absolute_op() -> None:
    """≥2 组区分度输入：相对 op（+5）与绝对 op（=重伤，段内按第一个 = 切分后
    整串归 v）——组装端只需 k=v 直拼，绝对 op 首字符等号不转义。"""
    _core_case(
        """
var out1 = rpInitSnapshot([{k: 'npc.阿比.好感度', v: '+5'}]);
if (out1 !== 'npc.阿比.好感度=+5') { console.error('case1: ' + out1); process.exit(1); }
var out2 = rpInitSnapshot([{k: 'player.伤势', v: '=重伤'}]);
if (out2 !== 'player.伤势==重伤') { console.error('case2: ' + out2); process.exit(1); }
"""
    )


def test_rp_init_snapshot_escape_comma_and_backslash() -> None:
    """转义例：值内逗号以 \\, 承载、反斜杠以 \\\\ 承载（反斜杠先替换——否则
    后替换的逗号转义会被二次转义破坏）；空 pairs = 空串（键缺省）。"""
    _core_case(
        """
var out = rpInitSnapshot([{k: 'k1', v: 'a,b'}]);
if (out !== 'k1=a\\\\,b') { console.error('comma: ' + out); process.exit(1); }
var out2 = rpInitSnapshot([{k: 'k1', v: 'a\\\\b'}]);
if (out2 !== 'k1=a\\\\\\\\b') { console.error('backslash: ' + out2); process.exit(1); }
var out3 = rpInitSnapshot([{k: 'k1', v: 'a\\\\,b'}]);
if (out3 !== 'k1=a\\\\\\\\\\\\,b') { console.error('mixed: ' + out3); process.exit(1); }
if (rpInitSnapshot([]) !== '') { console.error('empty nonzero'); process.exit(1); }
"""
    )


def test_rp_init_snapshot_parse_roundtrip_shape() -> None:
    """组装产物回代解析端同构切分（模拟 agent_config_load _split_escaped 口径）：
    多段切分 + 段内按第一个 = 切分 + 值域反转义，语义往返一致。"""
    _core_case(
        """
function splitEscaped(text, sep) {
  var parts = [], buf = [], esc = false;
  for (var i = 0; i < text.length; i++) {
    var ch = text.charAt(i);
    if (esc) { buf.push('\\\\'); buf.push(ch); esc = false; }
    else if (ch === '\\\\') { esc = true; }
    else if (ch === sep) { parts.push(buf.join('')); buf = []; }
    else { buf.push(ch); }
  }
  if (esc) buf.push('\\\\');
  parts.push(buf.join(''));
  return parts;
}
function unescapeCommas(v) {
  var out = [], esc = false;
  for (var i = 0; i < v.length; i++) {
    var ch = v.charAt(i);
    if (esc) { out.push(ch === ',' ? ',' : (ch === '\\\\' ? '\\\\' : '\\\\' + ch)); esc = false; }
    else if (ch === '\\\\') { esc = true; }
    else { out.push(ch); }
  }
  if (esc) out.push('\\\\');
  return out.join('');
}
var snap = rpInitSnapshot([
  {k: 'npc.阿比.好感度', v: '+5'},
  {k: 'player.伤势', v: '=重伤,昏迷'},
  {k: 'player.备注', v: '含\\\\斜杠'}
]);
var got = {};
splitEscaped(snap, ',').forEach(function (seg) {
  var idx = seg.indexOf('=');
  got[seg.slice(0, idx)] = unescapeCommas(seg.slice(idx + 1));
});
if (got['npc.阿比.好感度'] !== '+5') { console.error('rt1: ' + JSON.stringify(got)); process.exit(1); }
if (got['player.伤势'] !== '=重伤,昏迷') { console.error('rt2: ' + JSON.stringify(got)); process.exit(1); }
if (got['player.备注'] !== '含\\\\斜杠') { console.error('rt3: ' + JSON.stringify(got)); process.exit(1); }
"""
    )


def test_rp_init_pairs_collects_route_and_trait_effects() -> None:
    """effects 聚合：路线与词条 effects 合并为有序对；无 effects 条目跳过；
    多词条同键共现（后选覆盖语义归消费端结算，组装端保序透传）。"""
    _core_case(
        """
var route = {id: 'r1', effects: {a: '+1'}};
var traits = [{id: 't1', profile: 'p1'}, {id: 't2', effects: {b: '=x', c: '+2'}}];
var pairs = rpInitPairs(route, traits);
if (pairs.length !== 3) { console.error('len: ' + JSON.stringify(pairs)); process.exit(1); }
if (pairs[0].k !== 'a' || pairs[1].k !== 'b' || pairs[2].k !== 'c') {
  console.error('order: ' + JSON.stringify(pairs)); process.exit(1);
}
if (rpInitPairs(null, [{id: 't1'}]).length !== 0) { console.error('no-effects nonzero'); process.exit(1); }
"""
    )


# ── 谱系折叠 / 会话按包过滤 / profile 拼接 / 组合命名 ─────────────────────────


def test_rp_combo_fold_lineage_ranks() -> None:
    """谱系折叠：pure=仅①（rank 0）/ world=①②（rank 1）/ full=全部（rank 2）；
    非法或缺省档按 full（服务端归一剔除后面板缺省口径）；多包取最窄档；
    不选包 = full 不折叠。"""
    _core_case(
        """
var packs = [
  {id: 'p_pure', ui: {lineage: 'pure'}},
  {id: 'p_world', ui: {lineage: 'world'}},
  {id: 'p_full', ui: {lineage: 'full'}},
  {id: 'p_bare'},
  {id: 'p_bad', ui: {lineage: 'epic'}}
];
if (rpComboFold(packs, ['p_pure']) !== 0) { console.error('pure'); process.exit(1); }
if (rpComboFold(packs, ['p_world']) !== 1) { console.error('world'); process.exit(1); }
if (rpComboFold(packs, ['p_full']) !== 2) { console.error('full'); process.exit(1); }
if (rpComboFold(packs, ['p_bare']) !== 2) { console.error('bare'); process.exit(1); }
if (rpComboFold(packs, ['p_bad']) !== 2) { console.error('bad'); process.exit(1); }
if (rpComboFold(packs, ['p_full', 'p_pure']) !== 0) { console.error('narrowest'); process.exit(1); }
if (rpComboFold(packs, []) !== 2) { console.error('empty'); process.exit(1); }
"""
    )


def test_rp_sessions_for_pack_filter() -> None:
    """会话按包归属过滤（历史记录区/大厅徽标共用）：pack_ids 命中即留、保序；
    无归属/空表 = 空（诚实空态）。"""
    _core_case(
        """
var sessions = [
  {id: 's1', pack_ids: ['pa', 'pb']},
  {id: 's2', pack_ids: []},
  {id: 's3'},
  {id: 's4', pack_ids: ['pb']}
];
var hit = rpSessionsForPack(sessions, 'pb');
if (hit.length !== 2 || hit[0].id !== 's1' || hit[1].id !== 's4') {
  console.error('hit: ' + JSON.stringify(hit)); process.exit(1);
}
if (rpSessionsForPack(sessions, 'px').length !== 0) { console.error('miss nonzero'); process.exit(1); }
if (rpSessionsForPack(null, 'pa').length !== 0) { console.error('null nonzero'); process.exit(1); }
"""
    )


def test_rp_record_badges_retired() -> None:
    """退役改钉（用户裁定 2026-10-15：首页玩法卡不放记录，记录只住在玩法子页
    「历史记录」区）——徽标三函数自核心块整段拔除，卡面零记录元素。"""
    src = _panel_source()
    assert "rpRecordBadgeText" not in src
    assert "rpPackRecordBadge" not in src
    assert "rpComboRecordBadge" not in src
    hall = src.split("function hallPackCardHtml")[1].split("function hallComboCardHtml")[0]
    assert "🎬" not in hall


def test_rp_profile_from_selection_and_combo_naming() -> None:
    """wizard_profile 拼接：路线 profile → 词条 profile → 角色配置基段（全缺 =
    空串=键缺省）；组合命名 = 卡名 × 书名序；大厅副标按字段有无拼装。"""
    _core_case(
        """
var p1 = rpProfileFromSelection(
  {id: 'r', profile: '路线设定'},
  [{profile: '词条一'}, {profile: '词条二'}, {}],
  '能力：剑术');
if (p1 !== '路线设定；词条一；词条二；能力：剑术') { console.error('p1: ' + p1); process.exit(1); }
if (rpProfileFromSelection(null, [], '') !== '') { console.error('p2 nonzero'); process.exit(1); }
if (rpProfileFromSelection(null, [{profile: '仅词条'}], '') !== '仅词条') {
  console.error('p3: '); process.exit(1);
}
if (rpComboName('毒师', ['剑宗世界观', '镇民志']) !== '毒师 × 剑宗世界观 × 镇民志') {
  console.error('name1'); process.exit(1);
}
if (rpComboName('独行卡', []) !== '独行卡') { console.error('name2'); process.exit(1); }
var sum = rpComboSummary({card_id: 'c', lorebook_ids: ['a', 'b'], pack_ids: [], profile_extra: '开场重伤'}, '毒师');
if (sum.indexOf('卡「毒师」') < 0 || sum.indexOf('世界书 ×2') < 0 || sum.indexOf('初始状态') < 0) {
  console.error('sum: ' + sum); process.exit(1);
}
if (rpComboSummary({card_id: 'c'}, '毒师').indexOf('玩法包') > -1) {
  console.error('sum-empty: ' + sum); process.exit(1);
}
"""
    )


# ── 源码锚面：四视图 IA（大厅 / 玩法子页 / 组合 builder / 我的▾）──────────────


def test_panel_hall_renders_from_endpoint_payloads() -> None:
    """大厅载荷渲染：玩法包卡与自定义组合卡全部自 state.packs/state.combos
    映射生成（零硬编码包名铁律由 data-driven 测试另一面执法）；icon 缺省🎲、
    谱系徽标按 origin 二值、has_page 标识、谱系阶梯说明条与新的组合虚线卡在场。
    刀7 设计保真改钉（对照设计稿走查，分类 a 修正）：卡头图 96px、网格 14px 间距、
    阶梯 pill 样式、大厅副标行、虚线卡副注两行、组合卡封面=绑定卡 avatar 回落 🧩。
    刀8 记录徽标已随用户裁定 2026-10-15 退役（记录只住在玩法子页「历史记录」
    区，首页卡面零记录元素）。"""
    src = _panel_source()
    assert "state.packs.map(hallPackCardHtml)" in src
    assert "state.combos.map(hallComboCardHtml)" in src
    hall = src.split("function hallPackCardHtml")[1].split("function hallComboCardHtml")[0]
    assert "ui.icon || '🎲'" in hall
    assert "出厂玩法包" in hall
    assert "自定义玩法" in hall
    assert "p.has_page" in hall
    assert "rpPackRecordBadge" not in hall, "首页卡面零记录徽标（裁定退役）"
    assert "rec" not in hall, "无徽标残留"
    assert ".rp-hcv { height: 96px" in src, "卡头图 96px（设计稿对齐）"
    assert ".rp-hall { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 14px" in src, "大厅网格 14px 间距"
    combo = src.split("function hallComboCardHtml")[1].split("function hallLadderHtml")[0]
    assert "我的组合" in combo
    assert "rpComboSummary(" in combo
    assert "card.avatar" in combo, "组合卡封面=绑定卡 avatar（回落 🧩）"
    assert "rpComboRecordBadge" not in combo, "组合卡零记录徽标（裁定退役）"
    assert "rp-new-combo" in src
    assert "选卡+世界书+初始状态，存为自定义玩法" in src, "虚线卡副注两行（设计稿）"
    welcome = src.split("function showWelcome(")[1].split("function rpNewCombo")[0]
    assert "首页。点玩法进去" not in welcome, "大厅副标话术退役（刀10 用户裁定：解释性文字不进用户可见面）"
    ladder = src.split("function hallLadderHtml")[1].split("function showWelcome")[0]
    assert "完整玩法包" in ladder
    assert "＋世界观" in ladder
    assert 'class="rp-step accent"' in ladder, "阶梯末档 accent pill（设计稿样式）"


def test_panel_play_subpage_shell_history_top_game_below() -> None:
    """刀10 子页重排改钉（用户裁定 2026-10-17「进入页面=上历史条目、下玩法页；
    不需要分类不需要两个标签；无历史直接玩法」）：玩法子页壳=历史区+玩法区两容
    器，玩法页常驻其下（有历史=历史上/玩法下；无历史=直接玩法页，零空态占位）；
    「会话·对话流」「工作区·玩法界面」两页签与开发者话术 crumb（「玩法子页：
    先见历史记录…面板只做入口」）退役；顶部显著「🆕 新开一局」+「‹ 玩法大厅」。
    openPlay 落地 sessionId=null（新开显式，不静默直绑最新一局）；点记录行=
    bindPlaySession 进该局（data-sid 属性委托，不拼 onclick JS 汇点）；历史区独
    立渲染（壳持久化：数据刷新/选行只重绘该区不重建壳）。"""
    src = _panel_source()
    openplay = src.split("function openPlay(")[1].split("function startNewPlay")[0]
    assert "sessionId: null, newPlay: false" in openplay
    assert "shellPack: null" in openplay, "壳构建标记（renderPlay 换包重建壳）"
    assert "rpSessionsForPack" not in openplay, "落地不直绑最新一局（新开显式）"
    startnew = src.split("function startNewPlay(")[1].split("function bindPlaySession")[0]
    assert "p.sessionId = null" in startnew
    assert "p.newPlay = true" in startnew
    assert "playMount.key !== 'pack:' + p.packId" in startnew, \
        "从某局页切回 setup：废键强制重挂；已在 setup 页幂等不动"
    bind = src.split("function bindPlaySession(")[1].split("function renderPlayHistory")[0]
    assert "p.sessionId = sid" in bind
    assert "p.newPlay = false" in bind
    render = src.split("function renderPlay(")[1].split("// 页内开演回执")[0]
    assert "🆕 新开一局" in render, "顶部显著「🆕 新开一局」按钮（主区头动作位）"
    assert "startNewPlay()" in render, "新开按钮接线 startNewPlay"
    assert "play-history" in render and "play-game" in render, "壳=历史区+玩法区两容器"
    assert "玩法页装载中" in render, "玩法页常驻（无历史也直接呈现玩法区）"
    assert "rp-crumb" not in render, "开发者话术 crumb 退役（用户裁定）"
    assert "先见历史记录" not in src and "点某局进该局页面" not in src, \
        "子页 crumb 与大厅副标话术退役（解释性文字不进用户可见面）"
    assert "会话·对话流" not in src and "工作区·玩法界面" not in src, "两页签退役（用户裁定）"
    assert "playSubTabsHtml" not in src and "function playSubTab" not in src, "页签函数退役"
    hist = src.split("function renderPlayHistory(")[1].split("function mountPlayPageHtml")[0]
    assert "▶ 历史记录" in hist
    assert "if (!rows.length) { box.innerHTML = ''; return; }" in hist, "无记录=整区不渲染（零占位）"
    assert "rpSessionsForPack(state.sessions, pack.id)" in hist
    assert "data-sid=" in hist
    assert "点进查看" in hist


def test_panel_play_entry_requests_workspace_fullscreen() -> None:
    """进局即全屏（2026-10-17 用户裁定：开一局=玩法页独占工作区，不嵌小面板）：
    startNewPlay / bindPlaySession / rp:session-bound（页内开演成功）三个进局
    口全部上行 workspace.fullscreen 桥（宿主 WebviewWidget 白名单方法，载荷
    {on:true}）；window.agentos 缺席降级不全屏（直开页面零异常）。"""
    src = _panel_source()
    helper = src.split("function requestWorkspaceFullscreen(")[1].split("function startNewPlay")[0]
    assert "workspace.fullscreen" in helper, "宿主桥方法名"
    assert "on: true" in helper, "载荷 on:true（进全屏）"
    assert "if (!window.agentos) return;" in helper, "桥缺席降级（直开页面零异常）"
    for name, seg in (
        ("startNewPlay", src.split("function startNewPlay(")[1].split("function bindPlaySession")[0]),
        ("bindPlaySession", src.split("function bindPlaySession(")[1].split("function renderPlayHistory")[0]),
    ):
        assert "requestWorkspaceFullscreen();" in seg, f"{name} 进局口请求全屏"
    bound = src.split("window.addEventListener('rp:session-bound'")[1].split(
        "loadSessions().then(renderPlayHistory)"
    )[0]
    assert "requestWorkspaceFullscreen();" in bound, "页内开演成功=已进局，同口径全屏"


def test_panel_play_page_mount_single_owner_controller() -> None:
    """刀10 挂载生命周期单点化改钉（重影结构性根修，取代刀9 散装让位守卫——
    per-state 守卫随 state.play 重建归零、旧页脚本实例永生=多实例交叉写入）：
    唯一挂载口 mountPlayPage——键（会话 id 或 pack:<id>）不变=幂等免挂；换键先
    拆后挂（playPageDispose：页自报 window.__RP_PAGE_DISPOSE__ 先死+代际号+1）；
    请求捕获代际，resolve/catch 时与键双双比对单点判废。has_page 与无页包同模
    型（无页包=game_host 页 base64 解码内嵌挂载，无独立挂载函数）。注入器
    mountPlayPageHtml：注入前清容器+body 根类补挂+SESSION/PACK 上下文挂载前写
    入；setView 离开玩法视图必拆；错误重试废键重挂。"""
    src = _panel_source()
    ctrl = src.split("var playMount")[1].split("function mountPlayPageHtml")[0]
    assert "window.__RP_PAGE_DISPOSE__" in ctrl, "dispose 契约消费（页自报清理先死）"
    assert "playMount.epoch++" in ctrl, "代际号递增（在飞请求判废基准）"
    assert "try { fn(); } catch" in ctrl, "拆卸不被单页清理异常阻断（边界翻译 console.error）"
    fetch = src.split("function mountPlayPage(")[1].split("function retryPlayMount")[0]
    assert "playMount.key === key) return" in fetch, "同键幂等免挂（不重挂不重建）"
    assert "playPageDispose();" in fetch, "换键先拆后挂"
    assert "?session_id=" in fetch, "只读查询串形态（防线逐行扫描豁免类）"
    assert "?pack=" in fetch, "无会话 setup 阶段按包直取"
    assert "/ext/mode_roleplay/page/game-host" in fetch, "无页包=game_host 页内嵌挂载"
    assert "atob(out.body)" in fetch, "HTML 文档响应 base64 解码"
    assert fetch.count("playMount.epoch !== gen || playMount.key !== key") >= 2, \
        "resolve/catch 代际+键双比对（单点判废）"
    assert "pageInjected" not in src and "hostInjected" not in src, "散装 per-state 守卫位退役"
    assert "已挂载" not in src, "「防脚本重复执行」占位退役（幂等免挂语义取代）"
    assert "function mountGameHostPage" not in src and \
        "function remountPlayPage" not in src and "function remountGameHostPage" not in src, \
        "双挂载函数与手动重载口收敛进唯一挂载口"
    retry = src.split("function retryPlayMount")[1].split("function renderPlayFallback")[0]
    assert "playMount.key = null" in retry, "错误占位不算已挂实例：废键重挂"
    view = src.split("function setView(")[1].split("state.view = view")[0]
    assert "playPageDispose()" in view, "离开玩法视图：活页实例必拆"
    mount = src.split("function mountPlayPageHtml")[1].split("function playStateCardHtml")[0]
    assert "window.__RP_PAGE_SESSION__" in mount
    assert "window.__RP_PAGE_PACK__" in mount, "包上下文注入通道（页内 setup 分流与 init_ui 取数）"
    assert "tagName === 'SCRIPT'" in mount
    assert "host.innerHTML = ''" in mount, "注入前清空容器（容器恒单份页面）"
    assert "<body\\b" in mount, "页面 body 标签自原文提取（正则含词边界）"
    assert "classList.add" in mount, "body 根类补挂容器（包主题 css 作用域成立）"
    bound = src.split("window.addEventListener('rp:session-bound'")[1].split("loadSessions().then(renderPlayHistory)")[0]
    assert "p.sessionId = sid" in bound, "页内开演回执：面板同步锚不抢视图不重挂"
    ws = src.split("function playWorkspaceHtml(")[1].split("function mountPlayPage(")[0]
    assert "对话在系统对话页进行" in ws
    assert "playStateCardHtml(s)" in ws
    assert "playMessagesHtml" in src


def test_panel_play_setup_converged_into_webview_pages() -> None:
    """刀4 改钉（用户裁定「工作区玩法页全 webview 化」，台账归因）：面板不再
    渲染玩法 setup——开始配置表单段/主题注入段/playStart 提交段全部收敛出面
    板；init_ui 渲染与 rp_init 组装迁 webview 页内（stardew 页/game_host 消费
    rp-core 单源装配，消费面由 test_roleplay_pack_declarations 出厂页测试与
    test_roleplay_game_host setup 接线测试钉死）；核心块保持 rp_init 组装与
    init_ui 寻址单源；组合 builder 开演路径（comboFinish）保留。"""
    src = _panel_source()
    for gone in (
        "function startConfigHtml",
        "function playStart",
        "function playRoutesHtml",
        "function playTraitPoolsHtml",
        "function bindStartConfig",
        "function packThemeStyleHtml",
        "function packThemeClass",
        "function onRoutePick",
        "function onTraitToggle",
    ):
        assert gone not in src, f"纯玩法 setup 段应收敛出面板: {gone}"
    match = re.search(_CORE_MARK, src)
    assert match is not None
    core = match.group(1)
    assert "function rpInitSnapshot" in core, "rp_init 组装单源在核心块（页内 setup 经 rp-core 装配消费）"
    assert "function rpInitPairs" in core, "effects 聚合单源在核心块"
    assert "function findRoute" in core, "路线寻址单源在核心块"
    assert "function findTraitOptions" in core, "词条寻址单源在核心块"
    finish = src.split("function comboFinish(")[1].split("// ── 玩法子页")[0]
    assert "extra.wizard_pack_ids" in finish, "组合开演路径保留（面板仍有他用）"


def test_panel_pack_theme_consumption_moved_to_pages() -> None:
    """刀4 改钉（台账归因）：包主题消费随 setup 段收敛出面板——theme_css_text
    注入改在包页 serve-time 装配（game/page 单源机制）+ 主题根类由作者页自持
    （stardew 页 body.rp-theme-stardew）；面板零主题注入面。"""
    src = _panel_source()
    assert "theme_css_text" not in src, "面板不再消费包主题文本（页内/装配面承担）"
    assert "rp-theme-" not in src, "主题作用域根类由包页自持"


def test_panel_combo_builder_lifecycle() -> None:
    """组合 builder：①卡必选 ②世界书 chips ③初始状态自由文本（EXTRA_PRESETS +
    自定义增删）+ 玩法包直挂；谱系折叠按所选包 lineage 收起（fold 分级渲染）；
    save/delete 消费与 profile_extra 承载；编辑态保 id。"""
    src = _panel_source()
    render = src.split("function renderCombo(")[1].split("function comboIdSafe")[0]
    assert "rpComboFold(state.packs, w.packIds)" in render
    assert "fold >= 1" in render, "谱系折叠分级"
    assert "fold >= 2" in render, "谱系折叠分级"
    assert "wizStepCardHtml()" in render
    assert "wizStepBooksHtml()" in render
    assert "wizStepPacksHtml()" in render
    assert "comboExtrasHtml()" in render
    assert "comboDeleteArmed" in render, "删除两步确认"
    save = src.split("function comboSave(")[1].split("function comboDelete")[0]
    assert "'/ext/mode_roleplay/data/combos/save'" in save
    assert "profile_extra" in save, "组合③自由文本段 profile_extra 承载（刀1 契约）"
    assert "body.id = existing.id" in save, "编辑态保 id（同 id 覆盖）"
    assert "rpComboName(" in save
    delete = src.split("function comboDelete(")[1].split("function comboFinish")[0]
    assert "'/ext/mode_roleplay/data/combos/delete'" in delete
    finish = src.split("function comboFinish(")[1].split("// ── 玩法子页")[0]
    assert "if (w.bookIds.length) extra.wizard_lorebook_ids" in finish
    assert "if (w.packIds.length) extra.wizard_pack_ids" in finish
    assert "extra.wizard_profile = profile" in finish


def test_panel_me_menu_three_entries_and_views() -> None:
    """我的▾ 三入口：开演记录（全量 sessions 抽屉+详情）/你的设定（persona
    管理独立视图）/游戏界面指引（单页化改钉：指引名不含「工作区」tab 语义，
    游戏界面入口=玩法子页内）；主导航两签（玩法大厅/库与创作）。"""
    src = _panel_source()
    assert "openSessionsView()" in src
    assert "openPersonaView()" in src
    assert "openGuideView()" in src
    assert 'id="meMenu"' in src
    assert ">玩法大厅</button>" in src
    assert ">库与创作</button>" in src
    assert "游戏界面指引" in src
    assert "玩法子页=上历史记录、下玩法页，页常驻" in src, "指引文案钉子页重排语义（刀10）"
    persona = src.split("function openPersonaView(")[1].split("function openGuideView")[0]
    assert 'id="personaMgr"' in persona, "persona 管理面板复用（单例容器）"
    assert "state.view === 'persona'" in src, "persona 增删后按视图正确回写"


def test_panel_lib_view_preserves_filters_and_workbench() -> None:
    """库与创作重排零功能删除：卡库过滤行（全部/出厂/用户计数 + 搜索 + AI 生成）、
    书库搜索、既有工具行与渲染函数保留；drawer 按视图显隐。"""
    src = _panel_source()
    assert "function setCardFilter(" in src
    assert "function onCardSearch(" in src
    assert "function onBookSearch(" in src
    assert "state.cardFilter === 'builtin'" in src
    assert "state.cardFilter === 'user'" in src
    assert "wbOpenGenerate()" in src, "AI 生成入口在卡库过滤行（创作台同入口复用）"
    libtabs = src.split('id="libTabs"')[1].split('id="sessTabs"')[0]
    assert "tab-cards" in libtabs
    assert "tab-lorebooks" in libtabs
    assert "tab-drafts" in libtabs
    assert "🎭 角色卡库" in libtabs, "三签 emoji 前缀（设计稿对齐，刀7）"
    assert "✨ 创作台" in libtabs
    assert "['cards', 'lorebooks', 'sessions', 'drafts']" in src, "switchTab 页签清单保全"


def test_panel_core_block_pure_no_state_dependency() -> None:
    """核心单源块纯函数纪律：块内不触 state/DOM（node 直测可在无浏览器环境
    执行的前提）；块位于 rp-sanitize 单源之后（escapeHtml 已定义但块内不消费）。"""
    match = re.search(_CORE_MARK, _panel_source())
    assert match is not None
    body = match.group(1)
    assert "state." not in body, "核心块禁触面板 state"
    assert "document." not in body, "核心块禁触 DOM"
    assert "window." not in body, "核心块禁触宿主对象"


# ── 主题跟宿主（用户裁定 2026-10-15：配色直接根据主题来 / 顶栏无重色块）────────


def test_panel_theme_follows_host_tokens_no_raw_hex() -> None:
    """面板配色全部跟 --ag-* 主题 token：css 内 #hex 只准落在 var(--ag-*,
    fallback) 桥的 fallback 位（token 未到时回落亮暗基座，token 到达即整体跟随
    宿主主题，亮暗两态同一份规则成立）；accent 前景对比色走 --on-accent 桥
    （宿主未下行 --ag-accent-contrast 时回落白，契约槽预留）。顶栏/主导航轻量
    形态（设计稿 .rp-top/.rp-nav）：表面色底（--card）+边框分隔，active 项 =
    accent 前景+底部 2px 下划线，无大面积主题色底——「顶上那一大块重色」废除。"""
    style = _panel_source().split("<style>", 1)[1].split("</style>", 1)[0]
    residue = re.sub(r"var\(--[a-zA-Z-]+(?:\s*,[^()]*)?\)", "", style)
    raw_hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", residue)
    assert not raw_hexes, f"css 存在 var() 桥之外的直写色值: {raw_hexes}"
    on_accent = style.split("--on-accent:")[1].split(";")[0]
    assert "--ag-accent-contrast" in on_accent, "accent 前景对比色走 token 桥"
    header = style.split("header {")[1].split("}")[0]
    assert "background: var(--card)" in header, "顶栏表面色底"
    assert "--chip" not in header and "--accent" not in header, "顶栏零主题色/徽标色大面积底"
    nav = style.split(".rp-nav {")[1].split("}")[0]
    assert "background: var(--card)" in nav, "主导航表面色底（无重色块）"
    assert "--accent" not in nav
    nav_btn = style.split(".rp-nav button {")[1].split("}")[0]
    assert "border-bottom: 2px solid transparent" in nav_btn, "active 下划线占位"
    nav_active = style.split(".rp-nav button.active")[1].split("}")[0]
    assert "color: var(--accent)" in nav_active, "active 项 accent 前景"
    assert "border-bottom-color: var(--accent)" in nav_active, "active 项底部下划线"
