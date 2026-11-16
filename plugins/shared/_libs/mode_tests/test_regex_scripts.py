# @feature: FP-0.2.〇 RP批1a regex脚本执行面 | @ci: python-coverage
"""regex 脚本执行面测试（mode_roleplay/regex_scripts.py + 两消费点接线）。

覆盖：
- apply_scripts 引擎：捕获组替换 / flags / depth 窗口边界（恰等/越界）/
  多脚本声明序 / trim_out / ephemerality 两态 / setvar+addvar 解析（含非法
  delta 降级），parametrize ≥2 组区分度输入；
- fail-soft：坏 regex / 非法 affects / 非 dict 脚本 / 执行异常 → warning
  跳过不抛出，文本替换不受影响；
- 性质断言：无脚本输出恒等；声明序确定性；变量变更键 = 声明变量名集合；
- ST 导出样例 fixture ≥3 组（好感检测 / 文本清洗 / 变量埋点，真实 ST
  regex 配置形状按卡 yaml extensions.regex_scripts 声明面）；
- 消费点接线：material 用户输入（staged message op + 提升）、
  agent_config_load 写 context.regex_scripts、state_marker_parse AI 输出
  应用（脚本后文本落账 + 变量变更 warning 桥）；
- 兼容态：无 extensions.regex_scripts 的卡 = 零行为变化。

mock 仅外部依赖（无）：re/YAML/文件全走真的。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLEPLAY_DIR = os.path.join(MODES_DIR, "mode_roleplay")
SHARED_ROOT = MODES_DIR  # 平铺布局：共享根与模式包根同点（plugins/shared）

pytestmark = pytest.mark.unit


def _load_pkg_module(rel_name: str, file_name: str):
    """按真实导入名装载包内模块（与 material._load_lorebook 同机制）。"""
    name = f"mode_roleplay.{rel_name}"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(ROLEPLAY_DIR, file_name)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


regex_scripts = _load_pkg_module("regex_scripts", "regex_scripts.py")


def _script(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "script_name": "测试脚本",
        "find_regex": "微笑",
        "replace_with": "大笑",
        "affects": ["AI Response"],
    }
    base.update(overrides)
    return base


# ── 引擎：捕获组 / flags ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("find", "replace", "text", "expected"),
    [
        # $1 捕获组展开；$0 整匹配
        (r"(\w+)对你微笑", "{{setvar::affection::$1}}", "露娜对你微笑了", "了"),
        (r"(a)(b)", "[$2|$1|$0]", "ab", "[b|a|ab]"),
        # 无捕获组引用原样保留
        (r"abc", "x$1y", "abc", "xy"),
        # 未参与匹配的组 → 空串（JS 语义）
        (r"a(b)?c", "<$1>", "ac", "<>"),
    ],
)
def test_capture_group_expansion(find: str, replace: str, text: str, expected: str) -> None:
    out, vars_ = regex_scripts.apply_scripts(
        text, [_script(find_regex=find, replace_with=replace)], "AI Response", 0, {}
    )
    assert out == expected
    assert vars_ == {} or list(vars_) == ["affection"]


def test_setvar_extracts_var_and_strips_macro_from_text() -> None:
    out, vars_ = regex_scripts.apply_scripts(
        "露娜对你微笑了",
        [_script(find_regex=r"(\w+)对你微笑", replace_with="{{setvar::affection::$1}}")],
        "AI Response",
        0,
        {},
    )
    assert vars_ == {"affection": "露娜"}
    assert "setvar" not in out, "变量宏消耗出文本，不残留字面量"
    assert out == "了"


@pytest.mark.parametrize(
    ("pattern", "flags_str", "text", "expected"),
    [
        # i 忽略大小写
        ("hello", "i", "say HELLO now", "say 大笑 now"),
        # s 点号跨行
        ("a.b", "s", "a\nb", "大笑"),
        # m 多行锚
        ("^x", "m", "a\nx", "a\n大笑"),
        # 裸 pattern 无 flags
        ("微笑", "", "微笑一次", "大笑一次"),
    ],
)
def test_flags_subset(pattern: str, flags_str: str, text: str, expected: str) -> None:
    find = f"/{pattern}/{flags_str}" if flags_str else pattern
    out, _ = regex_scripts.apply_scripts(
        text, [_script(find_regex=find)], "AI Response", 0, {}
    )
    assert out == expected


def test_flags_case_sensitive_default() -> None:
    out, _ = regex_scripts.apply_scripts(
        "say HELLO", [_script(find_regex="hello", replace_with="X")], "AI Response", 0, {}
    )
    assert out == "say HELLO", "缺 i 标志大小写敏感"


# ── 引擎：depth 窗口 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("min_depth", "max_depth", "depth", "applies"),
    [
        (None, None, 0, True),
        (0, 0, 0, True),  # 恰等下界
        (0, 2, 2, True),  # 恰等上界
        (0, 2, 3, False),  # 越上界
        (1, None, 0, False),  # 越下界
        (1, 3, 2, True),
    ],
)
def test_depth_window_boundary(
    min_depth: Any, max_depth: Any, depth: int, applies: bool
) -> None:
    script = _script(min_depth=min_depth, max_depth=max_depth)
    out, _ = regex_scripts.apply_scripts("微笑", [script], "AI Response", depth, {})
    assert (out == "大笑") is applies


# ── 引擎：声明序 / trim_out / ephemerality ────────────────────────────────


def test_multiple_scripts_apply_in_declaration_order() -> None:
    scripts = [
        _script(script_name="一", find_regex="微笑", replace_with="大笑"),
        _script(script_name="二", find_regex="大笑", replace_with="狂笑"),
    ]
    out, _ = regex_scripts.apply_scripts("微笑", scripts, "AI Response", 0, {})
    assert out == "狂笑", "二消费一的产物 = 声明序生效"
    # 反序声明 → 结果不同（顺序确定性）
    out_rev, _ = regex_scripts.apply_scripts("微笑", list(reversed(scripts)), "AI Response", 0, {})
    assert out != out_rev


def test_trim_out_removes_substrings_before_match() -> None:
    script = _script(find_regex="abc", replace_with="X", trim_out=["【注】"])
    out, _ = regex_scripts.apply_scripts("a【注】bc", [script], "AI Response", 0, {})
    assert out == "X", "trim_out 先于替换移除子串（否则阻断匹配）"


def test_ephemerality_both_states_return_replacement_text() -> None:
    """ephemerality 两态本刀统一返回替换文本；语义区分由消费侧/文档承载。"""
    for ephemeral in (False, True):
        out, _ = regex_scripts.apply_scripts(
            "微笑", [_script(ephemerality=ephemeral)], "AI Response", 0, {}
        )
        assert out == "大笑"


# ── 引擎：变量解析与降级 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("replace", "expected_vars"),
    [
        ("{{setvar::好感::42}}", {"好感": "42"}),
        ("{{addvar::金币::5}}", {"金币": 5}),
        ("{{addvar::金币::-2.5}}", {"金币": -2.5}),
        ("前{{setvar::a::x}}后{{setvar::b::y}}", {"a": "x", "b": "y"}),
    ],
)
def test_var_macro_parsing(replace: str, expected_vars: dict) -> None:
    _, vars_ = regex_scripts.apply_scripts(
        "z", [_script(find_regex="z", replace_with=replace)], "AI Response", 0, {}
    )
    assert vars_ == expected_vars


@pytest.mark.parametrize("bad_delta", ["abc", "", "1.2.3"])
def test_addvar_non_numeric_delta_degrades(bad_delta: str) -> None:
    """非法 delta = warning 跳过该变量变更，不影响文本替换。"""
    out, vars_ = regex_scripts.apply_scripts(
        "z",
        [_script(find_regex="z", replace_with=f"Q{{{{addvar::金币::{bad_delta}}}}}")],
        "AI Response",
        0,
        {},
    )
    assert out == "Q", "文本替换不受变量解析失败影响"
    assert vars_ == {}


def test_same_var_later_declaration_wins() -> None:
    scripts = [
        _script(find_regex="a", replace_with="{{setvar::x::1}}"),
        _script(find_regex="b", replace_with="{{setvar::x::2}}"),
    ]
    _, vars_ = regex_scripts.apply_scripts("ab", scripts, "AI Response", 0, {})
    assert vars_ == {"x": "2"}, "同名变量声明序结算，后者覆盖"


# ── 引擎：fail-soft 铁律 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_script",
    [
        _script(find_regex="([unclosed", replace_with="X"),  # 编译失败
        _script(find_regex="", replace_with="X"),  # 空 pattern
        _script(affects="Slash Commands"),  # 一期外源
        _script(affects=123),  # 非法 affects 形态
        "不是字典",  # 非 dict 脚本
        _script(min_depth="很多"),  # 窗口字段垃圾值
        _script(find_regex=None),  # 缺 pattern
    ],
)
def test_bad_scripts_skip_without_raising(bad_script: Any) -> None:
    out, vars_ = regex_scripts.apply_scripts("微笑", [bad_script], "AI Response", 0, {})
    assert out == "微笑", "坏脚本跳过，原文不动"
    assert vars_ == {}


def test_execution_exception_fail_soft() -> None:
    """脚本执行期异常（如 trim_out 含非字符串）同待遇：跳过不阻断。"""
    out, _ = regex_scripts.apply_scripts(
        "微笑", [_script(trim_out=[123, None])], "AI Response", 0, {}
    )
    assert out == "微笑"


def test_affects_filter_selects_matching_source_only() -> None:
    scripts = [
        _script(script_name="ai", find_regex="a", replace_with="A"),
        _script(script_name="user", find_regex="b", replace_with="B", affects=["User Input"]),
    ]
    out_ai, _ = regex_scripts.apply_scripts("ab", scripts, "AI Response", 0, {})
    assert out_ai == "Ab"
    out_user, _ = regex_scripts.apply_scripts("ab", scripts, "User Input", 0, {})
    assert out_user == "aB"


def test_affects_single_string_form() -> None:
    out, _ = regex_scripts.apply_scripts(
        "微笑", [_script(affects="User Input")], "User Input", 0, {}
    )
    assert out == "大笑", "affects 标量串与列表两形等价"


# ── 性质断言 ─────────────────────────────────────────────────────────────


def test_no_scripts_identity() -> None:
    for text in ["", "原文", "多行\n文本\n微笑"]:
        out, vars_ = regex_scripts.apply_scripts(text, [], "AI Response", 0, {})
        assert out == text
        assert vars_ == {}


def test_var_changes_keys_are_declared_names_only() -> None:
    _, vars_ = regex_scripts.apply_scripts(
        "ab",
        [
            _script(find_regex="a", replace_with="{{setvar::甲::1}}"),
            _script(find_regex="b", replace_with="{{addvar::乙::2}}"),
        ],
        "AI Response",
        0,
        {},
    )
    assert set(vars_) == {"甲", "乙"}, "变量变更不含未声明键形态"
    assert all(isinstance(v, (str, int, float)) for v in vars_.values())


def test_scripts_mutation_free() -> None:
    scripts = [_script(trim_out=["a"])]
    regex_scripts.apply_scripts("aaa", scripts, "AI Response", 0, {})
    assert scripts[0]["trim_out"] == ["a"], "入参脚本不被就地修改"


# ── 边界分支（fail-soft 面/解析角形，diff coverage 满覆盖） ────────────────


def test_find_regex_slash_pair_empty_body_skipped() -> None:
    """``//`` → /p/f 解析体为空 = 非法，warning 跳过（原文不动）。"""
    out, _ = regex_scripts.apply_scripts(
        "微笑", [_script(find_regex="//")], "AI Response", 0, {}
    )
    assert out == "微笑"


