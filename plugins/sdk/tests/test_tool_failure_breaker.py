# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""同签名工具失败熔断器 + 结构化失败出口 env_face 增强单测。

背景（监控台账第 198-200 轮）：llm.complete_stream 同签名失败 91 次（每分钟
重试烧轮次配额），失败返回不带「已连败多次、勿再试」语义。修复 = SDK 失败
出口单点三合一（ADR 2026-11-17-run-lifecycle-defense-lines）：
①结构化失败出口（success:false）过 env_face 增强（此前只有异常出口过）；
②同 plugin+tool 连续 N 次失败 → 第 N+1 次失败返回带聚合诊断 + retryable:false；
③同点成功清零。断言风格与 test_env_face.py 同款（_FakeServer 直调分发层）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos_plugin_sdk import server as sdk_server
from agentos_plugin_sdk.failure_breaker import (
    DEFAULT_FAILURE_BREAKER_THRESHOLD,
    FAILURE_BREAKER_THRESHOLD_ENV,
    ToolFailureBreaker,
    resolve_failure_breaker_threshold,
)


class _FakeServer(sdk_server.McpServer):
    """绕过 __init__（不起 MCP/桥），仅装工具表供分发测试。"""


class _NullCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *a: Any) -> None:
        return None


def _make_server(handler: Any, tool_name: str = "boom") -> _FakeServer:
    srv = _FakeServer.__new__(_FakeServer)
    srv._tools = {
        tool_name: type(
            "TD",
            (),
            {"handler": handler, "schema": {"type": "object", "properties": {}}},
        )(),
    }
    srv._on_call_config = None
    srv._handler_locks = {}
    srv._handler_lock = lambda key: _NullCtx()  # type: ignore[method-assign]
    return srv


def _payload(result: Any) -> dict[str, Any]:
    assert result.content and isinstance(result.content[0].text, str)
    return json.loads(result.content[0].text)


def _boom_handler(state: Any = None) -> Any:
    raise RuntimeError("provider ? 缺 api_base（模型 gpt-4）")


# ═══════════════ 阈值解析（env 可调 N）═══════════════


def test_threshold_default_three() -> None:
    assert DEFAULT_FAILURE_BREAKER_THRESHOLD == 3
    assert resolve_failure_breaker_threshold({}) == 3


@pytest.mark.parametrize("raw", ["1", "5", " 7 "])
def test_threshold_env_override(raw: str) -> None:
    assert resolve_failure_breaker_threshold({FAILURE_BREAKER_THRESHOLD_ENV: raw}) == int(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "abc", ""])
def test_threshold_invalid_falls_back_default(raw: str) -> None:
    assert resolve_failure_breaker_threshold({FAILURE_BREAKER_THRESHOLD_ENV: raw}) == 3


# ═══════════════ 熔断器单元语义 ═══════════════


def test_breaker_semantics_threshold_and_reset() -> None:
    breaker = ToolFailureBreaker(threshold=3)
    assert not breaker.breaker_open("t")
    breaker.record_failure("t", "e1")
    breaker.record_failure("t", "e2")
    assert not breaker.breaker_open("t"), "前 N-1 次不熔断"
    breaker.record_failure("t", "e3")
    assert breaker.breaker_open("t"), "第 N 次后进入标记态（第 N+1 次起带标记）"
    assert breaker.streak("t") == 3
    breaker.record_success("t")
    assert not breaker.breaker_open("t"), "同点成功清零"
    assert breaker.streak("t") == 0


def test_breaker_keys_are_independent() -> None:
    breaker = ToolFailureBreaker(threshold=2)
    breaker.record_failure("a", "e")
    breaker.record_failure("a", "e")
    assert breaker.breaker_open("a")
    assert not breaker.breaker_open("b"), "不同签名互不计数"


def test_breaker_samples_truncated_and_bounded() -> None:
    breaker = ToolFailureBreaker(threshold=10)
    for i in range(9):
        breaker.record_failure("t", f"错误-{i}-" + "x" * 300)
    diagnosis = breaker.aggregated_diagnosis("t")
    assert "连续 9 次" in diagnosis
    assert "retryable=false" in diagnosis
    assert "x" * 300 not in diagnosis, "样本必须截断（不倒全量错误正文）"
    assert "错误-8" in diagnosis, "保留最近样本"


# ═══════════════ 分发出口端到端（异常出口）═══════════════


@pytest.mark.asyncio
async def test_fourth_consecutive_failure_carries_retryable_false() -> None:
    """连续 3 次失败 → 第 4 次失败返回带 retryable:false + 聚合诊断。"""
    srv = _make_server(_boom_handler)
    for _ in range(3):
        result = await srv._handle_tools_call({"name": "boom", "arguments": {}})
        payload = _payload(result)
        assert payload["success"] is False
        assert "retryable" not in payload, "未达阈值不得带熔断标记"
    fourth = await srv._handle_tools_call({"name": "boom", "arguments": {}})
    payload = _payload(fourth)
    assert payload["success"] is False
    assert payload["retryable"] is False, "第 4 次必须带 retryable:false"
    assert payload["error_code"] == "TOOL_EXECUTION_FAILED"
    assert "BREAKER: 工具 boom 已连续 4 次" in payload["error"], "聚合诊断含连击计数"
    assert "勿原地重试" in payload["error"]
    # env_face 增强保留（原错误 + 定向诊断不被聚合段顶掉）
    assert payload["error"].startswith("原错误: RuntimeError: provider ? 缺 api_base")
    assert "DIAGNOSIS: 模型 gpt-4 未在 provider 配置定义" in payload["error"]


