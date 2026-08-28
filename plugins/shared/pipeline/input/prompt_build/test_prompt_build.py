# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""prompt_build plugin TDD 测试（Step 6 重建）。

验证内容（与任务规格 5 个用例对齐）：
1. test_load_compression_without_backend_empty —— 无后端 → 压缩消息为 []
2. test_load_compression_filters_by_pipeline —— mock 后端返回含/不含 pipeline 标签的块 →
   只包含本管道块
3. test_load_compression_builds_messages —— 有 L1 块 → 消息含 `<compressed seq=` 格式
4. test_state_snapshot_message —— STATE_SNAPSHOT 块 → `<current_state>` 消息
5. test_local_config_parser —— 本地压缩配置读取 yaml，返回预算

测试不依赖真实记忆后端/真实 LLM——通过 FakeBackend 注入模块级 _memory_backend，
与 context_window_guard 的 set_memory_backend 模式保持一致。

[来源: docs/tasks Step 6 记忆插件接入 IMemoryBackend]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + pipeline 包加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_INPUT_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/pipeline/input/
if _INPUT_DIR not in sys.path:
    sys.path.insert(0, _INPUT_DIR)  # 供 _compression_config 导入 context_window_guard

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级 _memory_backend 跨测试污染）。"""
    mod_name = "prompt_build_plugin_test"
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
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    """构造最小 PluginContext。"""
    return PluginContext(state=dict(state or {}))


def _chunk(
    mem_id: str,
    content: str,
    tags: list[str],
) -> dict[str, Any]:
    """构造一条统一形态的后端 chunk 结果。"""
    return {
        "id": mem_id,
        "content": content,
        "score": 1.0,
        "memory_type": "chunk",
        "metadata": {"tags": tags},
    }


class FakeBackend:
    """记录 search 调用的伪 IMemoryBackend（duck-typed，无需继承 ABC）。"""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.search_returns: list[dict[str, Any]] = list(results or [])

    async def search(
        self,
        query: str = "",
        user_id: str = "",
        top_k: int = 5,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "query": query,
                "user_id": user_id,
                "top_k": top_k,
                "memory_type": memory_type,
                "tags": tags,
                "tags_match": tags_match,
            }
        )
        return list(self.search_returns)


# ═══════════════════════════════════════════════════════════
# 1. 无后端 → 空
# ═══════════════════════════════════════════════════════════


class TestLoadCompressionWithoutBackend:
    def test_load_compression_without_backend_empty(self) -> None:
        """未注入 _memory_backend → 压缩消息为 []，不崩溃。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"pipeline_id": "pipe-1", "user_id": "u-1"})

        messages = _run(plugin._load_compression_messages(ctx))

        assert messages == []


# ═══════════════════════════════════════════════════════════
# 2. 按 pipeline 标签过滤
# ═══════════════════════════════════════════════════════════


class TestLoadCompressionFiltersByPipeline:
    def test_load_compression_filters_by_pipeline(self) -> None:
        """只包含 metadata.tags 中 pipeline:pipe-1 的块，其他管道块被过滤。"""
        mod = _load_plugin_module()
        backend = FakeBackend(
            results=[
                _chunk("c1", "本管道摘要A", ["L1", "pipeline:pipe-1", "seq:1-5"]),
                _chunk("c2", "其他管道摘要B", ["L1", "pipeline:other-9", "seq:1-5"]),
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"pipeline_id": "pipe-1", "user_id": "u-1"})

        messages = _run(plugin._load_compression_messages(ctx))

        joined = "\n".join(m.get("content", "") for m in messages)
        assert "本管道摘要A" in joined
        assert "其他管道摘要B" not in joined


# ═══════════════════════════════════════════════════════════
# 3. L1 块组装为 <compressed seq= 消息
# ═══════════════════════════════════════════════════════════