def test_dollar_dollar_escapes_literal_dollar() -> None:
    out, _ = regex_scripts.apply_scripts(
        "微笑", [_script(find_regex="微笑", replace_with="a$$b")], "AI Response", 0, {}
    )
    assert out == "a$b", "$$ 转义字面 $（不吞字符）"


def test_pattern_length_cap_skips_script() -> None:
    """find_regex pattern 超长（>512 字符）= 脚本跳过原文不变（ReDoS 长度护栏）。

    长度护栏是缓解非根治（Python re 无求值超时，根治需引擎级=新依赖禁止，
    升级触发登记于模块 docstring）；边界内 pattern 不误伤。"""
    long_pattern = "a" * 600
    out, changes = regex_scripts.apply_scripts(
        "a" * 600, [_script(find_regex=f"/{long_pattern}/", replace_with="X")],
        "AI Response", 0, {},
    )
    assert out == "a" * 600, "超长 pattern 脚本整体跳过（文本恒等）"
    assert changes == {}
    benign, _ = regex_scripts.apply_scripts(
        "b" * 100, [_script(find_regex=f"/{'b' * 100}/", replace_with="X")],
        "AI Response", 0, {},
    )
    assert benign == "X", "边界内 pattern 照常生效（不误伤）"


def test_scripts_not_a_list_identity() -> None:
    out, vars_ = regex_scripts.apply_scripts("微笑", "不是列表", "AI Response", 0, {})
    assert out == "微笑"
    assert vars_ == {}


