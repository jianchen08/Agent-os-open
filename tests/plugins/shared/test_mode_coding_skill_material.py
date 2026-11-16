# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""编码模式包技能物料契约测试（方案 §2.3「三件既有强化」落位口径）。

背景：AI 辅助编程体系的三件既有技能强化（code-implement / code-self-review /
prompt-rule-engineering）按台账 R28/R29 落位 mode_coding 模式包技能物料
（`plugins/shared/mode_coding/skills/`），仓根 `skills/` 同名件保持同文——
模式包件在工作空间技能并集快照中覆盖仓根件，两处不同文即产生"提示词双真值"。

本测试断言的是**可观察的物料契约**（文件存在 + 规格锚点 + 两处同文 + 方案指针），
不触碰任何实现细节：

1. 模式包内三件技能物料齐备（prompt-rule-engineering 为本次新增落位）；
2. 三件的规格锚点在（方案 §2.3 对每件的增量要求）；
3. 仓根与模式包同名件逐字同文（双真值防线）；
4. 方案 §2.3 指针指向模式包落位（设计真值与物料对齐）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG_SKILLS = REPO_ROOT / "plugins" / "shared" / "mode_coding" / "skills"
ROOT_SKILLS = REPO_ROOT / "skills"
PLAN = REPO_ROOT / "docs" / "working" / "重要设计" / "AI辅助编程体系落地AgentOS方案_20261022.md"

# 三件强化件：模式包内文件名 → 规格锚点（锚点取方案 §2.3 原文用词）
STRENGTHENED = {
    "code-implement": ("辅助单测", "签发验收测试", "只读"),
    "code-self-review": ("应试优化检测", "验收测试只读核查"),
    "prompt-rule-engineering": ("退役评审", "最小充分集"),
}


def _pkg_bytes(name: str) -> bytes:
    return (PKG_SKILLS / name / "SKILL.md").read_bytes()


def _root_bytes(name: str) -> bytes:
    return (ROOT_SKILLS / name / "SKILL.md").read_bytes()


def _pkg_text(name: str) -> str:
    return (PKG_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(STRENGTHENED))
def test_mode_coding_package_carries_strengthened_skill(name: str) -> None:
    """模式包技能物料齐备：三件强化件在 mode_coding/skills/ 内有实体文件。"""
    skill_file = PKG_SKILLS / name / "SKILL.md"
    assert skill_file.is_file(), f"mode_coding 包内缺技能物料：{name}/SKILL.md"
    assert _pkg_text(name).strip(), f"{name} 物料内容为空"


@pytest.mark.parametrize("name", sorted(STRENGTHENED))
def test_strengthened_skill_carries_spec_anchors(name: str) -> None:
    """规格锚点在：每件强化件的方案 §2.3 增量要求落到物料正文（措辞可扩，锚点须在）。"""
    text = _pkg_text(name)
    for anchor in STRENGTHENED[name]:
        assert anchor in text, f"{name} 缺方案 §2.3 增量锚点：{anchor}"


@pytest.mark.parametrize("name", sorted(STRENGTHENED))
def test_package_and_repo_root_skill_are_identical(name: str) -> None:
    """两处同文（bytes 级）：模式包件在工作空间技能快照中覆盖仓根件，不同文即提示词双真值。

    P-17 收紧：断言用 read_bytes 而非 read_text——read_text 的 universal
    newlines 会把 CRLF/LF 归一，行尾漂移（E0-10：两副本 139 行行尾差）在
    文本级比较下假绿。byte-identical 才算同文。
    """
    root_file = ROOT_SKILLS / name / "SKILL.md"
    assert root_file.is_file(), f"仓根缺同名技能件：{name}/SKILL.md"
    assert _pkg_bytes(name) == _root_bytes(name), (
        f"{name} 仓根件与模式包件不同文（bytes 级，含行尾）——"
        "模式包件在技能并集快照中覆盖仓根件，双份不同文 = 提示词双真值；"
        "行尾分叉时归一两件为 LF 再提交"
    )


def test_plan_points_to_mode_coding_package_landing() -> None:
    """方案指针：§2.3 技能件清单指向 mode_coding 模式包落位（设计与物料对齐）。"""
    plan_text = PLAN.read_text(encoding="utf-8")
    assert "plugins/shared/mode_coding/skills/" in plan_text, (
        "方案 §2.3 未指向 mode_coding 模式包技能物料落位"
    )
