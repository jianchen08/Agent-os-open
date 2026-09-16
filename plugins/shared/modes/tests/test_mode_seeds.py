# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
# -*- coding: utf-8 -*-
"""五模式出厂种子契约测试：manifest / profile / server 三面同验。

种子单元自包含（目录级播种/升级/回退的结构前提），五份同构种子共用同一
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

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.abspath(os.path.join(MODES_DIR, "..", "..", ".."))

# panel_page_id 契约（设计 §4.2：mode.describe 透出，前端不硬编码映射）
SEEDS = [
    ("mode_coding", "coding", "coding_delivery"),
    ("mode_writing", "writing", "writing_workshop"),
    ("mode_roleplay", "roleplay", "roleplay_studio"),
    ("mode_research", "research", "research_desk"),
    ("mode_godot", "godot", "godot_dev"),
]

DESCRIBE_KEYS = {"mode", "name", "chain", "weights", "budget", "profile_path", "panel_page_id"}
PROFILE_REQUIRED_KEYS = {
    "mode", "name", "panel_page_id", "chain", "pipeline_profile", "suite",
    "material_scope", "levers", "verifier_families", "weights", "budget",
}

pytestmark = pytest.mark.unit


def _load_server(plugin_id: str):
    """按唯一模块名装载各插件 server.py（四份同构种子共存一进程，防裸名互覆）。"""
    path = os.path.join(MODES_DIR, plugin_id, "server.py")
    spec = importlib.util.spec_from_file_location(f"mode_seed_{plugin_id}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_profile(plugin_id: str) -> dict:
    path = os.path.join(MODES_DIR, plugin_id, "profile.yaml")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_describe_returns_contract_fields(plugin_id: str, mode: str, panel_page_id: str) -> None:
    module = _load_server(plugin_id)
    describe = asyncio.run(module.mode_describe())
    assert set(describe) == DESCRIBE_KEYS
    assert describe["mode"] == mode
    assert describe["panel_page_id"] == panel_page_id
    assert describe["name"]
    # profile_path 透出包内打包位置（种子单元自包含的可验证面）
    assert describe["profile_path"] == os.path.join(MODES_DIR, plugin_id, "profile.yaml")
    assert os.path.isfile(describe["profile_path"])


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_get_profile_returns_full_seed(plugin_id: str, mode: str, panel_page_id: str) -> None:
    module = _load_server(plugin_id)
    profile = asyncio.run(module.mode_get_profile())
    assert PROFILE_REQUIRED_KEYS <= set(profile)
    assert profile["mode"] == mode
    assert profile["panel_page_id"] == panel_page_id
    # describe 与 get_profile 同源一致（单一真值，不得两份内容漂移）
    describe = asyncio.run(module.mode_describe())
    assert describe["chain"] == profile["chain"]
    assert describe["weights"] == profile["weights"]
    assert describe["budget"] == profile["budget"]


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_profile_suite_paths_exist(plugin_id: str, mode: str, panel_page_id: str) -> None:
    profile = _load_profile(plugin_id)
    suite = profile["suite"]
    assert isinstance(suite, dict) and suite
    for tier, rel in suite.items():
        if rel is None:  # 预留档位（如 full 二期补全）允许缺席
            continue
        assert os.path.isfile(os.path.join(REPO_ROOT, str(rel))), f"{plugin_id} suite.{tier}"


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_profile_material_scope_targets_exist(plugin_id: str, mode: str, panel_page_id: str) -> None:
    """material_scope 是该模式允许被提案的 A 部类物料子集——登记的杠杆面必须真实存在。"""
    profile = _load_profile(plugin_id)
    for stage, targets in profile["material_scope"].items():
        assert isinstance(targets, list) and targets, f"{plugin_id} material_scope.{stage} 为空"
        for rel in targets:
            assert os.path.exists(os.path.join(REPO_ROOT, str(rel))), f"{plugin_id}: {rel}"


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_manifest_declares_mode_services(plugin_id: str, mode: str, panel_page_id: str) -> None:
    with open(os.path.join(MODES_DIR, plugin_id, "plugin.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["id"] == plugin_id
    assert manifest["plugin_type"] == "system"
    assert manifest["host_type"] == "sidecar"
    assert manifest["entry"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), "版本真值须 semver（发种 = bump version）"
    services = {svc["name"]: svc for svc in manifest["capabilities"]["services"]}
    assert set(services) == {"mode.describe", "mode.get_profile"}
    for name, svc in services.items():
        assert isinstance(svc.get("input_schema"), dict), name
        assert isinstance(svc.get("output_schema"), dict), name


@pytest.mark.parametrize("plugin_id,mode,panel_page_id", SEEDS)
def test_corrupt_profile_fails_closed(plugin_id: str, mode: str, panel_page_id: str,
                                      tmp_path) -> None:
    """profile 种子损坏（缺 mode 键）必须报错，不得静默返回半残 profile。"""
    module = _load_server(plugin_id)
    bad = tmp_path / "profile.yaml"
    bad.write_text("name: 空壳\n", encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_profile(str(bad))
