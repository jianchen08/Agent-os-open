# @feature: FP-0.2.CFG config 静态校验闸 | @ci: python-test
"""check_config_static.py 技能引用实体一致性单测（2026-09-29 R300 防回潮）。

行为契约（断输入→输出，不钉实现）：
- 权威源无实体的 ``skills/<名>/SKILL.md`` 引用 → 悬空违规（R300 事故链源头：
  提示词报路径不保证实体存在，模型按 {{user_root}} 拼绝对路径 File not found）；
- 权威源并集（仓根 skills/ ∪ 出厂模式包 skills/）内的引用 → 放行；
- 整行注释与通配形态（skills/x-*/SKILL.md）不入扫——文档举例不算引用；
- 负控制：真实仓状态全绿（现役 config 引用与权威源一致），合成悬空必须打红。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_config_static", SCRIPTS / "check_config_static.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_dangling_ref_flagged() -> None:
    known = {"skill-container-task-flow", "credit-delegation"}
    text = "必须先 file_read 加载 `skills/skill-container-task-flow/SKILL.md`\n"
    text += "再加载 `skills/ghost-skill/SKILL.md`\n"
    found = mod._skill_ref_violations(text, Path("agents/main/agentos.yaml"), known)
    assert len(found) == 1
    assert "ghost-skill" in found[0]
    assert "skill-container-task-flow" not in found[0]


@pytest.mark.parametrize(
    "line",
    [
        "1. 先 file_read 加载方法论 `skills/credit-delegation/SKILL.md`",
        "领域技能（skills/code-frontend/SKILL.md 或 skills/code-backend/SKILL.md）",
        "| novel | 小说 | skills/skill-solution-novel/SKILL.md |",
        "path: skills/skill-test-infra/SKILL.md",
    ],
)
def test_entity_backed_refs_pass(line: str) -> None:
    # 仓根技能与出厂模式包技能同判（权威源并集口径）
    known = {"credit-delegation", "code-frontend", "code-backend", "skill-solution-novel", "skill-test-infra"}
    assert mod._skill_ref_violations(line, Path("x.yaml"), known) == []


def test_comment_and_wildcard_not_scanned() -> None:
    known: set[str] = set()
    text = (
        "# path: \"skills/skill-code-impl/SKILL.md\"  # 模板示例注释\n"
        "以 skills/skill-solution-*/SKILL.md 为准（通配形态）\n"
        "真引用 skills/ghost/SKILL.md\n"
    )
    found = mod._skill_ref_violations(text, Path("t.yaml"), known)
    assert len(found) == 1
    assert "ghost" in found[0]


def test_known_skill_names_matches_repo_union() -> None:
    known = mod._known_skill_names()
    # 仓根技能（R300 事故主角）与出厂模式包技能都在权威源并集
    assert "skill-container-task-flow" in known
    assert "code-backend" in known
    assert "ghost-skill" not in known


def test_real_repo_state_green() -> None:
    """负控制（防恒绿）：检查器对真实仓必须可执行且绿——现役 config 技能
    引用与权威源一致；红路径已由上方合成悬空用例钉死。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_config_static.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
