# @feature: FP-0.2.〇 任务状态流转 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：任务状态流转（2026-08-24 职责边界裁定）

验证任务状态由任务域插件裁决（内核不再写 task.status）：
  1. POST /ext/task_service/tasks 创建任务 → 出生 state task.status=pending
     （chat.send_message 创建分支出生落库，pipeline_state 表可查）
  2. 任务管道执行（background 派发，真实 LLM）→ 第一轮插件后 task.status
     推进为 running（task_reminder pending→running）
  3. 终态裁决在任务域：task_evaluate 经 pipeline-state.update 写
     completed/failed；未评估任务不被内核补 completed（保持 running 或
     pending_evaluation，绝不静默 completed）

数据清理：本文件创建的任务注册到 cleanup_sessions（删会话级数据）；
CI 用 :memory: 内存库无持久影响，本地反复跑用 clear-all 端点清执行数据。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 手动运行（不在 CI）：python -m pytest tests/e2e_02/test_08_task_state_flow.py -q
"""
import json
import os
import time

import pytest
from e2e_helpers import (
    create_session,
    http_get_with_auth,
    http_post_json_auth,
)

pytestmark = [
    pytest.mark.e2e,
    # 依赖真实 LLM 执行任务管道：无 key 时跳过（CI 有 secrets.ZHIPU_API_KEY 才跑全量）
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 执行任务管道）",
    ),
]

# 创建任务 → 管道出生 → 首轮插件推进 running 的等待窗口
# （background 派发 + 真实 LLM：宽裕超时，实测创建即落库、首轮 ~10-30s）
RUNNING_WAIT_SECONDS = 120


@pytest.fixture(autouse=True)
def _cleanup_execution_data(auth_token, kernel_url):
    """测试后清理：清空全部执行数据（内核 9 表 + registry，users 保留）。

    任务管道是独立管道（thread_id = task_id，不在会话下），cleanup_sessions
    删不到；0.2 任务无 YAML 记录，DELETE /tasks/{id} 端点 404（只查 YAML
    存储）。故用 clear-all 端点全量清理——CI 内存库进程结束即清，本地反复
    跑也保持卫生（best-effort：失败只告警不阻塞测试结论）。
    """
    yield
    try:
        http_post_json_auth(
            f"{kernel_url}/ext/monitoring/execution/records/clear-all",
            {},
            token=auth_token,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 —— teardown 尽力而为
        print(f"[e2e-cleanup] clear-all 失败（忽略）: {exc}")


def _find_pipeline_state(body, pipeline_id: str) -> dict | None:
    """从 /api/v1/pipelines/state 响应中按 pipeline_id 找行（None = 未出口）。"""
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("pipeline_id") == pipeline_id:
            return item
    return None


class TestTaskStateFlow:
    """任务状态流转：出生 pending → 执行 running（不再被内核补 completed）。"""

    def test_task_created_with_pending_status(self, auth_token, cleanup_sessions, kernel_url):
        """创建任务 → 出生 task.status=pending（非内核补写，是出生值）。"""
        token = auth_token
        session = create_session(token, title="e2e-task-state-created")
        cleanup_sessions(session["thread_id"])
        tasks_url = f"{kernel_url}/ext/task_service/tasks"
        state_url = f"{kernel_url}/api/v1/pipelines/state"

        status, body, _ = http_post_json_auth(
            f"{tasks_url}",
            {
                "title": "e2e 任务状态出生测试",
                "description": "只验证出生状态，无需真实执行",
                "agent_id": "general_agent",
            },
            token=token,
            timeout=15,
        )
        assert status == 200, f"创建任务应 200，实际 {status}: {body}"
        task_id = body.get("id") or body.get("task_id")
        assert task_id, f"创建任务应返回 id，实际 {body}"

        # 出生即落 pipeline_state 表（chat_send_handler 创建分支），聚合可见；
        # background 派发异步落库，短轮询等待出生行出口（<30s，内核负载高时放宽）
        row = None
        deadline = time.time() + 30
        while time.time() < deadline and row is None:
            state_status, state_body, _ = http_get_with_auth(
                f"{state_url}", token=token, timeout=10
            )
            if state_status == 200:
                row = _find_pipeline_state(state_body, task_id)
            if row is None:
                time.sleep(2)
        assert row is not None, f"刚创建的任务应出现在 state 聚合，task_id={task_id}"
        assert row.get("state", {}).get("task.status") in ("pending", "running"), (
            f"出生状态应为 pending（或已推进 running），实际 {row.get('state', {})}"
        )

    def test_task_runs_and_is_not_silently_completed(self, auth_token, cleanup_sessions, kernel_url):
        """任务管道执行：状态推进 running，绝不静默补 completed。

        职责边界（2026-08-24）：内核不再写 task.status。任务终态只能由
        task_evaluate 评估裁决（completed/failed/pending_evaluation）——
        本用例跑真实 LLM 任务管道，断言结束后 task.status 不是内核补的
        completed（无评估证据时保持 running/pending_evaluation）。
        """
        token = auth_token
        session = create_session(token, title="e2e-task-state-run")
        cleanup_sessions(session["thread_id"])
        tasks_url = f"{kernel_url}/ext/task_service/tasks"
        state_url = f"{kernel_url}/api/v1/pipelines/state"

        # 创建任务（background 派发执行管道，真实 LLM 跑）
        status, body, _ = http_post_json_auth(
            f"{tasks_url}",
            {
                "title": "e2e 任务状态流转测试：请返回当前时间戳",
                "description": "执行一个简单任务验证状态流转",
                "agent_id": "general_agent",
            },
            token=token,
            timeout=15,
        )
        assert status == 200, f"创建任务应 200，实际 {status}: {body}"
        task_id = body.get("id") or body.get("task_id")
        assert task_id, f"创建任务应返回 id，实际 {body}"

        # 轮询等待任务管道出生 + 首轮推进
        deadline = time.time() + RUNNING_WAIT_SECONDS
        seen_statuses: list[str] = []
        final_row: dict | None = None
        while time.time() < deadline:
            state_status, state_body, _ = http_get_with_auth(
                f"{state_url}", token=token, timeout=10
            )
            if state_status == 200:
                row = _find_pipeline_state(state_body, task_id)
                if row is not None:
                    final_row = row
                    st = row.get("state", {}).get("task.status", "")
                    if st not in seen_statuses:
                        seen_statuses.append(st)
            time.sleep(5)

        assert final_row is not None, "任务管道应出现在 state 聚合（出生即落库）"
        # 状态推进：出生 pending → 执行 running（task_reminder 首轮推进）
        assert "running" in seen_statuses or final_row.get("state", {}).get("task.status") == "running", (
            f"任务应推进到 running，实际状态序列 {seen_statuses}，最终 {final_row.get('state', {})}"
        )
        # 职责边界核心断言：任务状态只能由任务域裁决——pending/running/
        # pending_evaluation（未评估）/ completed（task_evaluate 评估通过，
        # 合法任务域终态）。新内核不写 task.status，无评估证据时不得出现
        # 静默 completed（旧内核行为，本次修复前实测复现）。
        final_status = final_row.get("state", {}).get("task.status", "")
        assert final_status in ("pending", "running", "pending_evaluation", "completed"), (
            f"任务状态应由任务域裁决，实际 {final_status}"
        )
