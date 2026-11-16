"""复盘报告文件面——报告本体落盘 + ws 相对坐标读写（F9 收敛单通道）。

报告本体不入 state、不入记忆库：写面把完整报告 dict 落成任务执行空间
（ws）内的 JSON 文件，state 只经 pipeline-state 能力存索引（review_id →
ws 相对坐标 + status 生命周期）；读面按索引坐标 + 锚行 ``task.ws_meta.path``
定根直读文件。

[来源: 用户裁定 2026-10-08——大对象/文档本体走文件面，state 只存索引字段；
文件类产物位置一律用 agent 实际执行空间（ws）标定]
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 报告在 ws 内的固定目录（ws 相对坐标前缀）。docs/working 沿 review_agent
# 交付物先例（review_agent.yaml 的 deliverables.output_path）。
_REPORTS_REL_DIR = "docs/working/reviews"

# review_id 进路径，白名单字符防穿越（触发面自生成 review_<hex>；读取面
# 接受任意 LLM 入参，越界字符直接拒绝）。
_REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,128}$")


def report_rel_path(review_id: str) -> str:
    """review_id → ws 相对坐标 ``docs/working/reviews/{review_id}.json``。

    Raises:
        ValueError: review_id 含路径越界字符（读取面对任意入参 fail-closed）。
    """
    if not _REVIEW_ID_RE.match(review_id):
        raise ValueError(f"非法 review_id（允许 [A-Za-z0-9_:-]）: {review_id!r}")
    return f"{_REPORTS_REL_DIR}/{review_id}.json"


def write_report(ws_root: str, review_id: str, report: dict[str, Any]) -> str:
    """报告本体落盘到 ``{ws_root}/{rel}``，返回 ws 相对坐标。

    Args:
        ws_root: 被复盘任务的执行空间根（锚行 task.ws_meta.path）。
        review_id: 复盘 ID（进路径，须过白名单）。
        report: 完整报告 dict（含 review_id/status/summary/lessons 等）。

    Returns:
        ws 相对坐标（索引字段存这个，不存绝对路径）。

    Raises:
        ValueError: review_id 非法。
        OSError: 落盘失败（调用方告警降级，索引不写 path）。
    """
    rel = report_rel_path(review_id)
    path = Path(ws_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rel


def read_report(ws_root: str, review_id: str) -> dict[str, Any] | None:
    """按 ws 根 + review_id 直读报告本体；缺失/损坏/形态不符返回 None。

    单通道读面：state 索引（坐标）→ 文件面直读，无任何第二数据源回退。
    """
    try:
        rel = report_rel_path(review_id)
    except ValueError:
        return None
    path = Path(ws_root) / rel
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_recall_fact(review_id: str, report: dict[str, Any]) -> str:
    """报告 → 面向召回的抽取事实文本（Hindsight 写入形态，非报告全文 JSON）。

    事实 = 定向头（review_id/task_id）+ 总结 + 限额内教训/建议串联；截断
    限额防长报告灌爆记忆抽取管线——全文在文件面，记忆只留可召回线索。
    """
    parts = [f"复盘 {review_id}（任务 {report.get('task_id') or ''}）"]
    summary = str(report.get("summary") or "").strip()
    if summary:
        parts.append(f"总结：{summary}")
    lessons = [str(x) for x in report.get("lessons") or [] if str(x).strip()]
    if lessons:
        parts.append("教训：" + "；".join(lessons[:5]))
    recs = [str(x) for x in report.get("recommendations") or [] if str(x).strip()]
    if recs:
        parts.append("建议：" + "；".join(recs[:5]))
    return "  ".join(parts)
