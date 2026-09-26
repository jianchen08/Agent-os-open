# @feature: FP-0.2.二 复盘读面重构 | @vision: V3 可嵌入 | @ci: none-local
"""read_execution_detail 重构 TDD 测试（§10 设计裁定实施，工作包 A）。

裁定依据：docs/working/评估体系全景与自进化流水线_20260924.md §10.1/§10.4/§10.5：
1. 骨架以 traces patch 流为主干（每有更新 step 一行：iteration 归属 + 插件名 +
   patch_type 分型 + 关键变更字段），Error patch 独立锚点化，消息行退出骨架；
2. 全量必含项白名单：第 1 条消息 / 各轮用户消息 / 命令（tool_calls_json）/
   交互工具结果与选项全量直塞，不走 500 字符预览帽；
3. 查询边界：按复盘管道 state 登记的被复盘管道集合过滤，越界 pipeline_id 拒绝。

唯一外部依赖是注入的 _capability_caller（AsyncMock），不调用真实内核。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    mod_name = "system_tools_refactor_test"
    module_path = _PLUGIN_DIR / "system_tools.py"
    assert module_path.exists(), f"system_tools.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load system_tools.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    module = _load_module()
    module._capability_caller = None
    return module


def _make_message(
    seq: int,
    role: str,
    content_preview: str = "",
    tool_calls_json: str | None = None,
    run_id: str = "run-1",
    message_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    return {
        "message_id": message_id or f"msg-{seq}",
        "run_id": run_id,
        "branch_id": "main",
        "seq_in_branch": seq,
        "role": role,
        "content_preview": content_preview,
        "pipeline_id": "pipe-1",
        "tool_calls_json": tool_calls_json,
        "tool_call_id": tool_call_id,
        "created_at": "2026-09-25T00:00:00Z",
    }


def _make_trace(
    seq: int,
    plugin_id: str,
    patch_type: str,
    patch_data: dict[str, Any] | str,
    pipeline_id: str = "pipe-1",
) -> dict[str, Any]:
    data = json.dumps(patch_data) if isinstance(patch_data, dict) else patch_data
    return {
        "trace_id": f"t-{seq}",
        "pipeline_id": pipeline_id,
        "seq": seq,
        "plugin_id": plugin_id,
        "patch_type": patch_type,
        "patch_data": data,
        "created_at": "2026-09-25T00:00:00Z",
    }


# ═══════════════════════════════════════════════════════════
# A1. 骨架 = patch 流主干：patch_type 分型 + iteration 归属 + Error 锚点
# ═══════════════════════════════════════════════════════════


class TestSkeletonPatchFlow:
    async def test_skeleton_renders_patch_type_per_step(self, mod: Any) -> None:
        """骨架每行带 patch_type 分型（StateUpdate/Error/RouteSignal/...）。"""
        caller = AsyncMock()
        caller.side_effect = [
            # traces.list_by_pipeline
            [
                _make_trace(1, "pipeline_llm_core", "StateUpdate", {"core_type": "llm_call"}),
                _make_trace(2, "pipeline_task_reminder", "Error", {"raw_error": "llm timeout"}),
                _make_trace(3, "pipeline_tool_core", "RouteSignal", {"route": "loop"}),
            ],
            # messages.list（白名单与定位辅助仍需消息）
            [_make_message(1, "user", content_preview="做任务")],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        steps = result["trace_steps"]
        assert len(steps) == 3
        assert steps[0]["patch_type"] == "StateUpdate"
        assert steps[1]["patch_type"] == "Error"
        assert steps[2]["patch_type"] == "RouteSignal"

    async def test_skeleton_marks_error_anchors(self, mod: Any) -> None:
        """Error patch 独立锚点化：error_anchors 列表单独可查，不在普通行里淹没。"""
        caller = AsyncMock()
        caller.side_effect = [
            [
                _make_trace(1, "pipeline_llm_core", "StateUpdate", {"core_type": "llm_call"}),
                _make_trace(2, "pipeline_llm_core", "Error", {"raw_error": "boom"}),
                _make_trace(3, "pipeline_llm_core", "StateUpdate", {"core_type": "llm_call"}),
            ],
            [],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        anchors = result.get("error_anchors")
        assert anchors is not None and len(anchors) == 1
        assert anchors[0]["seq"] == 2
        assert anchors[0]["plugin"] == "pipeline_llm_core"

    async def test_skeleton_step_lines_carry_iteration_when_derivable(
        self, mod: Any
    ) -> None:
        """骨架行带 iteration 轮次归属（可从 state 键推导时）。"""
        caller = AsyncMock()
        caller.side_effect = [
            [
                _make_trace(1, "pipeline_llm_core", "StateUpdate", {"iteration": 1}),
                _make_trace(2, "pipeline_task_reminder", "StateUpdate", {"iteration": 1}),
                _make_trace(3, "pipeline_llm_core", "StateUpdate", {"iteration": 2}),
            ],
            [],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        steps = result["trace_steps"]
        assert [s.get("iteration") for s in steps] == [1, 1, 2]

    async def test_skeleton_uses_list_by_pipeline(self, mod: Any) -> None:
        """骨架查询走 traces.list_by_pipeline（按 pipeline 直查，不按 thread）。"""
        caller = AsyncMock()
        caller.side_effect = [[], []]
        mod.set_capability_caller(caller)

        await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        first_method, first_params = caller.await_args_list[0].args
        assert first_method == "traces.list_by_pipeline"
        assert first_params["pipeline_id"] == "pipe-1"


# ═══════════════════════════════════════════════════════════
# A2. 全量必含项白名单（不走 500 字符预览帽）
# ═══════════════════════════════════════════════════════════


class TestFullContentWhitelist:
    async def test_first_message_and_user_messages_full(self, mod: Any) -> None:
        """第 1 条消息与各轮用户消息全量返回（不受 500 字符帽限制）。"""
        long_user = "用户长指令" * 300  # ~1500 字符
        caller = AsyncMock()
        # L0 只调一次 messages.list
        caller.side_effect = [
            [
                _make_message(1, "user", content_preview=long_user),
                _make_message(2, "assistant", content_preview="收到"),
                _make_message(3, "user", content_preview="继续" * 400),
            ],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="L0")

        records = result["records"]
        assert records[0]["role"] == "user"
        # 白名单：首条消息与用户消息全量（>500 字符不截断）
        assert len(records[0]["content"]) > 500
        assert len(records[2]["content"]) > 500
        assert records[0]["full"] is True
        # 非白名单（assistant）仍截断到 500
        assert len(records[1]["content"]) <= 500
        assert records[1]["full"] is False

    async def test_command_tool_calls_full(self, mod: Any) -> None:
        """命令（tool_calls_json）全量返回。"""
        big_calls = json.dumps(
            [{"id": "c1", "name": "bash_execute", "arguments": {"command": "ls " * 400}}]
        )
        caller = AsyncMock()
        caller.side_effect = [
            [_make_message(1, "assistant", content_preview="", tool_calls_json=big_calls)],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="L0")

        assert result["records"][0]["tool_calls_json"] == big_calls

    async def test_interaction_tool_result_full(self, mod: Any) -> None:
        """交互工具结果（审批卡选项与用户选择）全量返回。"""
        approval = json.dumps({
            "response_type": "approved",
            "choice": "option_2",
            "options": ["option_1", "option_2", "option_3"],
            "reason": "同意方案二，理由……" * 100,
        })
        caller = AsyncMock()
        caller.side_effect = [
            [
                _make_message(1, "user", content_preview="派任务"),
                _make_message(2, "tool", content_preview=approval, tool_call_id="call-appr"),
            ],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="L0")

        rec = result["records"][1]
        assert rec["full"] is True
        assert len(rec["content"]) > 500  # 审批结果 >500 字符不截断

    async def test_plain_tool_result_still_truncated(self, mod: Any) -> None:
        """普通工具结果（非交互类）仍截断。"""
        big = "x" * 800
        caller = AsyncMock()
        caller.side_effect = [
            [_make_message(1, "tool", content_preview=big, tool_call_id="call-1")],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-1", level="L0")

        rec = result["records"][0]
        assert rec["full"] is False
        assert "truncated" in rec["content"]  # 500 帽 + 截断标记


# ═══════════════════════════════════════════════════════════
# A3. 查询边界：越界 pipeline_id 拒绝
# ═══════════════════════════════════════════════════════════


class TestQueryBoundary:
    async def test_out_of_scope_pipeline_rejected(self, mod: Any) -> None:
        """pipeline_run_id 不在登记集合 → 拒绝，不发起任何内核查询。"""
        caller = AsyncMock()
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-999", level="skeleton", allowed_pipelines=["pipe-1"]
        )

        assert "error" in result
        assert caller.await_count == 0

    async def test_in_scope_pipeline_passes(self, mod: Any) -> None:
        """登记集合内的 pipeline_id 正常查询。"""
        caller = AsyncMock()
        caller.side_effect = [[], []]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="skeleton", allowed_pipelines=["pipe-1", "pipe-2"]
        )

        assert "error" not in result

    async def test_no_boundary_registered_allows_query(self, mod: Any) -> None:
        """未登记边界（allowed_pipelines 缺省 None）保持旧行为放行（单任务复盘兼容）。"""
        caller = AsyncMock()
        caller.side_effect = [[], []]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(pipeline_run_id="pipe-x", level="L0")

        assert "error" not in result
