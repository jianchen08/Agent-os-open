#!/usr/bin/env python3
"""复盘触发脚本功能完整性验证脚本 - 可独立运行复现所有验证场景。

使用方法: python3 verify_reproduce.py
工作目录: 项目根目录（scripts/trigger_review.py 所在目录的上级）
"""
from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.memory.maintenance.review_engine import (
    ErrorRecord,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService

PASSED = 0
FAILED = 0


def report(name: str, ok: bool, detail: str = ""):
    global PASSED, FAILED
    status = "✓ PASS" if ok else "✗ FAIL"
    PASSED += 1 if ok else 0
    FAILED += 1 if not ok else 0
    msg = f"  [{status}] {name}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# 用户旅程 Step 1: 触发脚本执行（退出码0）
# ============================================================
separator("用户旅程 Step 1: 运行 trigger_review.py")
result = subprocess.run(
    [sys.executable, "scripts/trigger_review.py"],
    capture_output=True,
    text=True,
    timeout=30,
)
output = result.stdout + result.stderr
exit_code = result.returncode

report("退出码为 0", exit_code == 0, f"实际退出码: {exit_code}")
report("输出包含 '复盘触发脚本启动'", "复盘触发脚本启动" in output)
report("输出包含 '复盘流程全部完成'", "复盘流程全部完成" in output)


# ============================================================
# 用户旅程 Step 2: 复盘执行结果（3个pipeline全部完成）
# ============================================================
separator("用户旅程 Step 2: 复盘执行结果")
engine = ReviewEngine()
p1 = Pipeline(pipeline_id="p1", errors=[
    ErrorRecord("e1", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
    ErrorRecord("e2", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
])
p2 = Pipeline(pipeline_id="p2", errors=[
    ErrorRecord("e3", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
])
p3 = Pipeline(pipeline_id="p3", errors=[])
engine.register_pipelines([p1, p2, p3])

pending = engine.get_pending_pipelines()
report("注册后 pending 数量为 3", len(pending) == 3, f"实际: {len(pending)}")

review_result = engine.run_review()
report("处理数量为 3", review_result["processed"] == 3, f"实际: {review_result['processed']}")
report("总 pending 数为 3", review_result["total_pending"] == 3)

# 验证每个 pipeline 结果状态
for pr in review_result["pipeline_results"]:
    report(
        f"{pr['pipeline_id']} 状态为 completed",
        pr["status"] == "completed",
        f"实际: {pr['status']}",
    )


# ============================================================
# 用户旅程 Step 3: 经验提取正确性
# ============================================================
separator("用户旅程 Step 3: 经验提取正确性")
report(
    "pipeline-001 提取 2 条经验",
    review_result["pipeline_results"][0]["experience_count"] == 2,
    f"实际: {review_result['pipeline_results'][0]['experience_count']}",
)
report(
    "pipeline-002 提取 1 条经验",
    review_result["pipeline_results"][1]["experience_count"] == 1,
    f"实际: {review_result['pipeline_results'][1]['experience_count']}",
)
report(
    "pipeline-003 提取 0 条经验",
    review_result["pipeline_results"][2]["experience_count"] == 0,
    f"实际: {review_result['pipeline_results'][2]['experience_count']}",
)
report(
    "总经验数为 3",
    review_result["experiences_extracted"] == 3,
    f"实际: {review_result['experiences_extracted']}",
)

# 验证经验内容的具体字段
report("p1 的经验对象数量为 2", len(p1.experiences) == 2, f"实际: {len(p1.experiences)}")
if p1.experiences:
    exp = p1.experiences[0]
    report("经验包含 experience_id", bool(exp.experience_id), f"值: {exp.experience_id}")
    report("经验包含 lesson", bool(exp.lesson), f"值: {exp.lesson[:50]}...")
    report("经验包含 category", bool(exp.category), f"值: {exp.category}")
    report("经验包含 created_at", bool(exp.created_at), f"值: {exp.created_at}")


# ============================================================
# 用户旅程 Step 4: 状态变化验证（pending → completed）
# ============================================================
separator("用户旅程 Step 4: 状态变化 pending → completed")
for p in [p1, p2, p3]:
    report(
        f"{p.pipeline_id} 状态为 COMPLETED",
        p.status == ReviewStatus.COMPLETED,
        f"实际: {p.status}",
    )
    report(
        f"{p.pipeline_id} 有 reviewed_at 时间戳",
        p.reviewed_at is not None,
        f"值: {p.reviewed_at}",
    )


# ============================================================
# 用户旅程 Step 5: 接口兼容性诊断
# ============================================================
separator("用户旅程 Step 5: 接口兼容性诊断")
service = MemoryMaintenanceService()
configs = [{"pipeline_id": "svc-p1", "errors": [{"error_id": "se1", "error_type": "timeout", "message": "服务超时"}]}]
svc_result = service.trigger_review(configs)

interface_issues = svc_result["interface_check"]
report("检测到接口不匹配问题", len(interface_issues) > 0, f"检测到 {len(interface_issues)} 个问题")

missing_methods = [issue["missing_method"] for issue in interface_issues]
report("检测到 run_batch_review 缺失", "run_batch_review" in missing_methods)
report("检测到 get_summary 缺失", "get_summary" in missing_methods)
report("检测到 reset 缺失", "reset" in missing_methods)

# 验证严重度分级
for issue in interface_issues:
    if issue["missing_method"] == "run_batch_review":
        report("run_batch_review 严重度为 high", issue["severity"] == "high", f"实际: {issue['severity']}")

# Service 层复盘仍能完成（使用兼容的 run_review）
review_output = svc_result["review_result"]
report(
    "Service 层复盘成功完成",
    isinstance(review_output, dict) and review_output.get("processed") is not None,
    f"结果: {review_output}",
)


# ============================================================
# 补充场景 1: 错误输入 - 空 pipeline 列表
# ============================================================
separator("补充场景 1: 错误输入 - 空 pipeline 列表")
engine2 = ReviewEngine()
engine2.register_pipelines([])
pending2 = engine2.get_pending_pipelines()
report("空注册后 pending 数量为 0", len(pending2) == 0, f"实际: {len(pending2)}")

result2 = engine2.run_review()
report("空列表复盘 processed 为 0", result2["processed"] == 0, f"实际: {result2['processed']}")
report("空列表复盘 experiences_extracted 为 0", result2["experiences_extracted"] == 0)


# ============================================================
# 补充场景 2: 边界情况 - 大量错误记录的 pipeline
# ============================================================
separator("补充场景 2: 边界情况 - 大量错误记录")
engine3 = ReviewEngine()
many_errors = [ErrorRecord(f"err-{i:04d}", "timeout", f"错误 {i}", "2026-05-28T08:00:00Z") for i in range(100)]
big_pipeline = Pipeline(pipeline_id="big-pipeline", errors=many_errors)
engine3.register_pipeline(big_pipeline)

result3 = engine3.run_review()
report("大量错误复盘 processed 为 1", result3["processed"] == 1)
report("100条错误提取100条经验", result3["experiences_extracted"] == 100, f"实际: {result3['experiences_extracted']}")
report("big-pipeline 状态为 COMPLETED", big_pipeline.status == ReviewStatus.COMPLETED)

# 验证未知错误类型也能处理
engine4 = ReviewEngine()
unknown_err_pipeline = Pipeline(
    pipeline_id="unknown-err",
    errors=[ErrorRecord("ue1", "unknown_type", "未知类型错误", "2026-05-28T08:00:00Z")],
)
engine4.register_pipeline(unknown_err_pipeline)
result4 = engine4.run_review()
report("未知错误类型能正常处理", result4["processed"] == 1)
report("未知错误类型仍提取经验", result4["experiences_extracted"] == 1)
if unknown_err_pipeline.experiences:
    report(
        "未知错误分类为 unknown",
        unknown_err_pipeline.experiences[0].category == "unknown",
        f"实际: {unknown_err_pipeline.experiences[0].category}",
    )


# ============================================================
# 最终统计
# ============================================================
separator("验证结果汇总")
total = PASSED + FAILED
print(f"\n  总测试项: {total}")
print(f"  通过: {PASSED}")
print(f"  失败: {FAILED}")
print(f"  通过率: {PASSED/total*100:.1f}%")

if FAILED == 0:
    print("\n  🎉 所有验证项全部通过！复盘模块功能完整。")
    sys.exit(0)
else:
    print(f"\n  ⚠️ 有 {FAILED} 项验证失败，请检查。")
    sys.exit(1)
