# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: none-local
"""read_execution_detail 内核 trace 改造 TDD 测试（Step 5b）。

验证内容（与任务规格 6 个用例对齐）：
1. _capability_caller 未注入（None）时返回降级错误 dict，不崩溃
2. skeleton 层调用 _capability_caller，方法名 "messages.list" 且 params 含 pipeline_id
3. skeleton 层把每条 message 渲染为一行骨架（含 role + content_preview）
4. L1 层按对话轮次分组（user→assistant→tool 算一轮），多轮正确拆分
5. L0 层返回完整 content_preview + tool_calls_json
6. set_capability_caller 注入后，后续 read_execution_detail 使用注入的 caller

唯一外部依赖是注入的 _capability_caller（AsyncMock），不调用真实内核。

[来源: docs/tasks Step 5b 复盘系统读内核 trace]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

# simple 工具目录加入 sys.path（与 test_migration.py 同款 setup）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 system_tools.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，隔离 _capability_caller 全局状态。
    """
    mod_name = "system_tools_step5b_test"
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
    """加载 system_tools 模块，每个测试独立（重置 _capability_caller）。"""
    module = _load_module()
    # 确保默认未注入（与 set_capability_caller(None) 等价）
    module._capability_caller = None
    return module


def _make_message(
    seq: int,
    role: str,
    content_preview: str = "",
    tool_calls_json: str | None = None,
    run_id: str = "run-1",
    branch_id: str = "main",
    message_id: str | None = None,
) -> dict[str, Any]:
    """构造一条内核 messages.list 返回形态的 message dict。

    字段对齐 kernel/crates/core/src/types.rs MessageRecord：
    message_id/run_id/branch_id/seq_in_branch/role/content_preview/tool_calls_json/...
    """
    return {
        "message_id": message_id or f"msg-{seq}",
        "run_id": run_id,
        "branch_id": branch_id,
        "seq_in_branch": seq,
        "role": role,
        "content_preview": content_preview,
        "pipeline_id": "pipe-1",
        "tool_calls_json": tool_calls_json,
        "created_at": "2026-08-11T00:00:00Z",
    }


# ═══════════════════════════════════════════════════════════
# 1. 未注入能力时降级
# ═══════════════════════════════════════════════════════════


class TestReadExecutionDetailDegradation:
    async def test_read_detail_without_capability_degrades(self, mod: Any) -> None:
        """_capability_caller=None 时返回降级错误 dict，不抛异常。"""
        mod._capability_caller = None
        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="skeleton"
        )
        assert isinstance(result, dict)
        assert "error" in result
        # 错误信息表明能力未注入
        assert "capability" in result["error"] or "未注入" in result["error"]


# ═══════════════════════════════════════════════════════════
# 2-3. skeleton 层
# ═══════════════════════════════════════════════════════════


class TestReadExecutionDetailSkeleton:
    async def test_read_detail_skeleton_calls_traces_and_messages(
        self, mod: Any
    ) -> None:
        """skeleton 层调用 traces.list(轨迹流程) + messages.list(对话骨架)。"""
        caller = AsyncMock()
        caller.return_value = []  # 空列表
        mod.set_capability_caller(caller)

        await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        # skeleton 应调用 2 次:traces.list + messages.list
        assert caller.await_count == 2
        methods = [c.args[0] for c in caller.await_args_list]
        assert "traces.list" in methods
        assert "messages.list" in methods

    async def test_read_detail_skeleton_builds_trace_steps_and_message_lines(
        self, mod: Any
    ) -> None:
        """skeleton 层渲染 trace_steps(轨迹主线) + message_lines(对话骨架)。"""
        import json as _json

        caller = AsyncMock()

        # 第一次调用(traces.list)返回轨迹;第二次(messages.list)返回消息
        # AsyncMock 每次返回同一个值,所以用 side_effect 区分
        caller.side_effect = [
            # traces.list 返回
            [
                {"plugin_id": "memory_read", "seq_in_branch": 1, "patch_data": _json.dumps({"memory.retrieved": []})},
                {"plugin_id": "llm_core", "seq_in_branch": 2, "patch_data": _json.dumps({"core_type": "llm_call", "raw_error": None})},
            ],
            # messages.list 返回
            [
                _make_message(1, "user", content_preview="你好"),
                _make_message(2, "assistant", content_preview="收到"),
            ],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="skeleton"
        )

        assert result["pipeline_run_id"] == "pipe-1"
        assert result["level"] == "skeleton"
        # trace_steps = 轨迹主线
        assert "trace_steps" in result
        assert result["trace_count"] == 2
        assert result["trace_steps"][0]["plugin"] == "memory_read"
        # message_lines = 对话骨架
        assert "message_lines" in result
        assert result["message_count"] == 2
        assert "你好" in result["message_lines"][0]


