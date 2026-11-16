# @feature: FP-0.2.〇 管道引擎与插件执行模型（条件注入接线） | @ci: python-coverage
"""prompt_build 条件注入集成（组装路径接线 + 落定键路由）。

契约：命中规则（when.model 读落定键 state.model_id）的静态内容按 order
追加在 system_message 尾部——骨架与既有段落前缀逐字节不变（尾部追加，
前缀缓存稳定性依赖此）；未命中 = 零注入（输出与接线前完全一致）。
本测试走 shipped 规则文件（prompts/contributions.yaml）真实装载。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent

for _d in (_SHARED_DIR, _PLUGIN_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_contrib_integration_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


def _system_content(state: dict[str, Any]) -> str:
    plugin = _load_plugin_module().PromptBuildPlugin()
    updates = _run(plugin._do_work(_make_ctx(state)))
    return updates["system_message"]["content"]


class TestContributionWiring:
    def test_deepseek_model_appends_profile_tail(self) -> None:
        content = _system_content(
            {"context.system_prompt": "骨架内容", "model_id": "deepseek-v4-pro"}
        )

        assert content.startswith("骨架内容")
        assert "exit_code" in content

    @pytest.mark.parametrize("model_id", ["minimax-m3.1-flash-preview", "glm-4.7"])
    def test_other_model_zero_injection(self, model_id: str) -> None:
        content = _system_content({"context.system_prompt": "骨架内容", "model_id": model_id})

        assert content == "骨架内容"

    def test_no_model_key_zero_injection(self) -> None:
        """落定键缺席（落定步未接线/降级）→ 零注入，与接线前行为一致。"""
        content = _system_content({"context.system_prompt": "骨架内容"})

        assert content == "骨架内容"

    def test_prefix_byte_identical_tail_append(self) -> None:
        """性质断言：命中与否只差尾部追加——命中输出以未命中输出为前缀。"""
        baseline = _system_content({"context.system_prompt": "骨架内容"})
        hit = _system_content(
            {"context.system_prompt": "骨架内容", "model_id": "deepseek-v4-flash"}
        )

        tail = hit[len(baseline):].strip()
        assert hit.startswith(baseline)
        assert tail
        assert "exit_code" in tail

    def test_tier_fallback_routes_without_pin(self) -> None:
        """落定键缺席时经 model_tier 候选命中（兼容路径，任务稳定值）。"""
        content = _system_content(
            {"context.system_prompt": "骨架内容", "model_tier": "deepseek-v4-pro"}
        )

        assert "exit_code" in content
