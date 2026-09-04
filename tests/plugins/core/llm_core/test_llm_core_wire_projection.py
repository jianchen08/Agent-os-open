# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""llm_core 发送边界 wire 投影测试：管道内部字段不得进 LLM 载荷。

背景（2026-09-03 实锤）：内核把 client_message_id 幂等键放 user 消息 metadata
落库（router.rs ADR-2026-08-21），压缩块带 seq/metadata.compression_ref——
宽松 provider（minimax/deepseek）容忍未知字段，严格 provider（zhipu
错误码 1210/1214）对 messages 内未知字段整请求拒绝，主聊天全轮失败。

契约：history/compression_messages 出站载荷只含 wire 字段 + 适配层契约字段
（reasoning_content 由 provider 适配层的采样保留策略消费，不在此剥离）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _load_llm_core() -> Any:
    """唯一名动态加载 plugin.py（裸名 `import plugin` 会被兄弟插件目录串扰）。"""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins" / "shared" / "pipeline" / "core" / "llm_core" / "plugin.py"
    )
    mod_name = "llm_core_wire_projection_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def core() -> Any:
    mod = _load_llm_core()
    return mod.LLMCore.__new__(mod.LLMCore)  # 只用 _build_messages，绕开 __init__ 依赖


def test_user_message_metadata_not_sent(core: Any) -> None:
    """user 消息的 metadata（内核幂等键落库字段）不得进载荷——zhipu 1210 实锤。"""
    state = {
        "messages": [
            {
                "role": "user",
                "content": "你好",
                "metadata": {"client_message_id": "abc-123", "source": "User"},
                "seq": 7,
            }
        ],
    }
    msgs = core._build_messages(state)  # noqa: SLF001
    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "你好"}


def test_assistant_internal_fields_stripped_tool_calls_kept(core: Any) -> None:
    """assistant 的 status/llm_error_info 剥离；tool_calls 是 wire 字段必须保留。"""
    tool_calls = [{"id": "call_x", "type": "function",
                   "function": {"name": "file_read", "arguments": "{}"}}]
    state = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "status": "error",
                "llm_error_info": {"error_type": "RuntimeError", "error_message": "boom"},
                "tool_calls": tool_calls,
            }
        ],
    }
    msgs = core._build_messages(state)  # noqa: SLF001
    assert msgs[0]["tool_calls"] == tool_calls
    assert "status" not in msgs[0]
    assert "llm_error_info" not in msgs[0]


def test_reasoning_content_kept_for_provider_adapter(core: Any) -> None:
    """reasoning_content 不在边界剥离——provider 适配层的采样保留策略在下游消费它。"""
    state = {
        "messages": [
            {"role": "assistant", "content": "答", "reasoning_content": "推理过程"},
        ],
    }
    msgs = core._build_messages(state)  # noqa: SLF001
    assert msgs[0]["reasoning_content"] == "推理过程"


def test_tool_message_tool_result_envelope_stripped(core: Any) -> None:
    """tool 消息的 tool_result 持久化信封剥离，tool_call_id 保留。"""
    state = {
        "messages": [
            {
                "role": "tool",
                "content": "结果",
                "tool_call_id": "call_y",
                "tool_result": {"raw": {"v": 1}},
            }
        ],
    }
    msgs = core._build_messages(state)  # noqa: SLF001
    assert msgs[0] == {"role": "tool", "content": "结果", "tool_call_id": "call_y"}


def test_compression_block_only_wire_fields_sent(core: Any) -> None:
    """压缩块（seq/_context_form/metadata.compression_ref）只出 role/content/name。

    compression_messages 段与 history 段同契约——zhipu 严格校验下块消息的
    内部字段同样致命。
    """
    state = {
        "compression_messages": [
            {
                "role": "system",
                "name": "compressed",
                "content": "<compressed seq=\"1-9\" level=\"L1\">摘要</compressed>",
                "seq": 1,
                "_context_form": "recall",
                "metadata": {"compression_ref": {"kind": "process", "memory_ids": []}},
            }
        ],
    }
    msgs = core._build_messages(state)  # noqa: SLF001
    assert msgs == [
        {
            "role": "system",
            "name": "compressed",
            "content": "<compressed seq=\"1-9\" level=\"L1\">摘要</compressed>",
        }
    ]
