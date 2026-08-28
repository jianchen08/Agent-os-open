# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""静态变量 / 路径注入 / 知识检索 / 动态变量的行为测试。

覆盖既有测试未触达的行为面：
    - 静态变量：字符串形式（{{...}}）、dict 形式（enabled=false 跳过）、模型信息行
    - path 类型：绝对路径文件、相对路径文件（project_root）、目录遍历、读取失败留痕、
      路径不存在落空告警
    - reference/content/空 type 类型；tags 触发检索
    - 动态变量：agent 配置 > 插件默认、字符串形式、placeholder 类型、session/agent/
      model/reference/routed 类型、enabled=false 跳过、非 dict 跳过、content 为空跳过

（压缩块装配面已随 ADR 2026-08-28 compression-block-pointer-indirection 退役——
块消息是 message 序列里的普通消息，相关测试随之删除。）

外部依赖（文件系统/服务注册表）使用真实实现；异步内部方法用真实实现
（不 mock _build_system_content / _load_static_vars 等）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# 复制 server.py 的 sys.path 机制：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _THIS_DIR 显式路径加载（与 test_prompt_build.py 的 _load_plugin_module 同范式）。
_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_vars_test", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_vars_test"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin  # noqa: E402


def make_plugin(config: dict[str, Any] | None = None) -> PromptBuildPlugin:
    return PromptBuildPlugin(config=config or {})


def make_ctx(state: dict[str, Any] | None = None, services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}), _services=dict(services or {}))


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeMemoryService:
    """记录 retrieve 调用的伪 memory_service（duck-typed，不依赖内部实现）。"""

    def __init__(self, results: list[Any]) -> None:
        self.results = results

    async def retrieve(self, **kwargs: Any) -> list[Any]:
        return list(self.results)


class _Req:
    """检索结果条目（duck-type 带 .content 的对象）。"""

    def __init__(self, content: str) -> None:
        self.content = content


# ═══════════════════════════════════════════════════════════
# 静态变量加载（_load_static_vars）
# ═══════════════════════════════════════════════════════════


class TestLoadStaticVars:
    def test_no_static_vars_no_state_no_config(self) -> None:
        """state 与 config 皆无静态变量 → 空串。"""
        plugin = make_plugin()
        assert _run(plugin._load_static_vars(make_ctx({}))) == ""

    def test_config_fallback_when_state_empty(self) -> None:
        """state 为空时回退插件配置 static_vars。"""
        plugin = make_plugin({"static_vars": [{"type": "session", "name": "会话"}]})
        ctx = make_ctx({"context.session_id": "s-42"})
        out = _run(plugin._load_static_vars(ctx))
        assert "会话" in out and "s-42" in out

    def test_string_form_placeholder(self) -> None:
        """字符串形式 {{session}} 经占位符解析拼入。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.static_vars": ["{{session}}"], "context.session_id": "s-1"})
        out = _run(plugin._load_static_vars(ctx))
        assert "s-1" in out

    def test_disabled_var_skipped(self) -> None:
        """enabled=false 的 dict 变量跳过。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.static_vars": [
                    {"type": "session", "name": "禁用", "enabled": False},
                    {"type": "session", "name": "启用"},
                ],
                "context.session_id": "s-2",
            }
        )
        out = _run(plugin._load_static_vars(ctx))
        assert "禁用" not in out
        assert "启用" in out

    def test_non_dict_item_skipped(self) -> None:
        """既非 str 也非 dict 的条目被跳过，但 dict 条目仍产出。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.static_vars": [123, {"type": "session", "name": "ok"}],
                "context.session_id": "s-9",
            }
        )
        out = _run(plugin._load_static_vars(ctx))
        assert out.startswith("## 静态变量")
        assert "ok" in out

    def test_model_info_line(self) -> None:
        """context_window 存在时追加模型信息段。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.static_vars": [{"type": "session", "name": "s"}],
                "context.session_id": "x",
                "context_window": 64000,
                "llm_model": "deepseek-r1",
            }
        )
        out = _run(plugin._load_static_vars(ctx))
        assert "模型: deepseek-r1" in out and "64000 tokens" in out

    def test_model_info_without_model_name(self) -> None:
        """有 context_window 无 llm_model → 只有上下文窗口行。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.static_vars": [{"type": "session", "name": "s"}],
                "context.session_id": "x",
                "context_window": 32000,
            }
        )
        out = _run(plugin._load_static_vars(ctx))
        assert "模型:" not in out
        assert "32000 tokens" in out

    def test_empty_content_no_parts(self) -> None:
        """所有变量解析为空 → 空串（无 '## 静态变量' 壳）。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.static_vars": [{"type": "path", "name": "p", "path": "missing/x.md"}]})
        assert _run(plugin._load_static_vars(ctx)) == ""


