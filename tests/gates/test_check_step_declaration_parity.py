# @feature: FP-0.2.CI 步骤声明↔注册 parity 静态门禁 | @ci: python-test
"""check_step_declaration_parity.py 单测（2026-10-10 CI 防再犯清单 C 项）。

行为契约（断输入→输出，不钉实现）：
- 自名条目（name == 插件 id）豁免注册要求（method=None → ④直引语义）；
- 异名声明未注册 → 违规（调用即 STEP_NOT_FOUND 的潜伏雷）；
- 异名注册未声明 → 违规（③级索引仅聚合 manifests，死注册）；
- 异名双向一致 → 绿；
- 扫描器跳过 node_modules/.venv 等播种/依赖目录；
- 负控制：真实仓状态全绿（现役插件声明与注册一致，todo_track/builtin_tools
  两形态各归各位），合成错位必须打红。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_step_declaration_parity", SCRIPTS / "check_step_declaration_parity.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_self_named_entry_exempt_from_registration() -> None:
    problems = mod.check_plugin("agentos-builtin-tools", ["agentos-builtin-tools"], set())
    assert problems == [], "自名条目=④直引语义，不要求 @plugin.step 注册"


def test_foreign_declared_without_registration_flagged() -> None:
    problems = mod.check_plugin("p_x", ["todo.fold"], set())
    assert len(problems) == 1
    assert "声明未注册" in problems[0]
    assert "todo.fold" in problems[0]
    assert "p_x" in problems[0]


def test_foreign_registered_without_declaration_flagged() -> None:
    problems = mod.check_plugin("p_x", [], {"todo.fold"})
    assert len(problems) == 1
    assert "注册未声明" in problems[0]
    assert "todo.fold" in problems[0]


def test_foreign_parity_both_directions_green() -> None:
    assert mod.check_plugin("p_x", ["todo.fold", "todo.extract"], {"todo.fold", "todo.extract"}) == []


def test_mixed_self_and_foreign_only_foreign_checked() -> None:
    problems = mod.check_plugin("p_x", ["p_x", "a.step"], {"a.step"})
    assert problems == [], "自名+异名混合时只查异名，自名豁免"


def test_registered_regex_matches_decorator_and_call_forms() -> None:
    src = {
        "server.py": "@plugin.step('todo.fold')\ndef f(): ...\n\nother = plugin.step(\"todo.extract\")\n",
    }
    assert mod.registered_steps(src) == {"todo.fold", "todo.extract"}


def test_scan_skips_seeded_and_dependency_dirs(tmp_path: Path) -> None:
    _make_plugin(tmp_path / "plugins" / "real", "p_real", ["p_real"], [])
    # runtime/ 下的播种副本声明了未注册的异名步骤——必须不进扫
    _make_plugin(tmp_path / "plugins" / "real" / "runtime" / "seed", "p_seed", ["ghost.step"], [])
    assert mod.scan_plugins(tmp_path / "plugins") == []


def _make_plugin(dir_path: Path, plugin_id: str, declared: list[str], registered: list[str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    manifest = {"id": plugin_id, "capabilities": {"steps": [{"name": n} for n in declared]}}
    (dir_path / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    regs = "\n".join(f'@plugin.step("{n}")\ndef _s_{i}(): ...' for i, n in enumerate(registered))
    (dir_path / "server.py").write_text(f"plugin = object()\n{regs}\n", encoding="utf-8")


def test_scan_flags_misalignment_in_tmp_tree(tmp_path: Path) -> None:
    # 声明未注册 + 注册未声明两插件各一
    _make_plugin(tmp_path / "plugins" / "a", "p_a", ["a.ghost"], [])
    _make_plugin(tmp_path / "plugins" / "b", "p_b", [], {"b.orphan"})
    problems = mod.scan_plugins(tmp_path / "plugins")
    assert len(problems) == 2
    assert any("p_a" in p and "a.ghost" in p and "声明未注册" in p for p in problems)
    assert any("p_b" in p and "b.orphan" in p and "注册未声明" in p for p in problems)


def test_real_repo_state_green() -> None:
    """负控制：现役仓状态全绿（todo_track 声明=注册、builtin_tools 自名豁免）。"""
    problems = mod.scan_plugins(REPO_ROOT / "plugins")
    assert problems == [], f"真实仓出现声明↔注册错位: {problems}"


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main([str(SCRIPTS), str(tmp_path / "nonexistent")]) == 1
    _make_plugin(tmp_path / "plugins" / "ok", "p_ok", ["p_ok"], [])
    assert mod.main([str(SCRIPTS), str(tmp_path / "plugins")]) == 0
    out = capsys.readouterr().out
    assert "全绿" in out
