"""审批交互 E2E 测试。

验证审批请求的创建 → 查询 → 反馈（approve/reject）→ 取消完整生命周期。
对应 features.md 场景 8。

测试用例：
- test_create_review：创建审批请求
- test_get_review_detail：获取审批详情
- test_list_reviews_by_task：按任务 ID 查询审批列表
- test_mark_review_viewed：标记审批为已查看
- test_submit_approved_feedback：提交批准反馈
- test_submit_rejected_feedback：提交拒绝反馈
- test_cancel_review：取消审批
- test_approval_without_auth：无 Token 访问返回 401
"""

from __future__ import annotations

import uuid
from typing import Any


# ---------------------------------------------------------------------------
# 内部辅助 — 创建审批请求并返回 review_id
# ---------------------------------------------------------------------------

def _create_review(
    client: Any,
    headers: dict[str, str],
    task_id: str | None = None,
    title: str | None = None,
) -> str:
    """通过 API 创建审批请求，返回 review_id。

    Args:
        client: FastAPI TestClient
        headers: 认证头
        task_id: 任务 ID（默认自动生成）
        title: 审批标题

    Returns:
        创建的审批 ID
    """
    body = {
        "task_id": task_id or f"e2e_task_{uuid.uuid4().hex[:8]}",
        "thread_id": f"e2e_thread_{uuid.uuid4().hex[:8]}",
        "session_id": f"e2e_session_{uuid.uuid4().hex[:8]}",
        "tab_id": "e2e_tab_01",
        "title": title or "E2E 测试审批",
        "description": "由 E2E 测试自动创建的审批请求",
        "priority": "normal",
    }
    resp = client.post("/api/v1/reviews/", json=body, headers=headers)
    assert resp.status_code == 200, f"创建审批失败: {resp.text}"
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 创建审批测试
# ---------------------------------------------------------------------------

