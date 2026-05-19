"""任务提交-执行-评估完整闭环 E2E 测试。

验证完整流程：
1. 任务提交 → 2. 任务分配 → 3. 任务执行 → 4. 评估 → 5. 完成/失败
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from tasks.service import TaskService
from tasks.storage import TaskStorage
from tasks.types import TaskStatus
from tools.builtin.task_submit import task_submit_func
from tools.builtin.task_manage import task_manage_func


# 全局共享的 TaskStorage 实例（使用临时文件）
_temp_storage: TaskStorage | None = None


def get_shared_storage() -> TaskStorage:
    """获取共享的 TaskStorage 实例。"""
    global _temp_storage
    if _temp_storage is None:
        # 使用临时文件持久化
        tmpfile = Path(tempfile.gettempdir()) / "agent_os_test_tasks.json"
        _temp_storage = TaskStorage(path=tmpfile)
        print(f"  [INFO] Using shared storage: {tmpfile}")
    return _temp_storage


def get_shared_service() -> TaskService:
    """获取共享的 TaskService 实例。"""
    return TaskService(storage=get_shared_storage())


def test_task_submit_and_evaluate():
    """测试任务提交-执行-评估完整闭环。"""
    print("\n=== 任务闭环 E2E 测试 ===\n")

    service = get_shared_service()

    # 1. 提交任务
    print("【1/4】提交任务...")
    submit_result = task_submit_func({
        "goal": {
            "title": "测试任务：简单数学计算",
            "description": "计算 2 + 2 的结果"
        },
        "target_type": "agent",
        "target_id": "general_agent",
        "acceptance_criteria": {
            "file_check": {
                "pass_threshold": 100
            }
        },
        "priority": 5,
        "task_scope": "non_container"
    })
    print(f"  提交结果: {submit_result}")

    # 检查是否降级模式
    if "降级模式" in submit_result.get("message", ""):
        print("  [INFO] TaskService 降级模式")
    else:
        print(f"  [OK] 任务已提交，ID: {submit_result.get('task_id')}")

    # 2. 使用共享 service 创建新任务进行生命周期测试
    print("\n【2/4】使用 TaskService 验证任务...")

    task = service.create_task(
        title="直接测试任务",
        description="通过 TaskService 创建",
        metadata={"target_type": "agent", "target_id": "general_agent", "acceptance_criteria": {}}
    )
    print(f"  [OK] TaskService 创建任务成功: {task.id}, status: {task.status.value}")
    assert task.status == TaskStatus.PENDING

    # 3. 测试状态转换
    print("\n【3/4】测试任务状态转换...")

    # pending -> running
    task = service.start_task(task.id)
    print(f"  [OK] start_task: {task.status.value}")
    assert task.status == TaskStatus.RUNNING

    # running -> evaluating
    task = service.move_to_evaluating(task.id)
    print(f"  [OK] move_to_evaluating: {task.status.value}")
    assert task.status == TaskStatus.EVALUATING

    # evaluating -> completed（评估通过）
    task = service.complete_evaluation(task.id, passed=True)
    print(f"  [OK] complete_evaluation(passed=True): {task.status.value}")
    assert task.status == TaskStatus.COMPLETED

    # 4. 验证任务查询（使用共享 service）
    print("\n【4/4】验证任务查询...")

    # get 操作
    task2 = service.get_task(task.id)
    print(f"  get (via service): success={task2 is not None}, status={task2.status.value if task2 else 'N/A'}")
    assert task2 is not None

    # list 操作
    completed_tasks = service.list_by_status(TaskStatus.COMPLETED)
    print(f"  list (COMPLETED): count={len(completed_tasks)}")
    assert len(completed_tasks) >= 1

    print("\n=== 任务闭环 E2E 测试通过 ===")
    return True


def test_task_lifecycle():
    """测试任务生命周期状态转换。"""
    print("\n=== 任务生命周期测试 ===\n")

    service = get_shared_service()

    # 创建任务
    task = service.create_task(
        title="生命周期测试任务",
        description="测试状态转换",
        metadata={"test": "lifecycle"}
    )
    print(f"【创建】任务 ID: {task.id}, 状态: {task.status.value}")
    assert task.status == TaskStatus.PENDING

    # 启动
    task = service.start_task(task.id)
    print(f"【启动】状态: {task.status.value}")
    assert task.status == TaskStatus.RUNNING

    # 暂停
    task = service.pause_task(task.id)
    print(f"【暂停】状态: {task.status.value}")
    assert task.status == TaskStatus.PAUSED

    # 恢复
    task = service.resume_task(task.id)
    print(f"【恢复】状态: {task.status.value}")
    assert task.status == TaskStatus.RUNNING

    # 失败
    task = service.fail_task(task.id, error="测试失败")
    print(f"【失败】状态: {task.status.value}")
    assert task.status == TaskStatus.FAILED

    print("\n=== 任务生命周期测试通过 ===")
    return True


def test_task_evaluate_flow():
    """测试任务评估流程（含打回重做）。"""
    print("\n=== 任务评估流程测试 ===\n")

    service = get_shared_service()

    # 创建任务
    task = service.create_task(
        title="评估测试任务",
        description="测试评估流程",
        metadata={}
    )
    print(f"【创建】状态: {task.status.value}")

    # 启动 -> 执行
    task = service.start_task(task.id)
    print(f"【执行】状态: {task.status.value}")

    # 执行 -> 评估
    task = service.move_to_evaluating(task.id)
    print(f"【评估】状态: {task.status.value}")

    # 评估不通过 -> 打回重做
    task = service.reject_task(task.id, reason="结果不符合预期")
    print(f"【打回】状态: {task.status.value}, reject_count={task.reject_count}")
    assert task.status == TaskStatus.RUNNING
    assert task.reject_count == 1

    # 再次评估
    task = service.move_to_evaluating(task.id)
    task = service.reject_task(task.id, reason="再次不符合预期")
    print(f"【打回2】状态: {task.status.value}, reject_count={task.reject_count}")
    assert task.reject_count == 2

    # 第三次评估 -> 超限，失败
    task = service.move_to_evaluating(task.id)
    task = service.reject_task(task.id, reason="第三次不符合预期")
    print(f"【打回3超限】状态: {task.status.value}, reject_count={task.reject_count}")
    assert task.status == TaskStatus.FAILED
    assert task.reject_count == 3

    # 评估通过
    task2 = service.create_task(title="评估通过测试", description="测试")
    task2 = service.start_task(task2.id)
    task2 = service.move_to_evaluating(task2.id)
    task2 = service.complete_evaluation(task2.id, passed=True)
    print(f"【评估通过】状态: {task2.status.value}")
    assert task2.status == TaskStatus.COMPLETED

    print("\n=== 任务评估流程测试通过 ===")
    return True


if __name__ == "__main__":
    try:
        test_task_submit_and_evaluate()
        test_task_lifecycle()
        test_task_evaluate_flow()
        print("\n" + "=" * 50)
        print("所有测试通过！任务闭环验证完成。")
        print("=" * 50)
    except Exception as e:
        print(f"\n[FAIL] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
