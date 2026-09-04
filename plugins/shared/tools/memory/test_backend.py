# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""backend.py（自持 capability 客户端）单测。

覆盖 wire 契约：tool-executor.invoke 参数装配、{success,data} 信封解包、
降级签名判定、tags metadata 序列化、recall 结果统一映射、delete/import
降级返回与工厂校验。

真实依赖：仅 stdlib；capability_caller 属跨进程外部依赖，按测试纪律用
AsyncMock。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

import backend as backend_mod  # noqa: E402
from backend import HindsightBackend, get_memory_backend  # noqa: E402


def _invoke_params(mock: AsyncMock) -> dict[str, Any]:
    """取首次 invoke 调用的 params 断言参数装配。"""
    method, params = mock.call_args.args
    assert method == "tool-executor.invoke"
    assert params["plugin_id"] == "hindsight_memory_service"
    return params


def _args_for(tool_name: str, mock: AsyncMock) -> dict[str, Any]:
    params = _invoke_params(mock)
    assert params["tool_name"] == tool_name
    return params["args"]


# ─────────────────────────── add / retain ───────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        # 内核 invoker 归一形态：纯业务 dict 包 {success,data} 信封
        {"success": True, "data": {"id": "mem-1"}},
        # sidecar tools/call 直连无信封形态
        {"id": "mem-1"},
    ],
)
async def test_add_returns_id_from_both_envelope_shapes(raw: Any) -> None:
    caller = AsyncMock(return_value=raw)
    memory_id = await HindsightBackend(caller).add("u1", "内容")
    assert memory_id == "mem-1"


async def test_add_writes_wire_metadata_contract() -> None:
    """tags 序列化为 JSON 串；非 str metadata 值 json 化；tags/source 不可被覆盖。"""
    caller = AsyncMock(return_value={"data": {"id": "m"}})
    await HindsightBackend(caller).add(
        "u1",
        "内容",
        tags=["alpha", "beta"],
        source="memory_tool",
        metadata={"review_id": "r-9", "note": {"deep": True}, "source": "evil"},
    )
    args = _args_for("hindsight.retain", caller)
    meta = args["metadata"]
    assert meta["tags"] == json.dumps(["alpha", "beta"], ensure_ascii=False)
    assert meta["source"] == "memory_tool"  # 调用方注入的语义键被忽略
    assert meta["review_id"] == "r-9"
    assert json.loads(meta["note"]) == {"deep": True}


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        ({"success": False, "error": "bank offline"}, "bank offline"),
        ({"data": {"initialized": False}}, "not initialized"),
        ({"data": {}}, "写入未确认"),
        ("junk", "返回非预期类型"),
    ],
)
async def test_add_failures_raise_runtime_error(raw: Any, expected_fragment: str) -> None:
    """降级签名/空 id/形态违约 → 诚实上抛，不返回空 id 假成功。"""
    caller = AsyncMock(return_value=raw)
    with pytest.raises(RuntimeError) as exc_info:
        await HindsightBackend(caller).add("u1", "内容")
    assert expected_fragment in str(exc_info.value)


async def test_add_caller_failure_wrapped() -> None:
    """能力调用本身失败 → RuntimeError 包装（不吞）。"""
    caller = AsyncMock(side_effect=TimeoutError("bridge down"))
    with pytest.raises(RuntimeError) as exc_info:
        await HindsightBackend(caller).add("u1", "内容")
    assert "hindsight.retain 调用失败" in str(exc_info.value)
    assert "bridge down" in str(exc_info.value)


# ─────────────────────────── search / recall ───────────────────────────


async def test_search_maps_results_and_adds_session_tag() -> None:
    caller = AsyncMock(
        return_value={
            "data": {
                "results": [
                    {"id": "a", "content": "甲", "score": 0.9,
                     "metadata": {"memory_type": "episode"}},
                    {"id": "b", "content": "乙", "score": "0.5"},
                ]
            }
        }
    )
    results = await HindsightBackend(caller).search(
        "查", user_id="u1", top_k=5, session_id="s-7",
    )
    args = _args_for("hindsight.recall", caller)
    assert args["tags"] == ["session:s-7"]
    assert args["tags_match"] == "any"
    assert [(r["id"], r["score"]) for r in results] == [("a", 0.9), ("b", 0.5)]
    assert results[0]["memory_type"] == "episode"
    assert results[1]["memory_type"] == "semantic"  # 缺省回填


async def test_search_maps_score_from_nested_scores_final() -> None:
    """B6 回归：真实 recall 条目的相关度在嵌套 scores.final（RecallResult 无
    顶层 score）——映射必须取 final，否则所有结果 score 恒 0。"""
    caller = AsyncMock(
        return_value={
            "data": {
                "results": [
                    {"id": "a", "content": "甲",
                     "scores": {"final": 0.93, "semantic": 0.8}},
                    {"id": "b", "content": "乙", "scores": None},
                ]
            }
        }
    )
    results = await HindsightBackend(caller).search("查", user_id="u1")
    assert [(r["id"], r["score"]) for r in results] == [("a", 0.93), ("b", 0.0)]


