# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""出生组装测试（设计稿 §4.1 四路信号 / §5 出生接线 / §6.2 出生面信号）。

经 project_create 工具的 create 路径驱动（隔离登记目录 + 临时配方表）；
packs 真源默认指向仓内 packs/ci（真实依赖），冲突/版本闸场景用临时 mini pack。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.ci_packs.conftest import load_runner_module

pytestmark = pytest.mark.unit

_SHARED_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from project_manager import project_ci_assembly, project_create_tool  # noqa: E402


def _tool() -> Any:
    return project_create_tool.ProjectCreateTool()


def _godot_config(source_root: Path, ci_packs: list[str] | None = None) -> str:
    """隔离类型配方表（addon 源指向传入目录；ci_packs 可选注入）。"""
    text = (
        "project_types:\n"
        "  godot:\n"
        "    schema_summary: Godot 4 测试配方\n"
        "    detect_file: project.godot\n"
        "    addons_source: " + source_root.as_posix() + "\n"
        "    addons:\n"
        "      - agentos\n"
        "      - godot_mcp\n"
        "    project_file: project.godot\n"
        '    scaffold: "config_version=5\\n\\n[application]\\n\\nconfig/name=\\"{title}\\"\\n"\n'
        "    enable_section: editor_plugins\n"
        "    enable_entries:\n"
        "      - res://addons/agentos/plugin.cfg\n"
        "      - res://addons/godot_mcp/plugin.cfg\n"
    )
    if ci_packs is not None:
        text += "    ci_packs:\n"
        text += "".join(f"      - {p}\n" for p in ci_packs)
    return text


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离登记目录 + 工作空间基目录（与既有 test_project_create 同构）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_manager.project_registry.workspace_base_dir", lambda: ws_base)
    return {"tasks_root": tasks_root, "ws_base": ws_base}