@pytest.mark.asyncio
async def test_success_resets_streak() -> None:
    """同点成功清零：3 败 → 成功 → 再 3 败无标记（清零实证）→ 第 4 败带标记。"""
    script: list[str] = [
        "fail", "fail", "fail", "ok",
        "fail", "fail", "fail", "fail",
    ]

    def scripted(state: Any = None) -> Any:
        action = script.pop(0)
        if action == "ok":
            return {"success": True, "output": "ok"}
        raise RuntimeError("transient")

    srv = _make_server(scripted)
    payloads = []
    for _ in range(8):
        payloads.append(_payload(await srv._handle_tools_call({"name": "boom", "arguments": {}})))
    # 第 1-3 败：无标记。
    for payload in payloads[0:3]:
        assert "retryable" not in payload
    # 第 4 次：成功 → 计数清零。
    assert payloads[3]["success"] is True
    # 第 5-7 败：仍无标记（若不清零，第 7 次累计 6 败必已带标记）。
    for payload in payloads[4:7]:
        assert "retryable" not in payload, "成功必须清零连击计数"
    # 第 8 败（本轮第 4 败）：带标记。
    assert payloads[7].get("retryable") is False
    assert "已连续 4 次" in payloads[7]["error"]


@pytest.mark.asyncio
async def test_env_threshold_lower_moves_breaker_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 可调 N：阈值=1 时第 2 次失败即带标记。"""
    monkeypatch.setenv(FAILURE_BREAKER_THRESHOLD_ENV, "1")
    srv = _make_server(_boom_handler)
    first = _payload(await srv._handle_tools_call({"name": "boom", "arguments": {}}))
    assert "retryable" not in first, "第 1 次失败不标记"
    second = _payload(await srv._handle_tools_call({"name": "boom", "arguments": {}}))
    assert second.get("retryable") is False, "阈值 1 时第 2 次必须带标记"
    assert "已连续 2 次" in second["error"]


# ═══════════════ 结构化失败出口过 env_face（防线③①）═══════════════


@pytest.mark.asyncio
async def test_invalid_arguments_exit_enriched_and_counted() -> None:
    """参数绑定失败出口：env_face 增强 + 计入熔断（确定性失败）。"""
    srv = _make_server(_boom_handler, tool_name="strict")
    srv._tools["strict"].schema = {
        "type": "object",
        "properties": {"must": {"type": "string"}},
        "required": ["must"],
    }

    def need_must(must: str, state: Any = None) -> Any:
        return {"success": True}

    srv._tools["strict"].handler = need_must
    # 缺必参 → INVALID_ARGUMENTS 结构化失败，过 env_face（兜底诊断）。
    payload = _payload(await srv._handle_tools_call({"name": "strict", "arguments": {}}))
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_ARGUMENTS"
    assert payload["error"].startswith("原错误: "), "结构化失败出口必须过 env_face 增强"
    assert "DIAGNOSIS:" in payload["error"]
    # 连续 3 次同签名参数失败 → 第 4 次带熔断标记（同出口计数）。
    for _ in range(3):
        payload = _payload(await srv._handle_tools_call({"name": "strict", "arguments": {}}))
    assert payload.get("retryable") is False
    assert "BREAKER: 工具 strict" in payload["error"]


@pytest.mark.asyncio
async def test_business_success_false_enriched_and_counted() -> None:
    """业务级 success:False（91 连败事故形态）：过 env_face + 计入熔断。"""
    counter = {"n": 0}

    def business_fail(state: Any = None) -> Any:
        counter["n"] += 1
        return {"success": False, "error": "connection refused"}

    srv = _make_server(business_fail)
    for _ in range(3):
        result = await srv._handle_tools_call({"name": "boom", "arguments": {}})
        assert result.is_error is False, "业务失败信封保持 is_error=False（内核按 success 判）"
        payload = _payload(result)
        assert payload["success"] is False
        assert payload["error"].startswith("原错误: connection refused"), "业务失败也过 env_face"
        assert "WSL NAT" in payload["error"], "命中症状表（connection refused → NAT 诊断）"
        assert "retryable" not in payload
    payload = _payload(await srv._handle_tools_call({"name": "boom", "arguments": {}}))
    assert payload.get("retryable") is False, "业务级失败同样计入同签名熔断"
    assert "BREAKER: 工具 boom 已连续 4 次" in payload["error"]


@pytest.mark.asyncio
async def test_success_path_unmodified_by_breaker() -> None:
    """成功路径零变化（增强只挂失败出口；record_success 不产副作用输出）。"""

    def ok(state: Any = None) -> Any:
        return {"success": True, "output": "ok"}

    srv = _make_server(ok)
    for _ in range(5):
        result = await srv._handle_tools_call({"name": "boom", "arguments": {}})
        payload = _payload(result)
        assert payload == {"success": True, "output": "ok"}, "成功信封逐字节不变"
    assert srv._breaker().streak("boom") == 0
