# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-plugins-test
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
    async def test_read_detail_skeleton_calls_messages_list(
        self, mod: Any
    ) -> None:
        """skeleton 层调用 _capability_caller，方法名 messages.list 且 params 含 pipeline_id。"""
        caller = AsyncMock()
        caller.return_value = []  # 空列表，足以断言调用形态
        mod.set_capability_caller(caller)

        await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "messages.list"
        assert params["pipeline_id"] == "pipe-1"

    async def test_read_detail_skeleton_builds_lines(self, mod: Any) -> None:
        """skeleton 层把每条 message 渲染为一行骨架（含 role + content_preview）。"""
        caller = AsyncMock()
        caller.return_value = [
            _make_message(1, "user", content_preview="你好"),
            _make_message(2, "assistant", content_preview="收到"),
            _make_message(3, "tool", content_preview="result=42"),
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="skeleton"
        )

        assert result["pipeline_run_id"] == "pipe-1"
        assert result["level"] == "skeleton"
        assert result["total_records"] == 3
        lines = result["lines"]
        assert len(lines) == 3
        # 每行骨架应含 seq 标记 + role
        assert "user" in lines[0]
        assert "assistant" in lines[1]
        assert "tool" in lines[2]
        # user 行的 content_preview 应出现在骨架中
        assert "你好" in lines[0]


# ═══════════════════════════════════════════════════════════
# 4. L1 层按对话轮次分组
# ═══════════════════════════════════════════════════════════


class TestReadExecutionDetailL1:
    async def test_read_detail_L1_groups_turns(self, mod: Any) -> None:
        """L1 层按对话轮次分组：user→assistant→tool→user→assistant 应拆为 2 轮。

        新规则：每条 role=user 消息开启一个新 turn。
        """
        caller = AsyncMock()
        caller.return_value = [
            _make_message(1, "user", content_preview="第一问"),
            _make_message(2, "assistant", content_preview="第一答"),
            _make_message(3, "tool", content_preview="tool-result-1"),
            _make_message(4, "user", content_preview="第二问"),
            _make_message(5, "assistant", content_preview="第二答"),
        ]
        mod.set_capability_caller(caller)

        result = await mod.read_execution_detail(
            pipeline_run_id="pipe-1", level="L1"
        )

        assert result["level"] == "L1"
        # 轮次聚合结果：应有 2 轮
        turns = result.get("turns") or result.get("summary", {}).get("turns")
        assert turns is not None, "L1 结果应包含 turns（轮次列表）"
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
        caller.return_value = [_make_message(1, "user", content_preview="hi")]
        mod.set_capability_caller(caller)

        # 注入后模块级 _capability_caller 应为该 caller
        assert mod._capability_caller is caller

        await mod.read_execution_detail(pipeline_run_id="pipe-1", level="skeleton")

        caller.assert_awaited_once()
