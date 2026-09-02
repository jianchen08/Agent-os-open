# @feature: FP-0.2.二 可观测性 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring 插件 execution/records + sessions token-usage 域测试。

覆盖（对齐原 tests 语义 + 新 http.handle 分发层）：
1. GET /execution/records —— 单会话模式（seq 升序读入 → 最新在前）+ 全会话模式
   （最近 N 个去重管道消息拼装全局倒序，同管道多 run 去重）
2. GET /execution/records/sessions —— pipeline-runs + pipeline-state 聚合（标题/消息数）
3. GET group-summary / tree/{sid} / {rid}/children / {rid} —— 空结构与单条查找
4. POST clear-all —— 全量清理（内核 9 表 + registry + payload_diag 快照文件）
5. GET /sessions/{sid}/total-token-usage —— state 行 token 累计 + run 请求数
6. GET /sessions/{sid}/context-token-usage —— 估算形态（is_estimated）
7. 降级路径：provider 未注入 → HTTP 200 空载荷（前端契约不破坏）
8. 404 未知路由 / limit 非法回退 / _on_load 能力注入（get_capability 桩）

数据源为 kernel_reads 能力桥（pipeline-runs.list/messages.list/pipeline-state.list），
测试用 fake provider 注入，不接真实内核。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "monitoring"