def test_replace_with_none_means_delete() -> None:
    out, _ = regex_scripts.apply_scripts(
        "a微笑b", [_script(find_regex="微笑", replace_with=None)], "AI Response", 0, {}
    )
    assert out == "ab", "replace_with 缺省 = 删除匹配（ST 删除脚本形）"


def test_replace_with_non_string_skipped() -> None:
    out, _ = regex_scripts.apply_scripts(
        "微笑", [_script(find_regex="微笑", replace_with=123)], "AI Response", 0, {}
    )
    assert out == "微笑"


def test_non_string_text_fail_soft() -> None:
    """非字符串文本 = 单脚本执行异常通道：warning 跳过，原样返回不抛。"""
    out, vars_ = regex_scripts.apply_scripts(None, [_script()], "AI Response", 0, {})
    assert out is None
    assert vars_ == {}


# ── ST 导出样例 fixture（真实配置形状：卡 yaml extensions.regex_scripts） ──


ST_FIXTURE_AFFECTION = {
    "script_name": "好感检测",
    "find_regex": "/(\\w+)对(你|您)微笑/g",
    "replace_with": "{{setvar::好感度::$1}}",
    "affects": ["AI Response"],
    "min_depth": 0,
    "max_depth": 2,
    "trim_out": [],
    "ephemerality": False,
    "disabled": False,
}

