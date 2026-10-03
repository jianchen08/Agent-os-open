# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 消息级思考参数覆盖测试（思考全链路，选项即参数组语义）。

推演链：思考选择需求 → 决策（ADR 2026-10-03）「选项 = thinking_strength_params
配置的参数组本身，无档位映射层」——
- resolve_thinking_params：消息 thinking_strength = 参数组 JSON 串，解析 +
  白名单过滤即覆盖集；空/非 JSON/非对象 → 不覆盖（无档位查表）
- 白名单过滤：temperature/max_tokens 等采样参数永不随消息覆盖
- _call_llm 集成：state.thinking_strength 非空时 kwargs 被覆盖并最终传给
  adapter；关闭形态参数组与其他参数组同一条覆盖路径
"""

from __future__ import annotations

import json
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
from llm_core.plugin import LLMCore, resolve_thinking_params  # noqa: E402


def _json(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ─────────────────── 纯函数：参数组 JSON → 覆盖集 ───────────────────

def test_empty_or_non_json_means_no_override() -> None:
    """空串（未选择）/ 旧档位裸词 / 非 JSON / 非对象 → 不覆盖。"""
    assert resolve_thinking_params("") is None
    assert resolve_thinking_params("high") is None
    assert resolve_thinking_params("off") is None
    assert resolve_thinking_params("not-json") is None
    assert resolve_thinking_params(_json(["reasoning_effort", "max"])) is None


def test_param_set_passes_through() -> None:
    """参数组 JSON 原样透出（键在白名单内），无档位查表。"""
    params = {"reasoning_effort": "max", "thinking": {"type": "enabled"}}
    assert resolve_thinking_params(_json(params)) == params


def test_off_shape_is_a_param_set_not_a_skip_sentinel() -> None:
    """关闭形态参数组（thinking.type=disabled / reasoning_effort=none）与其他
    参数组同一条覆盖路径——选中即覆盖，模型 default_params 自带的思考开启
    被关闭值压制。"""
    assert resolve_thinking_params(_json({"thinking": {"type": "disabled"}})) == {
        "thinking": {"type": "disabled"}
    }
    assert resolve_thinking_params(_json({"reasoning_effort": "none"})) == {
        "reasoning_effort": "none"
    }


def test_sampling_params_filtered() -> None:
    """temperature/max_tokens 不随消息覆盖（白名单过滤，参数组里写了也忽略）。"""
    raw = _json({"reasoning_effort": "max", "temperature": 0.3, "max_tokens": 100000})
    assert resolve_thinking_params(raw) == {"reasoning_effort": "max"}
    # 全键被过滤 → 不覆盖
    assert resolve_thinking_params(_json({"temperature": 0.5})) is None


# ─────────────────── 真机配置闸（llm.yaml）───────────────────

def _llm_yaml() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(
        (_REPO_ROOT / "config" / "plugins" / "llm" / "llm.yaml").read_text(encoding="utf-8")
    )


def test_llm_yaml_carries_vendor_strength_param_sets() -> None:
    """真实配置源：llm.yaml providers 段承载厂商级参数组（选项真值源）。"""
    providers = _llm_yaml()["providers"]
    # GLM-5 系思考档位 = reasoning_effort low/high/max（BigModel coding-plan
    # 实际参数面，thinking.type 旧 4 档词汇废弃；键 = 设置页 levels 词汇）
    glm_params = {
        "high": {"reasoning_effort": "max"},
        "medium": {"reasoning_effort": "high"},
        "low": {"reasoning_effort": "low"},
    }
    assert providers["zhipu"]["thinking_strength_params"] == glm_params
    assert providers["zhipu_coding"]["thinking_strength_params"] == glm_params
    # 性质断言：档位键 = 设置页 levels 清单，值域 ⊆ GLM-5 实际 effort 档
    for pid in ("zhipu", "zhipu_coding"):
        params = providers[pid]["thinking_strength_params"]
        assert set(params) == {"high", "medium", "low"}
        assert all(
            set(v) == {"reasoning_effort"} and v["reasoning_effort"] in {"low", "high", "max"}
            for v in params.values()
        )
    # minimax 无厂商级参数组：M3 与 M3.1 两代契约不同（M3 认 thinking.type，
    # M3.1-Flash-Preview 只认 reasoning_effort 且强制 adaptive 无关闭档，
    # 2026-09-28 接口实测），单一厂商级表无法同时为真，参数组落模型级。
    assert "thinking_strength_params" not in providers["minimax"]
    assert providers["deepseek"]["thinking_strength_params"] == {
        "high": {"reasoning_effort": "max"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "off": {"thinking": {"type": "disabled"}},
    }
    assert providers["openai"]["thinking_strength_params"] == {
        "high": {"reasoning_effort": "high"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
    }


def test_llm_yaml_minimax_model_level_param_sets() -> None:
    """minimax 模型级参数组：M3 保 thinking 形态（M3 忽略 reasoning_effort，
    2026-09-28 接口实测）；M3.1-Flash-Preview 用 effort 参数组（high→max 同
    deepseek 先例；off 无厂商关闭形态，落最低档 low——厂商强制 adaptive
    thinking，none/disabled 均 400）。"""
    models = _llm_yaml()["models"]
    assert models["minimax-m3"]["thinking_strength_params"] == {
        "high": {"thinking": {"type": "adaptive"}},
        "low": {"thinking": {"type": "disabled"}},
        "medium": {"thinking": {"type": "adaptive"}},
        "off": {"thinking": {"type": "disabled"}},
    }
    assert models["minimax-m3.1-flash-preview"]["thinking_strength_params"] == {
        "high": {"reasoning_effort": "max"},
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "off": {"reasoning_effort": "low"},
    }


def test_llm_yaml_gear_keys_are_strings() -> None:
    """参数组键必须是字符串：YAML 1.1 把裸 ``off`` 解析为布尔 False
    （键是设置页编辑行标识与选项排序锚点，非字符串键破坏编辑面）。"""
    for provider_name, mapping in _llm_yaml()["providers"].items():
        params = mapping.get("thinking_strength_params")
        if not isinstance(params, dict):
            continue
        for gear in params:
            assert isinstance(gear, str), (
                f"providers.{provider_name} 参数组键 {gear!r} 非字符串"
                "（YAML 裸 off 会被解析成布尔 False）"
            )


def test_thinking_levels_endpoint_covers_every_reasoning_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端闸：每个推理模型的 thinking-levels 选项面非空且可解析——
    聊天页选择器对推理模型不空转（选项=参数组，value 即消息线上形态）。

    _llm_yaml_path 固定到出厂配置：闸守的是出厂种子面（用户空间接管文件
    是运行时生效视图，内容随时可变，不作闸真值源）。"""
    import yaml

    data = yaml.safe_load(
        (_REPO_ROOT / "config" / "plugins" / "llm" / "llm.yaml").read_text(encoding="utf-8")
    )
    spec = _load_routes_llm_config()
    monkeypatch.setattr(
        spec,
        "_llm_yaml_path",
        lambda: _REPO_ROOT / "config" / "plugins" / "llm" / "llm.yaml",
    )
    for model_id, model in data["models"].items():
        if not model.get("reasoning_model"):
            continue
        result = spec.get_thinking_levels(str(model.get("model_name") or model_id))
        assert result["options"], f"推理模型 {model_id} 无思考参数组选项"
        for option in result["options"]:
            assert isinstance(option["params"], dict) and option["params"]