def test_create_review(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """创建审批请求，返回审批详情。

    验证点：
    - POST /api/v1/reviews/ 返回 200
    - 响应包含 id, task_id, title, status 等字段
    - status 为 pending（待审批）
    """
    task_id = f"e2e_task_{uuid.uuid4().hex[:8]}"
    body = {
        "task_id": task_id,
        "thread_id": "e2e_thread_create",
        "session_id": "e2e_session_create",
        "tab_id": "e2e_tab_create",
        "title": "创建审批测试",
        "description": "验证审批创建功能",
        "priority": "high",
    }
    resp = test_client.post("/api/v1/reviews/", json=body, headers=auth_headers)
    assert resp.status_code == 200, f"创建审批失败: {resp.text}"

    data = resp.json()
    assert "id" in data, "创建响应缺少 id 字段"
    assert data["task_id"] == task_id, f"task_id 不匹配: {data.get('task_id')}"
    assert data["title"] == "创建审批测试", f"title 不匹配: {data.get('title')}"


# ---------------------------------------------------------------------------
# 详情查询测试
# ---------------------------------------------------------------------------

def test_get_review_detail(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """获取审批详情。

    验证点：
    - GET /api/v1/reviews/{review_id} 返回 200
    - 响应 id 与创建时一致
    """
    review_id = _create_review(test_client, auth_headers, title="详情查询测试")

    resp = test_client.get(
        f"/api/v1/reviews/{review_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取审批详情失败: {resp.text}"

    data = resp.json()
    assert data["id"] == review_id, f"审批 ID 不匹配: {data.get('id')}"


def test_get_review_not_found(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """查询不存在的审批返回错误响应。

    验证点：
    - GET /api/v1/reviews/nonexistent_id 返回包含 error 的响应
    """
    resp = test_client.get(
        "/api/v1/reviews/nonexistent_review_id_xyz",
        headers=auth_headers,
    )
    data = resp.json()
    assert "error" in data, "查询不存在的审批应返回 error 信息"


# ---------------------------------------------------------------------------
# 列表查询测试
# ---------------------------------------------------------------------------

def test_list_reviews_by_task(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """按任务 ID 查询审批列表。

    验证点：
    - 先创建审批（关联指定 task_id）
    - GET /api/v1/reviews/?task_id=xxx 返回 200
    - 响应包含 items 列表，且列表中包含刚创建的审批
    """
    task_id = f"e2e_list_task_{uuid.uuid4().hex[:8]}"
    review_id = _create_review(
        test_client, auth_headers, task_id=task_id, title="列表查询测试"
    )

    resp = test_client.get(
        f"/api/v1/reviews/?task_id={task_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取审批列表失败: {resp.text}"

    data = resp.json()
    assert "items" in data, "列表响应缺少 items"
    assert data["total"] > 0, "审批列表应包含至少 1 条记录"

    found_ids = [item.get("id") for item in data["items"]]
    assert review_id in found_ids, (
        f"列表中应包含刚创建的审批 {review_id}，实际: {found_ids}"
    )


def test_list_reviews_empty_without_task_id(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """不传 task_id 时返回空列表。

    验证点：
    - GET /api/v1/reviews/ 不传 task_id 返回 200
    - total 为 0，items 为空列表
    """
    resp = test_client.get("/api/v1/reviews/", headers=auth_headers)
    assert resp.status_code == 200, f"获取审批列表失败: {resp.text}"

    data = resp.json()
    assert data["total"] == 0, "不传 task_id 时 total 应为 0"


# ---------------------------------------------------------------------------
# 标记已查看测试
# ---------------------------------------------------------------------------

def test_mark_review_viewed(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """标记审批为已查看。

    验证点：
    - POST /api/v1/reviews/{review_id}/viewed 返回 200
    - 响应包含 id 和 viewed 字段
    """
    review_id = _create_review(test_client, auth_headers, title="标记已查看测试")

    resp = test_client.post(
        f"/api/v1/reviews/{review_id}/viewed",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"标记已查看失败: {resp.text}"

    data = resp.json()
    assert data["id"] == review_id
    assert "viewed" in data, "响应缺少 viewed 字段"


# ---------------------------------------------------------------------------
# 审批反馈测试
# ---------------------------------------------------------------------------

def test_submit_approved_feedback(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """提交批准反馈。

    验证点：
    - POST /api/v1/reviews/{review_id}/feedback 返回 200
    - 响应 response_type 为 approved
    """
    review_id = _create_review(test_client, auth_headers, title="批准反馈测试")

    # 先标记为已查看（反馈前通常需要查看）
    test_client.post(
        f"/api/v1/reviews/{review_id}/viewed",
        headers=auth_headers,
    )

    feedback_body = {
        "response_type": "approved",
        "overall_comment": "E2E 测试自动批准",
    }
    resp = test_client.post(
        f"/api/v1/reviews/{review_id}/feedback",
        json=feedback_body,
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"提交批准反馈失败: {resp.text}"

    data = resp.json()
    assert data.get("response_type") == "approved", (
        f"反馈类型应为 approved，得到 {data.get('response_type')}"
    )


def test_submit_rejected_feedback(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """提交拒绝反馈。

    验证点：
    - POST /api/v1/reviews/{review_id}/feedback 返回 200
    - 响应 response_type 为 rejected
    """
    review_id = _create_review(test_client, auth_headers, title="拒绝反馈测试")

    test_client.post(
        f"/api/v1/reviews/{review_id}/viewed",
        headers=auth_headers,
    )

    feedback_body = {
        "response_type": "rejected",
        "overall_comment": "E2E 测试自动拒绝",
    }
    resp = test_client.post(
        f"/api/v1/reviews/{review_id}/feedback",
        json=feedback_body,
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"提交拒绝反馈失败: {resp.text}"

    data = resp.json()
    assert data.get("response_type") == "rejected", (
        f"反馈类型应为 rejected，得到 {data.get('response_type')}"
    )


# ---------------------------------------------------------------------------
# 取消审批测试
# ---------------------------------------------------------------------------

def test_cancel_review(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """取消审批。

    验证点：
    - POST /api/v1/reviews/{review_id}/cancel 返回 200
    - 响应包含 id 和 cancelled 字段
    """
    review_id = _create_review(test_client, auth_headers, title="取消审批测试")

    resp = test_client.post(
        f"/api/v1/reviews/{review_id}/cancel",
        json={"reason": "E2E 测试取消"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"取消审批失败: {resp.text}"

    data = resp.json()
    assert data["id"] == review_id
    assert "cancelled" in data, "响应缺少 cancelled 字段"


# ---------------------------------------------------------------------------
# 认证测试
# ---------------------------------------------------------------------------

def test_approval_without_auth(test_client: Any) -> None:
    """无 Token 访问审批 API 应返回 401。

    验证点：
    - GET /api/v1/reviews/ 无认证返回 401
    """
    resp = test_client.get("/api/v1/reviews/")
    assert resp.status_code == 401, f"无 Token 应返回 401，得到 {resp.status_code}"
