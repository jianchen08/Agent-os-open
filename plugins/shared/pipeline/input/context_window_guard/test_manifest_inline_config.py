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
6. compression.model 单一真值 = manifest 内联字段（state/llm.yaml 解析链已退役）

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


# 与 manifest fields.default 同构的注入命名空间样例（点号展开后的形态；
# 预算只保留三类 2026-09-02 裁定：recent/L1/L2）
_INJECTED_WINDOW_CFG = {
    "compress_trigger_ratio": 0.3,
    "budgets": {
        "l2": 0.05,
        "l1": 0.2,
        "recent": 0.25,
    },
    "compression": {
        "enabled": True,
        "model": "compress-model-x",
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
        budgets = cfg.get_budgets()
        # 预算三类化（2026-09-02 裁定）：只有 recent/L1/L2，无其他维度
        assert set(budgets.keys()) == {"recent", "L1", "L2"}
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

    def test_manifest_budget_fields_only_three_kinds(self) -> None:
        """manifest 配置面只暴露三类预算（recent/L1/L2），死预算字段不回潮。"""
        import json

        manifest_path = _PLUGIN_DIR / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fields = manifest["config_files"][0]["fields"]
        budget_names = {
            f["name"] for f in fields if f["name"].startswith("budgets.")
        }
        assert budget_names == {"budgets.recent", "budgets.l1", "budgets.l2"}


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


class TestPerCallRuntimeConfig:
    """每调用注入重解析（2026-09-02 单一真值 + sidecar 每调用注入）。"""

    def test_per_call_injected_namespace_reresolves_ratio(self) -> None:
        """构造期空配置（合宿扇出可能给空 dict），execute 时 ctx.config 带注入
        → _trigger_ratio/_window_cfg 按注入值重解析。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin({})
        assert plugin._trigger_ratio == 0.55

        ctx = mod._make_minimal_ctx(
            state={},
            config={"context_window": {"compress_trigger_ratio": 0.06}},
            pipeline_id="pipe-1",
        )
        plugin._apply_runtime_config(ctx)
        assert plugin._trigger_ratio == 0.06
        assert plugin._window_cfg.get("compress_trigger_ratio") == 0.06

    def test_explicit_pipeline_ratio_wins_over_per_call_injection(self) -> None:
        """pipeline 显式 trigger_ratio 在每调用注入下仍最高。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.9})
        ctx = mod._make_minimal_ctx(
            state={},
            config={"context_window": {"compress_trigger_ratio": 0.06}},
            pipeline_id="pipe-1",
        )
        plugin._apply_runtime_config(ctx)
        assert plugin._trigger_ratio == 0.9

    def test_state_override_still_highest(self) -> None:
        """ctx.state context_guard.trigger_ratio（agent 运行时覆盖）仍最高。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.9})
        ctx = mod._make_minimal_ctx(
            state={"context_guard.trigger_ratio": 0.1},
            config={"context_window": {"compress_trigger_ratio": 0.06}},
            pipeline_id="pipe-1",
        )
        plugin._apply_runtime_config(ctx)
        assert plugin._trigger_ratio == 0.1

    def test_execute_uses_per_call_threshold(self) -> None:
        """行为级：构造期无注入（默认 0.55 → 触发线 70400），60000 tokens 不压；
        ctx.config 注入 0.06（触发线 7680）→ 同一上下文触发压缩。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None

        plugin = mod.ContextWindowGuardPlugin({})  # 构造期空 → 代码默认 0.55
        messages = [{"role": "user", "content": "hello", "seq": 0}]

        def make_service() -> MagicMock:
            service = MagicMock()
            service.setup = MagicMock()
            service.compress_messages = AsyncMock(return_value=None)
            service._last_deleted_seqs = []
            service._last_block_msgs = []
            return service

        # 无注入：60000 < 0.55*128000=70400 → 不压
        service_a = make_service()
        ctx_a = mod._make_minimal_ctx(
            state={
                "context_window": 128000,
                "messages": messages,
                "llm_usage": {"input_tokens": 60000},
            },
            pipeline_id="pipe-percall",
        )
        ctx_a._services["context_service"] = service_a
        _run(plugin.execute(ctx_a))
        service_a.compress_messages.assert_not_called()

        # 同一实例，每调用注入 0.06：60000 > 0.06*128000=7680 → 压
        service_b = make_service()
        ctx_b = mod._make_minimal_ctx(
            state={
                "context_window": 128000,
                "messages": messages,
                "llm_usage": {"input_tokens": 60000},
            },
            config={"context_window": {"compress_trigger_ratio": 0.06}},
            pipeline_id="pipe-percall",
        )
        ctx_b._services["context_service"] = service_b
        _run(plugin.execute(ctx_b))
        service_b.compress_messages.assert_called_once()


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
    """压缩模型单一真值 = manifest 内联 compression.model（state/llm.yaml 链退役）。"""

    def test_injected_model_is_sole_truth(self) -> None:
        """manifest 内联 model 非空 → 即为压缩模型。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx()
        resolved = mod._resolve_compress_model(
            ctx, injected={"model": "compress-model-x"}
        )
        assert resolved == "compress-model-x"

    def test_injected_model_whitespace_stripped(self) -> None:
        """表单输入的首尾空白不进模型名。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx()
        resolved = mod._resolve_compress_model(
            ctx, injected={"model": "  compress-model-x "}
        )
        assert resolved == "compress-model-x"

    def test_state_chain_retired(self) -> None:
        """state.model_id/model_tier 不再被消费（旧回退链退役的防回潮断言）。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx(
            state={"model_id": "deepseek-v4", "model_tier": "chat"}
        )
        assert mod._resolve_compress_model(ctx, injected={"model": ""}) == ""
        assert mod._resolve_compress_model(ctx, injected={}) == ""

    def test_no_injected_returns_empty(self) -> None:
        """无注入（injected=None）→ 未配置，返回空串。"""
        mod = _load_plugin_module()
        ctx = mod._make_minimal_ctx()
        assert mod._resolve_compress_model(ctx, injected=None) == ""

    def test_unconfigured_model_disables_compression(self) -> None:
        """compression.model 未配置 → 压缩服务不构建（不把空模型发往 llm_service）。"""
        mod = _load_plugin_module()
        mod._capability_caller = AsyncMock()
        mod._memory_backend = MagicMock()
        ctx = mod._make_minimal_ctx(state={"context_window": 128000})
        svc = mod.ContextWindowGuardPlugin._get_memory_service(
            ctx, {"compression": {"model": ""}}
        )
        assert svc is None

    def test_manifest_default_model_is_sole_truth(self) -> None:
        """manifest fields.default 携带非空压缩模型（真值本体，禁止回潮空默认+兜底叙事）。"""
        import json

        manifest_path = _PLUGIN_DIR / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fields = manifest["config_files"][0]["fields"]
        model_field = next(f for f in fields if f["name"] == "compression.model")
        assert model_field["default"]
        assert "留空则用默认" not in model_field["description"]
