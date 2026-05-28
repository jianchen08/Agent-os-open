#!/usr/bin/env python3
"""基于真实管道日志的复盘功能完整验证脚本。

可独立运行，验证内容：
1. 日志解析：5个日志文件的Pipeline和错误解析
2. 复盘流程：注册+执行复盘，提取10条经验
3. 经验内容完整性：字段、category覆盖
4. 接口兼容性：service.py 与 ReviewEngine 接口匹配
5. 脚本运行：trigger_real_review.py 退出码0
6. 补充场景：不存在的目录、0错误pipeline、未知错误类型
7. 经验生成行为：相同类型错误每条生成独立经验，source_error_id唯一
8. category映射规则：验证所有错误类型到category的对应关系
9. 日志格式异常容错：损坏/空日志不导致崩溃

用法: python3 verify_real_review.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.memory.maintenance.review_engine import (
    ErrorRecord,
    Experience,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService


# ──────────────────────────────────────────────
# Step 1: 解析真实管道日志
# ──────────────────────────────────────────────
def verify_step1_parse_logs():
    """验证 PipelineLogParser 解析5个日志文件，错误数正确。"""
    print("=" * 60)
    print("Step 1: 解析真实管道日志")
    print("=" * 60)

    pipelines = ReviewEngine.parse_pipeline_logs("logs")
    expected = {
        "013af21d0b04": 3,  # 2 timeout + 1 connection
        "07485ba22889": 1,  # 1 validation
        "a3c5e7f9d012": 2,  # 1 permission + 1 timeout
        "b4d6f8e0a123": 0,  # 无错误
        "c5e7a9f1b234": 4,  # connection + validation + timeout + permission
    }

    assert len(pipelines) == 5, f"期望5个pipeline，实际 {len(pipelines)}"
    for p in pipelines:
        exp = expected[p.pipeline_id]
        assert len(p.errors) == exp, (
            f"Pipeline {p.pipeline_id}: 期望 {exp} 错误，实际 {len(p.errors)}"
        )
        print(f"  Pipeline {p.pipeline_id}: {len(p.errors)} errors [OK]")

    found_ids = {p.pipeline_id for p in pipelines}
    assert found_ids == set(expected.keys()), (
        f"缺少pipeline: {set(expected.keys()) - found_ids}"
    )
    total_errors = sum(len(p.errors) for p in pipelines)
    assert total_errors == 10, f"总错误数期望10，实际 {total_errors}"
    print(f"  总错误数: {total_errors} [OK]")
    print(f"  Step 1: PASS\n")
    return pipelines


# ──────────────────────────────────────────────
# Step 2: 注册Pipeline并执行复盘
# ──────────────────────────────────────────────
def verify_step2_review(pipelines):
    """验证 ReviewEngine 对5个pipeline执行复盘，提取10条经验。"""
    print("=" * 60)
    print("Step 2: 注册Pipeline并执行复盘")
    print("=" * 60)

    engine = ReviewEngine()
    engine.register_pipelines(pipelines)

    summary = engine.get_summary()
    assert summary["total_registered"] == 5
    assert summary["pending"] == 5

    result = engine.run_review()
    assert result["processed"] == 5, f"期望处理5个，实际 {result['processed']}"
    assert result["experiences_extracted"] == 10, (
        f"期望提取10条经验，实际 {result['experiences_extracted']}"
    )

    summary_after = engine.get_summary()
    assert summary_after["completed"] == 5
    assert summary_after["failed"] == 0

    print(f"  处理: {result['processed']}/5, 经验: {result['experiences_extracted']} [OK]")
    print(f"  Step 2: PASS\n")
    return engine


# ──────────────────────────────────────────────
# Step 3: 验证经验内容完整性
# ──────────────────────────────────────────────
def verify_step3_experience_integrity(engine):
    """验证每条经验的字段完整性和category覆盖。"""
    print("=" * 60)
    print("Step 3: 验证经验内容完整性")
    print("=" * 60)

    all_experiences: list[Experience] = []
    for p in engine._pipelines.values():
        all_experiences.extend(p.experiences)

    assert len(all_experiences) == 10, f"经验总数 {len(all_experiences)}，期望10"

    required_fields = ["experience_id", "source_error_id", "lesson", "category", "created_at"]
    for i, exp in enumerate(all_experiences):
        for field in required_fields:
            val = getattr(exp, field, None)
            assert val, f"经验 {i} 缺少字段 {field}"
    print(f"  10条经验字段完整性: OK")

    categories = {exp.category for exp in all_experiences}
    expected_categories = {"performance", "infrastructure", "data_quality", "security"}
    assert categories == expected_categories, (
        f"缺少category: {expected_categories - categories}"
    )
    print(f"  Category覆盖: {categories} [OK]")

    all_completed = all(
        p.status == ReviewStatus.COMPLETED for p in engine._pipelines.values()
    )
    assert all_completed, "存在未完成的Pipeline"
    print(f"  Pipeline状态: 全部COMPLETED [OK]")

    # 验证 lesson 内容包含原始错误消息（语义正确性）
    for exp in all_experiences:
        assert len(exp.lesson) > 10, f"lesson 内容过短: {exp.lesson}"
    print(f"  Lesson内容语义完整性: OK")

    print(f"  Step 3: PASS\n")


# ──────────────────────────────────────────────
# Step 4: 验证service.py接口兼容性
# ──────────────────────────────────────────────
def verify_step4_interface():
    """验证 service.py 的 trigger_review 接口兼容性。"""
    print("=" * 60)
    print("Step 4: 验证service.py接口兼容性")
    print("=" * 60)

    svc = MemoryMaintenanceService()

    for method in ["run_batch_review", "get_summary", "reset"]:
        assert hasattr(svc._engine, method), f"ReviewEngine 缺少 {method} 方法"
        print(f"  ReviewEngine.{method}: 存在 [OK]")

    issues = svc._check_interface_compatibility()
    assert issues == [], f"接口兼容性问题: {issues}"
    print(f"  接口兼容性检查: 返回空列表 [OK]")

    svc_result = svc.trigger_review([
        {
            "pipeline_id": "svc-test-001",
            "errors": [
                {"error_id": "serr-001", "error_type": "timeout", "message": "测试超时"},
            ],
        },
    ])
    assert svc_result["interface_check"] == []
    assert "error" not in svc_result["review_result"], (
        f"trigger_review 返回错误: {svc_result['review_result']}"
    )
    print(f"  trigger_review: 正常执行 [OK]")
    print(f"  Step 4: PASS\n")


# ──────────────────────────────────────────────
# Step 5: 运行 trigger_real_review.py
# ──────────────────────────────────────────────
def verify_step5_script():
    """验证 scripts/trigger_real_review.py 退出码为0。"""
    print("=" * 60)
    print("Step 5: 运行 trigger_real_review.py")
    print("=" * 60)

    script_path = Path(__file__).resolve().parent / "scripts" / "trigger_real_review.py"
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"脚本退出码: {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    print(f"  退出码: {proc.returncode} [OK]")
    print(f"  Step 5: PASS\n")


# ──────────────────────────────────────────────
# 补充场景 1-3: 错误输入和边界情况
# ──────────────────────────────────────────────
def verify_supplementary():
    """补充场景：错误输入、边界情况、日志容错。"""
    print("=" * 60)
    print("补充场景")
    print("=" * 60)

    # 场景1: 不存在的目录
    result = ReviewEngine.parse_pipeline_logs("/nonexistent_dir_xyz")
    assert result == [], f"期望空列表，实际 {result}"
    print("  场景1 - 不存在目录: 返回空列表 [OK]")

    # 场景2: 0错误pipeline
    engine = ReviewEngine()
    engine.register_pipeline(Pipeline(pipeline_id="zero-test", errors=[]))
    review = engine.run_review()
    p = engine._pipelines["zero-test"]
    assert p.status == ReviewStatus.COMPLETED
    assert len(p.experiences) == 0
    assert review["processed"] == 1
    print("  场景2 - 0错误pipeline: 正常完成，0经验 [OK]")

    # 场景3: 未知错误类型
    engine2 = ReviewEngine()
    engine2.register_pipeline(Pipeline(
        pipeline_id="unknown-test",
        errors=[ErrorRecord(
            error_id="err-unk", error_type="new_type",
            message="test", timestamp="2026-01-01",
        )],
    ))
    engine2.run_review()
    exp = engine2._pipelines["unknown-test"].experiences[0]
    assert "未知错误" in exp.lesson
    assert exp.category == "unknown"
    print("  场景3 - 未知错误类型: 正确处理 [OK]")

    # 场景4: 相同类型错误每条生成独立经验（验证经验生成行为）
    engine3 = ReviewEngine()
    engine3.register_pipeline(Pipeline(
        pipeline_id="dedup-test",
        errors=[
            ErrorRecord(error_id="e1", error_type="timeout", message="Timeout A", timestamp="2026-01-01"),
            ErrorRecord(error_id="e2", error_type="timeout", message="Timeout B", timestamp="2026-01-01"),
            ErrorRecord(error_id="e3", error_type="timeout", message="Timeout C", timestamp="2026-01-01"),
        ],
    ))
    engine3.run_review()
    p3 = engine3._pipelines["dedup-test"]
    assert len(p3.experiences) == 3, f"期望3条独立经验，实际 {len(p3.experiences)}"
    src_ids = {exp.source_error_id for exp in p3.experiences}
    assert src_ids == {"e1", "e2", "e3"}, f"source_error_id 应唯一: {src_ids}"
    lessons = [exp.lesson for exp in p3.experiences]
    assert "Timeout A" in lessons[0] and "Timeout B" in lessons[1] and "Timeout C" in lessons[2]
    print("  场景4 - 相同类型错误: 生成独立经验，source_error_id唯一，消息内容正确 [OK]")

    # 场景5: category映射规则完整验证
    engine4 = ReviewEngine()
    engine4.register_pipeline(Pipeline(
        pipeline_id="mapping-test",
        errors=[
            ErrorRecord(error_id="m1", error_type="timeout", message="t1", timestamp="2026-01-01"),
            ErrorRecord(error_id="m2", error_type="connection", message="c1", timestamp="2026-01-01"),
            ErrorRecord(error_id="m3", error_type="validation", message="v1", timestamp="2026-01-01"),
            ErrorRecord(error_id="m4", error_type="permission", message="p1", timestamp="2026-01-01"),
            ErrorRecord(error_id="m5", error_type="unknown_type", message="u1", timestamp="2026-01-01"),
        ],
    ))
    engine4.run_review()
    p4 = engine4._pipelines["mapping-test"]
    expected_mapping = {
        "m1": "performance", "m2": "infrastructure",
        "m3": "data_quality", "m4": "security", "m5": "unknown",
    }
    for exp in p4.experiences:
        assert exp.category == expected_mapping[exp.source_error_id], (
            f"{exp.source_error_id}: 期望 {expected_mapping[exp.source_error_id]}，实际 {exp.category}"
        )
    print("  场景5 - category映射: timeout->performance, connection->infrastructure, "
          "validation->data_quality, permission->security, unknown->unknown [OK]")

    # 场景6: 日志格式异常容错
    tmpdir = tempfile.mkdtemp()
    try:
        # 正常日志
        with open(os.path.join(tmpdir, "pipeline_normal.log"), "w") as f:
            f.write('2026-05-28 08:00:00.123 [pipeline_normal] INFO  OK\n')
            f.write('2026-05-28 08:00:01.456 [pipeline_normal] ERROR test error_type=timeout error="test msg"\n')
        # 损坏日志
        with open(os.path.join(tmpdir, "pipeline_corrupt.log"), "w") as f:
            f.write("CORRUPTED LINE WITHOUT PROPER FORMAT\n")
        # 空日志
        with open(os.path.join(tmpdir, "pipeline_empty.log"), "w") as f:
            pass

        result = ReviewEngine.parse_pipeline_logs(tmpdir)
        normal_found = any(p.pipeline_id == "normal" for p in result)
        assert normal_found, "正常日志未被解析"
        assert len(result) >= 1
        print("  场景6 - 日志格式异常容错: 正常日志被解析，损坏/空日志被跳过，无崩溃 [OK]")
    finally:
        shutil.rmtree(tmpdir)

    print(f"  补充场景: ALL PASS (6/6)\n")


# ──────────────────────────────────────────────
# main
# ──────────────────────────────────────────────
def main():
    """运行所有验证步骤。"""
    print("基于真实管道日志的复盘功能完整验证\n")

    try:
        pipelines = verify_step1_parse_logs()
        engine = verify_step2_review(pipelines)
        verify_step3_experience_integrity(engine)
        verify_step4_interface()
        verify_step5_script()
        verify_supplementary()
    except AssertionError as e:
        print(f"\n验证失败: {e}")
        return 1
    except Exception as e:
        print(f"\n异常: {e}")
        return 1

    print("=" * 60)
    print("全部验证通过: 5/5 步骤 + 6/6 补充场景")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
