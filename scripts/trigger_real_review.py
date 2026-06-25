"""基于真实管道日志的复盘触发脚本。

从 logs/ 目录读取 pipeline_*.log 文件，解析为 Pipeline 对象，
执行复盘并输出中文报告。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.maintenance.review_engine import ReviewEngine
from src.memory.maintenance.service import MemoryMaintenanceService

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def main() -> int:
    """运行基于真实日志的复盘流程。"""
    print("=" * 60)
    print("基于真实管道日志的复盘触发脚本")
    print("=" * 60)

    # ============================================================
    # 阶段 1: 解析真实管道日志
    # ============================================================
    print("\n【阶段 1】解析管道日志")
    print("-" * 40)

    pipelines = ReviewEngine.parse_pipeline_logs(LOG_DIR)
    print(f"  日志目录: {LOG_DIR}")
    print(f"  解析出 {len(pipelines)} 个 pipeline:")

    log_files = sorted(LOG_DIR.glob("pipeline_*.log"))
    print("  扫描到的日志文件:")
    for lf in log_files:
        print(f"    - {lf.name}")

    for p in pipelines:
        print(f"  Pipeline {p.pipeline_id}: {len(p.errors)} 个错误")

    if not pipelines:
        print("  未找到任何管道日志，退出。")
        return 0

    # ============================================================
    # 阶段 2: 注册并执行复盘
    # ============================================================
    print("\n【阶段 2】注册 Pipeline 并执行复盘")
    print("-" * 40)

    engine = ReviewEngine()
    engine.register_pipelines(pipelines)

    # 检查引擎状态
    summary = engine.get_summary()
    print(f"  引擎状态: 已注册 {summary['total_registered']} 个 pipeline")

    # 执行复盘
    result = engine.run_review()
    print(f"  复盘完成: 处理 {result['processed']}/{result['total_pending']} 个 pipeline")
    print(f"  提取经验总数: {result['experiences_extracted']}")

    # ============================================================
    # 阶段 3: 输出详细经验报告
    # ============================================================
    print("\n【阶段 3】经验详情报告")
    print("-" * 40)

    total_experiences = 0
    for p in pipelines:
        if not p.experiences:
            print(f"\n  Pipeline {p.pipeline_id}: 无经验提取 (0 个错误)")
            continue

        print(f"\n  Pipeline {p.pipeline_id}: 提取 {len(p.experiences)} 条经验")
        for exp in p.experiences:
            total_experiences += 1
            print(f"    [{exp.category}] {exp.lesson}")
            print(f"      来源: error_id={exp.source_error_id}")

    # ============================================================
    # 阶段 4: 验证 service.py 的 trigger_review 接口
    # ============================================================
    print("\n【阶段 4】验证 MemoryMaintenanceService 接口")
    print("-" * 40)

    service = MemoryMaintenanceService()
    svc_result = service.trigger_review([
        {
            "pipeline_id": "svc-test-001",
            "errors": [
                {"error_id": "serr-001", "error_type": "timeout", "message": "测试超时"},
            ],
        },
    ])

    interface_issues = svc_result["interface_check"]
    if interface_issues:
        print(f"  接口兼容性检查发现问题: {interface_issues}")
    else:
        print("  接口兼容性检查: 全部通过 ✓")

    review_output = svc_result["review_result"]
    if isinstance(review_output, dict) and "error" not in review_output:
        print(f"  Service 层复盘完成: processed={review_output.get('processed', 'N/A')}")
    else:
        print(f"  Service 层复盘结果: {review_output}")

    # ============================================================
    # 最终总结
    # ============================================================
    print("\n" + "=" * 60)
    print(f"复盘完成: 共处理 {result['processed']} 个 pipeline，提取 {total_experiences} 条经验")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