def _load_routes_llm_config() -> Any:
    """按显式路径加载 llm_service routes_llm_config（裸名防劫持）。"""
    import importlib.util

    target = _SYSTEM_LLM_DIR / "routes_llm_config.py"
    spec = importlib.util.spec_from_file_location(
        "llm_routes_llm_config_strength_gate_test", str(target)
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_routes_llm_config_strength_gate_test"] = m
    spec.loader.exec_module(m)
    return m


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
async def test_call_llm_without_selection_is_noop() -> None:
    """未选择（thinking_strength=''）→ 不覆盖任何参数，采样参数保持 default_params。"""
    caller = _CapturingCaller()
    plugin = _make_plugin(caller)
    ctx = _make_ctx(
        {
            "thinking_strength": "",
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
async def test_call_llm_applies_selected_param_set() -> None:
    """选中参数组（JSON 串）→ 覆盖集直出（reasoning_effort=max），采样参数
    不随消息覆盖。"""
    caller = _CapturingCaller()
    plugin_mod.set_capability_caller(caller)
    plugin = LLMCore(
        {
            "provider": "openai",
            "model_name": "deepseek-v4-flash",
            "default_params": {"temperature": 0.7, "max_tokens": 100000},
        }
    )
    ctx = _make_ctx(
        {
            "thinking_strength": _json({"reasoning_effort": "max"}),
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
    assert args["reasoning_effort"] == "max"
    assert args["temperature"] == 0.7
    assert args["max_tokens"] == 100000


@pytest.mark.asyncio
async def test_call_llm_keeps_defaults_when_strength_missing() -> None:
    """无 thinking_strength 键 → 保持 default_params（现状不变）。"""
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
async def test_call_llm_legacy_gear_word_is_noop() -> None:
    """旧档位词汇（裸键名串）非 JSON → 不覆盖（一刀切，不抛错不静默映射）。"""
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
    assert "thinking" not in args


@pytest.mark.asyncio
async def test_call_llm_disable_param_set_overrides_enabled_default() -> None:
    """选中关闭形态参数组 → 覆盖 default_params 自带的思考开启。

    真机语义：default_params 自带 thinking=enabled + reasoning_effort=max，
    只覆盖 thinking=disabled（不覆盖 effort）实测已归零思考。"""
    caller = _CapturingCaller()
    plugin_mod.set_capability_caller(caller)
    plugin = LLMCore(
        {
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
            "default_params": {
                "temperature": 0.7,
                "max_tokens": 100000,
                "thinking": {"type": "enabled"},
                "reasoning_effort": "max",
            },
        }
    )
    ctx = _make_ctx(
        {
            "thinking_strength": _json({"thinking": {"type": "disabled"}}),
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
    # 关闭参数覆盖了 default_params 的开启值
    assert args["thinking"] == {"type": "disabled"}
    # default_params 的 effort 保留（参数组未携带不覆盖）；采样参数同
    assert args["reasoning_effort"] == "max"
    assert args["temperature"] == 0.7
    assert args["max_tokens"] == 100000


@pytest.mark.asyncio
async def test_call_llm_applies_thinking_toggle_param_set() -> None:
    """thinking 开关参数组（GLM 二态 thinking）→ thinking 直出，
    reasoning_effort 不出现。"""
    caller = _CapturingCaller()
    plugin_mod.set_capability_caller(caller)
    plugin = LLMCore(
        {
            "provider": "zhipu",
            "model_name": "glm-5.3-flash",
            "default_params": {"temperature": 0.7, "max_tokens": 4096},
        }
    )
    ctx = _make_ctx(
        {
            "thinking_strength": _json({"thinking": {"type": "enabled"}}),
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
