"""轨迹库先行筛选（纯函数核心）：假设 → 轨迹样本库证据核查（立项前置）。

流程挂点（ADR 2026-09-29-eval-repeat-control-screening）：T2 立项前可选前置
——假设声称的失败簇先对轨迹样本库（harvest_trajectories.py 产物）核查证据
量；不足阈值不立项，省真实题集运行预算（失败驱动先于盲搜）。证据阈值与
evolution_flow T2「同因 ≥2 case 才立项」同源。匹配口径 MVP = 模式 +
题名子串（嵌入相似度随提案校验⑦二期化，同一批引入）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MIN_CLUSTER_EVIDENCE = 2  # 与 evolution_flow T2「同因 ≥2 case 才立项」同源
LIBRARY_ENV = "AGENTOS_TRAJECTORY_LIBRARY"  # 库文件显式指定（测试/多库切换）


def screen(hypothesis: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    """假设证据核查：{mode, title_substring?} × 轨迹库 → 证据统计 + 裁决。

    只统计 outcome=failure 的样本（成功轨迹不构成失败假设的证据）；
    title_substring 缺省不过滤（按模式全量计）。库为空/缺 records 视为
    证据不足（裁决含义 = 不立项，非错误）。
    """
    mode = str(hypothesis.get("mode") or "")
    needle = str(hypothesis.get("title_substring") or "")
    records = [r for r in (library.get("records") or []) if isinstance(r, dict)]
    failures = [r for r in records
                if str(r.get("mode") or "") == mode and r.get("outcome") == "failure"]
    matched = [r for r in failures
               if not needle or needle in str(r.get("title") or "")]
    replayable = sum(1 for r in matched if r.get("trajectory_available"))
    sufficient = bool(records) and len(matched) >= MIN_CLUSTER_EVIDENCE
    if not records:
        reason = "轨迹样本库为空（先跑 harvest_trajectories.py 采集）"
    elif sufficient:
        reason = (f"命中 {len(matched)} 条失败轨迹（可重放 {replayable}），"
                  "证据充分，允许立项")
    else:
        reason = (f"仅命中 {len(matched)} 条失败轨迹（阈值 {MIN_CLUSTER_EVIDENCE}），"
                  "先经 T1 复跑确认或继续采集，暂不立项")
    return {"mode": mode, "title_substring": needle,
            "failure_samples": len(failures), "matched": len(matched),
            "replayable": replayable, "threshold": MIN_CLUSTER_EVIDENCE,
            "verdict": "sufficient" if sufficient else "insufficient",
            "reason": reason}


def library_summary(library: dict[str, Any]) -> dict[str, Any]:
    """面板/健康度投影：轨迹库计数（总量/失败/可重放/按模式分布）。"""
    records = [r for r in (library.get("records") or []) if isinstance(r, dict)]
    modes: dict[str, dict[str, int]] = {}
    failures = replayable = 0
    for r in records:
        bucket = modes.setdefault(str(r.get("mode") or ""),
                                  {"total": 0, "failures": 0, "replayable": 0})
        bucket["total"] += 1
        if r.get("outcome") == "failure":
            failures += 1
            bucket["failures"] += 1
            if r.get("trajectory_available"):
                replayable += 1
                bucket["replayable"] += 1
    return {"generated_tag": str(library.get("generated_tag") or ""),
            "total": len(records), "failures": failures,
            "replayable": replayable, "modes": modes}


def find_latest_library(reports_dir: str) -> str:
    """reports/eval/trajectory_library/ 下按文件名字典序取最新（tag=时间戳，
    字典序即时序）；目录缺失或无库文件返回空串。"""
    lib_dir = Path(reports_dir) / "trajectory_library"
    if not lib_dir.is_dir():
        return ""
    libraries = sorted(lib_dir.glob("*.json"))
    return str(libraries[-1]) if libraries else ""
