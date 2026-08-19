"""调试中心数据链行为测试（2026-08-19 修复批次）。

背景：execution/users 域 handler 原为 routes_missing 空 stub，调试中心
「执行记录/会话/用户」页恒空。修复后从内核能力（messages.list /
pipeline-runs.list / db-admin.table_query）拼装真实数据。

行为断言（注入假 provider，不依赖真内核）：
- 会话列表 = runs（时间倒序去重）⨝ state 摘要（标题/消息数）；
- 执行记录 = message_slots⨝blobs 的消息快照拼装（全文/tool_calls 解析/最新在前/
  offset 窗口），全会话模式跨会话按时间倒序合并；
- 用户列表脱敏（password 绝不出口）+ user_id→id 映射 + 统计计数；
- 内核能力不可用时降级空结构（HTTP 200 空载荷契约）。
"""

from __future__ import annotations

import tests._channel_api_path  # noqa: F401,I001 — 注入 channel_api 插件目录到 sys.path
import tests._isolation_path  # noqa: F401,I001 — 注入 system 目录（routes_missing 依赖 tasks 包）

import kernel_reads
import routes_missing as rm

# ── 假内核数据（形状对齐 PipelineRunInfo / MessageRecord / state 摘要 / users 行）──

_FAKE_RUNS = [
    {
        "run_id": "run-2", "pipeline_id": "pipeB", "thread_id": "thread-b",
        "status": "completed", "started_at": "2026-08-19T09:30:00Z",
        "ended_at": "2026-08-19T09:31:00Z",
    },
    {
        "run_id": "run-1b", "pipeline_id": "pipeA", "thread_id": "thread-a",
        "status": "running", "started_at": "2026-08-19T09:20:00Z",
        "ended_at": None,
    },
    {
        "run_id": "run-1a", "pipeline_id": "pipeA", "thread_id": "thread-a",
        "status": "completed", "started_at": "2026-08-19T08:00:00Z",
        "ended_at": "2026-08-19T08:05:00Z",
    },
]

_FAKE_STATES = [
    {"pipeline_id": "pipeA", "display_name": "灵汐", "message_count": 2},
    {"pipeline_id": "pipeB", "message_count": 3},
]

_FAKE_MESSAGES = {
    "pipeA": [
        {
            "message_id": "m-1", "run_id": "run-1a", "seq_in_branch": 1, "role": "user",
            "content_preview": "画一个贪吃蛇", "created_at": "2026-08-19T08:00:01Z",
            "pipeline_id": "pipeA",
        },
        {
            "message_id": "m-2", "run_id": "run-1a", "seq_in_branch": 2, "role": "assistant",
            "content_preview": "好的，开始编写", "created_at": "2026-08-19T08:00:05Z",
            "tool_calls_json": '[{"id": "tc1", "function": {"name": "write_file"}}]',
            "reasoning_content": "先想一下结构",
            "pipeline_id": "pipeA",
        },
    ],
    "pipeB": [
        {
            "message_id": "m-3", "run_id": "run-2", "seq_in_branch": 1, "role": "tool",
            "content_preview": "Error: boom", "created_at": "2026-08-19T09:30:10Z",
            "status": "failed", "error": "boom", "pipeline_id": "pipeB",
        },
    ],
}

_FAKE_USERS_RESULT = {
    "table": "users", "total": 2, "limit": 100, "offset": 0,
    "rows": [
        {
            "user_id": "u-1", "username": "admin", "password": "HASH_NEVER_LEAK",
            "email": "a@x.com", "role": "admin", "tenant_id": "default",
            "created_at": "2026-01-01T00:00:00Z", "last_login_at": "2026-08-19T01:00:00Z",
        },
        {
            "user_id": "u-2", "username": "bob", "password": "HASH2",
            "email": None, "role": "user", "tenant_id": "default",
            "created_at": "2026-02-01T00:00:00Z", "last_login_at": None,
        },
    ],
}


