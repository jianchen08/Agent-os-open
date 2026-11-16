# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""六模式出厂种子契约测试：manifest / profile / server 三面同验。

种子单元自包含（目录级播种/升级/回退的结构前提），六份同构种子共用同一
行为契约——按插件参数化逐份断言，各组互异输入天然构成防拟合的区分度；
profile 与 plugin.json 走真实文件读取，不 mock 文件面。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.abspath(os.path.join(MODES_DIR, "..", ".."))

# panel_page_id 契约（设计 §4.2：mode.describe 透出，前端不硬编码映射）
SEEDS = [
    ("mode_coding", "coding", "coding_delivery"),
    ("mode_writing", "writing", "writing_workshop"),
    ("mode_roleplay", "roleplay", "roleplay_studio"),
    ("mode_research", "research", "research_desk"),
    ("mode_godot", "godot", "godot_dev"),
    ("mode_evolution", "evolution", "evolution_panel"),
]

DESCRIBE_KEYS = {"mode", "name", "chain", "weights", "budget", "profile_path", "panel_page_id"}
PROFILE_REQUIRED_KEYS = {
    "mode", "name", "panel_page_id", "chain", "suite",
    "material_scope", "levers", "verifier_families", "weights", "budget",
}

# 服务面契约（每模式服务白名单；缺省 = describe/get_profile 基线两件）。
# mode_roleplay 批 4-Ⅰ 增 mode.game.push_delta（游戏域 delta 入队，宿主推送通道）。
EXPECTED_SERVICES = {
    # T6c：mode_roleplay 兼卡片/渲染域主，card.* 八服务 = card_lint 单源的服务
    # 透出面（provides namespace card；消费方 agent_config_load / mode_evolution）
    "mode_roleplay": {
        "mode.describe",
        "mode.get_profile",
        "mode.game.push_delta",
        "card.lint_decl",
        "card.lint_actions",
        "card.lint_pack_ui",
        "card.lint_extensions",
        "card.scan_suspicious",
        "card.scan_html_card",
        "card.scan_html_texts",
        "card.texts_of_tree",
    },
}

pytestmark = pytest.mark.unit


