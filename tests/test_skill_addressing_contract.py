# @feature: FP-0.2.CFG 技能寻址契约 | @ci: python-test
"""种子配置技能寻址契约测试（2026-09-29 R300 装机实证防回潮）。

契约：模型侧技能寻址一律落在**会话工作空间相对路径**（skills/<名>/SKILL.md，
工作空间启动时由 workspace_lifecycle 同步实体；容器 bash 以工作空间挂载点为
根，同一相对路径可达）。

R300 事故链：agentos.yaml「你的家 = {{user_root}}」教会给模型家相对寻址泛化，
装机形态下技能实体不在工作空间 → 模型到 {{user_root}} 下拼
skills/.../SKILL.md 绝对路径 → File not found → 多轮找文件烧上下文。
防回潮两断言：
1. 主 agent 种子提示词带「技能寻址」锚点行——把 skills/ 相对路径的解析基准
   钉在 {{session_workspace}} 并明令禁止到用户根拼绝对路径；
2. 全 config/ 无 {{user_root}} 前缀或盘符绝对路径形态的技能寻址（悬空实体
   一致性由 config-static 门禁第三查持有，本测试只管寻址形态）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / "config" / "agents"
MAIN_YAML = ROOT / "config" / "agents" / "main" / "agentos.yaml"

#: 违规寻址形态：用户根前缀 / 盘符绝对路径 + 技能路径。
FORBIDDEN_PATTERNS = [
    (re.compile(r"\{\{user_root\}\}\s*[\\/]+skills\b"), "{{user_root}} 前缀技能寻址"),
    (re.compile(r"[A-Za-z]:[\\/]+skills\b"), "盘符绝对路径技能寻址"),
]


def test_main_seed_prompt_has_skill_addressing_anchor() -> None:
    data = yaml.safe_load(MAIN_YAML.read_text(encoding="utf-8"))
    prompt = data["system_prompt"]
    anchor = [line for line in prompt.splitlines() if "技能寻址" in line]
    assert anchor, "主 agent 种子提示词缺「技能寻址」锚点行（R300 寻址漂移防回潮）"
    joined = "\n".join(anchor)
    # 解析基准 = 会话工作空间（相对路径），且明令禁止用户根寻址
    assert "{{session_workspace}}" in joined
    assert "相对路径" in joined
    assert "禁止" in joined
    assert "{{user_root}}" in joined


def test_workspace_anchor_line_precedes_skill_refs() -> None:
    """锚点行（相对路径基准声明）必须先于第一条 skills/ 引用出现——模型先知
    基准再遇引用，不得倒挂（{{path:}} 规则注入行不计，其内文不指向技能）。"""
    prompt = yaml.safe_load(MAIN_YAML.read_text(encoding="utf-8"))["system_prompt"]
    lines = prompt.splitlines()
    anchor_idx = next(i for i, line in enumerate(lines) if "技能寻址" in line)
    first_ref_idx = next(
        i
        for i, line in enumerate(lines)
        if "skills/" in line and not line.lstrip().startswith("{{path:")
    )
    assert first_ref_idx >= anchor_idx


@pytest.mark.parametrize(("pattern", "label"), FORBIDDEN_PATTERNS)
def test_no_user_root_or_absolute_skill_addressing(pattern: re.Pattern[str], label: str) -> None:
    offenders: list[str] = []
    for yf in sorted(AGENTS_DIR.rglob("*.yaml")):
        for i, line in enumerate(yf.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{yf.relative_to(ROOT)}:{i} [{label}]")
    assert not offenders, "违规技能寻址形态:\n" + "\n".join(offenders)