class TestLoadCompressionBuildsMessages:
    def test_load_compression_builds_messages(self) -> None:
        """有 L1 块 → 消息含 `<compressed seq="1-5" level="L1">` 格式。"""
        mod = _load_plugin_module()
        backend = FakeBackend(
            results=[
                _chunk("c1", "L1摘要内容", ["L1", "pipeline:pipe-1", "seq:1-5"]),
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"pipeline_id": "pipe-1", "user_id": "u-1"})

        messages = _run(plugin._load_compression_messages(ctx))

        assert messages, "应产出压缩消息"
        contents = "\n".join(m.get("content", "") for m in messages)
        assert '<compressed seq="1-5" level="L1">' in contents
        assert "L1摘要内容" in contents


# ═══════════════════════════════════════════════════════════
# 4. STATE_SNAPSHOT → <current_state> 消息
# ═══════════════════════════════════════════════════════════


class TestStateSnapshotMessage:
    def test_state_snapshot_message(self) -> None:
        """STATE_SNAPSHOT 块 → 产出 `<current_state>` 包裹的消息。"""
        mod = _load_plugin_module()
        backend = FakeBackend(
            results=[
                _chunk(
                    "s1",
                    '{"current_state": "进行中"}',
                    ["STATE_SNAPSHOT", "pipeline:pipe-1"],
                ),
            ]
        )
        mod.set_memory_backend(backend)
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"pipeline_id": "pipe-1", "user_id": "u-1"})

        messages = _run(plugin._load_state_snapshot_message(ctx, "pipe-1"))

        assert messages, "应产出状态快照消息"
        assert "<current_state>" in messages[0]["content"]
        assert "进行中" in messages[0]["content"]
        assert messages[0]["name"] == "state_snapshot"


# ═══════════════════════════════════════════════════════════
# 5. 本地压缩配置解析
# ═══════════════════════════════════════════════════════════


class TestLocalConfigParser:
    def test_local_config_parser(self) -> None:
        """压缩预算配置读取 yaml（读不到时回退默认），返回有效预算（单一实现直连 guard）。"""
        mod = _load_plugin_module()
        sys.modules.pop("memory.context_compressor", None)

        cfg = mod._compression_config(128000)
        budgets = cfg.get_budgets()

        assert budgets["recent"] == int(128000 * 0.18), "recent 预算应为 23040"
        assert budgets["L1"] == int(128000 * 0.1), "L1 预算应为 12800"
        assert budgets["L2"] == int(128000 * 0.05), "L2 预算应为 6400"
        assert cfg.get_trigger_threshold() == int(128000 * 0.55), "触发阈值应为 70400"
        # 不应导入 memory.context_compressor（0.2 中不存在）
        assert "memory.context_compressor" not in sys.modules


# ═══════════════════════════════════════════════════════════
# 6. routed 条件注入（按 state 键值路由，2026-08-15 增强）
# ═══════════════════════════════════════════════════════════