def _load_server(plugin_id: str):
    """按唯一模块名装载各插件 server.py（多份同构种子共存一进程，防裸名互覆）。"""
    path = os.path.join(MODES_DIR, plugin_id, "server.py")
    spec = importlib.util.spec_from_file_location(f"mode_seed_{plugin_id}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_profile(plugin_id: str) -> dict:
    path = os.path.join(MODES_DIR, plugin_id, "mode.yaml")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_describe_returns_contract_fields(plugin_id: str, mode: str, panel_page_id: str) -> None:
    module = _load_server(plugin_id)
    describe = asyncio.run(module.mode_describe())
    assert set(describe) == DESCRIBE_KEYS
    assert describe["mode"] == mode
    assert describe["panel_page_id"] == panel_page_id
    assert describe["name"]
    # profile_path 透出包内打包位置（种子单元自包含的可验证面）
    assert describe["profile_path"] == os.path.join(MODES_DIR, plugin_id, "mode.yaml")
    assert os.path.isfile(describe["profile_path"])


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_get_profile_returns_full_seed(plugin_id: str, mode: str, panel_page_id: str) -> None:
    module = _load_server(plugin_id)
    profile = asyncio.run(module.mode_get_profile())
    assert set(profile) >= PROFILE_REQUIRED_KEYS
    assert profile["mode"] == mode
    assert profile["panel_page_id"] == panel_page_id
    # describe 与 get_profile 同源一致（单一真值，不得两份内容漂移）
    describe = asyncio.run(module.mode_describe())
    assert describe["chain"] == profile["chain"]
    assert describe["weights"] == profile["weights"]
    assert describe["budget"] == profile["budget"]


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_profile_suite_paths_exist(plugin_id: str, mode: str, panel_page_id: str) -> None:
    profile = _load_profile(plugin_id)
    suite = profile["suite"]
    assert isinstance(suite, dict)
    assert suite
    for tier, rel in suite.items():
        if rel is None:  # 预留档位（如 full 二期补全）允许缺席
            continue
        assert os.path.isfile(os.path.join(REPO_ROOT, str(rel))), f"{plugin_id} suite.{tier}"


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_profile_material_scope_targets_exist(plugin_id: str, mode: str, panel_page_id: str) -> None:
    """material_scope 是该模式允许被提案的 A 部类物料子集——登记的杠杆面必须真实存在。"""
    profile = _load_profile(plugin_id)
    for stage, targets in profile["material_scope"].items():
        assert isinstance(targets, list), f"{plugin_id} material_scope.{stage} 须为列表"
        assert targets, f"{plugin_id} material_scope.{stage} 为空"
        for rel in targets:
            assert os.path.exists(os.path.join(REPO_ROOT, str(rel))), f"{plugin_id}: {rel}"


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_profile_chain_keys_whitelist(plugin_id: str, mode: str, panel_page_id: str) -> None:
    """chain 声明键白名单钉（P1 裁定收口，ADR 2026-10-09 决策 2）：执行者池
    声明全模式退役后，chain 只准声明 entry/expected_path/entry_override 三键；
    白名单外键（含已退役的执行者池）一律拒——防死声明借新键名回流。"""
    profile = _load_profile(plugin_id)
    unknown = sorted(set(profile.get("chain", {})) - {"entry", "expected_path", "entry_override"})
    assert unknown == [], f"{plugin_id} chain 白名单外声明键: {unknown}"


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_manifest_declares_mode_services(plugin_id: str, mode: str, panel_page_id: str) -> None:
    with open(os.path.join(MODES_DIR, plugin_id, "plugin.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["id"] == plugin_id
    assert manifest["plugin_type"] == "system"
    assert manifest["host_type"] == "sidecar"
    assert manifest["entry"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), "版本真值须 semver（发种 = bump version）"
    services = {svc["name"]: svc for svc in manifest["capabilities"]["services"]}
    assert set(services) == EXPECTED_SERVICES.get(
        plugin_id, {"mode.describe", "mode.get_profile"}
    )
    for name, svc in services.items():
        assert isinstance(svc.get("input_schema"), dict), name
        assert isinstance(svc.get("output_schema"), dict), name


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_corrupt_profile_fails_closed(plugin_id: str, mode: str, panel_page_id: str,
                                      tmp_path) -> None:
    """profile 种子损坏（缺 mode 键）必须报错，不得静默返回半残 profile。"""
    module = _load_server(plugin_id)
    bad = tmp_path / "mode.yaml"
    bad.write_text("name: 空壳\n", encoding="utf-8")
    with pytest.raises(ValueError, match="种子损坏"):
        module.load_profile(str(bad))


def _writing_pack_rules_text() -> str:
    rules_dir = os.path.join(MODES_DIR, "mode_writing", "rules")
    assert os.path.isdir(rules_dir), "mode_writing 包缺 rules/ 口径目录（context_build 模式段口径占位）"
    parts: list[str] = []
    for entry in sorted(Path(rules_dir).iterdir()):
        name = entry.name
        if name.endswith(".md"):
            with open(os.path.join(rules_dir, name), encoding="utf-8") as fh:
                parts.append(fh.read())
    return "\n".join(parts)


@pytest.mark.parametrize(
    "anchor",
    ["file_check", "产物文件"],
    ids=["acceptance_metric", "deliverable_filename"],
)
def test_writing_pack_rules_carry_dispatch_acceptance(anchor: str) -> None:
    """BUG-33 契约：写作模式包必须携带派发口径——派发写作成稿任务必须声明
    file_check 验收且 goal 写明产物文件名。无指标派发会让子任务在零验证下
    被标记完成（假成功面）；本口径经 context_build 模式段注入主 agent。"""
    assert anchor in _writing_pack_rules_text(), f"mode_writing 派发口径缺锚点 {anchor!r}"


# 批 B2 方案规划段领域规格注册（ADR 2026-10-09 决策 4）：四域包声明 plan_spec
# （值 = 技能名，包内 skills/ 随包经工作空间并集快照按名可读）；roleplay /
# evolution 显式缺省零声明（无方案规格的域不写键）。
PLAN_SPEC_EXPECTED = {
    "mode_coding": "skill-solution-software",
    "mode_writing": "skill-solution-novel",
    "mode_godot": "skill-solution-game",
    "mode_research": "skill-solution-research",
    "mode_roleplay": None,
    "mode_evolution": None,
}


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_plan_spec_declares_packaged_skill(plugin_id: str, mode: str, panel_page_id: str,
                                           tmp_path, monkeypatch) -> None:
    """plan_spec 种子契约：四域包声明且指向包内真实技能（按名可解析），
    两包显式缺省零声明；声明形态合法性由 parity/agent_manager 同源闸兜底。"""
    profile = _load_profile(plugin_id)
    expected = PLAN_SPEC_EXPECTED[plugin_id]
    assert profile.get("plan_spec") == expected, f"{plugin_id} plan_spec 声明漂移"
    if expected is None:
        return
    # 消费通道验收（骨架第零步注册表查询同路）：用户根钉空（双根用户赢解析
    # 与机器播种状态解耦）后，registry 名必须解析回本包技能目录。
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    shared_dir = os.path.abspath(os.path.join(MODES_DIR, ".."))
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)
    from mode_keys import find_mode_skill_dir

    skill_dir = find_mode_skill_dir(mode, expected)
    assert skill_dir is not None, f"{plugin_id}: plan_spec 技能 {expected} 不可按名解析"
    assert (skill_dir / "SKILL.md").is_file()
    assert os.path.dirname(os.path.dirname(str(skill_dir))) == os.path.join(MODES_DIR, plugin_id)


@pytest.mark.parametrize(("plugin_id", "mode", "panel_page_id"), SEEDS)
def test_planning_executors_zero_declaration_this_batch(
    plugin_id: str, mode: str, panel_page_id: str
) -> None:
    """批 B4 机制先行：六包本批均零声明 planning_executors（缺省 = 零贡献
    fail-closed）；声明留真实需求（E3 声明键补齐时逐包按需落键并同刀改本钉）。"""
    profile = _load_profile(plugin_id)
    assert "planning_executors" not in profile, (
        f"{plugin_id} 出现 planning_executors 声明：须有真实规划期外包需求"
        "并同刀更新本契约钉"
    )