ST_FIXTURE_CLEANUP = {
    "script_name": "舞台指令清洗",
    "find_regex": "/\\*\\*[^*]+\\*\\*/g",
    "replace_with": "",
    "affects": ["User Input", "AI Response"],
    "min_depth": None,
    "max_depth": None,
    "trim_out": ["（OOC）"],
    "ephemerality": False,
    "disabled": False,
}

ST_FIXTURE_STASH = {
    "script_name": "变量埋点",
    "find_regex": "/【记录:(.+?)】/g",
    "replace_with": "{{addvar::记录数::1}}",
    "affects": ["AI Response"],
    "min_depth": 0,
    "max_depth": None,
    "trim_out": [],
    "ephemerality": True,
    "disabled": False,
}


@pytest.mark.parametrize(
    ("fixture", "text", "expect_vars_nonempty"),
    [
        (ST_FIXTURE_AFFECTION, "露娜对你微笑，随后转身。", True),
        (ST_FIXTURE_CLEANUP, "（OOC）**压低声音**好的。", False),
        (ST_FIXTURE_STASH, "【记录:北境地图】已收好。", True),
    ],
)
def test_st_fixture_scripts(fixture: dict, text: str, expect_vars_nonempty: bool) -> None:
    scripts = [fixture]
    out, vars_ = regex_scripts.apply_scripts(text, scripts, "AI Response", 0, {})
    assert isinstance(out, str)
    assert len(out) < len(text), "匹配段被替换/清除"
    assert (len(vars_) > 0) is expect_vars_nonempty


