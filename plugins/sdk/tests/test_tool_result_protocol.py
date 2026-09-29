# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""tool_result_protocol 单测——工具结果协议固定函数路径（ADR 2026-09-28）。

覆盖：
- 契约夹具一致性：tests/contracts/tool_result_messages.fixture.json 全向量
  （与 tool_core Rust messages::rebuild 双车道同一真值，漂移即红）；
- tool_result_entry：entry 形状/call_id 缺席/扩展位（retry_allowed）顶层透传；
- build_tool_result_ops：entry 无 call_id 时 tc.id 回退与下标兜底、
  长度不一致显式报错（不静默截断）、失败 content 形态；
- serialize_for_content：None→空串、文档结束标记剥离性质断言。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.tool_result_protocol import (
    build_tool_result_ops,
    merge_pre_decided,
    serialize_for_content,
    tool_result_entry,
)

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "contracts" / "tool_result_messages.fixture.json"


def _load_vectors() -> list[dict[str, Any]]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    vectors = data["vectors"]
    assert len(vectors) >= 5, "夹具向量数异常（被清空即红）"
    return vectors


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_fixture_parity(vector: dict[str, Any]) -> None:
    """全向量：build_tool_result_ops 产物 == 夹具期望（tool_core Rust 同真值）。"""
    ops = build_tool_result_ops(vector["tool_calls"], vector["results"])
    msgs = [op["msg"] for op in ops["_ops"]]
    assert msgs == vector["expected_msgs"]


def test_entry_shape_and_call_id_optional() -> None:
    """entry 形状：七键 + call_id 存在时含之、缺席时不造键。"""
    with_id = tool_result_entry("bash_execute", call_id="c1", success=False, error="权限不足")
    assert with_id["call_id"] == "c1"
    assert with_id["tool_name"] == "bash_execute"
    assert with_id["success"] is False
    assert with_id["error"] == "权限不足"
    assert with_id["data"] is None
    assert with_id["metadata"] is None
    assert with_id["duration_ms"] == 0.0

    no_id = tool_result_entry("file_read", data={"ok": True}, duration_ms=3.0)
    assert "call_id" not in no_id
    assert no_id["success"] is True


def test_entry_extras_passthrough_top_level() -> None:
    """协议扩展位：retry_allowed/arguments 顶层透传（BUG-41 duplicate_check 认读）。"""
    entry = tool_result_entry(
        "bash_execute",
        call_id="c2",
        success=False,
        error="审批通道故障",
        metadata={"decided_by": "security_check"},
        retry_allowed=True,
        arguments={"command": "ls"},
    )
    assert entry["retry_allowed"] is True
    assert entry["arguments"] == {"command": "ls"}
    # 扩展位不进 envelope（七键封闭）：经 build_tool_result_ops 验证。
    ops = build_tool_result_ops(
        [{"name": "bash_execute", "id": "c2", "args": {}}], [entry]
    )
    envelope = ops["_ops"][0]["msg"]["tool_result"]
    assert set(envelope.keys()) == {
        "call_id", "tool_name", "success", "error", "data", "metadata", "duration_ms",
    }


def test_ops_call_id_fallbacks() -> None:
    """entry 无 call_id：tc.id 回退；tc 也无 id：下标兜底 call_{i}。"""
    ops = build_tool_result_ops(
        [{"name": "f", "id": "tc_id_1", "args": {}}],
        [tool_result_entry("f", success=True, data="ok")],
    )
    assert ops["_ops"][0]["msg"]["tool_call_id"] == "tc_id_1"

    ops = build_tool_result_ops(
        [{"name": "f", "args": {}}],
        [tool_result_entry("f", success=True, data="ok")],
    )
    assert ops["_ops"][0]["msg"]["tool_call_id"] == "call_0"


def test_ops_length_mismatch_raises() -> None:
    """长度不一致 = 配对契约破坏，显式报错不静默截断。"""
    with pytest.raises(ValueError, match="长度不一致"):
        build_tool_result_ops(
            [{"name": "f", "id": "c1", "args": {}}],
            [
                tool_result_entry("f", success=True),
                tool_result_entry("f", success=True),
            ],
        )


def test_ops_failure_without_error_says_unknown() -> None:
    """失败 entry 缺 error：content 归一为 Error: unknown（不抛错）。"""
    ops = build_tool_result_ops(
        [{"name": "f", "id": "c9", "args": {}}],
        [{"tool_name": "f", "success": False, "error": None, "data": None,
          "metadata": None, "duration_ms": 0.0}],
    )
    assert ops["_ops"][0]["msg"]["content"] == "Error: unknown"


def test_serialize_for_content_properties() -> None:
    """性质断言：None→空串；标量剥离文档结束标记（不以 ... 收尾）。"""
    assert serialize_for_content(None) == ""
    for scalar in ("plain text", "42", True, 5):
        out = serialize_for_content(scalar)
        assert not out.endswith("...\n"), f"文档结束标记未剥离: {out!r}"
    # dict：块风格 + 键排序。
    assert serialize_for_content({"b": 2, "a": 1}) == "a: 1\nb: 2\n"


def test_serialize_unserializable_falls_back_to_str() -> None:
    """YAML 无法表示的值（缓存可存任意 Python 对象）回落 str——读面不崩。"""

    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    assert serialize_for_content(_Opaque()) == "<opaque>"


def test_merge_pre_decided_composition() -> None:
    """多 guard 组合：同 call_id 后写赢、不同 call_id 并存、无 call_id 不入、
    None 现值可接。"""
    first = tool_result_entry("bash_execute", call_id="c1", success=False, error="validator 拒")
    second = tool_result_entry("file_write", call_id="c2", success=False, error="level 拒")
    # 后写赢同 call_id。
    c1_override = tool_result_entry("bash_execute", call_id="c1", success=False, error="isolation 拒")
    merged = merge_pre_decided([first, second], [c1_override])
    assert len(merged) == 2
    by_id = {e["call_id"]: e for e in merged}
    assert by_id["c1"]["error"] == "isolation 拒"
    assert by_id["c2"]["error"] == "level 拒"
    # 无 call_id 条目按工具名去重保留（幻觉调用按名兜底拦截面）。
    no_id = tool_result_entry("x_tool", success=False, error="无键调用")
    no_id_later = tool_result_entry("x_tool", success=False, error="后来者赢")
    assert merge_pre_decided([no_id], [no_id_later]) == [no_id_later]
    assert merge_pre_decided(None, [no_id]) == [no_id]
    # 名字条目与 call_id 条目互不吞并。
    assert len(merge_pre_decided([no_id], [first])) == 2