# ═══════════════════════════════════════════════════════════
# 单变量解析（_resolve_single_var_content）
# ═══════════════════════════════════════════════════════════


class TestResolveSingleVar:
    def test_placeholder_type(self) -> None:
        """placeholder 类型：把 name 里的占位符逐条解析，结果以换行拼接。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.session_id": "s-3"})
        var_def = {"type": "placeholder", "name": "{{session}} 和 {{session}}"}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "s-3"))
        assert out == "s-3\ns-3"  # 逐条解析后 join("\n")，原始分隔符不保留

    def test_placeholder_type_without_braces(self) -> None:
        """placeholder 类型但 name 不含 {{ → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "placeholder", "name": "无占位符文本"}
        assert _run(plugin._resolve_single_var_content(ctx, var_def, "")) == ""

    def test_path_file_absolute(self) -> None:
        """path 类型绝对路径文件注入。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "path", "name": "p", "path": str(Path(_THIS_DIR) / "plugin.py")}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, ""))
        assert out.startswith("<plugin>") and out.endswith("</plugin>")

    def test_path_file_relative_system_root(self, caplog, tmp_path, monkeypatch) -> None:
        """path 类型相对路径经系统项目根解析（AGENTOS_CONFIG_ROOT=<根>/config 基准），存在时不产生落空告警。"""
        import logging

        (tmp_path / "config").mkdir()
        (tmp_path / "plugin.py").write_text("# root file", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "path", "name": "p", "path": "plugin.py"}
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(ctx, var_def, ""))
        assert "<plugin>" in out and "# root file" in out
        assert not any("知识注入落空" in r.getMessage() for r in caplog.records)

    def test_path_missing_file_warns(self, caplog, monkeypatch, tmp_path) -> None:
        """path 类型找不到文件/目录 → 空串 + warning 留痕。"""
        import logging

        (tmp_path / "config").mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        plugin = make_plugin()
        var_def = {"type": "path", "name": "p", "path": "definitely-missing-xyz.md"}
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(make_ctx({}), var_def, ""))
        assert out == ""
        assert any("definitely-missing-xyz.md" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)

    def test_path_file_read_error_warns(self, caplog, monkeypatch, tmp_path) -> None:
        """path 类型文件读取失败 → warning 留痕 + 空串。"""
        import logging

        (tmp_path / "config").mkdir()
        (tmp_path / "doc.md").write_text("内容", encoding="utf-8")

        def boom_read_text(self: Any, *a: Any, **k: Any) -> str:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", boom_read_text)  # 文件系统故障（外部依赖）
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "path", "name": "p", "path": "doc.md"}
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(ctx, var_def, ""))
        assert out == ""
        assert any("读取静态变量文件失败" in r.getMessage() for r in caplog.records)

    def test_path_dir_inject(self, tmp_path, monkeypatch) -> None:
        """path 类型目录 → 遍历顶层文件并包裹 <files dir=...>。"""
        plugin = make_plugin()
        (tmp_path / "config").mkdir()
        (tmp_path / "a.md").write_text("AAA", encoding="utf-8")
        (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("CCC", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        ctx = make_ctx({})
        var_def = {"type": "path", "name": "d", "path": "."}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, ""))
        assert out.startswith('<files dir=".">')
        assert "--- a.md ---" in out and "AAA" in out
        assert "--- b.txt ---" in out  # 无扩展名过滤 → 全部顶层文件
        assert "CCC" not in out  # 非递归：子目录内容不注入

    def test_reference_content_and_value(self) -> None:
        """reference/content/空 type 走 content/value 字段。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "reference", "content": "R"}, "")) == "R"
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "content", "value": "V"}, "")) == "V"
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "", "content": "E"}, "")) == "E"

    def test_reference_tags_retrieval(self) -> None:
        """reference 无 content 但有 tags → 走知识检索。"""
        plugin = make_plugin()
        ctx = make_ctx({}, services={"memory_service": FakeMemoryService([_Req("KB 片段")])})
        var_def = {"type": "reference", "tags": ["a"]}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, ""))
        assert out == "KB 片段"

    def test_vector_mode_with_backend(self) -> None:
        """mode=vector + 有 memory_backend → 检索结果替换原内容。"""
        mod = _mod
        plugin = make_plugin()
        ctx = make_ctx({"user_id": "u-1"})

        class FakeBackend:
            async def search(self, **kwargs: Any) -> list[dict[str, str]]:
                return [{"content": "向量结果"}]

        old = mod._memory_backend
        mod._memory_backend = FakeBackend()
        try:
            out = _run(
                plugin._resolve_single_var_content(
                    ctx, {"type": "reference", "content": "原内容", "mode": "vector"}, ""
                )
            )
        finally:
            mod._memory_backend = old
        assert out == "向量结果"

    def test_hybrid_mode_keeps_content_and_appends(self) -> None:
        """mode=hybrid → 原文 + '### 相关检索结果' 拼接。"""
        plugin = make_plugin()
        ctx = make_ctx({"user_id": "u-2"})

        class FakeBackend:
            async def search(self, query: str = "", user_id: str = "", top_k: int = 5, memory_type: str | None = None, tags: list[str] | None = None, tags_match: str = "any") -> list[dict[str, str]]:
                return [{"content": "补充知识"}]

        old = _mod._memory_backend
        _mod._memory_backend = FakeBackend()
        try:
            out = _run(
                plugin._resolve_single_var_content(
                    ctx, {"type": "reference", "content": "原文", "mode": "hybrid"}, ""
                )
            )
        finally:
            _mod._memory_backend = old
        assert out == "原文\n\n### 相关检索结果\n补充知识"

    def test_vector_mode_no_backend_keeps_content(self) -> None:
        """mode=vector 但无后端 → 保留原内容（检索跳过）。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        old = _mod._memory_backend
        _mod._memory_backend = None
        try:
            out = _run(plugin._resolve_single_var_content(ctx, {"type": "reference", "content": "C", "mode": "vector"}, ""))
        finally:
            _mod._memory_backend = old
        assert out == "C"

    def test_output_format_summary(self) -> None:
        """output_format=summary → 前缀 [摘要]。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        out = _run(
            plugin._resolve_single_var_content(
                ctx, {"type": "content", "content": "长文本", "output_format": "summary"}, ""
            )
        )
        assert out == "[摘要] 长文本"