def _load_server() -> Any:
    """动态加载 monitoring/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "monitoring_exec_records_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["monitoring_exec_records_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _load_kernel_reads() -> Any:
    """加载插件内 kernel_reads 桥（provider 注册表目标）。"""
    import kernel_reads  # conftest 已注入插件目录 sys.path

    return kernel_reads


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, **kwargs: Any) -> dict[str, Any]:
    """同步调用 http.handle（测试侧统一 asyncio 跑）。"""
    return _run(server.http_handle(**kwargs))


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


# ── fake provider 数据 ────────────────────────────────────────────────────

_MSG_USER = {
    "message_id": "m1",
    "pipeline_id": "p1",
    "role": "user",
    "seq_in_branch": 1,
    "content_preview": "你好内核",
    "created_at": "2026-08-21T00:00:01Z",
    "status": "completed",
    "run_id": "r1",
}
_MSG_ASSISTANT = {
    "message_id": "m2",
    "pipeline_id": "p1",
    "role": "assistant",
    "seq_in_branch": 2,
    "content_preview": "你好，我是助手",
    "created_at": "2026-08-21T00:00:05Z",
    "tool_calls_json": '[{"id": "call_1", "name": "demo"}]',
    "tool_result_json": '{"ok": true}',
    "reasoning_content": "思考中",
    "status": "completed",
    "run_id": "r1",
}
_MSG_OTHER = {
    "message_id": "m3",
    "pipeline_id": "p2",
    "role": "user",
    "seq_in_branch": 1,
    "content_preview": "另一个会话",
    "created_at": "2026-08-21T01:00:00Z",
    "status": "completed",
    "run_id": "r2",
}
_RUN_P1 = {
    "run_id": "r1",
    "pipeline_id": "p1",
    "thread_id": "t1",
    "status": "completed",
    "started_at": "2026-08-21T00:00:00Z",
    "ended_at": "2026-08-21T00:01:00Z",
    "total_tokens": 100,
    "total_seconds": 60,
}
_RUN_P2 = {
    "run_id": "r2",
    "pipeline_id": "p2",
    "thread_id": "t2",
    "status": "completed",
    "started_at": "2026-08-21T01:00:00Z",
    "ended_at": "2026-08-21T01:00:10Z",
    "total_tokens": 50,
    "total_seconds": 10,
}
_STATE_P1 = {
    "pipeline_id": "p1",
    "display_name": "会话甲",
    "message_count": 2,
    "track.total_tokens": 100,
}
_STATE_P2 = {
    "pipeline_id": "p2",
    "display_name": "会话乙",
    "message_count": 1,
    "track.total_tokens": 50,
}


def install_providers(
    kr: Any,
    runs: list[dict[str, Any]] | None = None,
    messages: dict[str, list[dict[str, Any]]] | None = None,
    states: list[dict[str, Any]] | None = None,
) -> None:
    """注入 fake 内核读 provider（pipeline-runs/messages/pipeline-state）。"""
    kr.reset_providers()
    runs = runs or []

    async def _pipe_runs(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [r for r in runs if (not status or r.get("status") == status)]
        return rows[:limit]

    async def _msgs(pipeline_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        rows = (messages or {}).get(pipeline_id or "", [])
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def _states() -> list[dict[str, Any]]:
        return states or []

    kr.set_provider("pipeline-runs", _pipe_runs)
    kr.set_provider("messages", _msgs)
    kr.set_provider("pipeline-state", _states)


@pytest.fixture
def kr() -> Any:
    """kernel_reads 桥实例（每测试重置 provider 表）。"""
    mod = _load_kernel_reads()
    mod.reset_providers()
    yield mod
    mod.reset_providers()


# ── execution/records：列表 ───────────────────────────────────────────────


def test_records_single_session_mode(server: Any, kr: Any) -> None:
    """单会话模式：seq 升序读入 → newest-first 呈现，message_data 拼装解析。"""
    install_providers(kr, messages={"p1": [_MSG_USER, _MSG_ASSISTANT]})
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records",
        method="GET", query={"session_id": "p1"},
    ))
    assert status == 200
    assert body["session_id"] == "p1"
    assert body["total"] == 2
    # 反序：assistant（seq2）在前
    assert body["records"][0]["id"] == "m2"
    assert body["records"][1]["id"] == "m1"
    rec0 = body["records"][0]
    assert rec0["record_type"] == "assistant"
    assert rec0["depth"] == 0
    # tool_calls_json / tool_result_json / reasoning_content 解析为结构化
    md = rec0["message_data"]
    assert md["tool_calls"] == [{"id": "call_1", "name": "demo"}]
    assert md["tool_result"] == {"ok": True}
    assert md["reasoning_content"] == "思考中"
    assert md["role"] == "assistant"


def test_records_single_session_limit_offset(server: Any, kr: Any) -> None:
    """单会话模式分页：offset/limit 切片在反序后的列表上。"""
    install_providers(kr, messages={"p1": [dict(_MSG_USER, message_id="m1"), dict(_MSG_USER, message_id="m2")]})
    _, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records",
        method="GET", query={"session_id": "p1", "limit": "1", "offset": "1"},
    ))
    assert body["total"] == 2
    assert [r["id"] for r in body["records"]] == ["m1"]


def test_records_all_sessions_mode(server: Any, kr: Any) -> None:
    """全会话模式：最近 N 个去重管道消息拼装，全局 created_at 倒序。"""
    install_providers(
        kr,
        runs=[dict(_RUN_P1, pipeline_id="p1"), dict(_RUN_P1, pipeline_id="p1", run_id="r1b"), _RUN_P2],
        messages={"p1": [_MSG_USER, _MSG_ASSISTANT], "p2": [_MSG_OTHER]},
    )
    status, body = _decode_http(_call(server, path="/ext/monitoring/execution/records", method="GET"))
    assert status == 200
    assert body["session_id"] is None
    assert body["total"] == 3
    # 同管道多 run 去重（r1b 不重复取消息），全局倒序：p2(01:00) 在前
    assert [r["id"] for r in body["records"]] == ["m3", "m2", "m1"]


def test_records_unknown_session_empty(server: Any, kr: Any) -> None:
    """未知会话：能力桥返回空 → HTTP 200 空 records（契约不破坏）。"""
    install_providers(kr, messages={})
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records",
        method="GET", query={"session_id": "nope"},
    ))
    assert status == 200
    assert body == {"records": [], "total": 0, "session_id": "nope"}


def test_records_degrade_without_providers(server: Any, kr: Any) -> None:
    """provider 未注入：kernel_reads 降级空数据 → HTTP 200 空载荷。"""
    install_providers(kr)  # 空注册表挂空 provider —— 等价能力不可用
    status, body = _decode_http(_call(server, path="/ext/monitoring/execution/records", method="GET"))
    assert status == 200
    assert body["records"] == [] and body["total"] == 0


# ── execution/records：会话列表 / 分组 / 树 / children ────────────────────


def test_records_sessions_aggregate(server: Any, kr: Any) -> None:
    """会话列表：runs 倒序 + state 摘要（标题/消息数），同管道多 run 去重。"""
    install_providers(
        kr,
        runs=[_RUN_P1, _RUN_P2, dict(_RUN_P1, run_id="r1b")],
        states=[_STATE_P1, _STATE_P2],
    )
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/sessions", method="GET",
    ))
    assert status == 200
    assert body["total"] == 2
    first = body["sessions"][0]  # p2 更新（越新越前）
    assert first["id"] == "p2"
    assert first["title"] == "会话乙"
    assert first["record_count"] == 1
    second = body["sessions"][1]
    assert second["id"] == "p1"
    assert second["title"] == "会话甲"
    assert second["record_count"] == 2
    assert second["thread_id"] == "t1"
    assert second["run_status"] == "completed"


def test_records_group_summary_empty(server: Any, kr: Any) -> None:
    """分组概要：内核消息模型无 parent 层级 → 空结构。"""
    install_providers(kr)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/group-summary", method="GET",
        query={"session_id": "p1"},
    ))
    assert status == 200
    assert body == {"groups": [], "total_groups": 0}


def test_records_tree_empty(server: Any, kr: Any) -> None:
    """执行记录树：扁平槽位无层级 → 空树 + max_depth 原样回显。"""
    install_providers(kr)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/tree/p1", method="GET",
        query={"max_depth": "3"},
    ))
    assert status == 200
    assert body == {"tree": [], "total": 0, "session_id": "p1", "max_depth": 3}


def test_records_children_empty(server: Any, kr: Any) -> None:
    """children：无 parent_record_id 概念 → 空数组（status 200 裸数组）。"""
    install_providers(kr)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/m1/children", method="GET",
    ))
    assert status == 200
    assert body == []


# ── execution/records：单条 / 删除 / 清空 ─────────────────────────────────


def test_record_get_found(server: Any, kr: Any) -> None:
    """单条记录：全局无索引 → 最近 N 个会话消息快照线性查找命中。"""
    install_providers(
        kr,
        runs=[_RUN_P1, _RUN_P2],
        messages={"p1": [_MSG_USER], "p2": [_MSG_OTHER]},
    )
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/m3", method="GET",
    ))
    assert status == 200
    assert body["id"] == "m3"
    assert body["session_id"] == "p2"
    assert body["message_data"]["content"] == "另一个会话"


def test_record_get_not_found(server: Any, kr: Any) -> None:
    """单条记录未命中：原 stub 兜底形态（空 message_data）。"""
    install_providers(kr, runs=[_RUN_P1], messages={"p1": [_MSG_USER]})
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/ghost", method="GET",
    ))
    assert status == 200
    assert body == {"id": "ghost", "session_id": "", "message_data": {}, "created_at": ""}


def test_records_clear_all_success(server: Any, kr: Any, tmp_path: Path, monkeypatch: Any) -> None:
    """清空所有记录（stub 做实 2026-08-24）：内核清理信封 + payload_diag 文件清理。

    成功形态透出内核计数/备份路径；headers.authorization 透传给能力调用。
    """
    install_providers(kr)
    captured: dict[str, Any] = {}

    async def _clear(authorization: str = "") -> dict[str, Any]:
        captured["authorization"] = authorization
        return {"status": 200, "body": {
            "cleared": {"runs": 2, "traces": 2},
            "cleared_count": 4,
            "backup_path": "/x/kernel.db.clear-backup-1-0",
        }}

    kr.set_provider("db-admin-clear", _clear)
    monkeypatch.setattr(server, "_payload_diag_dir", lambda: str(tmp_path))
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")

    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/clear-all", method="POST",
        headers={"authorization": "Bearer tok-clear"},
    ))
    assert status == 200
    assert body["success"] is True
    assert body["cleared_count"] == 4
    assert body["tables"] == {"runs": 2, "traces": 2}
    assert body["backup_path"] == "/x/kernel.db.clear-backup-1-0"
    assert body["payload_files_deleted"] == 2
    assert not (tmp_path / "a.json").exists() and not (tmp_path / "b.json").exists()
    assert (tmp_path / "keep.txt").exists(), "非 json 文件不动（与列表范围一致）"
    assert captured["authorization"] == "Bearer tok-clear", "鉴权头须透传内核"


def test_records_clear_all_empty_dir_zero_files(server: Any, kr: Any, tmp_path: Path, monkeypatch: Any) -> None:
    """payload_diag 目录不存在/为空：文件清理 0 不崩（幂等）。"""
    install_providers(kr)

    async def _clear(authorization: str = "") -> dict[str, Any]:
        return {"status": 200, "body": {"cleared": {}, "cleared_count": 0, "backup_path": None}}

    kr.set_provider("db-admin-clear", _clear)
    monkeypatch.setattr(server, "_payload_diag_dir", lambda: str(tmp_path / "nonexistent"))
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/clear-all", method="POST",
    ))
    assert status == 200
    assert body["cleared_count"] == 0
    assert body["payload_files_deleted"] == 0


@pytest.mark.parametrize(
    ("envelope", "want_status", "want_detail"),
    [
        # 内核活跃防呆 409 → 原状态码透传（不吞错为假成功）
        ({"status": 409, "error": {"code": "409", "message": "管道 p1 正在运行，请等待任务结束后再清理"}}, 409, "正在运行"),
        # 非 admin 403
        ({"status": 403, "error": {"code": "403", "message": "写操作需要 admin 角色"}}, 403, "admin"),
        # 内核 500
        ({"status": 500, "error": {"code": "500", "message": "清理备份失败（已中止清理）"}}, 500, "备份失败"),
    ],
)
def test_records_clear_all_kernel_error_passthrough(
    server: Any, kr: Any, envelope: dict[str, Any], want_status: int, want_detail: str
) -> None:
    """内核信封非 200 → 原状态码透传（写面不降级假成功）。"""
    install_providers(kr)

    async def _clear(authorization: str = "") -> dict[str, Any]:
        return envelope

    kr.set_provider("db-admin-clear", _clear)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/clear-all", method="POST",
    ))
    assert status == want_status
    assert want_detail in body["detail"]


def test_records_clear_all_provider_missing_503(server: Any, kr: Any) -> None:
    """清理能力未注入（内核握手未完成）：503，绝不降级为假成功。"""
    install_providers(kr)  # 未注册 db-admin-clear
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/clear-all", method="POST",
    ))
    assert status == 503
    assert "不可用" in body["detail"]


def test_records_clear_all_provider_crash_502(server: Any, kr: Any) -> None:
    """能力调用抛异常 → 502（区别于能力缺失的 503）。"""
    install_providers(kr)

    async def _clear(authorization: str = "") -> dict[str, Any]:
        raise RuntimeError("capability channel broken")

    kr.set_provider("db-admin-clear", _clear)
    status, _ = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/clear-all", method="POST",
    ))
    assert status == 502


def test_execution_unknown_route_404(server: Any, kr: Any) -> None:
    """未知 execution 子路径 → 404（body {"error": "not found"}）。"""
    install_providers(kr)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/whatever", method="GET",
    ))
    assert status == 404


def test_execution_limit_invalid_fallback(server: Any, kr: Any) -> None:
    """limit 非法 → 回退默认 50（不 500）。"""
    install_providers(kr, messages={"p1": [_MSG_USER]})
    _, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records",
        method="GET", query={"session_id": "p1", "limit": "abc"},
    ))
    assert body["total"] == 1


# ── sessions token-usage ──────────────────────────────────────────────────


def test_session_total_token_usage(server: Any, kr: Any) -> None:
    """总 Token 用量接真：state 行 track.total_tokens + 该管道 run 请求数。"""
    install_providers(
        kr,
        runs=[_RUN_P1, dict(_RUN_P1, run_id="r1b"), _RUN_P2],
        states=[_STATE_P1],
    )
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/sessions/p1/total-token-usage", method="GET",
    ))
    assert status == 200
    assert body["session_id"] == "p1"
    assert body["total_tokens"] == 100
    assert body["request_count"] == 2  # r1 + r1b 同管道
    assert body["prompt_tokens"] == 0 and body["completion_tokens"] == 0


def test_session_total_token_usage_only_track_source(server: Any, kr: Any) -> None:
    """只有 track.total_tokens 计入；cost_control 侧历史键不再回退（已退役）。"""
    state = {
        "pipeline_id": "p2",
        "display_name": "会话乙",
        "message_count": 1,
        "cost_control.total_tokens": 50,
    }
    install_providers(kr, runs=[_RUN_P2], states=[state])
    _, body = _decode_http(_call(
        server, path="/ext/monitoring/sessions/p2/total-token-usage", method="GET",
    ))
    assert body["total_tokens"] == 0


def test_session_total_token_usage_missing_state(server: Any, kr: Any) -> None:
    """state 行缺失：total_tokens 0、request_count 0（stub 原形态）。"""
    install_providers(kr, runs=[], states=[])
    _, body = _decode_http(_call(
        server, path="/ext/monitoring/sessions/ghost/total-token-usage", method="GET",
    ))
    assert body == {
        "session_id": "ghost", "total_tokens": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "request_count": 0,
    }


def test_session_context_token_usage(server: Any, kr: Any) -> None:
    """上下文 Token 用量：估算形态 + total_tokens 兼容字段。"""
    install_providers(kr, states=[_STATE_P1])
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/sessions/p1/context-token-usage", method="GET",
        query={"parent_execution_record_id": "m1"},
    ))
    assert status == 200
    assert body["current_context_tokens"] == 100
    assert body["is_estimated"] is True
    assert body["total_tokens"] == 100
    assert body["model"] == "default"


def test_sessions_partial_route_404(server: Any, kr: Any) -> None:
    """sessions 域未匹配路径 → 404。"""
    install_providers(kr)
    status, _ = _decode_http(_call(
        server, path="/ext/monitoring/sessions/p1/other", method="GET",
    ))
    assert status == 404


# ── _on_load 能力注入 ─────────────────────────────────────────────────────


class _FakeCapabilityHandle:
    def __init__(self, side_effect: Any) -> None:
        self._side_effect = side_effect

    async def call(self, method: str, params: dict[str, Any]) -> Any:  # noqa: ARG002
        if callable(self._side_effect):
            return self._side_effect(method, params)
        return self._side_effect


def test_on_load_injects_kernel_reads_providers(server: Any, kr: Any) -> None:
    """_on_load 经 get_capability 注入六个 provider（与 channel_api 同构）。"""
    caps: dict[str, Any] = {
        "service-registry": _FakeCapabilityHandle(lambda m, p: []),
        "pipeline-state": _FakeCapabilityHandle([]),
        "db-admin": _FakeCapabilityHandle({"status": 200, "body": {"rows": [], "total": 0}}),
        # metrics-admin 读面：监控页接通插件运行态后 _on_load 注入的两个 provider
        "metrics-admin": _FakeCapabilityHandle([]),
    }
    server.plugin.get_capability = lambda name: caps[name]  # type: ignore[method-assign]

    _run(server._on_load({}))

    assert sorted(kr._PROVIDERS) == [
        "db-admin-clear", "messages", "metrics-admin-list", "metrics-admin-query",
        "pipeline-runs", "pipeline-state",
    ]
    # 注入的 provider 可真实调用（service-registry 信封 → kernel_reads._rows 收敛）
    rows = _run(kr.list_pipeline_runs())
    assert rows == []
    kr.reset_providers()


def test_capability_failure_degrades_at_call_time(server: Any, kr: Any) -> None:
    """能力未授予（get_capability 抛 KeyError）：注入为惰性闭包、注册期不抛，
    调用期 kernel_reads._call 吞错降级空数据——HTTP 200 空载荷契约不破坏。"""
    async def _raiser(status=None, limit=100):  # noqa: ARG001
        raise KeyError("capability not granted")

    kr.reset_providers()
    kr.set_provider("pipeline-runs", _raiser)
    kr.set_provider("pipeline-state", _raiser)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/execution/records/sessions", method="GET",
    ))
    assert status == 200
    assert body["sessions"] == [] and body["total"] == 0
    kr.reset_providers()


def test_domain_unrelated_routes_untouched(server: Any, kr: Any) -> None:
    """既有 monitoring 端点不受影响（回归护栏）：system/metrics 仍 200。"""
    install_providers(kr)
    status, body = _decode_http(_call(
        server, path="/ext/monitoring/system/metrics", method="GET",
    ))
    assert status == 200
    assert "metrics" in body
    assert "cpu_usage" in body["metrics"]
