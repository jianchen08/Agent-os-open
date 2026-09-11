# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""llm_core execute 公开入口的出站行为测试（wire 投影 + 多模态解析的接线面）。

分工：test_llm_core_wire_projection / test_llm_core_multimodal_resolve 在
_build_messages 级钉契约；本文件把同一契约抬到 execute 公开入口——经
set_capability_caller 替身捕获真正发往 llm_service 的载荷，钉「插件真的把
投影后的消息发给 LLM 边界」的接线（execute 绕过 _build_messages 时本测试红，
而 _build_messages 级测试仍绿）。

LLM API（llm_service 经 tool-executor 能力跨进程调用）是外部依赖，替身合法。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_CORE_DIR = _LLM_CORE_DIR.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
# 平铺 import 路径（同 test_llm_core_partial_persist）：后插入者在 sys.path 更前，
# _LLM_CORE_DIR 必须压过 _SYSTEM_LLM_DIR——两处均有 adapter.py，平铺
# `import adapter` 须命中 llm_core 版；_SYSTEM_LLM_DIR 供 _config_models 解析。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

from llm_core.plugin import LLMCore, set_capability_caller  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_capability_caller():
    """测后清空模块级能力调用器，防跨测试残留。"""
    yield
    set_capability_caller(None)


class _CapturingCaller:
    """捕获 complete_stream 入参并回放成功响应的能力替身。"""

    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    async def __call__(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG002
        self.params.append(params)
        return {
            "success": True,
            "data": {
                "status": "streamed",
                "stream_id": "stream_test",
                "partial": None,
                "text": "ok",
                "tool_calls": [],
                "thinking_text": None,
                "usage": {},
                "finish_reason": "stop",
            },
            "error": None,
        }


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )


def _make_plugin() -> Any:
    return LLMCore(
        config={
            "provider": "openai",
            "model_name": "gpt-test",
            "model_id": "gpt-test",
            "context_window": 128000,
            "default_params": {},
        }
    )


_WIRE_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "reasoning_content"}


class TestExecuteWireProjection:
    """出站载荷只含 wire 字段——经 execute 捕获的 llm_service 载荷断言。"""

    def test_internal_fields_never_reach_llm_boundary(self, monkeypatch: Any) -> None:
        """history/compression 段的内部字段在出站载荷中不存在（zhipu 1210 回归）。"""
        caller = _CapturingCaller()
        set_capability_caller(caller)

        state = {
            "streaming": True,
            "messages": [
                {
                    "role": "user",
                    "content": "你好",
                    "metadata": {"client_message_id": "abc-123", "source": "User"},
                    "seq": 7,
                },
                {
                    "role": "assistant",
                    "content": "答",
                    "status": "error",
                    "llm_error_info": {"error_type": "RuntimeError", "error_message": "boom"},
                    "tool_result": {"raw": {"v": 1}},
                    "tool_calls": [
                        {"id": "call_x", "type": "function",
                         "function": {"name": "file_read", "arguments": "{}"}}
                    ],
                },
            ],
            "compression_messages": [
                {
                    "role": "system",
                    "name": "compressed",
                    "content": "<compressed>摘要</compressed>",
                    "seq": 1,
                    "_context_form": "recall",
                    "metadata": {"compression_ref": {"kind": "process", "memory_ids": []}},
                }
            ],
        }
        result = _make_plugin().execute(SimpleNamespace(state=state))
        import asyncio

        asyncio.run(result)

        assert len(caller.params) == 1, "execute 应恰好发起一次 LLM 能力调用"
        wire_msgs = caller.params[0]["args"]["messages"]
        # 压缩段在前、history 在后，三条消息全部到达边界
        assert [m["role"] for m in wire_msgs] == ["system", "user", "assistant"]
        for msg in wire_msgs:
            extra = set(msg) - _WIRE_MESSAGE_KEYS
            assert not extra, f"内部字段泄漏到 LLM 载荷: {extra}（消息 role={msg['role']}）"
        # wire 字段本体保真：tool_calls 是合法出站字段必须保留
        assistant = next(m for m in wire_msgs if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["id"] == "call_x"

    def test_multimodal_local_ref_arrives_as_data_url(self, tmp_path: Any, monkeypatch: Any) -> None:
        """/uploads 本地引用在出站载荷里已是 data URL（二进制不落持久层）。"""
        (tmp_path / "cat.png").write_bytes(_png_bytes())
        monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
        caller = _CapturingCaller()
        set_capability_caller(caller)

        state = {
            "streaming": True,
            "messages": [{"role": "user", "content": "看图"}],
            "multimodal_content": [
                {"type": "image_url", "image_url": {"url": "/uploads/cat.png"}},
            ],
        }
        import asyncio

        plugin = _make_plugin()
        asyncio.run(plugin.execute(SimpleNamespace(state=state)))

        wire_msgs = caller.params[0]["args"]["messages"]
        last_user = [m for m in wire_msgs if m["role"] == "user"][-1]
        assert isinstance(last_user["content"], list), "多模态内容应合并为分段 content"
        img_part = next(p for p in last_user["content"] if p["type"] == "image_url")
        url = img_part["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        payload = url.split(",", 1)[1]
        assert base64.b64decode(payload) == _png_bytes(), "引用必须解析为原始字节"
