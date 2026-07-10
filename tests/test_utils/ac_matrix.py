"""AC 状态矩阵生成器。

对 15 条验收标准逐条追踪状态（通过/部分通过/未通过/未测试），
生成 HTML 片段嵌入 combined_report.html。

来源：docs/构建统一CICD测试体系_solution.md §3.4.2
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ACStatus(Enum):
    """AC 状态枚举。"""

    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_TESTED = "not_tested"


# ── 15 条 AC 定义 ──────────────────────────────────────

AC_DEFINITIONS: list[dict[str, str]] = [
    {"id": "AC-1", "title": "RBAC 权限检查恢复", "category": "安全"},
    {"id": "AC-2", "title": "5 个 Bug 全部修复", "category": "Bug修复"},
    {"id": "AC-3", "title": "冗余代码已清理，代码量减少", "category": "代码质量"},
    {"id": "AC-4", "title": "减代码/拆分未改变逻辑", "category": "代码质量"},
    {"id": "AC-5", "title": "过程文档已清理", "category": "文档"},
    {"id": "AC-6", "title": ".project/ 核心文档完善", "category": "文档"},
    {"id": "AC-7", "title": "API Key 迁移到环境变量", "category": "安全"},
    {"id": "AC-8", "title": "TokenManager Redis 迁移", "category": "安全"},
    {"id": "AC-9", "title": "所有后端配置映射到前端", "category": "功能"},
    {"id": "AC-10", "title": "配置修改实时写入文件", "category": "功能"},
    {"id": "AC-11", "title": "监控/模型名/上下文数据准确", "category": "功能"},
    {"id": "AC-12", "title": "接口规范落地", "category": "功能"},
    {"id": "AC-13", "title": "全量测试通过", "category": "质量"},
    {"id": "AC-14", "title": "热加载机制", "category": "功能"},
    {"id": "AC-15", "title": "通道适配器标准接口", "category": "功能"},
]

_STATUS_CONFIG: dict[ACStatus, dict[str, str]] = {
    ACStatus.PASSED: {"label": "✅ 通过", "color": "#4caf50", "bg": "#e8f5e9"},
    ACStatus.PARTIAL: {"label": "🟡 部分", "color": "#ff9800", "bg": "#fff3e0"},
    ACStatus.FAILED: {"label": "❌ 未通过", "color": "#f44336", "bg": "#ffebee"},
    ACStatus.NOT_TESTED: {"label": "⬜ 未测试", "color": "#9e9e9e", "bg": "#f5f5f5"},
}


@dataclass
class ACEntry:
    """单条 AC 的追踪记录。"""

    ac_id: str
    title: str
    category: str
    status: ACStatus = ACStatus.NOT_TESTED
    evidence: str = ""
    test_names: list[str] = field(default_factory=list)
    detail: str = ""


class ACMatrixTracker:
    """AC 状态矩阵追踪器。

    用法::

        tracker = ACMatrixTracker()
        tracker.update("AC-1", ACStatus.PASSED, evidence="RBAC 单测全部通过")
        tracker.update("AC-9", ACStatus.PARTIAL, detail="缺少 2 个配置页")
        html = tracker.to_html()

    也可从 pytest 结果自动推导状态（确定性，不依赖手动盖章）::

        mapping = collect_ac_test_mapping("tests/")
        tracker = ACMatrixTracker.from_test_results(
            ac_definitions=AC_DEFINITIONS,
            ac_test_mapping=mapping,
            test_results=pytest_report,
        )
    """

    def __init__(self) -> None:
        self._entries: dict[str, ACEntry] = {
            ac["id"]: ACEntry(ac_id=ac["id"], title=ac["title"], category=ac["category"])
            for ac in AC_DEFINITIONS
        }

    @classmethod
    def from_definitions(cls, ac_definitions: list[dict[str, str]]) -> ACMatrixTracker:
        """基于任意 AC 定义列表构造（不限于硬编码的 15 条）。

        供方案阶段从 frontmatter 读取的 AC 列表使用。
        """
        tracker = cls.__new__(cls)
        tracker._entries = {
            ac["id"]: ACEntry(
                ac_id=ac["id"],
                title=ac.get("title", ac.get("statement", ac["id"])),
                category=ac.get("category", ""),
            )
            for ac in ac_definitions
        }
        return tracker

    @classmethod
    def from_test_results(
        cls,
        ac_definitions: list[dict[str, str]],
        ac_test_mapping: dict[str, list[str]],
        test_results: dict[str, str],
    ) -> ACMatrixTracker:
        """从 AC 定义 + 测试映射 + pytest 结果，一次性自动推导全部状态。

        确定性推导，不调用任何 LLM、不手动盖章。无映射的 AC 保持 NOT_TESTED。
        """
        tracker = cls.from_definitions(ac_definitions)
        for ac in ac_definitions:
            ac_id = ac["id"]
            tests = ac_test_mapping.get(ac_id, [])
            status = derive_status(tests, test_results)
            tracker._entries[ac_id].status = status
            tracker._entries[ac_id].test_names = list(tests)
            tracker._entries[ac_id].evidence = (
                f"自动推导：{len(tests)} 个关联测试，结果={status.value}"
                if tests
                else ""
            )
        return tracker

    def update(
        self,
        ac_id: str,
        status: ACStatus,
        evidence: str = "",
        test_names: list[str] | None = None,
        detail: str = "",
    ) -> None:
        """更新指定 AC 的状态。"""
        if ac_id not in self._entries:
            return
        entry = self._entries[ac_id]
        entry.status = status
        if evidence:
            entry.evidence = evidence
        if test_names:
            entry.test_names = test_names
        if detail:
            entry.detail = detail

    def add_test_to_ac(self, ac_id: str, test_name: str) -> None:
        """将测试用例关联到指定 AC。"""
        if ac_id in self._entries:
            self._entries[ac_id].test_names.append(test_name)

    @property
    def entries(self) -> list[ACEntry]:
        """按定义顺序返回所有 AC 条目。"""
        return [self._entries[ac["id"]] for ac in AC_DEFINITIONS if ac["id"] in self._entries]

    def summary(self) -> dict[str, int]:
        """返回状态统计。"""
        counts = {s.value: 0 for s in ACStatus}
        for entry in self._entries.values():
            counts[entry.status.value] += 1
        return counts

    def to_dict(self) -> list[dict[str, Any]]:
        """序列化为字典列表。"""
        return [
            {
                "id": e.ac_id,
                "title": e.title,
                "category": e.category,
                "status": e.status.value,
                "evidence": e.evidence,
                "test_names": e.test_names,
                "detail": e.detail,
            }
            for e in self.entries
        ]

    def to_html(self) -> str:
        """生成 AC 状态矩阵的 HTML 片段。"""
        rows = ""
        for entry in self.entries:
            cfg = _STATUS_CONFIG[entry.status]
            tests_html = ""
            if entry.test_names:
                escaped_names = [n.replace("<", "&lt;") for n in entry.test_names[:5]]
                tests_html = '<div class="ac-tests">' + ", ".join(escaped_names)
                if len(entry.test_names) > 5:
                    tests_html += f" (+{len(entry.test_names) - 5})"
                tests_html += "</div>"

            detail_html = ""
            if entry.detail:
                detail_html = f'<div class="ac-detail">{entry.detail}</div>'

            evidence_html = ""
            if entry.evidence:
                evidence_html = f'<div class="ac-evidence">{entry.evidence}</div>'

            rows += f"""
            <tr>
                <td><span class="ac-id">{entry.ac_id}</span></td>
                <td>{entry.title}</td>
                <td><span class="ac-badge" style="background:{cfg['bg']};color:{cfg['color']}">{cfg['label']}</span></td>
                <td>{entry.category}</td>
                <td>{evidence_html}{detail_html}{tests_html}</td>
            </tr>"""

        counts = self.summary()
        total = len(self._entries)
        passed = counts[ACStatus.PASSED.value]
        pct = passed / total * 100 if total > 0 else 0

        bar_color = "#4caf50" if pct >= 80 else "#ff9800" if pct >= 50 else "#f44336"

        return f"""
        <div class="ac-matrix">
            <div class="section-header">
                <h2>✅ AC 状态矩阵</h2>
                <div class="ac-summary">
                    <span class="ac-stat" style="background:#e8f5e9">✅ 通过 {passed}</span>
                    <span class="ac-stat" style="background:#fff3e0">🟡 部分 {counts[ACStatus.PARTIAL.value]}</span>
                    <span class="ac-stat" style="background:#ffebee">❌ 未通过 {counts[ACStatus.FAILED.value]}</span>
                    <span class="ac-stat" style="background:#f5f5f5">⬜ 未测试 {counts[ACStatus.NOT_TESTED.value]}</span>
                </div>
            </div>
            <div class="bar"><div class="bar-fill" style="width:{pct:.0f}%;background:{bar_color}"></div></div>
            <table class="ac-table">
                <tr><th>编号</th><th>验收项</th><th>状态</th><th>分类</th><th>详情</th></tr>
                {rows}
            </table>
        </div>"""


# ── AC↔测试映射与状态自动推导（确定性，不依赖手动盖章） ──────────

# 匹配测试名 / 装饰器 / mark 中的 AC 标记：AC-1 / AC_1 / AC001 / AC:1 / AC 1
_AC_ID_RE = re.compile(r"AC[-_ :]?(\d+)", re.IGNORECASE)


def normalize_ac_id(raw: str) -> str:
    """把任意写法归一化为 'AC-{n}' 形式。

    >>> normalize_ac_id("AC_01")
    'AC-1'
    >>> normalize_ac_id("ac-1")
    'AC-1'
    """
    m = _AC_ID_RE.search(raw)
    if not m:
        return raw
    return f"AC-{int(m.group(1))}"


def collect_ac_test_mapping(tests_dir: str | Path) -> dict[str, list[str]]:
    """扫描测试目录，建立 AC-ID → [测试名] 映射。

    识别来源（任一命中即关联）：
    - 测试函数名 / 类名含 AC 标记（如 ``test_AC001_create_coupon``）
    - ``@pytest.mark.ac("AC-1")`` / ``@pytest.mark.AC_1``
    - docstring / 行内注释含 ``AC-1`` / ``AC:1``

    只读、不执行测试，因此是纯确定性的 grep。
    """
    root = Path(tests_dir)
    if not root.exists():
        return {}

    mapping: dict[str, list[str]] = {}
    for py_file in root.rglob("*.py"):
        if py_file.name.startswith("__"):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # 逐行扫描：装饰器行的 AC 标记先累积，遇到 def test_/class Test_ 时
        # 一次性归给该测试。这样 @decorator + def 总是关联到同一测试。
        pending_ac_ids: set[str] = set()
        for line in text.splitlines():
            ids = {normalize_ac_id(m.group(0)) for m in _AC_ID_RE.finditer(line)}
            # 装饰器行：累积 AC 标记，等下一个 def/class
            if line.lstrip().startswith("@"):
                pending_ac_ids |= ids
                continue
            name_match = re.search(r"def\s+(test_\w+)", line) or re.search(
                r"class\s+(Test\w+)", line
            )
            if name_match:
                test_name = f"{py_file.name}::{name_match.group(1)}"
                all_ids = ids | pending_ac_ids
                for ac_id in all_ids:
                    mapping.setdefault(ac_id, []).append(test_name)
                pending_ac_ids.clear()
            elif ids:
                # 非 def/装饰器行的 AC 标记（docstring/注释）也累积，
                # 归给下一个出现的测试
                pending_ac_ids |= ids
    return mapping


def derive_status(
    test_names: list[str],
    test_results: dict[str, str],
) -> ACStatus:
    """根据测试结果推导单个 AC 的状态（确定性）。

    - 关联测试全绿 → PASSED
    - 至少一个红（failed/error） → FAILED
    - 无关联测试 → NOT_TESTED

    PARTIAL 不在此自动推导中产生——它表示「部分通过」的人工判断，
    纯测试结果无法确定性给出，留给人工 update。
    """
    if not test_names:
        return ACStatus.NOT_TESTED
    statuses = [test_results.get(t, "not_run") for t in test_names]
    if all(s == "passed" for s in statuses):
        return ACStatus.PASSED
    if any(s in ("failed", "error") for s in statuses):
        return ACStatus.FAILED
    return ACStatus.NOT_TESTED


__all__ = [
    "ACMatrixTracker",
    "ACStatus",
    "AC_DEFINITIONS",
    "collect_ac_test_mapping",
    "derive_status",
    "normalize_ac_id",
]
