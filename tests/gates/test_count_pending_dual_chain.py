# @feature: FP-GATE P-14 待测双链计数 | @ci: python-coverage
"""count_pending_dual_chain 确定性回归锁：口径分类 / 聚合守恒 / 可复跑性。

意图：该脚本是提案 P-14（双链示范补写）的基线量化工具，口径必须与 graph
管线机械原语严格一致（同 iter_wikilinks 解析、同 rp_prompt_material 排除、
同 frontmatter 剥离）。本测试锁三件事——

1. 分类口径有区分度：真关系双链 / 裸引用双链 / 占位写法示例三分类，且
   代码块与行内代码内的形态描述文本不计（终稿核查 P-5 指认的口径污染面）；
2. 聚合守恒性质：分类计数之和恒等于总数、目录分布之和恒等于全域总数；
3. 确定性：同树两次运行输出逐字节一致（P-14 基线数可复跑对账的前提）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import count_pending_dual_chain as cpdc  # noqa: E402


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def mixed_root(tmp_path: Path) -> Path:
    """场景 A：五类形态各一——分类口径区分度输入。"""
    _write(
        tmp_path,
        "docs/working/有链文档.md",
        "---\ndoc_id: 20261104-mixed-a\ntype: report\nstatus: active\n---\n"
        "# 正文\n"
        "真关系双链 [[20261022-plan-x|依据|某方案某节]]。\n"
        "裸引用双链 [[docs/other.md]]。\n"
        "占位写法示例 [[目标]] 与 [[路径]]。\n"
        "行内代码不算：`[[目标|关系|说明]]`。\n"
        "```md\n围栏代码块不算：[[目标|归属]]\n```\n",
    )
    _write(tmp_path, "docs/working/无链文档.md", "# 无双链\n正文零链接。\n")
    _write(
        tmp_path,
        "docs/working/rp_prompt_material/mixed/素材.md",
        "[[thread]] 是素材模板语法非双链。\n",
    )
    return tmp_path


@pytest.fixture()
def focus_root(tmp_path: Path) -> Path:
    """场景 B：P-14 落位面（重要设计 + 两件真源 + 落地 ADR）计数输入。"""
    _write(
        tmp_path,
        "docs/working/重要设计/方案甲.md",
        "设计正文带归属示例 [[proj|归属|项目根]] 与依据 [[20261022-plan-y|依据|真值来源]]。\n",
    )
    _write(tmp_path, "docs/working/重要设计/方案乙.md", "# 零双链设计\n")
    _write(
        tmp_path,
        "docs/working/任务面板用户需求落地_台账.md",
        "台账续作 [[20261030-lead-b|续作|前序台账]]。\n",
    )
    _write(
        tmp_path,
        "docs/decisions/2026-10-27-ai-coding-system-agentos-landing.md",
        "落地 ADR [[20261022-ai-coding-system-agentos-landing|依据|体系方案]]。\n",
    )
    return tmp_path


class Test口径分类区分度:
    """三分类互斥且穷尽；代码块/行内代码的形态描述文本不入计数。"""

    def test_场景A_分类计数逐项命中(self, mixed_root: Path):
        result = cpdc.count_root(mixed_root)
        per_file = {f["path"]: f for f in result["files"]}
        doc = per_file["docs/working/有链文档.md"]
        # 真关系双链 1（行内代码与围栏内的 [[目标|关系|说明]] 不算）
        assert doc["relation_links"] == 1
        assert doc["relation_targets"] == [
            {
                "target": "20261022-plan-x",
                "relation": "依据",
                "note": "某方案某节",
                "reference_like": True,
            }
        ]
        # 裸引用双链 1（无关系段但目标形似真实引用）
        assert doc["bare_reference_links"] == 1
        # 占位写法示例 2（[[目标]] / [[路径]]）
        assert doc["placeholder_links"] == 2
        assert doc["links_total"] == 4
        # 素材池文件被排除，不进计数面
        assert "docs/working/rp_prompt_material/mixed/素材.md" not in per_file

    def test_场景A_全域汇总与零链文件(self, mixed_root: Path):
        result = cpdc.count_root(mixed_root)
        assert result["totals"]["files_scanned"] == 2
        assert result["totals"]["files_with_links"] == 1
        assert result["totals"]["links_total"] == 4
        assert result["totals"]["relation_links"] == 1
        assert result["totals"]["bare_reference_links"] == 1
        assert result["totals"]["placeholder_links"] == 2

    def test_场景B_落位面逐件计数(self, focus_root: Path):
        result = cpdc.count_root(focus_root)
        focus = {f["path"]: f for f in result["p14_focus"]}
        # 重要设计目录：2 件扫描、1 件含链、2 条关系双链
        design = focus["docs/working/重要设计/"]
        assert design["files_scanned"] == 2
        assert design["files_with_links"] == 1
        assert design["relation_links"] == 2
        # 两件真源之一：任务面板台账带 1 条续作链
        assert focus["docs/working/任务面板用户需求落地_台账.md"]["relation_links"] == 1
        # 落地 ADR 带 1 条依据链
        assert (
            focus["docs/decisions/2026-10-27-ai-coding-system-agentos-landing.md"][
                "relation_links"
            ]
            == 1
        )
        # v3 总结零双链（P-14 指认的真源缺口形态）
        assert (
            focus["docs/working/重要设计/AI辅助编程体系完整总结_修订版v3.md"]["missing"]
            is True
        )


class Test聚合守恒性质:
    """分类之和恒等总数；目录分布之和恒等全域——对任意构造树成立。"""

    @pytest.mark.parametrize(
        "root_fixture", ["mixed_root", "focus_root"], ids=["场景A", "场景B"]
    )
    def test_分类划分守恒(self, root_fixture: str, request: pytest.FixtureRequest):
        result = cpdc.count_root(request.getfixturevalue(root_fixture))
        totals = result["totals"]
        assert (
            totals["relation_links"] + totals["bare_reference_links"] + totals["placeholder_links"]
            == totals["links_total"]
        )
        for entry in result["files"]:
            assert (
                entry["relation_links"] + entry["bare_reference_links"] + entry["placeholder_links"]
                == entry["links_total"]
            )

    @pytest.mark.parametrize(
        "root_fixture", ["mixed_root", "focus_root"], ids=["场景A", "场景B"]
    )
    def test_目录分布加总守恒(self, root_fixture: str, request: pytest.FixtureRequest):
        result = cpdc.count_root(request.getfixturevalue(root_fixture))
        by_dir = result["by_directory"]
        assert sum(d["links_total"] for d in by_dir) == result["totals"]["links_total"]
        assert sum(d["files_scanned"] for d in by_dir) == result["totals"]["files_scanned"]
        assert sum(d["files_with_links"] for d in by_dir) == result["totals"]["files_with_links"]


class Test确定性:
    """同树两次运行逐字节一致；输出不含时间戳等环境变量。"""

    @pytest.mark.parametrize(
        "root_fixture", ["mixed_root", "focus_root"], ids=["场景A", "场景B"]
    )
    def test_两次运行同结果(self, root_fixture: str, request: pytest.FixtureRequest):
        root = request.getfixturevalue(root_fixture)
        first = cpdc.render_json(cpdc.count_root(root))
        second = cpdc.render_json(cpdc.count_root(root))
        assert first == second

    def test_输出无易变字段(self, mixed_root: Path):
        payload = json.loads(cpdc.render_json(cpdc.count_root(mixed_root)))
        forbidden = {"generated_at", "timestamp", "commit", "head", "now"}
        keys = set(payload) | set(payload["totals"])
        assert not keys & forbidden
