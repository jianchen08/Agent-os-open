"""触发器全链路 E2E 测试。

验证触发器的创建 → 查询列表 → 启停操作 → 删除完整生命周期。
对应 features.md 场景 5。

测试用例：
- test_list_triggers：查询触发器列表
- test_get_trigger_stats：查询触发器统计
- test_create_trigger：创建触发器
- test_get_trigger_detail：获取触发器详情
- test_update_trigger：更新触发器
- test_enable_trigger：启用触发器
- test_disable_trigger：禁用触发器
- test_manual_trigger：手动触发
- test_delete_trigger：删除触发器
- test_trigger_without_auth：无 Token 访问返回 401
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 列表查询测试
# ---------------------------------------------------------------------------

def test_list_triggers(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """查询触发器列表，返回 200 且结构正确。

    验证点：
    - GET /api/v1/triggers/ 返回 200
    - 响应包含 total 和 triggers 字段
    """
    resp = test_client.get("/api/v1/triggers/", headers=auth_headers)
    assert resp.status_code == 200, f"查询触发器列表失败: {resp.text}"

    data = resp.json()
    assert "total" in data, "响应缺少 total 字段"
    assert "triggers" in data, "响应缺少 triggers 字段"
    assert isinstance(data["triggers"], list), "triggers 应为列表类型"


def test_get_trigger_stats(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """查询触发器统计信息。

    验证点：
    - GET /api/v1/triggers/stats 返回 200
    - 响应包含 total_triggers, enabled_triggers, disabled_triggers 等字段
    """
    resp = test_client.get("/api/v1/triggers/stats", headers=auth_headers)
    assert resp.status_code == 200, f"查询触发器统计失败: {resp.text}"

    data = resp.json()
    assert "total_triggers" in data, "响应缺少 total_triggers 字段"
    assert "enabled_triggers" in data, "响应缺少 enabled_triggers 字段"
    assert "disabled_triggers" in data, "响应缺少 disabled_triggers 字段"


# ---------------------------------------------------------------------------
# 触发器 CRUD 测试
# ---------------------------------------------------------------------------

def test_create_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """创建触发器，返回 200 且响应包含消息。

    验证点：
    - POST /api/v1/triggers/ 返回 200
    - 响应包含 id 和 message 字段
    """
    trigger_data = {
        "name": "e2e_test_cron_trigger",
        "type": "schedule",
        "config": {"cron": "0 * * * *"},
        "action": "test_action",
    }
    resp = test_client.post(
        "/api/v1/triggers/",
        json=trigger_data,
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"创建触发器失败: {resp.text}"

    data = resp.json()
    assert "id" in data, "创建响应缺少 id 字段"
    assert "message" in data, "创建响应缺少 message 字段"


def test_get_trigger_detail(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """获取触发器详情。

    验证点：
    - GET /api/v1/triggers/{trigger_id} 返回 200
    - 响应包含 id 字段
    """
    trigger_id = "e2e_test_trigger_001"
    resp = test_client.get(
        f"/api/v1/triggers/{trigger_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取触发器详情失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id, f"触发器 ID 不匹配: {data.get('id')}"


def test_update_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """更新触发器配置。

    验证点：
    - PUT /api/v1/triggers/{trigger_id} 返回 200
    - 响应包含 id 和 message 字段
    """
    trigger_id = "e2e_test_trigger_update"
    update_data = {
        "name": "updated_trigger",
        "config": {"cron": "*/5 * * * *"},
    }
    resp = test_client.put(
        f"/api/v1/triggers/{trigger_id}",
        json=update_data,
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"更新触发器失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id, "更新响应 id 不匹配"
    assert "message" in data, "更新响应缺少 message"


# ---------------------------------------------------------------------------
# 启停操作测试
# ---------------------------------------------------------------------------

def test_enable_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """启用触发器。

    验证点：
    - POST /api/v1/triggers/{trigger_id}/enable 返回 200
    - 响应 enabled 字段为 True
    """
    trigger_id = "e2e_test_trigger_enable"
    resp = test_client.post(
        f"/api/v1/triggers/{trigger_id}/enable",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"启用触发器失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id
    assert data["enabled"] is True, f"启用后 enabled 应为 True，得到 {data['enabled']}"


def test_disable_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """禁用触发器。

    验证点：
    - POST /api/v1/triggers/{trigger_id}/disable 返回 200
    - 响应 enabled 字段为 False
    """
    trigger_id = "e2e_test_trigger_disable"
    resp = test_client.post(
        f"/api/v1/triggers/{trigger_id}/disable",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"禁用触发器失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id
    assert data["enabled"] is False, f"禁用后 enabled 应为 False，得到 {data['enabled']}"


def test_manual_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """手动触发触发器。

    验证点：
    - POST /api/v1/triggers/{trigger_id}/trigger 返回 200
    - 响应 triggered 字段为 True
    """
    trigger_id = "e2e_test_trigger_manual"
    resp = test_client.post(
        f"/api/v1/triggers/{trigger_id}/trigger",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"手动触发失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id
    assert data["triggered"] is True, f"手动触发后 triggered 应为 True，得到 {data['triggered']}"


# ---------------------------------------------------------------------------
# 删除测试
# ---------------------------------------------------------------------------

def test_delete_trigger(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """删除触发器。

    验证点：
    - DELETE /api/v1/triggers/{trigger_id} 返回 200
    - 响应包含 message 和 id 字段
    """
    trigger_id = "e2e_test_trigger_delete"
    resp = test_client.delete(
        f"/api/v1/triggers/{trigger_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"删除触发器失败: {resp.text}"

    data = resp.json()
    assert data["id"] == trigger_id, "删除响应 id 不匹配"
    assert "message" in data, "删除响应缺少 message"


# ---------------------------------------------------------------------------
# 认证测试
# ---------------------------------------------------------------------------

def test_trigger_without_auth(test_client: Any) -> None:
    """无 Token 访问触发器 API 应返回 401。

    验证点：
    - GET /api/v1/triggers/ 无认证返回 401
    """
    resp = test_client.get("/api/v1/triggers/")
    assert resp.status_code == 401, f"无 Token 应返回 401，得到 {resp.status_code}"
