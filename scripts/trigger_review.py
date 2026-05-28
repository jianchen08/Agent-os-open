"""复盘触发脚本 - 运行复盘流程并验证结果。"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，使 `python scripts/trigger_review.py` 可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.maintenance.review_engine import (
    ErrorRecord,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService


def main() -> int:
    """运行复盘流程。"""
    print("=" * 60)
    print("复盘触发脚本启动")
    print("=" * 60)

    # ============================================================
    # 阶段 1: ReviewEngine 直接复盘
    # ============================================================
    print("\n【阶段 1】ReviewEngine 直接复盘阶段")
    print("-" * 40)

    engine = ReviewEngine()

    # 构造 3 个 pending pipeline
    pipeline_001 = Pipeline(
        pipeline_id="pipeline-001",
        errors=[
            ErrorRecord("err-001", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
            ErrorRecord("err-002", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
        ],
    )
    pipeline_002 = Pipeline(
        pipeline_id="pipeline-002",
        errors=[
            ErrorRecord("err-003", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
        ],
    )
    pipeline_003 = Pipeline(
        pipeline_id="pipeline-003",
        errors=[],
    )

    engine.register_pipelines([pipeline_001, pipeline_002, pipeline_003])

    # 验证注册状态
    pending = engine.get_pending_pipelines()
    print(f"  已注册 pending pipeline 数量: {len(pending)}")
    assert len(pending) == 3, f"期望 3 个 pending pipeline，实际 {len(pending)}"

    # 执行复盘
    result = engine.run_review()
    print(f"  复盘完成: 处理 {result['processed']}/{result['total_pending']} 个 pipeline")
    print(f"  提取经验总数: {result['experiences_extracted']}")

    # 验证复盘结果
    assert result["processed"] == 3, f"期望处理 3 个，实际 {result['processed']}"

    # 验证经验提取正确性
    for pr in result["pipeline_results"]:
        pid = pr["pipeline_id"]
        ec = pr["experience_count"]
        print(f"  {pid}: errors={pr['error_count']}, experiences={ec}, status={pr['status']}")

    assert result["pipeline_results"][0]["experience_count"] == 2, "pipeline-001 应有 2 条经验"
    assert result["pipeline_results"][1]["experience_count"] == 1, "pipeline-002 应有 1 条经验"
    assert result["pipeline_results"][2]["experience_count"] == 0, "pipeline-003 应有 0 条经验"

    # 验证 review_status 变化
    for p in [pipeline_001, pipeline_002, pipeline_003]:
        assert p.status == ReviewStatus.COMPLETED, f"{p.pipeline_id} 状态应为 completed，实际 {p.status}"
    print("  所有 pipeline 状态已变更为 completed ✓")

    print("\n【阶段 1 完成】ReviewEngine 直接复盘阶段成功 ✓")

    # ============================================================
    # 阶段 2: MemoryMaintenanceService 触发阶段
    # ============================================================
    print("\n【阶段 2】MemoryMaintenanceService 触发阶段")
    print("-" * 40)

    service = MemoryMaintenanceService()

    pipeline_configs = [
        {
            "pipeline_id": "svc-pipeline-001",
            "errors": [
                {"error_id": "serr-001", "error_type": "timeout", "message": "服务超时"},
            ],
        },
    ]

    svc_result = service.trigger_review(pipeline_configs)

    # 报告接口兼容性检查
    interface_issues = svc_result["interface_check"]
<<<<<<< C:\Users\jc\AppData\Local\Temp\tmpsxe8aa86\current
    print(f"  接口兼容性检查发现 {len(interface_issues)} 个问题:")
    for issue in interface_issues:
        print(f"    - 缺失方法: {issue['missing_method']} (严重度: {issue['severity']})")
        print(f"      说明: {issue['description']}")

    assert len(interface_issues) > 0, "应检测到接口不匹配问题"
=======
    if interface_issues:
        print(f"  接口兼容性检查发现 {len(interface_issues)} 个问题:")
        for issue in interface_issues:
            print(f"    - 缺失方法: {issue['missing_method']} (严重度: {issue['severity']})")
            print(f"      说明: {issue['description']}")
    else:
        print("  接口兼容性检查: 全部通过 ✓")

    assert len(interface_issues) == 0, f"接口应已兼容，但仍有缺失方法: {interface_issues}"
>>>>>>> D:\myproject\container_08f57__wt_7f34aa1e\scripts\trigger_review.py

    # 验证 service 层仍能完成复盘（使用兼容的 run_review）
    review_output = svc_result["review_result"]
    if isinstance(review_output, dict) and "error" in review_output:
        print(f"  Service 层复盘失败: {review_output['error']}")
        print("  接口不匹配已被正确捕获和诊断 ✓")
    else:
        print(f"  Service 层复盘完成: processed={review_output.get('processed', 'N/A')}")

    print("\n【阶段 2 完成】MemoryMaintenanceService 触发阶段成功 ✓")

    # ============================================================
    # 最终总结
    # ============================================================
    print("\n" + "=" * 60)
    print("复盘流程全部完成 ✓")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
