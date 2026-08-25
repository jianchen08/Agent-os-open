# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 提供者拆分回归测试（task_kernel_cleanup_and_split 3a/3b）。

结构断言：`adapter.py` 不再包含任何具体提供者 hack 与诊断机制——已迁至
`llm_provider_*` 插件（deepseek/minimax/keypool）与 `_diagnostics.py`，
防止回退（llm_core 不绑定提供者）。

行为断言：迁移后的 provider 插件与注册表（`_provider_registry`）行为与
拆分前内联实现等价：
- MiniMax 消息角色安全修正（非首位 system → user）
- openai/ 前缀模型（DeepSeek 等 OpenAI 兼容中转）reasoning_effort/thinking
  → extra_body 透传
- <think/> 标签 reasoning 提取（DeepSeek/o1 类）
- 未命中任何 provider 规则时行为不变（内置 LiteLLM 直调）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core"
_LLM_CORE_DIR = _CORE_DIR / "llm_core"


def _reclaim_llm_core_path() -> None:
    """llm_core 目录去重插回 sys.path[0]，并逐出非 llm_core 的 adapter 缓存。

    车道共跑时其他插件测试（multimodal 等）会把自身目录残留于 sys.path 前部、
    把裸名 "adapter" 缓存换成自己的模块——provider 插件平铺
    ``from adapter import ...`` 只认 llm_core 目录解析。收集期（模块级）与
    执行期（导入 provider 插件前）都需调用，幂等。
    """
    for d in (str(_LLM_CORE_DIR), str(_CORE_DIR)):
        while d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)
    cached = sys.modules.get("adapter")
    cached_src = str(getattr(cached, "__file__", "") or "")
    if cached is not None and not cached_src.startswith(str(_LLM_CORE_DIR)):
        sys.modules.pop("adapter", None)


_reclaim_llm_core_path()

from _provider_registry import apply_pre_send, extract_thinking_from_content  # noqa: E402
from llm_provider_deepseek import (  # noqa: E402
    extract_thinking_from_content as ds_extract_thinking,
    move_to_extra_body,
)
from llm_provider_minimax import ensure_role_safety  # noqa: E402

# ─────────────────── 结构断言（防回退） ───────────────────

def test_adapter_no_longer_contains_provider_hacks() -> None:
    """adapter.py 不得再定义提供者 hack / 诊断 / KeyPool（已拆分）。"""
    src = (_LLM_CORE_DIR / "adapter.py").read_text(encoding="utf-8")
    for forbidden in (
        "def _ensure_minimax_role_safety",
        "def _move_to_extra_body",
        "def _extract_thinking_from_content",
        "class KeyPoolAdapter",
        "def _log_final_payload",
        "def _install_payload_diag_hook",
        "def _log_prompt_body",
        "def _redact_prompt",
        "_REDACT_PATTERNS",
    ):
        assert forbidden not in src, f"adapter.py 不应再包含 {forbidden}（已拆至 provider 插件/_diagnostics）"


def test_provider_plugins_are_standalone_packages() -> None:
    """三个 provider 插件各自独立可导入。"""
    # 执行期再恢复一次 sys.path/adapter 缓存（收集期之后车道内其他测试可能再次改写）。
    _reclaim_llm_core_path()
    import llm_provider_keypool  # noqa: PLC0415

    assert callable(ensure_role_safety)
    assert callable(ds_extract_thinking)
    assert callable(move_to_extra_body)
    assert llm_provider_keypool.KeyPoolAdapter is not None


# ─────────────────── 行为断言（与拆分前等价） ───────────────────

def test_minimax_role_safety_fixes_non_first_system() -> None:
    """MiniMax：非首位 system 消息 → user（原地修正 + 返回引用）。"""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "injected"},
    ]
    out = ensure_role_safety("minimax/MiniMax-M2.7", msgs)
    assert out is msgs, "应原地修改并返回同一引用"
    assert msgs[1]["role"] == "user"
    assert "name" not in msgs[1]


def test_minimax_role_safety_skips_non_minimax_model() -> None:
    """非 minimax 模型：不动消息。"""
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    out = ensure_role_safety("zai/glm-5.1", msgs)
    assert out[1]["role"] == "system"


def test_move_to_extra_body_passthrough() -> None:
    """openai/ 中转端点：reasoning_effort/thinking 挪进 extra_body。"""
    kwargs = {"reasoning_effort": "max", "thinking": {"type": "enabled"}, "temperature": 0.7}
    move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))
    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs
    assert kwargs["extra_body"] == {
        "reasoning_effort": "max",
        "thinking": {"type": "enabled"},
    }
    # 已存在的 extra_body 合并而非覆盖
    assert kwargs["temperature"] == 0.7


def test_think_tag_extraction() -> None:
    """<think/> 标签提取：返回 (thinking, cleaned)，支持标准与 MiniMax 变体。"""
    thinking, cleaned = ds_extract_thinking("aaa<think>inner</think>bbb")
    assert thinking == "inner"
    assert cleaned == "aaabbb"
    # 无标签：原样返回
    assert ds_extract_thinking("plain text") == (None, "plain text")
    assert ds_extract_thinking(None) == (None, None)
    # 多标签合并
    t, c = ds_extract_thinking("a<think>1</think>b<think>2</think>c")
    assert t == "1\n2"
    assert c == "abc"


# ─────────────────── 注册表分发断言 ───────────────────

def test_registry_applies_minimax_role_safety_by_model() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    out = apply_pre_send("minimax/MiniMax-M2.7", msgs, {})
    assert out[1]["role"] == "user"


