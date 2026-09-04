# @feature: FP-T07 llm api | @ci: python-coverage
"""思考参数 extra_body 透传通道测试（litellm type=openai/zai）。

背景：litellm 对 reasoning_effort/thinking 顶层 kwargs——模型不在其注册表时
抛 UnsupportedParamsError，在注册表时原样转发（OpenAI SDK create() 无此形参
→ TypeError）。这类 OpenAI 兼容端点的上游直接接受它们作为 body 字段，必须
经 extra_body 透传。

覆盖：
- _needs_extra_body_transport：前缀形态 / provider type 形态（生产传 model_id，
  litellm 前缀在 KeyPool 内层才拼上）/ 未注册回落 False
- completion() 真实链路：thinking/reasoning_effort 落进 extra_body 且与既有
  extra_body 合并；非通道 provider 参数保持顶层不动
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

pytestmark = pytest.mark.unit

import adapter as adapter_mod  # noqa: E402
import router_factory as rf_mod  # noqa: E402


def _seed_provider_maps(
    monkeypatch: pytest.MonkeyPatch,
    model_to_provider: dict[str, str],
    provider_types: dict[str, str],
) -> None:
    """替换 router_factory 模块级映射表（monkeypatch 负责恢复），短路懒加载。

    同时把裸名 router_factory 钉到本文件导入的实例：平铺布局下其他插件目录
    的测试会改写 sys.path/sys.modules，adapter 内的惰性 `from router_factory
    import ...` 必须解析到同一模块对象，seed 才生效。
    """
    monkeypatch.setitem(sys.modules, "router_factory", rf_mod)
    monkeypatch.setattr(rf_mod, "_model_to_provider", dict(model_to_provider))
    monkeypatch.setattr(rf_mod, "_provider_type_map", dict(provider_types))
    monkeypatch.setattr(rf_mod, "_ensure_provider_type_map_loaded", lambda: None)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-4o", True),
        ("zai/glm-5.3-flash", True),
        ("ZAI/GLM-5.3-Flash", True),
        ("minimax/MiniMax-M3", False),
        ("deepseek/deepseek-v4-pro", False),
    ],
)
def test_prefix_form(model: str, expected: bool) -> None:
    """model 自带 litellm 前缀的形态。"""
    assert adapter_mod._needs_extra_body_transport(model) is expected


def test_provider_type_form_model_id_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产形态：model 是不带前缀的 model_id，经 provider type 解析命中。"""
    _seed_provider_maps(monkeypatch, {"glm-5.3-flash": "zhipu"}, {"zhipu": "zai"})
    assert adapter_mod._needs_extra_body_transport("glm-5.3-flash") is True


def test_non_channel_provider_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """minimax/deepseek 等原生通道 provider 不走 extra_body。"""
    _seed_provider_maps(
        monkeypatch,
        {"MiniMax-M3": "minimax", "deepseek-v4-pro": "deepseek"},
        {"minimax": "minimax", "deepseek": "deepseek"},
    )
    assert adapter_mod._needs_extra_body_transport("MiniMax-M3") is False
    assert adapter_mod._needs_extra_body_transport("deepseek-v4-pro") is False


def test_unregistered_provider_falls_back_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """model 未注册 provider / provider 无 type 映射 → False（维持 litellm 原生行为）。"""
    _seed_provider_maps(monkeypatch, {}, {"zhipu": "zai"})
    assert adapter_mod._needs_extra_body_transport("glm-5.3-flash") is False
    # provider 注册了 model 但 type 表缺项：get_litellm_prefix 抛错 → False
    _seed_provider_maps(monkeypatch, {"glm-5.3-flash": "zhipu"}, {})
    assert adapter_mod._needs_extra_body_transport("glm-5.3-flash") is False


# ─────────────────── completion() 真实链路 ───────────────────


def _fake_response() -> Any:
    message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


class _CapturingAdapter(adapter_mod._BaseLiteLLMAdapter):
    """记录 _do_completion 实收 kwargs 的桩适配器（走真实 completion 链路）。"""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return _fake_response()


@pytest.mark.asyncio
async def test_completion_moves_thinking_to_extra_body_for_zai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_provider_maps(monkeypatch, {"glm-5.3-flash": "zhipu"}, {"zhipu": "zai"})
    stub = _CapturingAdapter()

    await stub.completion(
        model="glm-5.3-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        thinking={"type": "enabled"},
        reasoning_effort="max",
    )

    assert stub.captured["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    assert "thinking" not in stub.captured
    assert "reasoning_effort" not in stub.captured
    assert stub.captured["temperature"] == 0.7


@pytest.mark.asyncio
async def test_completion_merges_into_existing_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_provider_maps(monkeypatch, {"glm-5.2": "zhipu_coding"}, {"zhipu_coding": "zai"})
    stub = _CapturingAdapter()

    await stub.completion(
        model="glm-5.2",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"tool_stream": True},
        thinking={"type": "enabled"},
    )

    assert stub.captured["extra_body"] == {
        "tool_stream": True,
        "thinking": {"type": "enabled"},
    }


@pytest.mark.asyncio
async def test_completion_keeps_top_level_for_other_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 extra_body 通道 provider：thinking 保持顶层（MiniMax 原生接受）。"""
    _seed_provider_maps(monkeypatch, {"MiniMax-M3": "minimax"}, {"minimax": "minimax"})
    stub = _CapturingAdapter()

    await stub.completion(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "adaptive"},
    )

    assert stub.captured["thinking"] == {"type": "adaptive"}
    assert "extra_body" not in stub.captured
