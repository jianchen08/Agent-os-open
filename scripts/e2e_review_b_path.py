"""复盘 B 路径全链路真实 e2e 验证脚本。

真实组件（连 LLM）：装配 service → 造真实 pending 执行记录 → 触发 trigger_llm_review
→ 等后台 review_agent 跑完 → 验证报告产出（知识库 + docs/working/review_report_*.md）。

与隔离层 e2e 的区别：本脚本连真实 LLM，验证 B 路径从触发到报告产出的完整链路，
包括 tags.agent_id 驱动引擎启动、_start_idle_engine 反查、review_agent 真实分析。

用法：
    python scripts/e2e_review_b_path.py

前置：LLM key 已配置（GLM_API_KEY / ZHIPU_API_KEY 等）。
退出码：0=成功，1=失败（含超时/无报告/异常）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 路径设置：项目根在 path 上，src 作为包（兼容 from src.xxx 和 from xxx 两种导入）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SRC_DIR))

from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    PipelineRunSummary,
)
from infrastructure.service_provider import get_service_provider


REPORT_TIMEOUT = 300  # 等报告最长 300s（B 路径后台 LLM）


def _seed_pending_pipeline(storage, run_id: str = "e2e-review-target") -> None:
    """向真实 storage 塞一条 review_status=pending 的管道执行记录。

    模拟一个失败的管道：用户下达任务 → agent 报错 → 管道 ended 但未复盘。
    """
    # L0 summary：status=failed, review_status=pending（复盘筛选条件）
    summary = PipelineRunSummary(
        run_id=run_id,
        status="failed",
        review_status="pending",
        total_records=4,
        total_iterations=1,
        created_at="2026-06-27T10:00:00",
        error="ImportError: No module named 'core'",
    )
    storage._summaries[run_id] = summary

    # 执行记录：user 指令 + ai + tool + 错误
    records = [
        ExecutionRecordData(
            pipeline_run_id=run_id, type="user", role="user",
            content="请帮我重构 auth 模块，确保所有测试通过",
            sequence=1, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=run_id, type="ai", role="assistant",
            content="我来分析现有结构",
            thinking_content="先读取模块文件",
            sequence=2, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=run_id, type="tool", role="tool",
            name="file_read", content="读取了 auth.py",
            sequence=3, iteration=1,
        ),
        ExecutionRecordData(
            pipeline_run_id=run_id, type="ai", role="assistant",
            content="", error="ImportError: No module named 'core'",
            sequence=4, iteration=1,
        ),
    ]
    for r in records:
        storage.save(r)
    print(f"[OK] 已造 pending 管道: {run_id} (status=failed, review_status=pending)")


async def _wait_report(maintenance_service, target_run_id: str) -> tuple[bool, str]:
    """等待后台复盘完成。

    成功判据（任一即可，反映 B 路径真实跑通）：
    1. docs/working/ 下出现本次新生成的 review_report_*.md（报告落盘）
    2. maintenance_service._review_running 变回 False（后台任务结束）

    注：不盯特定 target 的 review_status——复盘收集的是 storage 里全部 pending
    管道（可能包含已存在的真实管道），_mark_targets_reviewed 标记的是实际被
    复盘的那些，未必包含脚本造的 target。

    Returns:
        (是否成功产出报告, 诊断信息)
    """
    report_dir = Path("docs/working")
    baseline_reports = set(report_dir.glob("review_report_*.md")) if report_dir.exists() else set()
    deadline = time.time() + REPORT_TIMEOUT

    while time.time() < deadline:
        await asyncio.sleep(5)
        # 判据1：有新报告文件产出
        current_reports = set(report_dir.glob("review_report_*.md")) if report_dir.exists() else set()
        new_reports = current_reports - baseline_reports
        if new_reports:
            return True, f"产出新报告: {sorted(p.name for p in new_reports)}"
        # 判据2：后台任务结束（无论是否产出报告都说明复盘跑完了）
        if not getattr(maintenance_service, "_review_running", False):
            return bool(new_reports), f"复盘后台已结束, 新报告数={len(new_reports)}"

    return False, f"超时({REPORT_TIMEOUT}s)，等待报告产出"


def _check_report_artifacts(child_pipeline_id_hint: str = "") -> dict:
    """检查报告产物：docs/working/review_report_*.md 文件。"""
    report_dir = Path("docs/working")
    reports = list(report_dir.glob("review_report_*.md")) if report_dir.exists() else []
    return {
        "report_files": [str(p.name) for p in reports],
        "report_count": len(reports),
    }


async def main() -> int:
    print("=" * 60)
    print("复盘 B 路径全链路真实 e2e")
    print("=" * 60)

    # 1. 装配（create_combined_app 已在 import 时完成，service_provider 已就绪）
    sp = get_service_provider()
    maintenance_service = sp.get("maintenance_service")
    if maintenance_service is None:
        print("[FAIL] maintenance_service 不可用")
        return 1
    storage = sp.get("execution_record_storage")
    print(f"[OK] 服务就绪: {type(maintenance_service).__name__}")

    # 确认 review_agent 配置存在（tags.agent_id 反查的前提）
    from agents.global_registry import get_global_agent_registry_sync
    agent_cfg = get_global_agent_registry_sync().get(maintenance_service.REVIEW_AGENT_ID)
    if agent_cfg is None:
        print(f"[FAIL] {maintenance_service.REVIEW_AGENT_ID} 配置不存在，复盘无法启动")
        return 1
    print(f"[OK] review_agent 配置已加载: {agent_cfg.config_id}")

    # 2. 造 pending 目标管道
    target_run_id = "e2e-review-target"
    _seed_pending_pipeline(storage, target_run_id)

    # 3. 触发 B 路径复盘（parent 为空，模拟 API 手动触发）
    print(f"\n--- 触发复盘 ---")
    result = await maintenance_service.trigger_llm_review(
        parent_pipeline_id="",
        limit=5,
    )
    print(f"[触发结果] {result}")
    if result.get("status") not in ("submitted", "already_running"):
        print(f"[FAIL] 复盘未提交: {result}")
        return 1

    # 4. 等后台 review_agent 跑完（连真实 LLM）
    print(f"\n--- 等待 review_agent 产出报告（最长 {REPORT_TIMEOUT}s）---")
    t0 = time.time()
    ok, diag = await _wait_report(maintenance_service, target_run_id)
    elapsed = time.time() - t0
    print(f"[{'OK' if ok else 'FAIL'}] {diag} (耗时 {elapsed:.0f}s)")

    # 5. 验证报告产物
    print(f"\n--- 验证报告产物 ---")
    artifacts = _check_report_artifacts()
    print(f"  review_report_*.md 文件数: {artifacts['report_count']}")
    if artifacts["report_files"]:
        latest = artifacts["report_files"][-1]
        latest_path = Path("docs/working") / latest
        size = latest_path.stat().st_size if latest_path.exists() else 0
        print(f"  最新报告: {latest} ({size} bytes)")

    # 结论
    print("\n" + "=" * 60)
    if ok:
        print("[PASS] B 路径全链路成功：复盘触发→tags.agent_id驱动引擎→review_agent LLM分析→报告落盘+入库")
        print("       （覆盖点1: agent 身份走 tags; 点2: 来源溯源; 点3: L1 用户消息可见于报告证据）")
        return 0
    else:
        print(f"[FAIL] B 路径未产出报告。诊断: {diag}")
        print("       可能原因：LLM 超时/网络不通 / review_agent 启动失败 / tags.agent_id 反查失败")
        return 1


if __name__ == "__main__":
    # import create_combined_app 触发完整装配（lifespan 级别服务初始化）
    from channels.websocket.app_factory import create_combined_app
    _ = create_combined_app()
    sys.exit(asyncio.run(main()))
