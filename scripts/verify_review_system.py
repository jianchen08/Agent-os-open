"""复盘系统最小生效验证脚本（不依赖外部脚本和服务）。

验证项：
1. ReviewEngine 简化版流程：注册 pending pipeline → run_review → 状态翻更为 completed
2. 经验提取数量与错误记录一致
3. get_summary 返回正确状态
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.memory.maintenance.review_engine import (  # noqa: E402
    ErrorRecord,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)


def main() -> int:
    """执行最小复盘验证流程并打印结果。"""
    engine = ReviewEngine()

    pipelines = [
        Pipeline(
            pipeline_id="verify-001",
            errors=[
                ErrorRecord("e1", "timeout", "API 调用超时", "2026-06-02T08:00:00Z"),
                ErrorRecord("e2", "connection", "数据库连接断开", "2026-06-02T08:01:00Z"),
            ],
        ),
        Pipeline(pipeline_id="verify-002", errors=[]),
    ]
    engine.register_pipelines(pipelines)

    result = engine.run_review()

    print("=== 复盘运行结果 ===")
    print(f"total_pending       = {result['total_pending']}")
    print(f"processed           = {result['processed']}")
    print(f"experiences_extracted = {result['experiences_extracted']}")
    for pr in result["pipeline_results"]:
        print(
            f"  - {pr['pipeline_id']}: status={pr['status']} "
            f"errors={pr['error_count']} experiences={pr['experience_count']}"
        )

    print("\n=== 单个 Pipeline 状态校验 ===")
    for p in pipelines:
        summary = engine.get_summary(p.pipeline_id)
        print(
            f"  - {p.pipeline_id}: pipeline.status={p.status.value} "
            f"summary.review_status={summary.review_status if summary else None}"
        )

    assert result["total_pending"] == 2, "应有 2 个 pending pipeline"
    assert result["processed"] == 2, "应处理 2 个 pipeline"
    assert result["experiences_extracted"] == 2, "应提取 2 条经验"
    assert all(p.status == ReviewStatus.COMPLETED for p in pipelines), "状态未翻更为 completed"
    print("\n[OK] 复盘系统核心流程生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