def test_registry_applies_extra_body_for_openai_prefix() -> None:
    kwargs = {"reasoning_effort": "max"}
    apply_pre_send("openai/deepseek-v4-flash", [{"role": "user", "content": "hi"}], kwargs)
    assert kwargs["extra_body"]["reasoning_effort"] == "max"


def test_registry_no_match_keeps_behavior_unchanged() -> None:
    """未命中任何 provider 规则：messages/kwargs 原样（内置 LiteLLM 直调语义）。"""
    msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]
    kwargs = {"temperature": 0.7}
    out = apply_pre_send("zai/glm-5.1", msgs, kwargs)
    assert out is msgs
    assert kwargs == {"temperature": 0.7}


def test_registry_extract_thinking_dispatches() -> None:
    assert extract_thinking_from_content("a<think>t</think>b") == ("t", "ab")


# ═══════════════════════════════════════════════════════════
# provider 前缀映射零静默回退（兜底反模式审查 P8，2026-08-20）
# ═══════════════════════════════════════════════════════════


def _load_llm_core_plugin():
    """按唯一模块名加载 llm_core/plugin.py（避免与其它插件的 plugin 撞名）。"""
    import importlib.util

    _shared_dir = _REPO_ROOT / "plugins" / "shared"
    # 执行期自持（前序测试的 fixture 可能已改写 sys.path[0]/裸名缓存）：
    # 去重插 [0] + 弹 plugin.py 平铺依赖的裸名，保证 from adapter import
    # 解析到 llm_core 本体而非车道内其他插件的同名模块。
    for _d in (str(_LLM_CORE_DIR), str(_CORE_DIR), str(_shared_dir)):
        while _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    for _m in ("adapter", "_message_normalizer", "_stream_repeat_monitor"):
        sys.modules.pop(_m, None)
    mod_name = "llm_core_plugin_prefix_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _bare_plugin(mod, provider: str, model: str = "test-model"):
    """绕过重量级 __init__ 构造最小实例（_get_model_string 只看 _provider/_model）。"""
    plugin = object.__new__(mod.LLMCore)
    plugin._provider = provider  # type: ignore[attr-defined]
    plugin._model = model  # type: ignore[attr-defined]
    return plugin


def _stub_router_factory(monkeypatch, prefix_by_provider: dict[str, str]):
    """注入 router_factory 桩模块（get_litellm_prefix 按表返回，未命中返回原名）。"""
    import types

    stub = types.ModuleType("router_factory_stub_p8")

    def get_litellm_prefix(provider_name: str) -> str:
        return prefix_by_provider.get(provider_name, provider_name)

    stub.get_litellm_prefix = get_litellm_prefix  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "router_factory", stub)


class TestProviderPrefixMappingNoSilentFallback:
    """P8：动态映射失败不静默回退——except/未命中留痕，自定义 provider 回退自身报配置错误。"""

    def test_import_failure_warns_and_uses_builtin(self, monkeypatch, caplog):
        """router_factory 加载失败 → warning + 内置映射兜底（openai 恒等）。"""
        import logging

        mod = _load_llm_core_plugin()
        monkeypatch.setitem(sys.modules, "router_factory", None)  # import → ImportError
        plugin = _bare_plugin(mod, "openai")
        with caplog.at_level(logging.WARNING):
            assert plugin._get_model_string() == "openai/test-model"
        assert any("加载失败" in r.getMessage() for r in caplog.records)

    def test_miss_with_remapping_warns(self, monkeypatch, caplog):
        """未命中且内置表改变前缀（zhipu→zai）→ warning 留痕。"""
        import logging

        mod = _load_llm_core_plugin()
        _stub_router_factory(monkeypatch, {})  # 未配置任何映射
        plugin = _bare_plugin(mod, "zhipu")
        with caplog.at_level(logging.WARNING):
            assert plugin._get_model_string() == "zai/test-model"
        assert any("回退内置映射" in r.getMessage() for r in caplog.records)

    def test_custom_provider_fallback_to_self_raises(self, monkeypatch):
        """自定义 provider 未命中内置表 → 显式配置错误（拒绝 provider_name 充当前缀）。"""
        mod = _load_llm_core_plugin()
        _stub_router_factory(monkeypatch, {})  # apigo 未配置 → 返回原名（未命中）
        plugin = _bare_plugin(mod, "apigo")
        with pytest.raises(ValueError, match="前缀映射缺失"):
            plugin._get_model_string()

    def test_dynamic_hit_no_warning(self, monkeypatch, caplog):
        """动态映射命中（apigo→openai）→ 正常前缀，无回退告警。"""
        import logging

        mod = _load_llm_core_plugin()
        _stub_router_factory(monkeypatch, {"apigo": "openai"})
        plugin = _bare_plugin(mod, "apigo")
        with caplog.at_level(logging.WARNING):
            assert plugin._get_model_string() == "openai/test-model"
        assert not [r for r in caplog.records if "回退" in r.getMessage()]

    def test_identity_builtin_miss_stays_quiet(self, monkeypatch, caplog):
        """恒等映射（openai）未命中：回退结果与动态结果相同 → 仅 debug，不告警。"""
        import logging

        mod = _load_llm_core_plugin()
        _stub_router_factory(monkeypatch, {})
        plugin = _bare_plugin(mod, "openai")
        with caplog.at_level(logging.WARNING):
            assert plugin._get_model_string() == "openai/test-model"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
