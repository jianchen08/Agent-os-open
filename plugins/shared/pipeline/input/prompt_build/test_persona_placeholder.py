# @feature: FP-0.2.四 模式体系 附身人设接管 | @vision: V1 可进化 | @ci: none-local
"""{{persona:<缺省路径>}} 占位符行为测试（人设注入点通用机制，2026-09-28）。

契约：
1. state.context.persona_text 在场（模式接管）→ 展开为该文本（优先级最高）；
2. 无接管 → 读缺省路径文件（主 agent 人设，相对系统项目根）；
3. 两者皆缺 → 空串 + warning 留痕（提示词渲染永不失败）；
4. 端到端：system_prompt 模板中 {{persona:...}} 经 _resolve_placeholders 整体
   替换，骨架其余部分不动。

文件面一律真实临时目录；monkeypatch 仅注入系统根。
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
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_persona_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _plugin_with_root(tmp_path: Path) -> Any:
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    plugin._system_root = lambda: tmp_path  # 系统根注入（相对路径解析基准）
    return plugin


def test_persona_override_in_state_wins(tmp_path: Path) -> None:
    """state.context.persona_text 在场 → 展开为该文本（模式接管优先）。"""
    plugin = _plugin_with_root(tmp_path)
    ctx = PluginContext(state={"context.persona_text": "一位沉默寡言的老船长。"})
    out = _run(plugin._resolve_placeholder(ctx, "persona:config/agents/main/persona/p.md"))
    assert out == "一位沉默寡言的老船长。"


def test_persona_falls_back_to_default_file(tmp_path: Path) -> None:
    """无接管 → 读缺省路径文件内容。"""
    persona_file = tmp_path / "config" / "agents" / "main" / "persona" / "p.md"
    persona_file.parent.mkdir(parents=True)
    persona_file.write_text("你是灵汐，最贴心的伙伴。", encoding="utf-8")
    plugin = _plugin_with_root(tmp_path)
    out = _run(
        plugin._resolve_placeholder(PluginContext(state={}), "persona:config/agents/main/persona/p.md")
    )
    assert out == "你是灵汐，最贴心的伙伴。"


def test_persona_both_missing_yields_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """无接管且缺省路径不可读 → 空串 + warning（渲染不失败）。"""
    plugin = _plugin_with_root(tmp_path)
    with caplog.at_level("WARNING"):
        out = _run(
            plugin._resolve_placeholder(PluginContext(state={}), "persona:config/nope/p.md")
        )
    assert out == ""
    assert "persona" in caplog.text


def test_persona_placeholder_in_system_prompt_end_to_end(tmp_path: Path) -> None:
    """端到端：模板 {{persona:...}} 整体替换，骨架其余文本原样保留。"""
    persona_file = tmp_path / "persona.md"
    persona_file.write_text("卡人设文本。", encoding="utf-8")
    plugin = _plugin_with_root(tmp_path)
    ctx = PluginContext(
        state={"context.persona_text": "", "context.system_prompt": "# 你是灵汐\n\n{{persona:persona.md}}\n\n## 流程\n做事。"}
    )
    out = _run(plugin._resolve_placeholders(ctx, ctx.state["context.system_prompt"]))
    assert out.startswith("# 你是灵汐\n\n") and out.endswith("\n\n## 流程\n做事。")
    # 接管在场时同一模板换源
    ctx2 = PluginContext(
        state={"context.persona_text": "卡人设文本。", "context.system_prompt": "# 你是灵汐\n\n{{persona:persona.md}}\n\n## 流程\n做事。"}
    )
    out2 = _run(plugin._resolve_placeholders(ctx2, ctx2.state["context.system_prompt"]))
    assert "卡人设文本。" in out2 and "你是灵汐，最贴心" not in out2


# ── {{state:键}} 占位符（状态统一标记机制注入面） ────────────────────────


def test_state_placeholder_reads_state_key() -> None:
    """state 键有值 → 原文透出。"""
    plugin = _plugin_with_root(tmp_path := Path(__import__("tempfile").mkdtemp()))
    ctx = PluginContext(state={"context.character_state_text": "好感度 42"})
    out = _run(plugin._resolve_placeholder(ctx, "state:context.character_state_text"))
    assert out == "好感度 42"


def test_state_placeholder_missing_or_non_string_yields_empty() -> None:
    """键无值/非字符串 → 空串（零注入语义）。"""
    plugin = _plugin_with_root(Path(__import__("tempfile").mkdtemp()))
    for state in ({}, {"context.character_state_text": None}, {"context.character_state_text": 42}):
        out = _run(plugin._resolve_placeholder(PluginContext(state=state), "state:context.character_state_text"))
        assert out == ""


def test_state_placeholder_in_dynamic_vars_shape() -> None:
    """端到端（动态变量消费形态）：items 字符串含 {{state:键}} 每轮解析新鲜值。"""
    plugin = _plugin_with_root(Path(__import__("tempfile").mkdtemp()))
    item = "当前角色状态：{{state:context.character_state_text}}"
    ctx_a = PluginContext(state={"context.character_state_text": "好感度 1"})
    ctx_b = PluginContext(state={"context.character_state_text": "好感度 99"})
    out_a = _run(plugin._resolve_placeholders(ctx_a, item))
    out_b = _run(plugin._resolve_placeholders(ctx_b, item))
    assert out_a == "当前角色状态：好感度 1"
    assert out_b == "当前角色状态：好感度 99", "同模板随 state 变化取新鲜值"
