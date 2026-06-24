"""记忆与知识 E2E 测试。

验证记忆的存储 → 检索 → 详情查询 → 删除完整链路。
对应 features.md 场景 6。

测试用例：
- test_list_memories：查询记忆列表
- test_search_memories_hit：搜索记忆命中关键词
- test_search_memories_post：POST 方式搜索记忆
- test_get_memory_detail：获取记忆详情
- test_get_memory_not_found：查询不存在的记忆返回 404
- test_delete_memory：删除记忆
- test_memory_stats：获取记忆统计
- test_list_semantic_memories：获取语义记忆列表
- test_store_then_search_roundtrip：存储 → 搜索回环（通过 API store 单例）
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 内部辅助 — 通过 store 单例预置记忆数据
# ---------------------------------------------------------------------------

def _seed_memory(
    content: str,
    memory_type: str = "semantic",
    tags: list[str] | None = None,
) -> str:
    """通过 MemoryStore 单例创建记忆条目，返回记忆 ID。

    由于记忆 API 没有公开的 POST 创建端点，
    通过底层 store 单例预置数据供 E2E 测试使用。

    Args:
        content: 记忆内容
        memory_type: 记忆类型 (semantic/episode/procedural)
        tags: 标签列表

    Returns:
        创建的记忆 ID
    """
    from channels.api.memory_store import store

    memory = store.create_memory(
        content=content,
        memory_type=memory_type,
        tags=tags or [],
    )
    return memory["id"]


# ---------------------------------------------------------------------------
# 列表查询测试
# ---------------------------------------------------------------------------

def test_list_memories(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """查询记忆列表，返回 200 且结构正确。

    验证点：
    - GET /api/v1/memory/ 返回 200
    - 响应包含 items（列表）和 total（整数）
    """
    resp = test_client.get("/api/v1/memory/", headers=auth_headers)
    assert resp.status_code == 200, f"查询记忆列表失败: {resp.text}"

    data = resp.json()
    assert "items" in data, "响应缺少 items 字段"
    assert "total" in data, "响应缺少 total 字段"
    assert isinstance(data["items"], list), "items 应为列表类型"
    assert isinstance(data["total"], int), "total 应为整数类型"


def test_list_memories_with_type_filter(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """按记忆类型筛选列表。

    验证点：
    - GET /api/v1/memory/?memory_type=semantic 返回 200
    - 响应中所有条目的 memory_type 均为 semantic
    """
    resp = test_client.get(
        "/api/v1/memory/?memory_type=semantic",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"按类型筛选失败: {resp.text}"

    data = resp.json()
    for item in data.get("items", []):
        assert item.get("memory_type") == "semantic", (
            f"筛选结果应全部为 semantic 类型，发现 {item.get('memory_type')}"
        )


# ---------------------------------------------------------------------------
# 搜索测试
# ---------------------------------------------------------------------------

def test_search_memories_hit(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """搜索记忆命中关键词。

    预置一条含特定关键词的记忆，通过 GET 搜索验证命中。

    验证点：
    - GET /api/v1/memory/search?query=xxx 返回 200
    - 搜索结果中包含预置的记忆
    """
    unique_keyword = f"e2e_search_test_{uuid.uuid4().hex[:8]}"
    _seed_memory(
        content=f"This is a test memory about {unique_keyword}",
        memory_type="semantic",
    )

    resp = test_client.get(
        f"/api/v1/memory/search?query={unique_keyword}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"搜索记忆失败: {resp.text}"

    data = resp.json()
    assert data["total"] > 0, "搜索结果应至少有 1 条"
    found = any(
        unique_keyword in item.get("content", "")
        for item in data["items"]
    )
    assert found, f"搜索结果应包含关键词 '{unique_keyword}'"


def test_search_memories_post(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """POST 方式搜索记忆。

    验证点：
    - POST /api/v1/memory/search 返回 200
    - 响应包含 items 和 total
    """
    resp = test_client.post(
        "/api/v1/memory/search",
        json={"query": "test", "top_k": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"POST 搜索失败: {resp.text}"

    data = resp.json()
    assert "items" in data, "POST 搜索响应缺少 items"
    assert "total" in data, "POST 搜索响应缺少 total"


# ---------------------------------------------------------------------------
# 详情查询测试
# ---------------------------------------------------------------------------

def test_get_memory_detail(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """获取记忆详情。

    验证点：
    - GET /api/v1/memory/{memory_id} 返回 200
    - 响应的 id 与请求一致
    """
    memory_id = _seed_memory("e2e detail test memory")
    resp = test_client.get(
        f"/api/v1/memory/{memory_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"获取记忆详情失败: {resp.text}"

    data = resp.json()
    assert data["id"] == memory_id, f"记忆 ID 不匹配: {data.get('id')}"


def test_get_memory_not_found(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """查询不存在的记忆返回 404。

    验证点：
    - GET /api/v1/memory/nonexistent_id 返回 404
    """
    resp = test_client.get(
        "/api/v1/memory/nonexistent_memory_id_xyz",
        headers=auth_headers,
    )
    assert resp.status_code == 404, f"不存在的记忆应返回 404，得到 {resp.status_code}"


# ---------------------------------------------------------------------------
# 删除测试
# ---------------------------------------------------------------------------

def test_delete_memory(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """删除记忆，删除后再查询返回 404。

    验证点：
    - DELETE /api/v1/memory/{memory_id} 返回 200
    - 删除后再 GET 返回 404
    """
    memory_id = _seed_memory("e2e delete test memory")

    del_resp = test_client.delete(
        f"/api/v1/memory/{memory_id}",
        headers=auth_headers,
    )
    assert del_resp.status_code == 200, f"删除记忆失败: {del_resp.text}"

    get_resp = test_client.get(
        f"/api/v1/memory/{memory_id}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404, (
        f"删除后查询应返回 404，得到 {get_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 统计与分类查询
# ---------------------------------------------------------------------------

def test_memory_stats(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """获取记忆统计信息。

    验证点：
    - GET /api/v1/memory/stats 返回 200
    - 响应包含 episode_count, knowledge_count, total_count
    """
    resp = test_client.get("/api/v1/memory/stats", headers=auth_headers)
    assert resp.status_code == 200, f"获取记忆统计失败: {resp.text}"

    data = resp.json()
    assert "episode_count" in data, "响应缺少 episode_count"
    assert "knowledge_count" in data, "响应缺少 knowledge_count"
    assert "total_count" in data, "响应缺少 total_count"


def test_list_semantic_memories(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """获取语义记忆列表。

    验证点：
    - GET /api/v1/memory/semantic 返回 200
    - 响应包含 items 和 total
    """
    resp = test_client.get("/api/v1/memory/semantic", headers=auth_headers)
    assert resp.status_code == 200, f"获取语义记忆列表失败: {resp.text}"

    data = resp.json()
    assert "items" in data, "响应缺少 items"
    assert "total" in data, "响应缺少 total"


# ---------------------------------------------------------------------------
# 存储 → 搜索回环
# ---------------------------------------------------------------------------

def test_store_then_search_roundtrip(
    test_client: Any,
    auth_headers: dict[str, str],
) -> None:
    """存储记忆后通过 API 搜索命中，验证存储 → 检索一致性。

    验证点：
    - 通过 store 预置语义记忆
    - GET 搜索命中该记忆
    - 搜索结果 content 包含预置内容
    """
    unique_content = f"roundtrip_test_content_{uuid.uuid4().hex[:8]}"
    _seed_memory(
        content=f"Roundtrip verification: {unique_content}",
        memory_type="semantic",
        tags=["e2e_test", "roundtrip"],
    )

    resp = test_client.get(
        f"/api/v1/memory/search?query={unique_content}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, f"搜索失败: {resp.text}"

    data = resp.json()
    assert data["total"] > 0, "存储后搜索应命中"

    found = any(
        unique_content in item.get("content", "")
        for item in data["items"]
    )
    assert found, "搜索结果应包含刚存储的内容"


# ---------------------------------------------------------------------------
# 认证测试
# ---------------------------------------------------------------------------

def test_memory_without_auth(test_client: Any) -> None:
    """无 Token 访问记忆 API 应返回 401。

    验证点：
    - GET /api/v1/memory/ 无认证返回 401
    """
    resp = test_client.get("/api/v1/memory/")
    assert resp.status_code == 401, f"无 Token 应返回 401，得到 {resp.status_code}"