@pytest.fixture
def godot_env(env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """godot 配方（ci_packs=[base, godot]）+ 仓内真实 packs 真源。"""
    src_root = tmp_path / "addons_src"
    for name in ("agentos", "godot_mcp"):
        addon_dir = src_root / name
        addon_dir.mkdir(parents=True)
        (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
    cfg = tmp_path / "project_create.yaml"
    cfg.write_text(_godot_config(src_root, ci_packs=["base", "godot"]), encoding="utf-8")
    monkeypatch.setattr(project_create_tool, "_TYPE_CONFIG_PATH", cfg)
    monkeypatch.delenv("GODOT_BIN", raising=False)
    return {**env, "cfg": cfg, "src_root": src_root}


def _mini_pack(
    root: Path, name: str, version: str, gates: list[dict], run_py: str = "print('V')\n"
) -> Path:
    """临时 mini pack（冲突/版本闸场景不需要真实 checker）。"""
    pack = root / name
    (pack / "checks").mkdir(parents=True, exist_ok=True)
    (pack / "pack.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    (pack / "gates.json").write_text(json.dumps({"gates": gates}), encoding="utf-8")
    (pack / "run.py").write_text(run_py, encoding="utf-8")
    return pack


def _gate(gate_id: str, check: str) -> dict:
    return {
        "id": gate_id,
        "lane": "commit",
        "tier": "block",
        "run": ["python", f"checks/{check}.py"],
        "est_seconds": 1,
    }


class TestBirthAssembly:
    async def test_godot_create_installs_base_and_godot(self, godot_env: dict[str, Any]) -> None:
        """验收①：create godot → .agentos/ci/gates.json packs 含 base+godot，钩子在位。"""
        target = godot_env["ws_base"] / "g1"
        r = await _tool().execute({"goal": "G1", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        ci = target / ".agentos" / "ci"
        gates = json.loads((ci / "gates.json").read_text(encoding="utf-8"))
        assert set(gates["packs"]) == {"base", "godot"}
        ids = {g["id"] for g in gates["gates"]}
        assert "no_build_artifacts" in ids and "godot_headless_import" in ids
        assert (ci / "run.py").is_file() and (ci / "README.md").is_file()
        assert (ci / "local").is_dir()
        installed = {p.name for p in (ci / "checks").glob("*.py")}
        assert {"no_build_artifacts.py", "project_godot_hygiene.py"} <= installed
        assert len(installed) == 12  # base 六门 + 共享件 _common + godot 五门
        hook = target / ".git" / "hooks" / "pre-commit"
        assert hook.is_file() and "run.py" in hook.read_text(encoding="utf-8")
        report = r.output["init"]["ci"]
        assert report["installed"] is True
        assert report["hook_installed"] is True
        assert "godot_bin" in report["probes"]

    async def test_assembled_gates_pass_runner_contract(self, godot_env: dict[str, Any]) -> None:
        """验收①：组装产物 gates.json 通过 runner 契约校验（烘焙后的 argv 仍合法）。"""
        target = godot_env["ws_base"] / "g1c"
        r = await _tool().execute({"goal": "G1c", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        runner = load_runner_module()
        gates = json.loads(
            (target / ".agentos" / "ci" / "gates.json").read_text(encoding="utf-8")
        )["gates"]
        assert runner.validate_gates(gates, "factory", target / ".agentos" / "ci") == []

    async def test_rerun_idempotent_no_overwrite(self, godot_env: dict[str, Any]) -> None:
        """验收④：重复 create 幂等——物料不重复不覆盖。"""
        target = godot_env["ws_base"] / "g2"
        assert (await _tool().execute({"goal": "G2", "path": str(target), "project_type": "godot"})).success
        run_py = target / ".agentos" / "ci" / "run.py"
        run_py.write_text("# 用户手改\n", encoding="utf-8")
        r2 = await _tool().execute({"goal": "G2", "path": str(target), "project_type": "godot"})
        assert r2.success and r2.output["created"] is False
        report = r2.output["init"]["ci"]
        assert report["installed"] is True
        assert any("版本闸" in s for s in report["packs_skipped"])
        assert run_py.read_text(encoding="utf-8") == "# 用户手改\n"  # 同版本不覆盖

    async def test_version_gate_strictly_greater_overwrites(
        self, env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收④：出厂版本严格大于已装版本才覆盖（J5/BuiltinFirst 同构）。"""
        src_root = tmp_path / "addons_src"
        for name in ("agentos", "godot_mcp"):
            addon_dir = src_root / name
            addon_dir.mkdir(parents=True)
            (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
        cfg = tmp_path / "project_create.yaml"
        cfg.write_text(_godot_config(src_root, ci_packs=["base"]), encoding="utf-8")
        monkeypatch.setattr(project_create_tool, "_TYPE_CONFIG_PATH", cfg)
        packs_root = tmp_path / "packs"
        _mini_pack(packs_root, "base", "1.0.0", [_gate("b1", "c1")], run_py="print('V1')\n")
        monkeypatch.setattr(project_ci_assembly, "PACKS_ROOT", packs_root)

        target = env["ws_base"] / "g3"
        assert (await _tool().execute({"goal": "G3", "path": str(target), "project_type": "godot"})).success
        run_py = target / ".agentos" / "ci" / "run.py"
        run_py.write_text("# TAMPERED\n", encoding="utf-8")
        # 同版本重跑：版本闸跳过，手改保留
        await _tool().execute({"goal": "G3", "path": str(target), "project_type": "godot"})
        assert run_py.read_text(encoding="utf-8") == "# TAMPERED\n"
        # 出厂升版：工厂面刷新
        _mini_pack(packs_root, "base", "1.1.0", [_gate("b1", "c1")], run_py="print('V2')\n")
        r3 = await _tool().execute({"goal": "G3", "path": str(target), "project_type": "godot"})
        assert r3.success
        assert run_py.read_text(encoding="utf-8") == "print('V2')\n"
        gates = json.loads((target / ".agentos" / "ci" / "gates.json").read_text(encoding="utf-8"))
        assert gates["packs"]["base"] == "1.1.0"

    async def test_local_face_exempt_from_version_gate(self, godot_env: dict[str, Any]) -> None:
        """验收④/§5：local/ 豁免于版本闸——重跑 create 后沉淀门禁原样存活。"""
        target = godot_env["ws_base"] / "g4"
        assert (await _tool().execute({"goal": "G4", "path": str(target), "project_type": "godot"})).success
        local = target / ".agentos" / "ci" / "local"
        (local / "gates.json").write_text(
            json.dumps({"gates": [{"id": "reg", "lane": "commit", "tier": "warn", "run": ["python", "-c", "import sys; sys.exit(0)"], "est_seconds": 1}]}),
            encoding="utf-8",
        )
        (local / "asset_rules.json").write_text("{}", encoding="utf-8")
        r2 = await _tool().execute({"goal": "G4", "path": str(target), "project_type": "godot"})
        assert r2.success
        assert "reg" in (local / "gates.json").read_text(encoding="utf-8")
        assert (local / "asset_rules.json").is_file()

    async def test_cross_pack_id_conflict_fails_closed(
        self, env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收⑤/§6.2：组装期两 pack 同 id → 整体报错，不留半成品。"""
        src_root = tmp_path / "addons_src"
        for name in ("agentos", "godot_mcp"):
            addon_dir = src_root / name
            addon_dir.mkdir(parents=True)
            (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
        cfg = tmp_path / "project_create.yaml"
        cfg.write_text(_godot_config(src_root, ci_packs=["base", "extra"]), encoding="utf-8")
        monkeypatch.setattr(project_create_tool, "_TYPE_CONFIG_PATH", cfg)
        packs_root = tmp_path / "packs"
        _mini_pack(packs_root, "base", "1.0.0", [_gate("dup", "c1")])
        _mini_pack(packs_root, "extra", "1.0.0", [_gate("dup", "c2")])
        monkeypatch.setattr(project_ci_assembly, "PACKS_ROOT", packs_root)

        target = env["ws_base"] / "g5"
        r = await _tool().execute({"goal": "G5", "path": str(target), "project_type": "godot"})
        assert not r.success
        assert "dup" in (r.error or "")
        assert not (target / ".agentos" / "ci" / "gates.json").exists()

    async def test_existing_hook_not_overwritten_with_warning(self, godot_env: dict[str, Any]) -> None:
        """§6.2 钩子冲突：已有同名 pre-commit → 不覆盖 + 出生报告 warn（用户资产优先）。"""
        target = godot_env["ws_base"] / "g6"
        target.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(target), capture_output=True)  # noqa: S603,S607
        hook = target / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        r = await _tool().execute({"goal": "G6", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho custom\n"
        report = r.output["init"]["ci"]
        assert report["hook_installed"] is False
        assert report["hook_skipped_reason"]

    async def test_four_signals_report_skipped_unshipped_packs(
        self, godot_env: dict[str, Any]
    ) -> None:
        """§4.1 四路信号：语言清单/代码文件命中 → 候选 pack 记入出生报告（未出厂=跳过）。"""
        target = godot_env["ws_base"] / "g7"
        target.mkdir(parents=True)
        (target / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (target / "util.py").write_text("print(1)\n", encoding="utf-8")
        r = await _tool().execute({"goal": "G7", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        report = r.output["init"]["ci"]
        assert set(report["packs"]) == {"base", "godot"}
        assert any("python" in s and "未出厂" in s for s in report["packs_skipped"])

    async def test_recipe_without_ci_packs_defaults_to_base(
        self, env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺省 [base]：配方未写 ci_packs 字段 → 只装底座。"""
        src_root = tmp_path / "addons_src"
        for name in ("agentos", "godot_mcp"):
            addon_dir = src_root / name
            addon_dir.mkdir(parents=True)
            (addon_dir / "plugin.cfg").write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")
        cfg = tmp_path / "project_create.yaml"
        cfg.write_text(_godot_config(src_root), encoding="utf-8")  # 无 ci_packs
        monkeypatch.setattr(project_create_tool, "_TYPE_CONFIG_PATH", cfg)
        target = env["ws_base"] / "g8"
        r = await _tool().execute({"goal": "G8", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        gates = json.loads((target / ".agentos" / "ci" / "gates.json").read_text(encoding="utf-8"))
        assert set(gates["packs"]) == {"base"}

    async def test_godot_bin_probe_baked_into_gates(
        self, godot_env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§3.3：出生探测结果烘焙进 gates.json（GODOT_BIN env → PATH 顺序）。"""
        from tests.ci_packs.conftest import make_tool_shim

        bin_path = make_tool_shim(tmp_path, "bakedgodot", 0)
        monkeypatch.setenv("GODOT_BIN", bin_path)
        target = godot_env["ws_base"] / "g9"
        r = await _tool().execute({"goal": "G9", "path": str(target), "project_type": "godot"})
        assert r.success, r.error
        gates = json.loads((target / ".agentos" / "ci" / "gates.json").read_text(encoding="utf-8"))
        import_gate = next(g for g in gates["gates"] if g["id"] == "godot_headless_import")
        assert bin_path in import_gate["run"]
        assert import_gate["run"][-2] == "--godot"
        # addons 承诺同步烘焙
        addons_gate = next(g for g in gates["gates"] if g["id"] == "addons_present")
        assert "agentos,godot_mcp" in addons_gate["run"]


class TestDetectFunction:
    """四路信号检测函数的单元面（排序确定性 + 信号归因）。"""

    def test_signal_order_and_dedup(self, tmp_path: Path) -> None:
        (tmp_path / "project.godot").write_text("x", encoding="utf-8")
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        recipes = {"godot": {"detect_file": "project.godot", "ci_packs": ["base", "godot"]}}
        available = {"base", "godot", "go", "python"}
        packs, report = project_ci_assembly.detect_ci_packs(
            str(tmp_path), recipes["godot"], recipes, available
        )
        assert packs == ["base", "godot", "go"]
        assert report == []

    def test_unavailable_candidate_reported_with_reason(self, tmp_path: Path) -> None:
        (tmp_path / "util.py").write_text("x", encoding="utf-8")
        packs, report = project_ci_packs_detect(tmp_path)
        assert packs == ["base"]
        assert any("python" in s and "未出厂" in s for s in report)

    def test_code_scan_prunes_noise_dirs(self, tmp_path: Path) -> None:
        """信号扫描剪枝：.git/node_modules 等目录不参与（防误报与扫描开销）。"""
        noise = tmp_path / "node_modules" / "pkg"
        noise.mkdir(parents=True)
        (noise / "x.py").write_text("x", encoding="utf-8")
        packs, report = project_ci_packs_detect(tmp_path)
        assert packs == ["base"]
        assert report == []  # node_modules 内的 .py 不触发 python 信号


def project_ci_packs_detect(project: Path) -> tuple[list[str], list[str]]:
    return project_ci_assembly.detect_ci_packs(str(project), None, {}, {"base"})
