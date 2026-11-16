# @feature: FP-0.2.四 模式体系 mode.yaml 声明面聚合 | @vision: V1 可进化 | @ci: none-local
"""agent_manager modes API 测试——mode.yaml 单一真值聚合面（2026-09-28 设计 D5）。

覆盖：
1. 真实出厂包聚合：六包在册、roleplay 运行时声明（pipelines=[{name:roleplay,
   context:conversation}] / presenter=data_cards / tool_card=collapse）、缺省语义
   （其余包 pipelines=[] / tool_card=native）、errors 空
2. schema 校验 fail-closed：未知字段拒（含退役单值 pipeline / pipeline_profile
   白名单外拒）/ 错枚举拒 / 缺必需键拒 / pipelines 列表结构拒（非列表/空列表/
   缺 context/错 context/name 编排键形态/name 重复/source:package 待拍板拒）
3. 单包坏不拖垮好包：好包在册 + 坏包缺位 + errors 记原因
4. 空目录 = 空 modes（合法形态，非降级）
5. http 分发：GET /ext/agent_manager/modes 走 http_handle 分支
6. mode.list 服务：{{mode_catalog}} 渲染取数单源——条目
   [{mode,name,description,pipelines:[{name,context}],chain:{entry}}]、按 mode 键
   排序确定性、声明缺省回落（空列表/None）、坏包缺位同口径

mock 仅用于目录根注入（monkeypatch _factory_modes_dir），文件 IO 一律真实。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
# 拍平布局（ADR 2026-10-13）：出厂模式包平铺共享根，mode_* 前缀识别
_REPO_MODES_DIR = _PLUGIN_DIR.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "agent_manager_modes_api"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_pkg(root: Path, dirname: str, decl: dict[str, Any] | str) -> Path:
    pkg = root / dirname
    pkg.mkdir(parents=True)
    text = decl if isinstance(decl, str) else yaml.safe_dump(decl, allow_unicode=True)
    (pkg / "mode.yaml").write_text(text, encoding="utf-8")
    return pkg


# ── 1. 真实出厂包聚合 ────────────────────────────────────────────────────


def test_aggregate_real_factory_packages() -> None:
    mod = _load_module()
    out = mod.aggregate_modes()
    assert out["errors"] == []
    modes = {m["mode"]: m for m in out["modes"]}
    assert set(modes) == {
        "coding", "evolution", "godot", "research", "roleplay", "writing",
        "personal",
    }
    rp = modes["roleplay"]
    assert rp["pipelines"] == [{"name": "roleplay", "context": "conversation"}]
    assert rp["presenter"] == {"source": "data_cards"}
    assert rp["tool_card"] == "collapse"
    assert rp["plugin_id"] == "mode_roleplay"
    # 批 G⑤：icon 声明随聚合透出；theme 无声明 = None（消费通道在 G-B，未消费不声明）
    assert rp["icon"] == "🍺"
    assert rp["theme"] is None
    # 批 G-B②：persona 声明随聚合透出（前端附身注入键按 from 派生）；未声明 = None
    assert rp["persona"] == {"replace": True, "from": "roleplay_persona"}
    assert modes["coding"]["persona"] is None
    # 缺省语义：未声明专属管道/卡片策略的包回落 空列表（消费侧 autonomous）/native
    assert modes["coding"]["pipelines"] == []
    assert modes["coding"]["tool_card"] == "native"
    assert modes["coding"]["icon"] == "🔨"
    assert modes["writing"]["presenter"] == {"source": "none"}
    # 批 B2：plan_spec 随聚合透出（四域包声明，roleplay 显式缺省零声明 = None）
    assert modes["coding"]["plan_spec"] == "skill-solution-software"
    assert modes["research"]["plan_spec"] == "skill-solution-research"
    assert modes["roleplay"]["plan_spec"] is None
    # M1 并入刀：personal 包 default:true 随聚合透出（唯一默认模式）
    assert modes["personal"]["default"] is True
    assert sum(1 for m in modes.values() if m.get("default")) == 1
    # R10③ 补充裁定：前台可选声明随聚合透出——personal（默认兜底无需选）与
    # evolution（后台型）显式声明 false；其余包缺省归一 True（向后兼容）
    assert modes["personal"]["foreground_selectable"] is False
    assert modes["evolution"]["foreground_selectable"] is False
    assert modes["coding"]["foreground_selectable"] is True


# ── 2. schema 校验 fail-closed ───────────────────────────────────────────


def test_validate_rejects_unknown_field() -> None:
    mod = _load_module()
    decl, err = mod.validate_mode_declaration({"mode": "x", "name": "X", "levors": {}})
    assert decl is None and "未知字段" in err and "levors" in err


# ── 2b. foreground_selectable（R10③ 补充裁定：前台可选声明） ─────────────


def test_validate_foreground_selectable_bool_forms() -> None:
    """布尔两态收、非布尔拒、缺省不写键（缺省语义不靠空值表达）。"""
    mod = _load_module()
    for value in (False, True):
        decl, err = mod.validate_mode_declaration(
            {"mode": "x", "name": "X", "foreground_selectable": value}
        )
        assert decl is not None and err == ""
        assert decl["foreground_selectable"] is value
    decl, err = mod.validate_mode_declaration(
        {"mode": "x", "name": "X", "foreground_selectable": "no"}
    )
    assert decl is None and "foreground_selectable" in err
    minimal, _ = mod.validate_mode_declaration({"mode": "x", "name": "X"})
    assert minimal is not None and "foreground_selectable" not in minimal


def test_aggregate_foreground_selectable_default_true_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """聚合归一：显式 false 透出 False，缺省（None/True）归一 True——前端按
    `!== false` 过滤，缺省出现（向后兼容既有声明），显式声明才隐藏。"""
    mod = _load_module()
    _write_pkg(
        tmp_path,
        "mode_hidden_test",
        {"mode": "hidden_test", "name": "隐藏型", "foreground_selectable": False},
    )
    _write_pkg(tmp_path, "mode_shown_test", {"mode": "shown_test", "name": "缺省型"})
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    out = mod.aggregate_modes()
    assert out["errors"] == []
    modes = {m["mode"]: m for m in out["modes"]}
    assert modes["hidden_test"]["foreground_selectable"] is False
    assert modes["shown_test"]["foreground_selectable"] is True


@pytest.mark.parametrize(
    "decl,fragment",
    [
        ({"mode": "x", "name": "X", "tool_card": "fold"}, "tool_card"),
        ({"mode": "x", "name": "X", "presenter": {"source": "cards"}}, "presenter"),
        ({"name": "X"}, "mode"),
        ({"mode": "x"}, "name"),
        # 退役面回拒（批 F）：单值 pipeline 与 pipeline_profile 均白名单外
        ({"mode": "x", "name": "X", "pipeline": "x_pipe"}, "pipeline"),
        ({"mode": "x", "name": "X", "pipeline_profile": {"pipeline": "a"}}, "pipeline_profile"),
        # pipelines 列表结构（D2 多管列表化）
        ({"mode": "x", "name": "X", "pipelines": "roleplay"}, "pipelines"),
        ({"mode": "x", "name": "X", "pipelines": []}, "pipelines"),
        ({"mode": "x", "name": "X", "pipelines": ["roleplay"]}, "pipelines"),
        ({"mode": "x", "name": "X", "pipelines": [{"name": "p"}]}, "context"),
        ({"mode": "x", "name": "X", "pipelines": [{"context": "conversation"}]}, "name"),
        ({"mode": "x", "name": "X", "pipelines": [{"name": "p", "context": "chat"}]}, "context"),
        ({"mode": "x", "name": "X", "pipelines": [{"name": "p", "context": None}]}, "context"),
        ({"mode": "x", "name": "X", "pipelines": [{"name": "mode_x/main", "context": "task"}]}, "name"),
        ({"mode": "x", "name": "X", "pipelines": [{"name": "p", "context": "task", "source": "mirror"}]}, "source"),
        # name 唯一（一管一配置，D2 不变量）
        (
            {"mode": "x", "name": "X",
             "pipelines": [{"name": "p", "context": "conversation"}, {"name": "p", "context": "task"}]},
            "重复",
        ),
    ],
)
def test_validate_rejects(decl: dict[str, Any], fragment: str) -> None:
    mod = _load_module()
    got, err = mod.validate_mode_declaration(decl)
    assert got is None and fragment in err


def test_validate_rejects_package_source_with_pending_verdict() -> None:
    """source:package 包内自持落地通道留拍板（D2）——fail-closed 拒绝并明示原因，
    防声明了走不通。"""
    mod = _load_module()
    got, err = mod.validate_mode_declaration({
        "mode": "x", "name": "X",
        "pipelines": [{"name": "p", "context": "conversation", "source": "package"}],
    })
    assert got is None
    assert "包内自持待拍板" in err and "registry" in err


def test_validate_accepts_minimal_and_full() -> None:
    mod = _load_module()
    minimal, err = mod.validate_mode_declaration({"mode": "x", "name": "X"})
    assert minimal is not None and err == ""
    full, err = mod.validate_mode_declaration({
        "mode": "x", "name": "X", "pipelines": [
            {"name": "x_pipe", "context": "conversation"},
            {"name": "x_task", "context": "task", "source": "registry"},
        ], "panel_page_id": "p",
        "presenter": {"source": "agent_registry"}, "tool_card": "hide",
        "material": "material.py::build_injection",
        "chain": {"entry": "main"}, "suite": {"smoke": "a.yaml"},
        "theme": "ocean_dark", "icon": "🌊",
        "plan_spec": "skill-solution-x",
    })
    assert full is not None and err == ""


@pytest.mark.parametrize(
    "plan_spec",
    [
        "",                                  # 空串
        "   ",                               # 纯空白
        42,                                  # 非字符串
        None,                                # null
        ["skill-solution-x"],                # 列表
        "skills/skill-solution-x/SKILL.md",  # 路径形态（只收技能名，防第二路径回流）
        "../escape",                         # 穿越形态
    ],
)
def test_validate_rejects_malformed_plan_spec(plan_spec: Any) -> None:
    """plan_spec 形态闸（批 B2）：声明了就必须是技能名形态；不声明 = 不写该键。"""
    mod = _load_module()
    got, err = mod.validate_mode_declaration({"mode": "x", "name": "X", "plan_spec": plan_spec})
    assert got is None
    assert "技能名形态" in err


@pytest.mark.parametrize(
    ("decl", "fragment"),
    [
        ({"mode": "x", "name": "X", "theme": ""}, "theme"),
        ({"mode": "x", "name": "X", "theme": "   "}, "theme"),
        ({"mode": "x", "name": "X", "theme": 42}, "theme"),
        ({"mode": "x", "name": "X", "icon": ""}, "icon"),
        ({"mode": "x", "name": "X", "icon": None}, "icon"),
        ({"mode": "x", "name": "X", "icon": ["🎭"]}, "icon"),
    ],
)
def test_validate_rejects_malformed_theme_icon(decl: dict[str, Any], fragment: str) -> None:
    """theme/icon 简单形态校验（批 G⑤）：声明了就必须是非空字符串（不声明 = 缺省）。"""
    mod = _load_module()
    got, err = mod.validate_mode_declaration(decl)
    assert got is None
    assert fragment in err
    assert "非空字符串" in err


@pytest.mark.parametrize(
    "planning_executors",
    [
        [],                     # 空列表（零声明 = 不写该键，缺省语义不靠空值）
        "research_agent",       # 非列表
        [42],                   # 元素非字符串
        ["../evil"],            # 穿越形态
        ["Mode_X/agent"],       # 模式名大写
        ["mode_x/a-b"],         # stem 连字符
        ["mode_x/a/b"],         # 多级段
        [""],                   # 空串
    ],
)
def test_validate_rejects_malformed_planning_executors(planning_executors: Any) -> None:
    """planning_executors 形态闸（批 B4）：非空字符串列表，元素 = mode_X/<stem>
    或系统 agent 裸名；零声明 = 不写该键。"""
    mod = _load_module()
    got, err = mod.validate_mode_declaration(
        {"mode": "x", "name": "X", "planning_executors": planning_executors}
    )
    assert got is None


def test_validate_accepts_declared_planning_executors() -> None:
    """合法声明（模式键 + 系统 agent 裸名混排）过闸；重复键不在此拒——聚合面去重。"""
    mod = _load_module()
    got, err = mod.validate_mode_declaration({
        "mode": "x", "name": "X",
        "planning_executors": ["mode_x/planner_agent", "research_agent"],
    })
    assert got is not None and err == ""


# ── 3. 单包坏不拖垮好包 ──────────────────────────────────────────────────


def test_fail_closed_single_bad_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    _write_pkg(tmp_path, "mode_good", {"mode": "good", "name": "好"})
    _write_pkg(tmp_path, "mode_bad", "mode: bad\nname: 坏\nmystery_key: 1\n")
    (tmp_path / "mode_rusty").mkdir()
    (tmp_path / "mode_rusty" / "mode.yaml").write_text("{a: [unclosed", encoding="utf-8")
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    out = mod.aggregate_modes()
    assert [m["mode"] for m in out["modes"]] == ["good"]
    assert out["total"] == 1
    assert len(out["errors"]) == 2
    assert any("mode_bad" in e and "未知字段" in e for e in out["errors"])
    assert any("mode_rusty" in e and "不可读" in e for e in out["errors"])


# ── 4. 空目录合法 ────────────────────────────────────────────────────────


def test_empty_root_is_legal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    out = mod.aggregate_modes()
    assert out == {"modes": [], "total": 0, "errors": []}


def test_missing_root_is_legal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path / "nope")
    assert mod.aggregate_modes() == {"modes": [], "total": 0, "errors": []}


# ── 5. http 分发 ─────────────────────────────────────────────────────────


def test_http_route_serves_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: _REPO_MODES_DIR)
    res = asyncio.run(mod.http_handle(path="/ext/agent_manager/modes", method="GET"))
    assert res["success"] is True
    body = json.loads(base64.b64decode(res["data"]["body"]))
    assert body["total"] == 7
    assert any(
        m["mode"] == "roleplay"
        and m["pipelines"] == [{"name": "roleplay", "context": "conversation"}]
        for m in body["modes"]
    )


# ── 6. mode.list 服务（{{mode_catalog}} 渲染取数单源，设计 D5/D10） ────────


def test_list_modes_real_factory_packages() -> None:
    """真实出厂包：六模式全在、按 mode 键排序、条目字段齐全（批 F3 起六包全
    声明 description——主 agent 匹配决策模式行退役后的唯一域路由知识）。"""
    mod = _load_module()
    entries = mod.list_modes()
    assert [e["mode"] for e in entries] == sorted(
        ["coding", "evolution", "godot", "research", "roleplay", "writing", "personal"]
    )
    rp = next(e for e in entries if e["mode"] == "roleplay")
    assert rp == {
        "mode": "roleplay",
        "name": "酒馆",
        "description": (
            "角色扮演对话（开演/附身/角色卡与世界书）。对话链模式：main 即执行者"
            "不派发任务，附身时执行身份切换为所演卡 agent（链入口不变）"
        ),
        "pipelines": [{"name": "roleplay", "context": "conversation"}],
        "chain": {"entry": "main"},
        "icon": "🍺",   # 批 G⑤ 随载荷透出（{{mode_catalog}} 渲染面不消费）
        "theme": None,  # 本批零包声明 theme（消费通道 G-B，未消费不声明）
        "plan_spec": None,  # 批 B2：roleplay/evolution 显式缺省零声明
        "planning_executors": None,  # 批 B4：本批六包零声明（机制先行）
        "default": False,  # M1 并入刀：出生默认绑定标志（仅 personal 声明 true）
    }
    coding = next(e for e in entries if e["mode"] == "coding")
    assert coding["pipelines"] == [] and coding["chain"] == {"entry": "main"}
    assert coding["icon"] == "🔨"
    # 全七包 icon 声明齐全（选择器选项图标，D1 registry 驱动声明面）
    assert {e["icon"] for e in entries} == {
        "🔨", "✍️", "🍺", "🔍", "🎮", "🧬", "🌱",
    }
    # 批 B2：四域包 plan_spec 声明随载荷透出（骨架第零步注册表查询取数面），
    # roleplay/evolution 零声明 = None
    assert {e["mode"]: e["plan_spec"] for e in entries} == {
        "coding": "skill-solution-software",
        "writing": "skill-solution-novel",
        "godot": "skill-solution-game",
        "research": "skill-solution-research",
        "roleplay": None,
        "evolution": None,
        "personal": None,
    }
    # 批 B4：planning_executors 七包零声明（机制先行，声明留真实需求）→ 全 None
    assert {e["planning_executors"] for e in entries} == {None}
    # M1 并入刀：default 至多一个——唯一默认 = personal，其余 False
    assert {e["mode"]: e["default"] for e in entries}["personal"] is True
    assert sum(1 for e in entries if e["default"]) == 1


def test_list_modes_descriptions_carry_routing_knowledge() -> None:
    """批 F3 接管口径：主 agent 匹配决策模式行退役后，目录条目 description 是
    L1 唯一域路由知识——六包全声明非空 description，且独立回答三问
    （什么任务归我 / 直派谁 / 编排链要点）：直派快道键或编排链要点在场。"""
    mod = _load_module()
    entries = {e["mode"]: e for e in mod.list_modes()}
    for mode_key, entry in entries.items():
        desc = entry["description"]
        assert isinstance(desc, str) and desc.strip(), f"{mode_key} description 缺失/为空"
        assert "。" in desc, f"{mode_key} description 未成句（路由知识不可过薄）: {desc!r}"
    # 路由键逐包在场（与其 chain 声明同源：直派快道 / 编排链执行者）。
    # coding 自 2026-10-23 用户裁定起一律派执行型编排 L2，不再直派 L3 执行者
    # （L3 无 task_submit，收尾/审查类任务书会撞派发死墙）——直派键须不在场。
    coding = entries["coding"]["description"]
    assert "mode_coding/programming_orchestrator_agent_v2" in coding
    assert "mode_coding/code_writer" not in coding
    assert "不再直派" in coding
    godot = entries["godot"]["description"]
    assert "mode_godot/godot_expert" in godot
    assert "mode_godot/godot_orchestrator_agent" in godot
    assert "novel_orchestrator_agent" in entries["writing"]["description"]
    research = entries["research"]["description"]
    assert "research_agent" in research and "research_orchestrator_agent" in research
    assert "main 即执行者" in entries["roleplay"]["description"]
    assert "mode_evolution/evolution" in entries["evolution"]["description"]


def test_mode_list_service_envelope_shape() -> None:
    """mode.list 服务返回 {"modes": [...], "total": N}（服务面信封）。"""
    mod = _load_module()
    out = asyncio.run(mod.mode_list())
    assert out["total"] == len(out["modes"]) == 7
    assert all(
        {
            "mode", "name", "description", "pipelines", "chain", "icon", "theme",
            "plan_spec", "planning_executors",
        } <= set(e)
        for e in out["modes"]
    )


def test_aggregate_transports_declared_theme_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明面映射：theme/icon 按声明透出、未声明 = None（theme 通道为 G-B 备好，
    本批出厂零声明——未消费不声明防空转）。"""
    mod = _load_module()
    _write_pkg(tmp_path, "mode_themed", {
        "mode": "themed", "name": "T", "theme": "ocean_dark", "icon": "🌊",
        "plan_spec": "skill-solution-x",
        "planning_executors": ["mode_themed/planner", "research_agent"],
    })
    _write_pkg(tmp_path, "mode_bare", {"mode": "bare", "name": "B"})
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    by_mode = {m["mode"]: m for m in mod.aggregate_modes()["modes"]}
    assert by_mode["themed"]["theme"] == "ocean_dark"
    assert by_mode["themed"]["icon"] == "🌊"
    assert by_mode["themed"]["plan_spec"] == "skill-solution-x"
    assert by_mode["themed"]["planning_executors"] == ["mode_themed/planner", "research_agent"]
    assert by_mode["bare"]["theme"] is None
    assert by_mode["bare"]["icon"] is None
    assert by_mode["bare"]["plan_spec"] is None
    assert by_mode["bare"]["planning_executors"] is None