def test_st_fixture_disabled_script_noop() -> None:
    disabled = dict(ST_FIXTURE_AFFECTION, disabled=True)
    out, vars_ = regex_scripts.apply_scripts(
        "露娜对你微笑", [disabled], "AI Response", 0, {}
    )
    assert out == "露娜对你微笑"
    assert vars_ == {}


# ── 消费点 1：material 用户输入（staged message op + 提升） ────────────────


def _load_material():
    return _load_pkg_module("material", "material.py")


def _write_user_card(agents_dir, card: dict) -> None:
    card_dir = agents_dir / "mode_roleplay"
    # os.makedirs 直建（不经 Path.mkdir——同车道其他测试会打桩该方法；
    # 全量车道偶发 FNF 写失败的实证面，见 2026-10-11 推送轮记录）
    os.makedirs(card_dir, exist_ok=True)
    (card_dir / "card_rx.yaml").write_text(
        yaml.safe_dump(card, allow_unicode=True), encoding="utf-8"
    )


def test_material_applies_user_input_scripts_stages_message_op(tmp_path) -> None:
    material = _load_material()
    card = {
        "config_id": "card_rx",
        "name": "测试卡",
        "extensions": {"regex_scripts": [ST_FIXTURE_CLEANUP]},
    }
    _write_user_card(tmp_path / "agents", card)
    state: dict[str, Any] = {
        "agent.id": "mode_roleplay/card_rx",
        "messages": [
            {"role": "user", "seq": 7, "content": "（OOC）**小声**你好"},
            {"role": "assistant", "seq": 8, "content": "你好呀"},
            {"role": "user", "seq": 9, "content": "（OOC）**压低声音**继续"},
        ],
    }
    pkg_dir = tmp_path / "pkg"
    (pkg_dir / "agents").mkdir(parents=True)
    out = material.build_injection(state, str(pkg_dir), user_agents_dir=str(tmp_path / "agents"))
    assert isinstance(out, str)
    ops = state.get("_material_message_ops")
    assert ops
    assert ops[0]["op"] == "set"
    assert ops[0]["seq"] == 9
    assert ops[0]["msg"]["content"] == "继续", "最后一条用户消息文本被脚本改写"
    assert state["messages"][-1]["content"] == "（OOC）**压低声音**继续", "staging 不就地改消息"


def test_material_no_scripts_no_staging(tmp_path) -> None:
    material = _load_material()
    card = {"config_id": "card_rx", "name": "无脚本卡"}
    _write_user_card(tmp_path / "agents", card)
    state: dict[str, Any] = {
        "agent.id": "mode_roleplay/card_rx",
        "messages": [{"role": "user", "seq": 1, "content": "普通输入"}],
    }
    pkg_dir = tmp_path / "pkg"
    (pkg_dir / "agents").mkdir(parents=True)
    material.build_injection(state, str(pkg_dir), user_agents_dir=str(tmp_path / "agents"))
    assert "_material_message_ops" not in state, "无脚本卡零暂存（兼容态）"


# ── 消费点 1b：mode_material_inject 提升暂存 op ──────────────────────────


