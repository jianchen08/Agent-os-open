# @feature: FP-T07 llm api | @ci: none-local（不在任何 CI 车道：python-coverage 的 BASE_TEST_PATHS 未收集本文件）
"""llm_core 思考强度 → 模型参数路由测试（思考强度全链路）。

推演链：思考强度需求 → 决策「强度随 user_input 透传，llm_core 在请求构造时
覆盖模型参数（temperature/max_tokens/reasoning_effort）」→ 功能点：
- resolve_thinking_strength_params：off/缺失 → 不覆盖；low/medium/high →
  对应参数集（与前端 STRENGTH_TO_PARAMS 对齐）
- _call_llm 集成：state.thinking_strength 非空时 kwargs 被覆盖并最终传给 adapter
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
for _d in (_LLM_CORE_DIR, _CORE_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import llm_core.plugin as plugin_mod  # noqa: E402
from llm_core.plugin import LLMCore, resolve_thinking_strength_params  # noqa: E402

# ─────────────────── 纯函数：强度 → 参数映射 ───────────────────

def test_strength_params_mapping() -> None:
    """low/medium/high → 思考参数集（仅 reasoning_effort）；off/未知/空 → None。"""
    assert resolve_thinking_strength_params("low") == {"reasoning_effort": "low"}
    assert resolve_thinking_strength_params("medium") == {"reasoning_effort": "medium"}
    assert resolve_thinking_strength_params("high") == {"reasoning_effort": "high"}
    assert resolve_thinking_strength_params("off") is None
    assert resolve_thinking_strength_params("") is None
    assert resolve_thinking_strength_params("ultra") is None


def test_strength_params_temperature_max_tokens_ignored() -> None:
    """temperature/max_tokens 不随强度覆盖（白名单过滤，即使用户配置里写了也忽略）。"""
    # 用户配置里写了采样参数 → 被过滤，只留思考参数
    model_params = {
        "high": {
            "temperature": 0.3,
            "max_tokens": 100000,
            "reasoning_effort": "max",
        },
    }
    assert resolve_thinking_strength_params("high", model_params) == {
        "reasoning_effort": "max"
    }
    # thinking 开关属于思考参数，保留；与内置 effort 字段级合并（模型字段覆盖）
    model_params2 = {"medium": {"thinking": {"type": "enabled"}, "temperature": 0.5}}
    merged = resolve_thinking_strength_params("medium", model_params2)
    assert merged == {
        "reasoning_effort": "medium",
        "thinking": {"type": "enabled"},
    }


def test_strength_params_model_config_override() -> None:
    """模型配置了 thinking_strength_params → 按模型路由规则覆盖（逐档）。"""
    model_params = {
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "max"},
    }
    assert resolve_thinking_strength_params("low", model_params) == {
        "reasoning_effort": "low",
    }
    assert resolve_thinking_strength_params("high", model_params) == {
        "reasoning_effort": "max",
    }
    # off/未知 → 仍不覆盖
    assert resolve_thinking_strength_params("off", model_params) is None
    assert resolve_thinking_strength_params("nope", model_params) is None


def test_strength_params_partial_model_config_falls_back() -> None:
    """模型只配置了部分档位 → 未配置的档位回退内置默认表（不丢覆盖能力）。"""
    model_params = {"high": {"reasoning_effort": "max"}}
    assert resolve_thinking_strength_params("high", model_params) == {
        "reasoning_effort": "max"
    }
    # low 未配置 → 回退内置默认
    assert resolve_thinking_strength_params("low", model_params) == {
        "reasoning_effort": "low",
    }


# ─────────────────── 集成：_call_llm 覆盖 kwargs ───────────────────

def _make_plugin(adapter: Any) -> LLMCore:
    return LLMCore(
        {
            "provider": "openai",
            "model_name": "deepseek-v3",
            "default_params": {"temperature": 0.7, "max_tokens": 4096},
        },
        adapter=adapter,
    )


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
async def test_call_llm_applies_thinking_strength_params() -> None:
    """state.thinking_strength=high → 仅覆盖思考参数（reasoning_effort），
    temperature/max_tokens 保持 default_params 不变。"""
    adapter = MagicMock()
    adapter.completion = AsyncMock(
        return_value=MagicMock(choices=[], usage=None, content="ok"),
    )
    plugin = _make_plugin(adapter)
    ctx = _make_ctx(
        {
            "thinking_strength": "high",
            "pipeline_id": "p1",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    await plugin._call_llm(
        [{"role": "user", "content": "hi"}],
        ctx,
        stream=False,
    )

    kwargs = adapter.completion.call_args.kwargs
    assert kwargs["reasoning_effort"] == "high"
    # 采样参数不随强度覆盖（保持 default_params）
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_call_llm_applies_model_config_strength_params() -> None:
    """模型级 thinking_strength_params（llm.yaml）优先于内置表；只覆盖思考参数。"""
    adapter = MagicMock()
    adapter.completion = AsyncMock(
        return_value=MagicMock(choices=[], usage=None, content="ok"),
    )
    plugin = LLMCore(
        {
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
            "default_params": {"temperature": 0.7, "max_tokens": 100000},
            "thinking_strength_params": {
                "low": {"reasoning_effort": "low"},
                "medium": {"reasoning_effort": "medium"},
                "high": {"reasoning_effort": "max"},
            },
        },
        adapter=adapter,
    )
    ctx = _make_ctx(
        {
            "thinking_strength": "high",
            "pipeline_id": "p1",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    await plugin._call_llm(
        [{"role": "user", "content": "hi"}],
        ctx,
        stream=False,
    )

    kwargs = adapter.completion.call_args.kwargs
    # 模型级配置生效：reasoning_effort=max（非内置 high 的 high）
    assert kwargs["reasoning_effort"] == "max"
    # temperature/max_tokens 不随强度覆盖
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 100000


@pytest.mark.asyncio
async def test_call_llm_keeps_defaults_when_strength_missing() -> None:
    """无 thinking_strength → 保持 default_params（现状不变）。"""
    adapter = MagicMock()
    adapter.completion = AsyncMock(
        return_value=MagicMock(choices=[], usage=None, content="ok"),
    )
    plugin = _make_plugin(adapter)
    ctx = _make_ctx(
        {
            "pipeline_id": "p1",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    await plugin._call_llm(
        [{"role": "user", "content": "hi"}],
        ctx,
        stream=False,
    )

    kwargs = adapter.completion.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 4096
    assert "reasoning_effort" not in kwargs


@pytest.mark.asyncio
async def test_call_llm_off_keeps_defaults() -> None:
    """thinking_strength=off → 不覆盖（显式关闭 = 普通模式，保持默认参数）。"""
    adapter = MagicMock()
    adapter.completion = AsyncMock(
        return_value=MagicMock(choices=[], usage=None, content="ok"),
    )
    plugin = _make_plugin(adapter)
    ctx = _make_ctx(
        {
            "thinking_strength": "off",
            "pipeline_id": "p1",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    await plugin._call_llm(
        [{"role": "user", "content": "hi"}],
        ctx,
        stream=False,
    )

    kwargs = adapter.completion.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert "reasoning_effort" not in kwargs
