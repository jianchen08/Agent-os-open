# @feature: FP-GATE None | @ci: python-coverage
"""PILOT-02 签发验收测试：共享执行报告模板补签发分离 2 行。

@feature PILOT-02 签发分离试点·执行报告模板承载签发分离检查 | @vision V1 可进化

需求来源：docs/working/AI辅助编程体系落地台账_20261024.md R35 轮次记录
（「共享执行报告模板 config/templates/execution_report_template.md 的
『自审结论』表未增签发分离 2 行——列为后续同步项」）。

权威定义（前置契约）：skills/code-self-review/SKILL.md
「检查项（签发分离 2 项）」表——应试优化检测 / 验收测试只读核查；
其步骤 4-5 规定：任务书携带签发验收测试时跑这 2 项，结果填写
执行报告模板的「自审结论」章节——故模板承载位 =「自审结论」节。

起草方：签发分离试点起草轮（2026-10-28）。
状态：**已冻结 v1.0**（2026-10-28 签发轮：复检通过后由签发轮落位于本路径，断言与逻辑零改动）。
签发记录（复检方/复检结论/签发方/C1-C3 裁决/F3·F4' 处置留档）：
docs/working/签发分离试点_20261028/试点方案.md §九。

可渲染约定：测试名含需求 ID + 场景；四段式；失败报告人类化；
parametrize 两检查项（≥2 组有区分度输入）+ 同源性质断言 + 上游守卫断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --- 输入数据（前置契约锚点，结构化声明） ---

# 仓根锚定：从本文件位置向上探测 pyproject.toml——对草案位（docs/working/...）
# 与落位形态（tests/signed/）均鲁棒，不硬编码层级（复检 F1 修正，方案 b）。


def _locate_repo_root() -> Path:
    """向上逐级探测含 pyproject.toml 的目录作为仓根，找不到即显式失败。"""
    for d in Path(__file__).resolve().parents:
        if (d / "pyproject.toml").is_file():
            return d
    raise RuntimeError(
        "未找到 pyproject.toml——本件应位于仓内（草案位或 tests/signed/ 落位）；"
        "移出仓外属落位契约破坏，回签发复核。"
    )


_REPO_ROOT = _locate_repo_root()
_TEMPLATE = _REPO_ROOT / "config" / "templates" / "execution_report_template.md"
_SKILL = _REPO_ROOT / "skills" / "code-self-review" / "SKILL.md"

# 签发分离 2 项：名称须与 skills/code-self-review/SKILL.md「检查项（签发分离 2 项）」表逐字同源
_SIGNED_REVIEW_ITEMS: tuple[tuple[str, str], ...] = (
    ("应试优化检测", "实现是否只在被断言行为上正确"),
    ("验收测试只读核查", "是否动过签发验收测试"),
)

_SKILL_SECTION_ANCHOR = "检查项（签发分离 2 项）"


def _describe(scenario: str, requirement_id: str, subject: str,
              expected: str, actual: str) -> str:
    """失败报告人类化：场景/需求ID/对象/期望/实际/定位。"""
    return (
        f"[{requirement_id}] 场景: {scenario}\n"
        f"  对象: {subject}\n"
        f"  期望: {expected}\n"
        f"  实际: {actual}"
    )


def _template_self_review_section() -> str:
    """提取模板「自审结论」节文本（## 自审结论 至下一个 ## 标题）。"""
    text = _TEMPLATE.read_text(encoding="utf-8")
    start = text.find("## 自审结论")
    assert start != -1, "模板缺少「自审结论」节——结构变更，回签发复核"
    rest = text[start + len("## 自审结论"):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


class TestPILOT02签发分离两项承载:
    """模板「自审结论」节须承载签发分离 2 项检查行（AC-1）。

    两行是否与 code-self-review 技能逐字同源由同源断言组把关；
    本组断言行在场且为表格行形态（| 开头），防止只在正文散文里
    提及而无填写位。
    """

    @pytest.mark.parametrize(("item_name", "_semantic"), _SIGNED_REVIEW_ITEMS)
    def test_PILOT_02_检查项行在自审结论节内(self, item_name: str,
                                               _semantic: str) -> None:
        # --- 输入数据 ---
        scenario = f"自审结论节承载_{item_name}"

        # --- 执行 ---
        section = _template_self_review_section()
        hit_lines = [ln.strip() for ln in section.splitlines()
                     if item_name in ln]

        # --- 输出断言 ---
        # 契约粒度：行存在 + 表格行形态（自审结论节的检查项以表格行承载）
        assert hit_lines, _describe(
            scenario=scenario,
            requirement_id="PILOT-02",
            subject=f"{_TEMPLATE.name}「自审结论」节",
            expected=f"存在含「{item_name}」的行",
            actual="该节内未找到任何含该检查项名的行",
        )
        assert any(ln.startswith("|") for ln in hit_lines), _describe(
            scenario=scenario,
            requirement_id="PILOT-02",
            subject=f"{_TEMPLATE.name}「自审结论」节",
            expected=f"「{item_name}」以表格行（| 开头）承载填写位",
            actual=f"命中行均非表格行: {hit_lines}",
        )

        # --- 副作用断言 ---
        # 无副作用：本测试只读模板文件，不写任何状态。


class TestPILOT02同源与守卫:
    """同源性质断言 + 上游守卫（AC-2）。

    - 同源：模板两项名称与 code-self-review SKILL.md 逐字一致，
      防模板与技能双真值漂移（仓内同文纪律同款口径）。
    - 守卫：SKILL.md 中两项定义在场——若上游改名，本守卫先红，
      指引回签发复核同步本件，而不是让承载断言静默失效。
    """

    def test_PILOT_02_两项名称与code_self_review技能同源_防双真值漂移(self) -> None:
        # --- 输入数据 ---
        scenario = "模板与技能的两项名称逐字同源"

        # --- 执行 ---
        section = _template_self_review_section()
        skill_text = _SKILL.read_text(encoding="utf-8")

        # --- 输出断言 ---
        for item_name, semantic in _SIGNED_REVIEW_ITEMS:
            assert item_name in section, _describe(
                scenario=scenario,
                requirement_id="PILOT-02",
                subject=item_name,
                expected="模板自审结论节含与技能同源的检查项名",
                actual="模板中未找到该名称",
            )
            # 守卫：上游技能定义在场（先于模板断言失效）
            assert item_name in skill_text, _describe(
                scenario=scenario,
                requirement_id="PILOT-02",
                subject=f"{_SKILL.name}:{_SKILL_SECTION_ANCHOR}",
                expected="上游技能含该检查项定义（守卫）",
                actual="上游技能未找到该名称——上游已改名，本件须回签发复核同步",
            )
            # 语义守卫：技能侧判定语义片段在场（防上游释义漂移后模板失配）
            assert semantic in skill_text, _describe(
                scenario=scenario,
                requirement_id="PILOT-02",
                subject=f"{item_name} 判定语义",
                expected=f"技能侧含语义片段「{semantic}」",
                actual="技能侧语义片段缺失——上游释义已变，本件须回签发复核",
            )

        # --- 副作用断言 ---
        # 无副作用：只读两文件，不写任何状态。