def _load_material_inject_plugin() -> Any:
    name = "mode_material_inject_plugin_test"
    path = os.path.join(SHARED_ROOT, "mode_material_inject", "plugin.py")
    if name in sys.modules:
        del sys.modules[name]
    for p in (SHARED_ROOT, os.path.join(SHARED_ROOT, "mode_material_inject")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_material_inject_promotes_staged_ops() -> None:
    mod = _load_material_inject_plugin()
    plugin = mod.ModeMaterialInjectPlugin()
    state: dict[str, Any] = {
        "execution_context": {"mode": "roleplay"},
        "messages": [{"role": "user", "seq": 3, "content": "原文"}],
        "_material_message_ops": [{"op": "set", "seq": 3, "msg": {"role": "user", "seq": 3, "content": "改写"}}],
    }
    ctx = type("Ctx", (), {"state": state})()
    result = asyncio.run(plugin.execute(ctx))
    assert result.state_updates.get("messages") == {
        "_ops": [{"op": "set", "seq": 3, "msg": {"role": "user", "seq": 3, "content": "改写"}}]
    }
    assert "_material_message_ops" not in state, "暂存键提升后清除，不泄漏"


def test_material_inject_no_staged_ops_zero_message_writes() -> None:
    mod = _load_material_inject_plugin()
    plugin = mod.ModeMaterialInjectPlugin()
    state: dict[str, Any] = {"execution_context": {"mode": "roleplay"}}
    ctx = type("Ctx", (), {"state": state})()
    result = asyncio.run(plugin.execute(ctx))
    assert "messages" not in result.state_updates


# ── 消费点 2a：agent_config_load 写 context.regex_scripts ────────────────


def _load_agent_config_load_plugin() -> Any:
    name = "agent_config_load_plugin_test"
    plugin_dir = os.path.join(SHARED_ROOT, "agent_config_load")
    path = os.path.join(plugin_dir, "plugin.py")
    if name in sys.modules:
        del sys.modules[name]
    # 步内模块（rp_packs）与共享根面（_root_api/_libs）双位在 path——对齐生产
    # bootstrap（T6c 起声明面 lint 经 card 质检服务，不再依赖共享根 card_lint）
    for p in (SHARED_ROOT, plugin_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _agent_cfg_load_with_yaml(tmp_path, monkeypatch, agent_yaml: dict) -> Any:
    """构造用户层 agent yaml 后执行插件，返回 state_updates。"""
    mod = _load_agent_config_load_plugin()
    agents_dir = tmp_path / "agents"
    (agents_dir / "mode_roleplay").mkdir(parents=True)
    (agents_dir / "mode_roleplay" / "card_rx.yaml").write_text(
        yaml.safe_dump(agent_yaml, allow_unicode=True), encoding="utf-8"
    )
    import user_space as user_space_mod

    monkeypatch.setattr(user_space_mod, "user_config_dir", lambda: str(tmp_path))
    plugin = mod.AgentConfigLoadPlugin()
    state: dict[str, Any] = {"agent.id": "mode_roleplay/card_rx"}
    ctx = type("Ctx", (), {"state": state})()
    result = asyncio.run(plugin.execute(ctx))
    return result.state_updates


def test_agent_config_load_writes_regex_scripts(tmp_path, monkeypatch) -> None:
    updates = _agent_cfg_load_with_yaml(
        tmp_path,
        monkeypatch,
        {"config_id": "card_rx", "extensions": {"regex_scripts": [ST_FIXTURE_AFFECTION]}},
    )
    assert updates.get("context.regex_scripts") == [ST_FIXTURE_AFFECTION]


def test_agent_config_load_no_extensions_zero_write(tmp_path, monkeypatch) -> None:
    updates = _agent_cfg_load_with_yaml(tmp_path, monkeypatch, {"config_id": "card_rx"})
    assert "context.regex_scripts" not in updates, "无脚本卡零写入（兼容态）"


# ── 消费点 2b：state_marker_parse AI 输出应用 ────────────────────────────


def _load_state_marker_plugin() -> Any:
    name = "state_marker_parse_plugin_test"
    plugin_dir = os.path.join(SHARED_ROOT, "state_marker_parse")
    path = os.path.join(plugin_dir, "plugin.py")
    if name in sys.modules:
        del sys.modules[name]
    # 插件目录随 SHARED_ROOT 入 sys.path：plugin.py 的步内模块导入
    # （state_contract，RP 批 2-Ⅰ）按生产 bootstrap 同款布局解析。
    for p in (SHARED_ROOT, plugin_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeCaller:
    def __init__(self, render_text: str = "") -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.render_text = render_text

    async def __call__(self, tool_name: str, plugin_id: str, args: dict) -> dict:
        self.calls.append((tool_name, plugin_id, args))
        return {"data": {"text": self.render_text}}


def _run_marker_plugin(state: dict) -> tuple[dict, Any]:
    mod = _load_state_marker_plugin()
    caller = _FakeCaller(render_text="状态")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    updates = asyncio.run(plugin.execute(type("Ctx", (), {"state": state})())).state_updates
    return updates, caller


def _assistant_state(
    text: str, scripts: Any = None, *, allow_state: bool = False
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "agent.id": "mode_roleplay/card_rx",
        "messages": [{"role": "assistant", "seq": 5, "content": text}],
    }
    if scripts is not None:
        state["context.regex_scripts"] = scripts
    if allow_state:
        # 回退开关显式开启（2026-10-13 动作驱动基线）：state 字段恢复合法来源
        state["context.rp_engine"] = {"allow_state_proposals": True}
    return state


def test_marker_parse_applies_ai_response_scripts_and_rewrites_message() -> None:
    text = "露娜对你微笑。\n\n<state>{\"state\": {\"好感度\": 1}}</state>"
    updates, _ = _run_marker_plugin(
        _assistant_state(text, [ST_FIXTURE_AFFECTION], allow_state=True)
    )
    ops = updates.get("messages", {}).get("_ops")
    assert ops
    assert ops[0]["op"] == "set"
    assert ops[0]["seq"] == 5
    new_content = ops[0]["msg"]["content"]
    assert "对你微笑" not in new_content, "匹配段被变量宏消耗清除"
    # 落账文本 = 脚本后文本：span 与改写后消息自洽
    payload = updates["context.state_updates"]
    assert new_content[payload["span"][0]:payload["span"][1]].startswith("<state>")
    assert payload["entries"]["state"] == {"好感度": 1}, "标记落账不破"


def test_marker_parse_no_scripts_zero_change() -> None:
    text = "纯文本。\n\n<state>{\"state\": {\"a\": 1}}</state>"
    updates, _ = _run_marker_plugin(_assistant_state(text, allow_state=True))
    assert "messages" not in updates, "无脚本卡零消息改写（兼容态）"
    assert updates["context.state_updates"]["entries"] == {"state": {"a": 1}}


def test_marker_parse_bad_scripts_zero_change() -> None:
    text = "原文。\n\n<state>{\"state\": {}}</state>"
    updates, _ = _run_marker_plugin(
        _assistant_state(
            text, [{"script_name": "坏", "find_regex": "([x"}], allow_state=True
        )
    )
    assert "messages" not in updates, "全部坏脚本 = 零改写"
    assert updates["context.state_updates"]["entries"] == {"state": {}}


def test_marker_parse_scripts_variable_changes_logged_not_ledgered(caplog) -> None:
    """变量变更：warning 记录，不落账（无 character_state.update 混入变量载荷）。"""
    text = "露娜对你微笑。"
    mod = _load_state_marker_plugin()
    caller = _FakeCaller(render_text="")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    with caplog.at_level(logging.WARNING, logger="plugin"):
        asyncio.run(
            plugin.execute(type("Ctx", (), {"state": _assistant_state(text, [ST_FIXTURE_AFFECTION])})())
        )
    assert any("regex" in r.message and "好感度" in r.message for r in caplog.records), (
        "变量变更经 warning 记录（批 2 前临时桥）"
    )
    assert not any(t == "character_state.update" and "好感度" in a.get("changes", {})
                   for t, _, a in caller.calls), "变量变更不落账"


def test_marker_parse_mode_package_missing_zero_change(monkeypatch) -> None:
    """引擎约定模块缺失（非模式包/包损坏）= 零动作不阻断。"""
    text = "正文。<state>{\"state\": {}}</state>"
    state = _assistant_state(text, [ST_FIXTURE_AFFECTION])
    mod = _load_state_marker_plugin()
    monkeypatch.setattr(mod, "_find_mode_package_dir", lambda _mode: None)
    caller = _FakeCaller(render_text="")
    plugin = mod.StateMarkerParsePlugin(invoke_caller=caller)
    updates = asyncio.run(plugin.execute(type("Ctx", (), {"state": state})())).state_updates
    assert "messages" not in updates
