# @feature: FP-0.2.二 写作模式执行链 | @ci: python-coverage
"""BUG-33 父回报面物料契约：主 agent 回报产物文件路径前必须存在性校验。

BUG-33 现场：子任务在零验收下完成，父 agent 把执行者自我报告中的文件路径
未核实直接回报给用户（路径不存在，用户被误导）。评估面已改为如实标注
（无指标完成带「未经评估」前缀），本契约锁住物料侧最后一道口——主 agent
的 hard_constraints 必须携带回报前验证指令。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MAIN_YAML = ROOT / "config" / "agents" / "main" / "agentos.yaml"


def test_main_agent_hard_constraint_requires_path_verification_before_report() -> None:
    """hard_constraints 必须含「回报前验证文件存在」约束（缺失 = 父回报假路径
    物料回退征兆）。"""
    with open(MAIN_YAML, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    constraints = cfg.get("hard_constraints")
    assert isinstance(constraints, list) and constraints, "主 agent hard_constraints 缺失"
    verification_rules = [
        c for c in constraints
        if isinstance(c, str) and "回报" in c and "存在" in c
    ]
    assert verification_rules, (
        "hard_constraints 缺少「向用户回报文件路径前必须验证文件存在」约束"
    )
    joined = "\n".join(verification_rules)
    assert "file_read" in joined, "回报前验证约束必须指明用 file_read 校验"
