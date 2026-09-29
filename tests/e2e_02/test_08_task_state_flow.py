# @feature: FP-0.2.〇 任务状态流转 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：任务状态出生语义（2026-08-24 职责边界裁定）

验证任务状态由任务域插件裁决（内核不再写 task.status）：
  1. POST /ext/task_service/tasks 创建任务 → 出生 state task.status=pending
     （chat.send_message 创建分支出生落库，pipeline_state 表可查）
  2. 用户旅程断言：创建后 GET /ext/task_service/tasks/{id} 详情必须 200
     （详情读面与列表同源 state；回归防护：详情读面曾钉死退役 YAML 镜像，
     列表可见而详情 404）

执行期终态语义（不静默 completed / 评估裁决 / llm_usage 指纹）不由本文件
持有：确定性路径见 test_task_lifecycle_paths_e2e.py（A3/D1，stub 矩阵），
真 LLM 评估闭合见 test_12_pipeline_completion_e2e.py。

数据清理：本文件创建的任务注册到 cleanup_sessions（删会话级数据）；
CI 用 :memory: 内存库无持久影响，本地反复跑用 clear-all 端点清执行数据。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 手动运行（不在 CI）：python -m pytest tests/e2e_02/test_08_task_state_flow.py -q
"""
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


def _assert_detail_openable(token, kernel_url, task_id: str, title: str) -> None:
    """用户旅程断言：任务详情必须打得开（创建 → 列表可见 → 详情 200）。

    出生 state 在创建请求返回前已三段落库，正常一次即 200；短轮询只为吸收
    聚合读取的亚秒级传播延迟。
    """
    deadline = time.time() + 15
    last_status, last_body = 0, None
    while time.time() < deadline:
        last_status, last_body, _ = http_get_with_auth(
            f"{kernel_url}/ext/task_service/tasks/{task_id}", token=token, timeout=10
        )
        if last_status == 200:
            break
        time.sleep(1)
    assert last_status == 200, (
        f"任务详情应 200（详情读面须与列表同源），task_id={task_id}，"
        f"实际 {last_status}: {last_body}"
    )
    assert last_body is not None, f"详情应返回该任务，实际 {last_body}"
    assert last_body.get("id") == task_id, f"详情应返回该任务，实际 {last_body}"
    assert last_body.get("title") == title, f"详情 title 应一致，实际 {last_body}"


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
        _assert_detail_openable(token, kernel_url, str(task_id), "e2e 任务状态出生测试")
        # 出生即落 pipeline_state 表（chat_send_handler 创建分支），聚合可见；
        # background 派发异步落库，轮询等待出生行出口。窗口取与执行用例同量级
        # （120s）：clear-all teardown 的服务端异步清理可能残留到下一批用例
        # 开头（多文件合跑时把首个任务刚落的出生行清掉），短窗口会确定性误报。
        row = None
        deadline = time.time() + RUNNING_WAIT_SECONDS
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