# ═══════════════════════════════════════════════════════════
# 知识检索（_retrieve_by_tags）
# ═══════════════════════════════════════════════════════════


class TestRetrieveByTags:
    def test_no_tags_empty(self) -> None:
        """无 tags → 空串（不调服务）。"""
        plugin = make_plugin()
        assert _run(plugin._retrieve_by_tags(make_ctx({}), {"tags": []})) == ""

    def test_summary_inject_type_truncates_long(self) -> None:
        """inject_type=summary → '- 前200字…' 行；短内容不加省略号。"""
        plugin = make_plugin()
        long_text = "长" * 250
        ctx = make_ctx({}, services={"memory_service": FakeMemoryService([_Req(long_text)])})
        out = _run(plugin._retrieve_by_tags(ctx, {"tags": ["a"], "inject_type": "summary"}))
        assert "- 长" in out and "..." in out
        assert "长" * 250 not in out  # 截断后不超过 200 字 + 省略号

        ctx2 = make_ctx({}, services={"memory_service": FakeMemoryService([_Req("短")])})
        out2 = _run(plugin._retrieve_by_tags(ctx2, {"tags": ["a"], "inject_type": "summary"}))
        assert out2 == "- 短"

    def test_full_inject_type_joins(self) -> None:
        """inject_type=full → 多结果以空行拼接。"""
        plugin = make_plugin()
        ctx = make_ctx({}, services={"memory_service": FakeMemoryService([_Req("A"), _Req("B")])})
        out = _run(plugin._retrieve_by_tags(ctx, {"tags": ["a", "b"]}))
        assert out == "A\n\nB"

    def test_no_results_empty(self) -> None:
        """检索无结果 → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({}, services={"memory_service": FakeMemoryService([])})
        assert _run(plugin._retrieve_by_tags(ctx, {"tags": ["a"]})) == ""


# ═══════════════════════════════════════════════════════════
# 动态变量（_build_dynamic_vars 的其余分支）
# ═══════════════════════════════════════════════════════════


class TestBuildDynamicVars:
    def test_string_form(self) -> None:
        """字符串形式 {{session}} 解析后拼入。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.dynamic_vars": ["{{session}}"], "context.session_id": "s-5"})
        msg = _run(plugin._build_dynamic_vars(ctx))
        assert msg is not None
        assert "s-5" in msg["content"]
        assert msg["role"] == "user"

    def test_placeholder_type(self) -> None:
        """dict 形式 placeholder 类型。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.dynamic_vars": [{"type": "placeholder", "name": "{{session}}"}],
                "context.session_id": "s-6",
            }
        )
        msg = _run(plugin._build_dynamic_vars(ctx))
        assert "s-6" in msg["content"]

    def test_placeholder_empty_content_skipped(self) -> None:
        """placeholder 解析为空 → 不拼入（无输出）。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.dynamic_vars": [{"type": "placeholder", "name": ""}]})
        assert _run(plugin._build_dynamic_vars(ctx)) is None

    def test_session_agent_model_types(self) -> None:
        """session/agent/model 三类直接渲染。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.dynamic_vars": [
                    {"type": "session", "name": "sid"},
                    {"type": "agent", "name": "an"},
                    {"type": "model", "name": "mn"},
                ],
                "context.session_id": "s-7",
                "context.agent_name": "助手",
                "llm_model": "deepseek-v3",
            }
        )
        msg = _run(plugin._build_dynamic_vars(ctx))
        content = msg["content"]
        assert "- sid: s-7" in content
        assert "- an: 助手" in content
        assert "- mn: deepseek-v3" in content

    def test_reference_content_inline(self) -> None:
        """reference/content/inline/空 type 走 content 字段。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.dynamic_vars": [
                    {"type": "reference", "name": "r", "content": "R"},
                    {"type": "inline", "name": "i", "content": "I"},
                    {"type": "", "name": "e", "content": "E"},
                ]
            }
        )
        msg = _run(plugin._build_dynamic_vars(ctx))
        for needle in ["- r: R", "- i: I", "- e: E"]:
            assert needle in msg["content"]

    def test_reference_empty_content_skipped(self) -> None:
        """content 为空 → 该条不产出。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.dynamic_vars": [{"type": "reference", "name": "r", "content": ""}]})
        assert _run(plugin._build_dynamic_vars(ctx)) is None

    def test_routed_type(self) -> None:
        """routed 类型走 _resolve_routed_var。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.dynamic_vars": [
                    {"type": "routed", "name": "rr", "route_key": "tier", "routes": {"big": "大档", "_default": "兜底"}}
                ],
                "tier": "big",
            }
        )
        msg = _run(plugin._build_dynamic_vars(ctx))
        assert "- rr: 大档" in msg["content"]

    def test_non_dict_and_disabled_skipped(self) -> None:
        """非 dict 条目与 enabled=false 跳过。"""
        plugin = make_plugin()
        ctx = make_ctx(
            {
                "context.dynamic_vars": [
                    42,
                    {"type": "session", "name": "禁用", "enabled": False},
                    {"type": "session", "name": "启用"},
                ],
                "context.session_id": "s-8",
            }
        )
        msg = _run(plugin._build_dynamic_vars(ctx))
        assert "启用" in msg["content"]
        assert "禁用" not in msg["content"]

    def test_timestamp_default_format(self) -> None:
        """timestamp 无 format → 默认 %Y-%m-%d %H:%M:%S 且带时区后缀。"""
        import re

        plugin = make_plugin()
        ctx = make_ctx({"context.dynamic_vars": [{"type": "timestamp", "name": "t"}]})
        msg = _run(plugin._build_dynamic_vars(ctx))
        assert re.search(
            r"^- t: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC[+-]\d",
            msg["content"],
            re.MULTILINE,
        )
