#!/usr/bin/env python3
"""前端功能对齐 P2+P8 后端验证 — 可复现脚本。

覆盖场景（对应 docs/working/frontend_ui_alignment_function_verify_report.md）：
  P2 搜索框合并-后端部分：
    S1 type=session -> 200 返回匹配会话列表（patch 存储层）
    S2 type=message -> 200 返回匹配消息列表（patch 存储层）
    S3 type=all    -> 200 同时返回会话与消息
    S4 q 空/纯空白 -> 200 空结果
    S5 type 非法   -> 422（VAL_ENUM_7002）
    S6 无 token    -> 401
    S7 limit 越界  -> 422（ge=1 le=100）
    S8 存储层 helper 直接验证（_search_sessions 按用户过滤 + _search_messages 子串匹配）
  P8 模型显示（等价逻辑核对，非 TS 实测——node 环境不可用）：
    M1 model='large' + tiers.large='deepseek-chat' -> 'deepseek-chat'
    M2 具体模型名原样返回
    M3 空值 -> 空串
    M4 tiers 缺失/键缺失/键值为空 -> 回退原值

用法：
    env PYTHONPATH=src python3 verify_reproduce.py

预期输出：全部 PASS。

[来源: docs/working/frontend_ui_alignment_function_verify_report.md]
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# 项目 src 目录（脚本位于项目根）
SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import APIError, api_error_handler, require_auth
from channels.api.routes_search import router

_results: list[tuple[str, str, bool]] = []


def record(name: str, detail: str, ok: bool) -> None:
    _results.append((name, detail, ok))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}: {detail}")


# ── 测试客户端（覆盖认证依赖 + 异常处理） ──────────────────────────


def make_client(authed: bool = True) -> TestClient:
    app = FastAPI()
    if authed:
        async def _mock_auth():
            return {"sub": "test_user", "username": "tester"}

        app.dependency_overrides[require_auth] = _mock_auth
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(router)
    return TestClient(app)


# ── S1-S7: HTTP 端点全场景 ──────────────────────────────────────


def s1_session_search() -> None:
    """type=session -> 200 返回匹配会话列表。"""
    with patch(
        "channels.api.routes_search._search_sessions",
        return_value=[
            {"id": "t1", "title": "项目计划讨论", "updated_at": "", "message_count": 3}
        ],
    ):
        resp = make_client().get("/api/v1/search", params={"q": "计划", "type": "session"})
        ok = (
            resp.status_code == 200
            and resp.json()["type"] == "session"
            and len(resp.json()["sessions"]) == 1
            and resp.json()["sessions"][0]["id"] == "t1"
            and resp.json()["messages"] == []
        )
        record("S1 type=session", f"status={resp.status_code} body={resp.json()}", ok)


def s2_message_search() -> None:
    """type=message -> 200 返回匹配消息列表。"""
    with patch(
        "channels.api.routes_search._search_messages",
        return_value=[
            {
                "id": "r2",
                "session_id": "pipe-1",
                "role": "assistant",
                "content": "分析结果：存在内存泄漏",
                "timestamp": "",
                "sequence": 1,
            }
        ],
    ):
        resp = make_client().get("/api/v1/search", params={"q": "内存泄漏", "type": "message"})
        data = resp.json()
        ok = (
            resp.status_code == 200
            and len(data["messages"]) == 1
            and data["messages"][0]["session_id"] == "pipe-1"
            and data["sessions"] == []
        )
        record("S2 type=message", f"status={resp.status_code} body={data}", ok)


def s3_all_search() -> None:
    """type=all -> 200 同时返回会话与消息。"""
    with (
        patch(
            "channels.api.routes_search._search_sessions",
            return_value=[{"id": "t1", "title": "测试", "updated_at": "", "message_count": 1}],
        ),
        patch(
            "channels.api.routes_search._search_messages",
            return_value=[
                {"id": "r1", "session_id": "pipe-1", "role": "assistant",
                 "content": "测试内容", "timestamp": "", "sequence": 1}
            ],
        ),
    ):
        resp = make_client().get("/api/v1/search", params={"q": "测试", "type": "all"})
        data = resp.json()
        ok = (
            resp.status_code == 200
            and data["type"] == "all"
            and len(data["sessions"]) == 1
            and len(data["messages"]) == 1
        )
        record("S3 type=all", f"status={resp.status_code} body={data}", ok)


def s4_empty_q() -> None:
    """q 空 -> 200 空结果（不报错）。"""
    resp = make_client().get("/api/v1/search", params={"q": "", "type": "session"})
    data = resp.json()
    ok = resp.status_code == 200 and data["sessions"] == [] and data["messages"] == []
    record("S4 q 空", f"status={resp.status_code} body={data}", ok)

    # 纯空白 q
    resp2 = make_client().get("/api/v1/search", params={"q": "   ", "type": "all"})
    ok2 = resp2.status_code == 200 and resp2.json()["sessions"] == [] and resp2.json()["messages"] == []
    record("S4b q 纯空白", f"status={resp2.status_code} body={resp2.json()}", ok2)


def s5_invalid_type() -> None:
    """type 非法 -> 422。"""
    resp = make_client().get("/api/v1/search", params={"q": "x", "type": "invalid"})
    ok = resp.status_code == 422
    record("S5 type 非法", f"status={resp.status_code} body={resp.text[:120]}", ok)


def s6_unauthorized() -> None:
    """无 token -> 401。"""
    resp = make_client(authed=False).get("/api/v1/search", params={"q": "x"})
    ok = resp.status_code == 401
    record("S6 无 token", f"status={resp.status_code} body={resp.text[:120]}", ok)


def s7_limit_out_of_range() -> None:
    """limit 越界 -> 422（ge=1 le=100）。"""
    resp = make_client().get("/api/v1/search", params={"q": "x", "type": "all", "limit": 0})
    ok = resp.status_code == 422
    record("S7 limit=0 越界", f"status={resp.status_code}", ok)

    resp2 = make_client().get("/api/v1/search", params={"q": "x", "type": "all", "limit": 101})
    ok2 = resp2.status_code == 422
    record("S7b limit=101 越界", f"status={resp2.status_code}", ok2)


# ── S8: 存储层 helper 直接验证（mock 数据驱动模块调用链） ───────────


def _make_thread(thread_id: str, title: str, user_id: str = "test_user") -> dict:
    return {
        "id": thread_id,
        "user_id": user_id,
        "title": title,
        "intent": title,
        "agent_id": "agentos",
        "metadata": {"session_type": "main_pipeline"},
        "current_state": "active",
        "pipeline_ids": [],
        "active_pipeline_id": "",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }


class _FakeRecord:
    def __init__(self, record_id: str, pipeline_run_id: str, content: str):
        self.record_id = record_id
        self.pipeline_run_id = pipeline_run_id
        self.content = content
        self.type = "ai"
        self.role = "assistant"
        self.sequence = 1
        self.created_at = "2026-08-01T00:00:00"


class _FakeStorage:
    def __init__(self, records: list[_FakeRecord]):
        self._records = {f"{r.record_id}::{r.sequence}": r for r in records}

    def search_records(self, keyword: str, limit: int = 50) -> list[_FakeRecord]:
        needle = keyword.lower()
        hits = [r for r in self._records.values() if needle in (r.content or "").lower()]
        return hits[:limit]


def s8_helpers() -> None:
    """_search_sessions 按用户过滤 + _search_messages 子串匹配。"""
    store = type("FakeStore", (), {"threads": {}})()
    store.threads = {
        "t1": _make_thread("t1", "项目计划讨论", user_id="test_user"),
        "t2": _make_thread("t2", "计划周报", user_id="other_user"),
        "t3": _make_thread("t3", "无关话题", user_id="test_user"),
    }
    with patch("channels.api.memory_store.store", store):
        from channels.api.routes_search import _search_sessions

        hits = _search_sessions("test_user", "计划", limit=10)
        ids = [h["id"] for h in hits]
        ok = ids == ["t1"]  # t2 属 other_user 不返回
        record("S8a _search_sessions 按用户过滤", f"ids={ids} (期望 ['t1'])", ok)

    storage = _FakeStorage(
        [
            _FakeRecord("r1", "pipe-1", "分析代码性能"),
            _FakeRecord("r2", "pipe-2", "无关内容"),
        ]
    )
    with patch("channels.api.routes_search._get_storage", return_value=storage):
        from channels.api.routes_search import _search_messages

        hits = _search_messages("代码", limit=10)
        ok2 = len(hits) == 1 and hits[0]["id"] == "r1"
        record("S8b _search_messages 子串匹配", f"hits={[h['id'] for h in hits]} (期望 ['r1'])", ok2)

    # 存储不可用 -> 空列表（不抛 500）
    with patch("channels.api.routes_search._get_storage", return_value=None):
        from channels.api.routes_search import _search_messages

        hits = _search_messages("代码", limit=10)
        ok3 = hits == []
        record("S8c 存储不可用回退空", f"hits={hits} (期望 [])", ok3)


# ── M1-M4: P8 模型名解析等价逻辑核对（与 modelName.ts 逐行一致） ────


def resolve_model_display_name(model, tiers):
    """与 frontend/src/utils/modelName.ts resolveModelDisplayName 等价：
    - model 为空 -> ''
    - tiers 为空 -> 原值
    - model 命中 tiers 键且值非空白 -> 映射值
    - 否则原样返回
    """
    if not model:
        return ""
    if not tiers:
        return model
    resolved = tiers.get(model)
    if resolved and resolved.strip():
        return resolved
    return model


def m_model_name_mapping() -> None:
    # M1: 分级键 large -> deepseek-chat
    out = resolve_model_display_name("large", {"large": "deepseek-chat", "medium": "gpt-4o"})
    record("M1 large -> deepseek-chat", f"out={out!r}", out == "deepseek-chat")

    # M2: 具体模型名原样返回
    out = resolve_model_display_name("deepseek-chat", {"large": "deepseek-chat"})
    record("M2 具体名原样", f"out={out!r}", out == "deepseek-chat")

    # M3: 空值 -> 空串
    out = resolve_model_display_name("", {"large": "deepseek-chat"})
    record("M3 空值 -> 空串", f"out={out!r}", out == "")
    out = resolve_model_display_name(undefined := None, {"large": "deepseek-chat"})
    record("M3b undefined -> 空串", f"out={out!r}", out == "")

    # M4: tiers 缺失 / 键缺失 / 键值为空 -> 回退原值
    out = resolve_model_display_name("large", None)
    record("M4 tiers 缺失回退原值", f"out={out!r}", out == "large")
    out = resolve_model_display_name("large", {"medium": "gpt-4o"})
    record("M4b 键缺失回退原值", f"out={out!r}", out == "large")
    out = resolve_model_display_name("large", {"large": "   "})
    record("M4c 键值为空白回退原值", f"out={out!r}", out == "large")


def main() -> None:
    print("=" * 60)
    print("P2 搜索 API 实测")
    print("=" * 60)
    s1_session_search()
    s2_message_search()
    s3_all_search()
    s4_empty_q()
    s5_invalid_type()
    s6_unauthorized()
    s7_limit_out_of_range()
    s8_helpers()

    print()
    print("=" * 60)
    print("P8 模型名解析（等价逻辑核对，node 不可用非 TS 实测）")
    print("=" * 60)
    m_model_name_mapping()

    print()
    print("=" * 60)
    failed = [r for r in _results if not r[2]]
    print(f"总计: {len(_results)}  通过: {len(_results) - len(failed)}  失败: {len(failed)}")
    if failed:
        for name, detail, _ in failed:
            print(f"  FAIL: {name}: {detail}")
        sys.exit(1)
    print("全部 PASS ✅")


if __name__ == "__main__":
    main()
