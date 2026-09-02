# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入
"""manifest 内联配置（fields.default）消费链测试。

配置值内联进 plugin.json config_files.fields.default 后，插件从注入命名空间
（config["context_window"]）读取压缩触发比例/预算/开关/压缩模型，不再依赖
独立的 config/system/context_window_config.yaml 文件（2026-09-02 ADR：
context-window-config-inline-manifest）。

覆盖契约：
1. from_yaml_config 注入 dict 优先于代码默认（compress_trigger_ratio + budgets）
2. 无注入时回退链保留（ConfigCenter 兼容 → 代码默认）
3. 插件实例化 _trigger_ratio 读注入命名空间的 compress_trigger_ratio
4. pipeline/agent 显式 trigger_ratio 仍最高优先级
5. compression.enabled=false → execute 早退不压缩
6. compression.model 注入 → 压缩模型选择优先于 state 解析链

[来源: ADR 2026-09-02-context-window-config-inline-manifest]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + pipeline 包加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "context_window_guard_plugin_inline_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，与既有测试同款策略，避免交叉污染）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# 与旧 YAML 同构的注入命名空间样例（fields.default 点号展开后的形态）
_INJECTED_WINDOW_CFG = {
    "compress_trigger_ratio": 0.3,
    "budgets": {
        "system_prompt": 0.06,
        "tools_description": 0.0,
        "static_vars": 0.03,
        "dynamic_variables": 0.03,
        "l3": 0.02,
        "l2": 0.05,
        "l1": 0.2,
        "recent": 0.25,
        "retrieval": 0.1,
        "response_reserve": 0.14,
    },
    "compression": {
        "enabled": True,
        "model": "compress-model-x",
        "layer_trigger_ratio": 0.8,
        "max_turn_ratio": 0.5,
    },
}


class TestFromYamlConfigInjected:
    def test_injected_dict_prefers_over_defaults(self) -> None:
        """注入 dict（manifest fields.default 展开）优先于代码默认。"""
        mod = _load_plugin_module()
        cfg = mod.CompressionConfig.from_yaml_config(
            128000, injected=_INJECTED_WINDOW_CFG
        )
        assert cfg.compress_trigger_ratio == 0.3
        assert cfg.l1_ratio == 0.2
        assert cfg.l2_ratio == 0.05
        assert cfg.recent_ratio == 0.25
        assert cfg.retrieval_ratio == 0.1
        budgets = cfg.get_budgets()
        assert budgets["recent"] == int(128000 * 0.25)
        assert budgets["L1"] == int(128000 * 0.2)
        assert cfg.get_trigger_threshold() == int(128000 * 0.3)

    def test_no_injected_falls_back_to_defaults(self) -> None:
        """无注入时回退链保留（本环境 config_center 缺失 → 代码默认）。"""
        mod = _load_plugin_module()
        cfg = mod.CompressionConfig.from_yaml_config(128000)
        assert cfg.compress_trigger_ratio == 0.55
        assert cfg.recent_ratio == 0.18

    def test_empty_injected_falls_back_to_defaults(self) -> None:
        """注入空 dict 等同未注入（不覆盖默认）。"""
        mod = _load_plugin_module()
        cfg = mod.CompressionConfig.from_yaml_config(128000, injected={})
        assert cfg.compress_trigger_ratio == 0.55


class TestPluginTriggerRatio:
    def test_trigger_ratio_from_injected_namespace(self) -> None:
        """插件实例化 _trigger_ratio 读注入命名空间 context_window.compress_trigger_ratio。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin(
            {"context_window": {"compress_trigger_ratio": 0.3}}
        )
        assert plugin._trigger_ratio == 0.3

    def test_explicit_trigger_ratio_wins_over_injected(self) -> None:
        """pipeline/agent 显式 trigger_ratio 仍最高优先级（覆盖注入值）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin(
            {
                "trigger_ratio": 0.2,
                "context_window": {"compress_trigger_ratio": 0.3},
            }
        )
        assert plugin._trigger_ratio == 0.2

    def test_no_config_falls_back_to_default(self) -> None:
        """无任何配置 → 代码默认 0.55（回退链终点）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin({})
        assert plugin._trigger_ratio == 0.55


class TestCompressionEnabled:
    def test_execute_skips_when_compression_disabled(self) -> None:
        """compression.enabled=false → 阈值检查与压缩全部跳过（不调 compress_messages）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        injected = dict(_INJECTED_WINDOW_CFG)
        injected["compression"] = {"enabled": False}
        plugin = mod.ContextWindowGuardPlugin({"context_window": injected})

        messages = [
            {"role": "user", "content": f"msg {i} " + "x" * 4000, "seq": i}
            for i in range(1, 16)
        ]
        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)
        mock_service._last_deleted_seqs = []
        mock_service._last_block_msgs = []

        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128000,
                "messages": messages,
                "llm_usage": {"input_tokens": 80000},
            },
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        result = _run(plugin.execute(ctx))
        mock_service.compress_messages.assert_not_called()
        # 早退无消息写入
        assert "messages" not in result.state_updates

    def test_execute_compresses_when_enabled(self) -> None:
        """compression.enabled=true（缺省）→ 阈值超限时照常调 compress_messages。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin(
            {"context_window": dict(_INJECTED_WINDOW_CFG)}
        )

        messages = [
            {"role": "user", "content": f"msg {i} " + "x" * 4000, "seq": i}
            for i in range(1, 16)
        ]
        mock_service = MagicMock()
        mock_service.setup = MagicMock()
        mock_service.compress_messages = AsyncMock(return_value=None)
        mock_service._last_deleted_seqs = []
        mock_service._last_block_msgs = []

        ctx = mod._make_minimal_ctx(
            state={
                "context_window": 128000,
                "messages": messages,
                "llm_usage": {"input_tokens": 80000},
            },
            pipeline_id="pipe-1",
        )
        ctx._services["context_service"] = mock_service

        _run(plugin.execute(ctx))
        mock_service.compress_messages.assert_awaited_once()


class TestCompressModel:
    def test_injected_model_preferred_over_state_chain(self) -> None:
        """注入 compression.model 非空 → 压缩模型优先用它（跳过 state 解析链）。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx()  # state 无 model_id/model_tier
        resolved = mod._resolve_compress_model(
            ctx, injected={"model": "compress-model-x"}
        )
        assert resolved == "compress-model-x"

    def test_no_injected_model_falls_back_to_state_chain(self) -> None:
        """注入 model 为空 → 回退 state 解析链（model_id → tier → defaults）。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx(state={"model_id": "deepseek-v4"})
        resolved = mod._resolve_compress_model(ctx, injected={"model": ""})
        assert resolved == "deepseek-v4"
