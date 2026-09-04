# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 思考强度 → 模型参数路由测试（思考强度全链路）。

推演链：思考强度需求 → 决策「强度随 user_input 透传，llm_core 在请求构造时
覆盖思考参数」→ 裁定（2026-09-03）：所有映射显式，不在代码里靠推断——
- resolve_thinking_strength_params：厂商级映射（providers.<name>）> 模型级
  手填（models.<id>）；无显式映射 → 不覆盖（无内置兜底表）
- 白名单过滤：temperature/max_tokens 等采样参数永不随强度覆盖
- _call_llm 集成：state.thinking_strength 非空且映射命中时 kwargs 被覆盖并
  最终传给 adapter
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
# 去重插入到 [0]：车道共跑时其他插件目录（如 channel_feishu）可能残留于
# sys.path 前部，幂等跳过会让 plugin.py 的 `from adapter import` 命中他插件。
for _d in (_LLM_CORE_DIR, _CORE_DIR, _SHARED_DIR, _SYSTEM_LLM_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

import llm_core.plugin as plugin_mod  # noqa: E402
from llm_core.plugin import LLMCore, resolve_thinking_strength_params  # noqa: E402

# ─────────────────── 纯函数：强度 → 参数映射 ───────────────────

def test_no_mapping_means_no_override() -> None:
    """无任何显式映射 → 任何档位都不覆盖（无代码内置兜底，映射全显式）。"""
    for strength in ("low", "medium", "high"):
        assert resolve_thinking_strength_params(strength) is None
        assert resolve_thinking_strength_params(strength, None, provider_params=None) is None
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
    # thinking 开关属于思考参数，保留；映射直接生效（无内置表字段合并）
    model_params2 = {"medium": {"thinking": {"type": "enabled"}, "temperature": 0.5}}
    merged = resolve_thinking_strength_params("medium", model_params2)
    assert merged == {"thinking": {"type": "enabled"}}


def test_strength_params_model_config_override() -> None:
    """模型配置了 thinking_strength_params → 按模型路由规则覆盖（逐档直取）。"""
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


def test_strength_params_partial_model_config_missing_tier_no_override() -> None:
    """模型只配置了部分档位 → 未配置的档位不覆盖（显式映射缺失即无操作，不回退代码）。"""
    model_params = {"high": {"reasoning_effort": "max"}}
    assert resolve_thinking_strength_params("high", model_params) == {
        "reasoning_effort": "max"
    }
    # low 未配置 → 不覆盖
    assert resolve_thinking_strength_params("low", model_params) is None


# ─────────────── 厂商级映射（providers.<name>.thinking_strength_params）───────

def test_provider_params_map_directly_without_builtin_merge() -> None:
    """厂商级映射命中档位 → 直接映射（厂商参数即上游真实契约）。"""
    provider_params = {
        "high": {"thinking": {"type": "enabled"}},
        "low": {"thinking": {"type": "disabled"}},
    }
    assert resolve_thinking_strength_params("high", provider_params=provider_params) == {
        "thinking": {"type": "enabled"}
    }
    assert resolve_thinking_strength_params("low", provider_params=provider_params) == {
        "thinking": {"type": "disabled"}
    }


def test_provider_params_filtered_by_whitelist() -> None:
    """厂商映射同样只透传思考参数（采样参数不随强度覆盖）。"""
    provider_params = {"high": {"thinking": {"type": "enabled"}, "temperature": 0.3}}
    assert resolve_thinking_strength_params("high", provider_params=provider_params) == {
        "thinking": {"type": "enabled"}
    }


def test_provider_params_wins_over_model_manual_params() -> None:
    """厂商级映射优先于模型手填：同一档位厂商值生效。"""
    provider_params = {"high": {"thinking": {"type": "enabled"}}}
    manual = {"high": {"reasoning_effort": "max"}}
    assert resolve_thinking_strength_params(
        "high", manual, provider_params=provider_params
    ) == {"thinking": {"type": "enabled"}}


def test_provider_params_missing_strength_falls_through() -> None:
    """off/未知档位 → 不覆盖；厂商映射缺档位 → 回退手填，再缺 → 不覆盖。"""
    provider_params = {"high": {"thinking": {"type": "enabled"}}}
    manual = {"low": {"reasoning_effort": "low"}}
    assert resolve_thinking_strength_params("off", provider_params=provider_params) is None
    assert resolve_thinking_strength_params("ultra", provider_params=provider_params) is None
    # 厂商映射缺 low 档 → 手填参与
    assert resolve_thinking_strength_params(
        "low", manual, provider_params=provider_params
    ) == {"reasoning_effort": "low"}
    # 厂商映射缺 low 档、无手填 → 不覆盖（显式映射缺失即无操作）
    assert resolve_thinking_strength_params(
        "low", None, provider_params=provider_params
    ) is None


def test_llm_yaml_carries_vendor_strength_mappings() -> None:
    """真实配置源：llm.yaml providers 段承载厂商级映射（厂商事实唯一落点）。"""
    import yaml

    data = yaml.safe_load(
        (_REPO_ROOT / "config" / "models" / "llm.yaml").read_text(encoding="utf-8")
    )
    providers = data["providers"]
    glm_params = {
        "high": {"thinking": {"type": "enabled"}},
        "low": {"thinking": {"type": "disabled"}},
        "medium": {"thinking": {"type": "enabled"}},
    }
    assert providers["zhipu"]["thinking_strength_params"] == glm_params
    assert providers["zhipu_coding"]["thinking_strength_params"] == glm_params
    assert providers["minimax"]["thinking_strength_params"] == {
        "high": {"thinking": {"type": "adaptive"}},
        "low": {"thinking": {"type": "disabled"}},
        "medium": {"thinking": {"type": "adaptive"}},
    }
    assert providers["deepseek"]["thinking_strength_params"] == {
        "high": {"reasoning_effort": "max"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
    }
    assert providers["openai"]["thinking_strength_params"] == {
        "high": {"reasoning_effort": "high"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
    }


# ─────────────────── 集成：_call_llm 覆盖 kwargs ───────────────────

class _CapturingCaller:
    """伪 capability caller：记录 tool-executor 能力调用的 method 与 args。

    method 契约：capability 短名（SDK 句柄内部组装 <capability>.<method> 全名，
    传全名会双重前缀——真机 method not implemented 的回归形态）。"""

    def __init__(self) -> None:
        self.captured_args: dict[str, Any] = {}
        self.captured_method: str | None = None

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.captured_method = method
        self.captured_args = dict(params["args"])
        # tool-executor.invoke 信封（内核 ToolExecutionResult 序列化形态）
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
        }


def _make_plugin(caller: Any) -> LLMCore:
    plugin_mod.set_capability_caller(caller)
    return LLMCore(
        {
            "provider": "openai",
            "model_name": "deepseek-v3",
            "default_params": {"temperature": 0.7, "max_tokens": 4096},
        }
    )


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
async def test_call_llm_without_mapping_strength_is_noop() -> None:
    """无任何显式映射 + strength=high → 不覆盖任何参数（映射全显式，无代码兜底），
    采样参数保持 default_params。"""
    caller = _CapturingCaller()
    plugin = _make_plugin(caller)
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

    args = caller.captured_args
    # capability 短名契约：传全名会被 SDK 拼成双重前缀（真机 not implemented）
    assert caller.captured_method == "invoke"
    assert "reasoning_effort" not in args
    assert "thinking" not in args
    assert args["temperature"] == 0.7
    assert args["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_call_llm_applies_model_config_strength_params() -> None:
    """模型级 thinking_strength_params（llm.yaml）按档直取（provider 未配置厂商
    映射场景）；只覆盖思考参数。"""
    caller = _CapturingCaller()
    plugin_mod.set_capability_caller(caller)
    plugin = LLMCore(
        {
            "provider": "openai",
            "model_name": "deepseek-v4-flash",
            "default_params": {"temperature": 0.7, "max_tokens": 100000},
            "thinking_strength_params": {
                "low": {"reasoning_effort": "low"},
                "medium": {"reasoning_effort": "medium"},
                "high": {"reasoning_effort": "max"},
            },
        }
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

    args = caller.captured_args
    # 模型级配置生效：reasoning_effort=max（非内置 high 的 high）
    assert args["reasoning_effort"] == "max"
    # temperature/max_tokens 不随强度覆盖
    assert args["temperature"] == 0.7
    assert args["max_tokens"] == 100000


@pytest.mark.asyncio
async def test_call_llm_keeps_defaults_when_strength_missing() -> None:
    """无 thinking_strength → 保持 default_params（现状不变）。"""
    caller = _CapturingCaller()
    plugin = _make_plugin(caller)
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

    args = caller.captured_args
    assert args["temperature"] == 0.7
    assert args["max_tokens"] == 4096
    assert "reasoning_effort" not in args


@pytest.mark.asyncio
async def test_call_llm_off_keeps_defaults() -> None:
    """thinking_strength=off → 不覆盖（显式关闭 = 普通模式，保持默认参数）。"""
    caller = _CapturingCaller()
    plugin = _make_plugin(caller)
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

    args = caller.captured_args
    assert args["temperature"] == 0.7
    assert "reasoning_effort" not in args


@pytest.mark.asyncio
async def test_call_llm_applies_vendor_params_for_glm() -> None:
    """厂商级映射（桥接注入 provider_thinking_strength_params）→ high 直接映射
    GLM thinking 二态，reasoning_effort 不出现。"""
    caller = _CapturingCaller()
    plugin_mod.set_capability_caller(caller)
    plugin = LLMCore(
        {
            "provider": "zhipu",
            "model_name": "glm-5.3-flash",
            "default_params": {"temperature": 0.7, "max_tokens": 4096},
            "provider_thinking_strength_params": {
                "high": {"thinking": {"type": "enabled"}},
                "low": {"thinking": {"type": "disabled"}},
                "medium": {"thinking": {"type": "enabled"}},
            },
        }
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

    args = caller.captured_args
    assert args["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in args
    assert args["temperature"] == 0.7