def test_list_modes_shapes_declared_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """声明面映射：description/pipelines/chain.entry 按声明透出，缺省回落
    空列表 / None；排序与包目录创建顺序无关（确定性）。"""
    mod = _load_module()
    _write_pkg(
        tmp_path, "mode_zeta",
        {"mode": "zeta", "name": "Z", "description": "Z 类任务",
         "pipelines": [{"name": "zeta_pipe", "context": "conversation"}],
         "chain": {"entry": "main"}},
    )
    _write_pkg(
        tmp_path, "mode_alpha",
        {"mode": "alpha", "name": "A"},  # 无 description/pipelines/chain
    )
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    entries = mod.list_modes()
    assert [e["mode"] for e in entries] == ["alpha", "zeta"], "按 mode 键排序，非目录序"
    assert entries[0] == {
        "mode": "alpha", "name": "A", "description": None,
        "pipelines": [], "chain": {"entry": None},
        "icon": None, "theme": None, "plan_spec": None,
        "planning_executors": None, "default": False,
    }
    assert entries[1]["description"] == "Z 类任务"
    assert entries[1]["pipelines"] == [{"name": "zeta_pipe", "context": "conversation"}]
    assert entries[1]["chain"] == {"entry": "main"}


def test_list_modes_skips_bad_packages_like_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏包（未知字段）fail-closed 缺位，好包照常——与聚合读面同口径。"""
    mod = _load_module()
    _write_pkg(tmp_path, "mode_good", {"mode": "good", "name": "好"})
    _write_pkg(tmp_path, "mode_bad", "mode: bad\nname: 坏\nmystery_key: 1\n")
    monkeypatch.setattr(mod, "_factory_modes_dir", lambda: tmp_path)
    assert [e["mode"] for e in mod.list_modes()] == ["good"]