def _install_fake_providers() -> None:
    async def fake_pipeline_runs(status=None, limit=100):
        return _FAKE_RUNS[:limit]

    async def fake_messages(pipeline_id, limit=None):
        msgs = _FAKE_MESSAGES.get(pipeline_id, [])
        return msgs[:limit] if limit else msgs

    async def fake_states():
        return _FAKE_STATES

    async def fake_query_table(table, limit=50, offset=0, authorization=""):
        assert table == "users"
        return _FAKE_USERS_RESULT

    kernel_reads.set_provider("pipeline-runs", fake_pipeline_runs)
    kernel_reads.set_provider("messages", fake_messages)
    kernel_reads.set_provider("pipeline-state", fake_states)
    kernel_reads.set_provider("db-admin", fake_query_table)


async def test_sessions_assembled_from_runs_and_states():
    _install_fake_providers()
    result = await rm.get_execution_record_sessions()
    sessions = result["sessions"]
    # pipeA 两次 run 去重为一条；按 started_at 倒序 pipeB 在前
    assert [s["id"] for s in sessions] == ["pipeB", "pipeA"]
    assert result["total"] == 2
    pipe_a = sessions[1]
    assert pipe_a["title"] == "灵汐"  # state.display_name 优先
    assert pipe_a["record_count"] == 2  # state.message_count
    assert pipe_a["updated_at"] == "2026-08-19T09:20:00Z"  # 最新 run 未结束 → 回退 started_at
    assert pipe_a["run_status"] == "running"  # 最新 run 状态
    # pipeB 无 display_name → 回退 thread_id
    assert sessions[0]["title"] == "thread-b"


async def test_records_assemble_message_snapshot_per_session():
    _install_fake_providers()
    result = await rm.list_execution_records(session_id="pipeA")
    records = result["records"]
    assert result["total"] == 2
    # 最新在前（内核 seq 升序读入后反转）
    assert records[0]["id"] == "m-2"
    assert records[0]["record_type"] == "assistant"
    assert records[0]["status"] == "completed"
    snap = records[0]["message_data"]
    assert snap["content"] == "好的，开始编写"
    assert snap["tool_calls"][0]["function"]["name"] == "write_file"  # JSON 已解析
    assert snap["reasoning_content"] == "先想一下结构"
    # 工具失败态透传
    failed = await rm.list_execution_records(session_id="pipeB")
    assert failed["records"][0]["status"] == "failed"
    assert failed["records"][0]["message_data"]["error"] == "boom"
    # offset/limit 窗口
    paged = await rm.list_execution_records(session_id="pipeA", limit=1, offset=1)
    assert [r["id"] for r in paged["records"]] == ["m-1"]


async def test_records_all_sessions_merged_desc():
    _install_fake_providers()
    result = await rm.list_execution_records()
    ids = [r["id"] for r in result["records"]]
    assert ids == ["m-3", "m-2", "m-1"]  # 跨会话按 created_at 倒序
    assert all(r["session_id"] in ("pipeA", "pipeB") for r in result["records"])


async def test_get_record_found_and_not_found():
    _install_fake_providers()
    found = await rm.get_execution_record("m-3")
    assert found["id"] == "m-3"
    assert found["session_id"] == "pipeB"
    missing = await rm.get_execution_record("no-such")
    assert missing["message_data"] == {}


async def test_users_password_never_exposed():
    _install_fake_providers()
    users = await rm.list_users(skip=0, limit=100, authorization="Bearer x")
    assert len(users) == 2
    for u in users:
        assert "password" not in u
        assert u["id"] in ("u-1", "u-2")
    assert users[0]["username"] == "admin"
    assert users[0]["is_active"] is True


async def test_user_stats_counts():
    _install_fake_providers()
    stats = await rm.get_user_stats(authorization="Bearer x")
    assert stats == {"total_users": 2, "active_users": 2, "admin_count": 1}


async def test_degraded_to_empty_without_providers():
    kernel_reads.reset_providers()
    sessions = await rm.get_execution_record_sessions()
    assert sessions == {"sessions": [], "total": 0}
    records = await rm.list_execution_records(session_id="pipeA")
    assert records["records"] == []
    users = await rm.list_users()
    assert users == []
