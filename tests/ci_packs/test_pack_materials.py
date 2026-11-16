# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""packs/ci 物料契约测试（工厂面出厂件的静态契约，设计稿 §3.1/§3.3）。

断言的是可观察物料：pack.json/gates.json 形态、checker 齐备、gates 通过 runner
契约校验、stdlib-only 红线（设计稿 §3.1：runner 与 checker 均 stdlib 零三方）。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from tests.ci_packs.conftest import BASE_PACK, GODOT_PACK, PACKS_ROOT, RUN_PY, load_runner_module

pytestmark = pytest.mark.unit

BASE_GATES = ["no_build_artifacts", "oversized_files", "repo_size_budget", "doc_links", "secrets_scan", "asset_conventions"]
GODOT_GATES = {
    "project_godot_hygiene": "block",
    "addons_present": "block",
    "gdparse_syntax": "block",
    "godot_headless_import": "block",
    "godot_boot_smoke": "warn",
}


def _pack_json(pack: Path) -> dict:
    return json.loads((pack / "pack.json").read_text(encoding="utf-8"))


def _gates_json(pack: Path) -> list[dict]:
    data = json.loads((pack / "gates.json").read_text(encoding="utf-8"))
    return data["gates"] if isinstance(data, dict) else data


class TestBasePack:
    def test_pack_json_shape(self) -> None:
        data = _pack_json(BASE_PACK)
        assert data["name"] == "base"
        assert data["version"].count(".") == 2

    def test_six_checkers_present(self) -> None:
        for gate_id in BASE_GATES:
            assert (BASE_PACK / "checks" / f"{gate_id}.py").is_file(), f"缺 checker: {gate_id}"

    def test_runner_and_hook_and_readme_present(self) -> None:
        assert RUN_PY.is_file()
        hook = BASE_PACK / "hooks" / "pre-commit"
        assert hook.is_file()
        hook_text = hook.read_text(encoding="utf-8")
        assert "run.py" in hook_text and "--lane commit" in hook_text
        readme = (BASE_PACK / "README.md").read_text(encoding="utf-8")
        assert "local/gates.json" in readme  # 补门姿势一页说明
        assert "asset_rules.json" in readme  # 素材规范引擎的规则文件说明

    def test_gates_pass_runner_contract(self) -> None:
        runner = load_runner_module()
        errors = runner.validate_gates(_gates_json(BASE_PACK), "factory", BASE_PACK)
        assert errors == []


class TestGodotPack:
    def test_pack_json_shape(self) -> None:
        data = _pack_json(GODOT_PACK)
        assert data["name"] == "godot"
        assert data["version"].count(".") == 2

    def test_five_gates_with_adjudicated_tiers(self) -> None:
        gates = {g["id"]: g for g in _gates_json(GODOT_PACK)}
        assert set(gates) == set(GODOT_GATES)
        for gate_id, tier in GODOT_GATES.items():
            assert gates[gate_id]["tier"] == tier, f"{gate_id} 档位与裁定不符"
            assert gates[gate_id]["lane"] == "commit"

    def test_gates_pass_runner_contract(self) -> None:
        runner = load_runner_module()
        errors = runner.validate_gates(_gates_json(GODOT_PACK), "factory", GODOT_PACK)
        assert errors == []


class TestFactoryInvariants:
    def test_no_id_collision_across_packs(self) -> None:
        """组装不变量（§6.2 同 id 冲突整体报错的前置）：出厂件自身无跨包同 id。"""
        ids = [g["id"] for g in _gates_json(BASE_PACK)] + [g["id"] for g in _gates_json(GODOT_PACK)]
        assert len(ids) == len(set(ids))

    def test_pack_scripts_are_stdlib_only(self) -> None:
        """设计红线（§3.1）：pack 内 Python 件零三方依赖（项目环境不可假设有 PyYAML）。

        包内共享件（同目录 <name>.py 实体存在，如 checks/_common.py）不算三方。
        """
        assert hasattr(sys, "stdlib_module_names")
        pack_modules = {p.stem for p in PACKS_ROOT.rglob("*.py")}  # 装配后 checks/ 平铺合并
        offenders: list[str] = []
        for py in sorted(PACKS_ROOT.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top in sys.stdlib_module_names or top in pack_modules:
                        continue
                    offenders.append(f"{py.relative_to(PACKS_ROOT)}: {name}")
        assert not offenders, f"pack 内出现三方导入: {offenders}"
