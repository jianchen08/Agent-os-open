# @feature: FP-MIGR 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P2 编排运行面 | @ci: python-coverage
"""task_submit MCP 适配层（server.py）失败载荷契约测试。

覆盖：失败结果透传 error_code 与结构化 metadata（H3/M2：编排键不存在/
完备性失败等错误携 {missing_fields, orchestration_key, suggestion,
available_orchestrations} 随载荷可编程消费——错误是值，不只给人读文本）；
成功原样返回 output；无 error_code/metadata 的失败保持 ``{"error": …}``
旧形态（additive，旧消费方不破）。

装配同 test_task_server.py 先例：importlib 显式路径加载 + 唯一模块名 +
裸名 "tool" 槽位治理。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"

for _d in (str(_TS_DIR), str(_TASKS_DIR), str(_SYSTEM_DIR), str(_SHARED_ROOT)):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

for _bare in ("tool",):
    sys.modules.pop(_bare, None)
sys.modules.pop("task_submit_server_adapter_test", None)
_spec = importlib.util.spec_from_file_location(
    "task_submit_server_adapter_test", _TS_DIR / "server.py"
)
assert _spec is not None
assert _spec.loader is not None
_server_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_submit_server_adapter_test"] = _server_mod
_spec.loader.exec_module(_server_mod)

import tool as _tool_mod  # noqa: E402 — 裸名 tool（_TS_DIR 优先解析到 task_submit）


@pytest.fixture(autouse=True)
def _restore_tool_slot():
    """裸名 "tool" 槽位治理：用例前重绑本目录 tool.py，用后还原。"""
    sys.modules["tool"] = _tool_mod
    saved = sys.modules.get("tool")
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["tool"] = saved
        else:
            sys.modules.pop("tool", None)


class _FakeTool:
    """execute 返回预置结果的工具替身（server 适配层不触达真实闸门）。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.seen_inputs: dict[str, Any] | None = None

    async def execute(self, inputs: dict[str, Any]) -> Any:
        self.seen_inputs = inputs
        return self._result


async def test_failure_payload_carries_structured_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H3 结构化错误随载荷透传：error/error_code + metadata 字段可编程消费。"""
    from agentos_plugin_sdk import create_failure_result

    fake = _FakeTool(
        create_failure_result(
            error="编排键不存在: mode_writing/nope。当前可用编排: ['autonomous']",
            error_code="ORCHESTRATION_KEY_NOT_FOUND",
            metadata={
                "action": "task_submit",
                "orchestration_key": "mode_writing/nope",
                "available_orchestrations": ["autonomous"],
            },
        )
    )
    monkeypatch.setattr(_server_mod, "TaskSubmitTool", lambda: fake)
    out = await _server_mod.task_submit(goal_title="t", goal_description="d")
    assert out["error_code"] == "ORCHESTRATION_KEY_NOT_FOUND"
    assert out["orchestration_key"] == "mode_writing/nope"
    assert out["available_orchestrations"] == ["autonomous"]
    assert "mode_writing/nope" in out["error"]
    assert out["action"] == "task_submit"
    # 入参原样进 execute（schema 展开 kwargs）
    assert fake.seen_inputs == {"goal_title": "t", "goal_description": "d"}


async def test_failure_payload_carries_completeness_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 完备性错误结构：{missing_fields, orchestration_key, suggestion}。"""
    from agentos_plugin_sdk import create_failure_result

    fake = _FakeTool(
        create_failure_result(
            error="初始输入不完备",
            error_code="INCOMPLETE_INITIAL_INPUTS",
            metadata={
                "missing_fields": ["tool_ids"],
                "orchestration_key": "autonomous",
                "suggestion": "补齐后重提",
            },
        )
    )
    monkeypatch.setattr(_server_mod, "TaskSubmitTool", lambda: fake)
    out = await _server_mod.task_submit(goal_title="t", goal_description="d")
    assert out["missing_fields"] == ["tool_ids"]
    assert out["orchestration_key"] == "autonomous"
    assert out["suggestion"] == "补齐后重提"


async def test_success_returns_output_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功路径原样返回 output（data dict），无错误键混入。"""
    from agentos_plugin_sdk import create_success_result

    fake = _FakeTool(
        create_success_result(
            data={"task_id": "abcdef123456", "status": "running"},
            metadata={"action": "task_submit"},
        )
    )
    monkeypatch.setattr(_server_mod, "TaskSubmitTool", lambda: fake)
    out = await _server_mod.task_submit(goal_title="t", goal_description="d")
    assert out == {"task_id": "abcdef123456", "status": "running"}


async def test_plain_failure_keeps_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 error_code/metadata 的失败保持旧形态 {"error": …}（additive 不破旧）。"""
    from agentos_plugin_sdk import create_failure_result

    fake = _FakeTool(create_failure_result(error="必须提供 goal（含 title 字段）"))
    monkeypatch.setattr(_server_mod, "TaskSubmitTool", lambda: fake)
    out = await _server_mod.task_submit(goal_title="t", goal_description="d")
    assert out == {"error": "必须提供 goal（含 title 字段）"}
