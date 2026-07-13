"""任务提交全流程 E2E 测试。

验证任务创建（pending）→ 状态更新 → 评估链路。
对应 features.md 场景 2。

由于完整任务执行依赖管道引擎和 LLM，本测试聚焦 API 层面的
任务 CRUD 和状态转换验证，不触发实际 Agent 执行。

数据清理：所有测试创建的任务由 created_tasks fixture 在 teardown 统一删除，
避免污染 DB（DELETE /api/v1/tasks/{id}）。
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 数据清理 fixture — 收集创建的 task_id，teardown 统一删除
# ---------------------------------------------------------------------------

@pytest.fixture
def created_tasks(test_client: Any, auth_headers: dict[str, str]) -> list[str]:
    """收集测试创建的 task_id，teardown 时批量删除（防 DB 污染）。

    用法：测试里 _create_task(...) 后把 id append 进 created_tasks。
    """
    task_ids: list[str] = []

    yield task_ids

    # teardown：尽力删除，失败不阻断（任务可能已被测试自身删除）
    for tid in task_ids:
        try:
            test_client.delete(f"/api/v1/tasks/{tid}", headers=auth_headers)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 内部辅助 — 创建任务并返回 ID（自动登记到 created_tasks 清理）
# ---------------------------------------------------------------------------

def _create_task(
    test_client: Any,
    auth_headers: dict[str, str],
    agent_id: str,
    title: str = "E2E 测试任务",
    created_tasks: list[str] | None = None,
    **extra: Any,
) -> str:
    """创建任务并返回任务 ID（供需要 task_id 的测试函数复用）。

    Args:
        test_client: FastAPI TestClient
        auth_headers: 认证请求头
        agent_id: Agent ID
        title: 任务标题
        created_tasks: 清理列表（非 None 时登记 id 供 teardown 删除）
        **extra: 额外字段（如 priority、tags）

    Returns:
        创建的任务 ID
    """
    payload: dict[str, Any] = {"title": title, "agent_id": agent_id}
    payload.update(extra)
    resp = test_client.post("/api/v1/tasks/", json=payload, headers=auth_headers)
    assert resp.status_code == 201, f"创建任务失败: {resp.text}"
    task_id = resp.json()["id"]
    if created_tasks is not None:
        created_tasks.append(task_id)
    return task_id


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_create_task_without_auth(test_client: Any) -> None:
    """无认证创建任务应返回 401。

    验证点：
    - POST /api/v1/tasks/ 无 Token 返回 401
    """
    resp = test_client.post(
        "/api/v1/tasks/",
        json={"title": "test", "agent_id": "some-agent"},
    )
    assert resp.status_code == 401, f"无认证应返回 401，得到 {resp.status_code}"


def test_create_task_without_agent_id_rejected(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """缺少 agent_id 创建任务应被拒绝。

    验证点：
    - POST /api/v1/tasks/ 缺少 agent_id 返回 400
    - 响应体包含错误详情（非空 detail）
    """
    resp = test_client.post(
        "/api/v1/tasks/",
        json={"title": "test task"},
        headers=auth_headers,
    )
    assert resp.status_code == 400, (
        f"缺少 agent_id 应返回 400，得到 {resp.status_code}: {resp.text}"
    )
    # 验证响应体包含诊断信息（兼容 detail 和 error 两种格式）
    resp_data = resp.json()
    has_detail = "detail" in resp_data
    has_error = "error" in resp_data and isinstance(resp_data["error"], dict) and "message" in resp_data["error"]
    assert has_detail or has_error, (
        f"400 响应应包含 detail 或 error.message 字段，得到: {resp_data}"
    )


def test_create_task_pending(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
    created_tasks: list[str],
) -> None:
    """创建任务，初始状态为 pending。

    验证点：
    - POST /api/v1/tasks/ 返回 201
    - 任务 status 为 "pending"
    - 任务 title 与请求一致
    """
    resp = test_client.post(
        "/api/v1/tasks/",
        json={
            "title": "E2E 测试任务",
            "description": "验证任务创建流程",
            "agent_id": available_agent_id,
            "priority": 3,
            "tags": ["e2e", "test"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, f"创建任务失败: {resp.text}"

    task = resp.json()
    assert task["status"] == "pending", f"初始状态应为 pending，得到 {task['status']}"
    assert task["title"] == "E2E 测试任务"
    assert task["id"], "任务 ID 不应为空"
    created_tasks.append(task["id"])


def test_get_task_detail(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
    created_tasks: list[str],
) -> None:
    """获取任务详情。

    验证点：
    - 先创建任务
    - GET /api/v1/tasks/{id} 返回 200
    - 返回的 task id 与创建时一致
    """
    task_id = _create_task(
        test_client, auth_headers, available_agent_id, title="查询测试",
        created_tasks=created_tasks,
    )

    resp = test_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200, f"获取任务详情失败: {resp.text}"

    task = resp.json()
    assert task["id"] == task_id
    assert task["title"] == "查询测试"


def test_task_not_found(test_client: Any, auth_headers: dict[str, str]) -> None:
    """查询不存在的任务返回 404。

    验证点：
    - GET /api/v1/tasks/nonexistent 返回 404
    """
    resp = test_client.get(
        "/api/v1/tasks/nonexistent_id_12345",
        headers=auth_headers,
    )
    assert resp.status_code == 404, f"不存在的任务应返回 404，得到 {resp.status_code}"


def test_task_status_transitions(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
    created_tasks: list[str],
) -> None:
    """任务状态转换：pending → running → completed。

    验证点：
    - 创建任务初始为 pending
    - PATCH 更新为 running
    - PATCH 更新为 completed
    - 每次 GET 验证状态正确
    """
    task_id = _create_task(
        test_client, auth_headers, available_agent_id, title="状态转换测试",
        created_tasks=created_tasks,
    )

    # 验证初始状态
    resp = test_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert resp.json()["status"] == "pending"

    # pending → running
    resp = test_client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "running"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"更新为 running 失败: {resp.text}"
    assert resp.json()["status"] == "running"

    # running → completed
    resp = test_client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "completed"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"更新为 completed 失败: {resp.text}"
    assert resp.json()["status"] == "completed"

    # 最终 GET 验证
    resp = test_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_task_list_with_pagination(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
    created_tasks: list[str],
) -> None:
    """任务列表分页查询。

    验证点：
    - 创建多个任务
    - GET /api/v1/tasks/ 返回分页结果
    - total 字段正确
    """
    for i in range(3):
        _create_task(
            test_client, auth_headers, available_agent_id, title=f"分页测试 {i}",
            created_tasks=created_tasks,
        )

    resp = test_client.get(
        "/api/v1/tasks/?limit=2&offset=0",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取任务列表失败: {resp.text}"

    data = resp.json()
    assert "items" in data, "响应缺少 items"
    assert "total" in data, "响应缺少 total"
    assert data["total"] >= 3, f"total 应 >= 3，得到 {data['total']}"
    assert len(data["items"]) <= 2, f"items 数量应 <= limit=2，得到 {len(data['items'])}"


def test_task_list_filter_by_status(
    test_client: Any,
    auth_headers: dict[str, str],
    available_agent_id: str,
    created_tasks: list[str],
) -> None:
    """任务列表按状态筛选。

    验证点：
    - 创建任务
    - 将状态更新为 completed
    - GET /api/v1/tasks/?status=completed 返回包含该任务
    """
    task_id = _create_task(
        test_client, auth_headers, available_agent_id, title="筛选测试",
        created_tasks=created_tasks,
    )

    test_client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "completed"},
        headers=auth_headers,
    )

    resp = test_client.get(
        "/api/v1/tasks/?status=completed",
        headers=auth_headers,
    )
    assert resp.status_code == 200

    data = resp.json()
    task_ids = [t["id"] for t in data["items"]]
    assert task_id in task_ids, "已完成的任务应出现在 status=completed 的结果中"
