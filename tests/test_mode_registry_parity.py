# @feature: FP-0.2.二 内部模块manifest(模式注册 parity 闸) | @vision: V3 可嵌入 | @ci: python-coverage
"""check_mode_registry_parity 机械闸测试——三向对账的纯函数与端到端面。

覆盖：schema 校验正负例（与 agent_manager 同源白名单）/ 组装器声明-实物双向
对账 / 端到端 main 在真实出厂包上通过。TASK_MODES 字面量提取已随批 G⑦ 退役
（前端冻结键集删除，选择器选项 = registry 运行时派生，防漂移职责由 vitest
合成 registry 两态断言承接——见 frontend modeOptions.test.ts）。
mock 零使用：文件面一律真实临时目录 + 真实仓内出厂包。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_mode_registry_parity.py"
_spec = importlib.util.spec_from_file_location("check_mode_registry_parity", _SCRIPT)
assert _spec and _spec.loader
parity = importlib.util.module_from_spec(_spec)
sys.modules["check_mode_registry_parity"] = parity
_spec.loader.exec_module(parity)


def _write_pkg(modes_dir: Path, name: str, decl: str | dict, with_material: bool = False) -> Path:
    pkg = modes_dir / name
    pkg.mkdir(parents=True)
    text = decl if isinstance(decl, str) else yaml.safe_dump(decl, allow_unicode=True)
    (pkg / "mode.yaml").write_text(text, encoding="utf-8")
    if with_material:
        (pkg / "material.py").write_text(
            "def build_injection(state, pkg_dir):\n    return ''\n", encoding="utf-8"
        )
    return pkg


GOOD_DECL = "mode: x\nname: X\npipelines:\n  - name: x_pipe\n    context: conversation\n"

# ── schema 校验 ──────────────────────────────────────────────────────────


def test_validate_accepts_good_and_rejects_variants() -> None:
    assert parity.validate_declaration(yaml.safe_load(GOOD_DECL)) == ""
    assert "未知字段" in parity.validate_declaration({"mode": "x", "name": "X", "foo": 1})
    assert "枚举非法" in parity.validate_declaration(
        {"mode": "x", "name": "X", "tool_card": "fold"}
    )
    assert "形态非法" in parity.validate_declaration(
        {"mode": "x", "name": "X",
         "pipelines": [{"name": "mode_x/main", "context": "conversation"}]}
    )
    # 退役面同源摘除（批 F）：单值 pipeline 与 pipeline_profile 均白名单外
    assert "未知字段" in parity.validate_declaration({"mode": "x", "name": "X", "pipeline": "x"})
    assert "未知字段" in parity.validate_declaration(
        {"mode": "x", "name": "X", "pipeline_profile": {"pipeline": "x"}}
    )
    # pipelines 列表结构（与 agent_manager 同源拒绝集）
    assert "须为非空列表" in parity.validate_declaration(
        {"mode": "x", "name": "X", "pipelines": []}
    )
    assert "context 枚举非法" in parity.validate_declaration(
        {"mode": "x", "name": "X", "pipelines": [{"name": "p"}]}
    )
    assert "重复" in parity.validate_declaration(
        {"mode": "x", "name": "X",
         "pipelines": [{"name": "p", "context": "conversation"},
                       {"name": "p", "context": "task"}]}
    )
    assert "包内自持待拍板" in parity.validate_declaration(
        {"mode": "x", "name": "X",
         "pipelines": [{"name": "p", "context": "task", "source": "package"}]}
    )


# ── main 端到端（真实出厂包） ─────────────────────────────────────────────


def test_main_passes_on_real_factory_packages() -> None:
    repo = _SCRIPT.parent.parent
    code = parity.main.__wrapped__() if hasattr(parity.main, "__wrapped__") else None
    _ = code
    argv = ["--modes-dir", str(repo / "plugins/shared/modes")]
    original = sys.argv
    sys.argv = ["check_mode_registry_parity.py", *argv]
    try:
        assert parity.main() == 0
    finally:
        sys.argv = original


def test_main_fails_on_orphan_material(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_pkg(tmp_path / "modes", "mode_orphan",
               {"mode": "orphan", "name": "孤儿"}, with_material=True)
    original = sys.argv
    sys.argv = ["check_mode_registry_parity.py",
                "--modes-dir", str(tmp_path / "modes")]
    try:
        assert parity.main() == 1
    finally:
        sys.argv = original
    assert "孤儿组装器" in capsys.readouterr().out