async def test_search_knowledge_name_filters_client_side() -> None:
    caller = AsyncMock(
        return_value={
            "results": [
                {"id": "k1", "metadata": {"knowledge_name": "api"}},
                {"id": "k2", "metadata": {"knowledge_name": "ops"}},
            ]
        }
    )
    results = await HindsightBackend(caller).search(
        "查", user_id="u1", knowledge_name="api",
    )
    assert [r["id"] for r in results] == ["k1"]


async def test_search_degraded_signature_raises() -> None:
    caller = AsyncMock(return_value={"data": {"error": "bank degraded"}})
    with pytest.raises(RuntimeError) as exc_info:
        await HindsightBackend(caller).search("查", user_id="u1")
    assert "bank degraded" in str(exc_info.value)


async def test_search_accepts_bare_list_results() -> None:
    caller = AsyncMock(return_value=[{"id": "x", "content": "y"}])
    results = await HindsightBackend(caller).search("查", user_id="u1")
    assert len(results) == 1
    assert results[0]["content"] == "y"


# ─────────────────────── delete / import_document ───────────────────────


async def test_delete_success_mapping() -> None:
    caller = AsyncMock(return_value={"data": {"deleted": True}})
    ok = await HindsightBackend(caller).delete("u1", memory_id="mem://d-1")
    assert ok is True
    args = _args_for("hindsight.delete", caller)
    # memory:// 前缀剥除是 tool 层职责；backend 原样透传
    assert args == {"bank_id": "u1", "memory_id": "mem://d-1"}


async def test_delete_caller_failure_raises_with_detail() -> None:
    """能力调用失败 → RuntimeError 携带原因（吞成 False 会被掩蔽成
    "tool execution failed"）。"""
    caller = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError) as exc_info:
        await HindsightBackend(caller).delete("u1")
    assert "hindsight.delete 调用失败" in str(exc_info.value)
    assert "down" in str(exc_info.value)


async def test_delete_deleted_false_raises_with_detail() -> None:
    """deleted=false（含服务端 error）→ RuntimeError 携带具体原因。"""
    caller = AsyncMock(
        return_value={"data": {"deleted": False, "error": "(404) not found"}}
    )
    with pytest.raises(RuntimeError) as exc_info:
        await HindsightBackend(caller).delete("u1", memory_id="m1")
    assert "hindsight.delete 失败" in str(exc_info.value)
    assert "(404)" in str(exc_info.value)


async def test_import_document_unwraps_envelope_and_defaults_name() -> None:
    caller = AsyncMock(return_value={"data": {"chunks_imported": 3}})
    out = await HindsightBackend(caller).import_document("u1", text="长文", name="kb-a")
    assert out == {"chunks_imported": 3, "name": "kb-a"}
    args = _args_for("hindsight.import_document", caller)
    assert args["text"] == "长文"
    assert args["knowledge_name"] == "kb-a"


async def test_import_document_failure_degrades_with_error_field(
    caplog: Any,
) -> None:
    caller = AsyncMock(side_effect=RuntimeError("storage full"))
    with caplog.at_level("WARNING"):
        out = await HindsightBackend(caller).import_document(
            "u1", file_path="/tmp/x.md", name="kb-b",
        )
    assert out["chunks_imported"] == 0
    assert "storage full" in out["error"]
    assert any("[HindsightBackend.import_document]" in r.getMessage()
               for r in caplog.records)


# ─────────────────────────── 工厂 ───────────────────────────


def test_factory_requires_caller() -> None:
    """capability_caller 缺失 → ValueError（fail loudly）。"""
    cases: tuple[dict[str, Any] | None, ...] = ({}, None)
    for cfg in cases:
        with pytest.raises(ValueError, match="capability_caller 必须注入"):
            get_memory_backend(config=cfg, capability_caller=None)


def test_factory_rejects_retired_kernel_backend() -> None:
    """已退役 kernel 后端配置 → ValueError，不留备用真值糊弄。"""

    async def caller(method: str, params: dict[str, Any]) -> Any:
        return None

    with pytest.raises(ValueError, match="已退役"):
        get_memory_backend(config={"backend": "kernel"}, capability_caller=caller)


def test_factory_default_builds_hindsight_backend() -> None:
    async def caller(method: str, params: dict[str, Any]) -> Any:
        return None

    backend = get_memory_backend(config={}, capability_caller=caller)
    assert isinstance(backend, backend_mod.HindsightBackend)