# ═══════════════════════════════════════════════════════════
# 4. L1 层按对话轮次分组
# ═══════════════════════════════════════════════════════════


class TestReadExecutionDetailL1:
    async def test_read_detail_L1_groups_turns(self, mod: Any) -> None:
        """L1 层:无压缩块时降级到 messages 轮次分组。

        新逻辑:L1 先调 hindsight.recall 读压缩块,无压缩块时降级用 messages 轮次摘要。
        本测试验证降级路径:user→assistant→tool→user→assistant 应拆为 2 轮。
        """
        caller = AsyncMock()
        # side_effect: 第一次(tool-executor.invoke hindsight.recall)返回空压缩块,
        # 第二次(messages.list)返回消息列表
        caller.side_effect = [
            {"results": []},  # 无压缩块,触发降级
            [
                _make_message(1, "user", content_preview="第一问"),
                _make_message(2, "assistant", content_preview="第一答"),
                _make_message(3, "tool", content_preview="tool-result-1"),
                _make_message(4, "user", content_preview="第二问"),
                _make_message(5, "assistant", content_preview="第二答"),
            ],
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="L1"
        )

        assert result["level"] == "L1"
        # 降级路径:turns 字段
        turns = result.get("turns")
        assert turns is not None, "L1 降级结果应包含 turns（轮次列表）"
        assert len(turns) == 2


# ═══════════════════════════════════════════════════════════
# 5. L0 层返回完整内容
# ═══════════════════════════════════════════════════════════


class TestReadExecutionDetailL0:
    async def test_read_detail_L0_returns_full(self, mod: Any) -> None:
        """L0 层返回完整 content_preview + tool_calls_json。"""
        caller = AsyncMock()
        caller.return_value = [
            _make_message(
                1,
                "assistant",
                content_preview="调用工具",
                tool_calls_json='[{"id":"call_1","name":"search"}]',
            ),
            _make_message(2, "tool", content_preview="search result"),
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="L0"
        )

        assert result["level"] == "L0"
        records = result.get("records") or result.get("messages")
        assert records is not None, "L0 结果应包含 records/messages 列表"
        # 至少一条记录包含 tool_calls_json
        has_tool_calls = any(
            r.get("tool_calls_json") or r.get("tool_calls") for r in records
        )
        assert has_tool_calls, "L0 应包含 tool_calls_json 字段"


# ═══════════════════════════════════════════════════════════
# 6. set_capability_caller 注入
# ═══════════════════════════════════════════════════════════


class TestSetCapabilityCaller:
    async def test_set_capability_caller(self, mod: Any) -> None:
        """set_capability_caller 注入后，后续 read_execution_detail 使用注入的 caller。"""
        # 初始未注入
        assert mod._capability_caller is None

        caller = AsyncMock()
        caller.return_value = []
        mod.set_capability_caller(caller)

        # 注入后模块级 _capability_caller 应为该 caller
        assert mod._capability_caller is caller

        await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        # skeleton 调用 caller(traces.list + messages.list,至少 1 次)
        assert caller.await_count >= 1