class TestRoutedVar:
    """routed 变量：精确/通配/布尔规范化/_default 兜底。"""

    def test_exact_match(self) -> None:
        """精确匹配：state 值命中的 routes 键内容直接返回。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "llm_call"})
        var_def = {
            "route_key": "core_type",
            "routes": {
                "llm_call": "LLM 轮次提示",
                "tool_execute": "工具轮次提示",
                "_default": "兜底提示",
            },
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "LLM 轮次提示"

    def test_default_fallback(self) -> None:
        """未命中 → _default 兜底。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "other"})
        var_def = {
            "route_key": "core_type",
            "routes": {"llm_call": "A", "_default": "兜底"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "兜底"

    def test_wildcard_match(self) -> None:
        """fnmatch 通配键：deepseek-* 命中 state 值；_default 不参与通配。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"model_tier": "large"})
        var_def = {
            "route_key": "model_tier",
            "routes": {"large*": "大档系", "small": "小档", "_default": "兜底"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "大档系"

    def test_boolean_normalization(self) -> None:
        """布尔规范化：yaml 键 true 匹配 state 布尔 True（含 routes 键本身是布尔）。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"ended": True})
        var_def = {
            "route_key": "ended",
            "routes": {True: "已结束", False: "进行中"},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "已结束"

    def test_nested_content_dict(self) -> None:
        """routes 值为 dict 时按 content 字段返回（嵌套定义）。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"core_type": "llm_call"})
        var_def = {
            "route_key": "core_type",
            "routes": {"llm_call": {"type": "content", "content": "嵌套内容"}},
        }
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "嵌套内容"


# ═══════════════════════════════════════════════════════════
# 未知占位符可观测（兜底反模式审查 P2，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestUnknownPlaceholderWarns:
    """P2：未识别占位符替换为空串必须 warning 留痕（含原文）。"""

    def test_unknown_placeholder_warns_with_original_text(self, caplog) -> None:
        """拼错的占位符 → 空串 + warning 含占位符原文。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, "timestmp"))
        assert out == ""
        assert "未识别" in caplog.text and "timestmp" in caplog.text

    def test_empty_placeholder_warns(self, caplog) -> None:
        """空占位符（{{}}）同样未识别 → 留痕。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, ""))
        assert out == ""
        assert "未识别" in caplog.text

    def test_known_placeholder_no_warning(self, caplog) -> None:
        """合法占位符（session）正常解析且不触发未识别告警。"""
        import logging as _logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx({"context.session_id": "s-1"})
        with caplog.at_level(_logging.WARNING):
            out = _run(plugin._resolve_placeholder(ctx, "session"))
        assert out == "s-1"
        assert "未识别" not in caplog.text


# ═══════════════════════════════════════════════════════════
# 压缩预算配置回退可观测（兜底反模式审查 P12，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestCompressionConfigFallbackWarns:
    """P12：预算配置读取失败回退代码默认必须 warning 留痕（path + 异常）。"""

    def test_from_yaml_config_failure_warns(self, caplog, monkeypatch) -> None:
        import logging

        mod = _load_plugin_module()
        monkeypatch.setitem(sys.modules, "config.config_center", None)  # 模拟配置中心不可达
        with caplog.at_level(logging.WARNING):
            cfg = mod._compression_config(64000)
        assert cfg.context_window == 64000, "回退代码默认（行为保持）"
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("压缩预算配置读取失败" in m for m in msgs)
        assert any("context_window_config.yaml" in m for m in msgs), "留痕需带配置路径"


# ═══════════════════════════════════════════════════════════
# 知识注入/状态快照静默落空可观测（兜底反模式审查 P16/P17，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestKnowledgeAndSnapshotObservability:
    def test_memory_service_missing_warns(self, caplog) -> None:
        """P16：static_vars 声明知识注入但 memory_service 未注册 → warning。"""
        import logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin(config={})
        ctx = _make_ctx({})  # 无 services → get_service 抛 KeyError
        var_def = {
            "type": "retrieval",
            "name": "知识",
            "tags": ["a", "b"],
            "top_k": 3,
            "inject_type": "full",
        }
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(ctx, var_def, "sess-1"))
        assert out == "", "降级语义保持（空知识）"
        assert any("memory_service 未注册" in r.getMessage() for r in caplog.records)

    def test_state_snapshot_retrieval_failure_warns(self, caplog, monkeypatch) -> None:
        """P17：快照检索异常 → 空消息 + warning（不再静默 pass）。"""
        import logging

        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin(config={})

        class BoomBackend:
            async def search(self, **kwargs):
                raise RuntimeError("snapshot backend down")

        monkeypatch.setattr(mod, "_memory_backend", BoomBackend())
        ctx = _make_ctx({"user_id": "u1"})
        with caplog.at_level(logging.WARNING):
            msgs = _run(plugin._load_state_snapshot_message(ctx, "pipe-1"))
        assert msgs == [], "降级语义保持（缺 <current_state>）"
        assert any("状态快照检索失败" in r.getMessage() for r in caplog.records)
