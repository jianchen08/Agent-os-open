# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""静态变量 / 路径注入 / 知识检索 / 压缩加载 / 动态变量的行为测试。

覆盖既有测试未触达的行为面：
    - 静态变量：字符串形式（{{...}}）、dict 形式（enabled=false 跳过）、模型信息行
    - path 类型：绝对路径文件、相对路径文件（project_root）、目录遍历、读取失败留痕
    - reference/content/空 type 类型；tags 触发检索
    - 压缩加载：无 pipeline_id / 后端异常降级、预算耗尽跳过、去重、L1→L2 降级、
      _estimate_tokens_for_budget 上下界、_parse_seq 各种形态
    - 动态变量：agent 配置 > 插件默认、字符串形式、placeholder 类型、session/agent/
      model/reference/routed 类型、enabled=false 跳过、非 dict 跳过、content 为空跳过
    - 工具估算：空串 0；非空至少 1 且 len//2 上界
    - 状态快照：非 dict 结果跳过、无 STATE_SNAPSHOT 标签 → 空列表

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


def _chunk(content: str, tags: list[str]) -> dict[str, Any]:
    """构造一条统一形态的后端 chunk 结果。"""
    return {"id": "id-x", "content": content, "metadata": {"tags": tags}}


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
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "s-3", {}))
        assert out == "s-3\ns-3"  # 逐条解析后 join("\n")，原始分隔符不保留

    def test_placeholder_type_without_braces(self) -> None:
        """placeholder 类型但 name 不含 {{ → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "placeholder", "name": "无占位符文本"}
        assert _run(plugin._resolve_single_var_content(ctx, var_def, "", {})) == ""

    def test_path_file_absolute(self) -> None:
        """path 类型绝对路径文件注入。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        var_def = {"type": "path", "name": "p", "path": str(Path(_THIS_DIR) / "plugin.py")}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "", {}))
        assert out.startswith("<plugin>") and out.endswith("</plugin>")

    def test_path_file_relative_project_root(self) -> None:
        """path 类型相对路径经 project_root 解析。"""
        plugin = make_plugin()
        ctx = make_ctx({"project_root": str(_THIS_DIR)})
        var_def = {"type": "path", "name": "p", "path": "plugin.py"}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "", {}))
        assert "<plugin>" in out

    def test_path_missing_file(self) -> None:
        """path 类型找不到文件/目录 → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({"project_root": str(_THIS_DIR)})
        var_def = {"type": "path", "name": "p", "path": "definitely-missing-xyz.md"}
        assert _run(plugin._resolve_single_var_content(ctx, var_def, "", {})) == ""

    def test_path_file_read_error_warns(self, caplog, monkeypatch, tmp_path) -> None:
        """path 类型文件读取失败 → warning 留痕 + 空串。"""
        import logging

        plugin = make_plugin()
        (tmp_path / "doc.md").write_text("内容", encoding="utf-8")

        def boom_read_text(self: Any, *a: Any, **k: Any) -> str:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", boom_read_text)  # 文件系统故障（外部依赖）
        ctx = make_ctx({"project_root": str(tmp_path)})
        var_def = {"type": "path", "name": "p", "path": "doc.md"}
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_single_var_content(ctx, var_def, "", {}))
        assert out == ""
        assert any("读取静态变量文件失败" in r.getMessage() for r in caplog.records)

    def test_path_dir_inject(self, tmp_path) -> None:
        """path 类型目录 → 遍历顶层文件并包裹 <files dir=...>。"""
        plugin = make_plugin()
        (tmp_path / "a.md").write_text("AAA", encoding="utf-8")
        (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("CCC", encoding="utf-8")
        ctx = make_ctx({"workspace": str(tmp_path)})
        var_def = {"type": "path", "name": "d", "path": "."}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "", {}))
        assert out.startswith('<files dir=".">')
        assert "--- a.md ---" in out and "AAA" in out
        assert "--- b.txt ---" in out  # 无扩展名过滤 → 全部顶层文件
        assert "CCC" not in out  # 非递归：子目录内容不注入

    def test_reference_content_and_value(self) -> None:
        """reference/content/空 type 走 content/value 字段。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "reference", "content": "R"}, "", {})) == "R"
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "content", "value": "V"}, "", {})) == "V"
        assert _run(plugin._resolve_single_var_content(ctx, {"type": "", "content": "E"}, "", {})) == "E"

    def test_reference_tags_retrieval(self) -> None:
        """reference 无 content 但有 tags → 走知识检索。"""
        plugin = make_plugin()
        ctx = make_ctx({}, services={"memory_service": FakeMemoryService([_Req("KB 片段")])})
        var_def = {"type": "reference", "tags": ["a"]}
        out = _run(plugin._resolve_single_var_content(ctx, var_def, "", {}))
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
                    ctx, {"type": "reference", "content": "原内容", "mode": "vector"}, "", {}
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
            async def search(self, query: str = "", user_id: str = "", top_k: int = 5, memory_type: str | None = None) -> list[dict[str, str]]:
                return [{"content": "补充知识"}]

        old = _mod._memory_backend
        _mod._memory_backend = FakeBackend()
        try:
            out = _run(
                plugin._resolve_single_var_content(
                    ctx, {"type": "reference", "content": "原文", "mode": "hybrid"}, "", {}
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
            out = _run(plugin._resolve_single_var_content(ctx, {"type": "reference", "content": "C", "mode": "vector"}, "", {}))
        finally:
            _mod._memory_backend = old
        assert out == "C"

    def test_output_format_summary(self) -> None:
        """output_format=summary → 前缀 [摘要]。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        out = _run(
            plugin._resolve_single_var_content(
                ctx, {"type": "content", "content": "长文本", "output_format": "summary"}, "", {}
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
# 压缩加载（_load_compression_messages / 预算 / 过滤 / 估算）
# ═══════════════════════════════════════════════════════════


class _Backend:
    """记录调用并返回固定结果的伪 IMemoryBackend。"""

    def __init__(self, results: list[Any]) -> None:
        self.results = results

    async def search(self, query: str = "", user_id: str = "", top_k: int = 5, memory_type: str | None = None) -> list[Any]:
        return list(self.results)


class TestLoadCompression:
    def test_no_pipeline_id_returns_empty(self) -> None:
        """无 pipeline_id → 空列表（即使有后端也不查询）。"""
        plugin = make_plugin()
        backend = _Backend([_chunk("x", ["L1", "pipeline:p-1"])])
        _mod._memory_backend = backend
        try:
            assert _run(plugin._load_compression_messages(make_ctx({"user_id": "u"}))) == []
        finally:
            _mod._memory_backend = None

    def test_backend_error_warns_and_returns_empty(self, caplog) -> None:
        """后端 search 抛异常 → warning + 空列表。"""
        import logging

        plugin = make_plugin()

        class BoomBackend:
            async def search(self, **kwargs: Any) -> list[Any]:
                raise RuntimeError("backend down")

        _mod._memory_backend = BoomBackend()
        try:
            with caplog.at_level(logging.WARNING):
                out = _run(plugin._load_compression_messages(make_ctx({"pipeline_id": "p-1"})))
        finally:
            _mod._memory_backend = None
        assert out == []
        assert any("读取压缩块失败" in r.getMessage() for r in caplog.records)

    def test_l2_blocks_first_then_l1(self) -> None:
        """L2 块组装在 L1 之前，且预算充足时两者都保留。"""
        plugin = make_plugin()
        backend = _Backend(
            [
                _chunk("L1 摘要", ["L1", "pipeline:p-1", "seq:1-5"]),
                _chunk("L2 摘要", ["L2", "pipeline:p-1", "seq:6-10"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "context_window": 128000})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        joined = "\n".join(m["content"] for m in out)
        assert joined.index('level="L2"') < joined.index('level="L1"')
        assert "L1 摘要" in joined and "L2 摘要" in joined

    def test_budget_dedup_by_seq_start(self) -> None:
        """seq_start 重叠块被去重（嵌套在更新块范围内的旧块丢弃）。"""
        plugin = make_plugin()
        backend = _Backend(
            [
                _chunk("老块", ["L1", "pipeline:p-1", "seq:15-16"]),
                _chunk("新块", ["L1", "pipeline:p-1", "seq:10-20"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "context_window": 128000})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        contents = "\n".join(m["content"] for m in out)
        assert contents.count("<compressed") == 1  # 去重后只剩一块
        assert "新块" in contents
        assert "老块" not in contents

    def test_budget_exhausted_skips_compression(self, caplog) -> None:
        """已用 token 超过触发阈值 → 跳过压缩块加载（L1/L2 都无）。"""
        import logging

        plugin = make_plugin()
        backend = _Backend([_chunk("L1 摘要", ["L1", "pipeline:p-1", "seq:1-5"])])
        _mod._memory_backend = backend
        try:
            ctx = make_ctx(
                {
                    "pipeline_id": "p-1",
                    "context_window": 100,
                    "system_message": {"content": "x" * 200},
                    "messages": [],
                }
            )
            with caplog.at_level(logging.INFO):
                out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        assert out == []
        assert any("无可用预算" in r.getMessage() for r in caplog.records)

    def test_budget_exhausted_skips_everything(self) -> None:
        """预算耗尽 → 压缩块跳过；且当前实现连状态快照也不加载（预算检查在快照之前）。

        [现状]：有压缩块但预算耗尽的路径直接 return []，与"无压缩块→加载快照"
        路径不一致——快照被一起丢弃。此处按现状行为断言（疑似缺陷，见报告）。
        """
        plugin = make_plugin()
        backend = _Backend(
            [
                _chunk("L1 摘要", ["L1", "pipeline:p-1", "seq:1-5"]),
                _chunk('{"state": "ok"}', ["STATE_SNAPSHOT", "pipeline:p-1"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "context_window": 100, "system_message": {"content": "x" * 200}})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        assert out == []  # 现状：预算耗尽 → 快照也被丢弃

    def test_l1_to_l2_fallback_when_l1_full(self) -> None:
        """L1 预算耗尽后，带 L2 内容的较老块降级进 L2（新→老按序分配）。"""
        plugin = make_plugin()
        # 最新块（seq 20-30）L1 内容吃满 L1 预算；较老块（seq 10-20）L1 被拒
        # 但其 L2 内容较小 → 落入 L2
        backend = _Backend(
            [
                _chunk("B" * 25400, ["L1", "pipeline:p-1", "seq:20-30"]),
                _chunk("B" * 25400, ["L1", "pipeline:p-1", "seq:10-20"]),
                _chunk("老块L2", ["L2", "pipeline:p-1", "seq:10-20"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "context_window": 128000})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        contents = "\n".join(m["content"] for m in out)
        assert 'seq="20-30" level="L1"' in contents  # 最新块在 L1
        assert 'seq="10-20" level="L2"' in contents  # 较老块降级 L2
        assert contents.index('level="L2"') < contents.index('level="L1"')

    def test_empty_content_dropped(self) -> None:
        """L1/L2 都无内容 → 块被丢弃，只保留状态快照（若有）。"""
        plugin = make_plugin()
        backend = _Backend([_chunk("", ["L1", "pipeline:p-1", "seq:1-5"])])
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "context_window": 128000})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        assert out == []

    def test_compression_missing_chunks_still_loads_snapshot(self) -> None:
        """无本管道压缩块 → 直接加载状态快照（不跑预算逻辑）。"""
        plugin = make_plugin()
        backend = _Backend(
            [
                _chunk("别的管道", ["L1", "pipeline:other", "seq:1-5"]),
                _chunk('{"state": "ok"}', ["STATE_SNAPSHOT", "pipeline:p-1"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "user_id": "u-1"})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        assert len(out) == 1
        assert "<current_state>" in out[0]["content"]

    def test_compression_missing_chunks_and_no_snapshot_empty(self) -> None:
        """无压缩块也无快照 → 空列表。"""
        plugin = make_plugin()
        backend = _Backend([_chunk("别的管道", ["L1", "pipeline:other", "seq:1-5"])])
        _mod._memory_backend = backend
        try:
            ctx = make_ctx({"pipeline_id": "p-1", "user_id": "u-1"})
            out = _run(plugin._load_compression_messages(ctx))
        finally:
            _mod._memory_backend = None
        assert out == []

    def test_estimate_tokens_for_budget_bounds(self) -> None:
        """token 估算：空串 0；非空 ≥1 且不超过 len(text)//2+1。"""
        plugin = make_plugin()
        assert plugin._estimate_tokens_for_budget("") == 0
        for text in ["a", "ab", "abc", "x" * 10]:
            est = plugin._estimate_tokens_for_budget(text)
            assert 1 <= est <= len(text) // 2 + 1
        assert plugin._estimate_tokens_for_budget("abcd") == 2  # 性质基准：偶数长度


# ═══════════════════════════════════════════════════════════
# 过滤与序列解析
# ═══════════════════════════════════════════════════════════


class TestFilterPipelineChunks:
    def test_filters_and_merges(self) -> None:
        """非 dict / 非列表 tags / 非本管道 / 缺 L1|L2 标签 → 过滤；L1/L2 合并。"""
        results = [
            "not-a-dict",  # 非 dict
            {"metadata": {"tags": "not-a-list"}},  # tags 非列表
            {"metadata": {"tags": ["L1", "pipeline:other"]}},  # 其他管道
            {"metadata": {"tags": ["pipeline:p-1"]}},  # 无 L1/L2
            {"metadata": {"tags": ["L1", "pipeline:p-1", "seq:1-5"]}, "content": "一"},
            {"metadata": {"tags": ["L2", "pipeline:p-1", "seq:1-5"]}, "content": "二"},
        ]
        merged = _mod.PromptBuildPlugin._filter_pipeline_chunks(results, "p-1")
        assert len(merged) == 1
        assert merged[0]["seq"] == "1-5"
        assert merged[0]["l1_content"] == "一"
        assert merged[0]["l2_content"] == "二"

    def test_seq_end_zero_dropped(self) -> None:
        """seq 解析不到 → seq_end=0 被丢弃。"""
        results = [{"metadata": {"tags": ["L1", "pipeline:p-1"]}, "content": "x"}]
        assert _mod.PromptBuildPlugin._filter_pipeline_chunks(results, "p-1") == []

    def test_parse_seq_forms(self) -> None:
        """seq 标签：范围/单值/非数字/非 str/缺 seq 标签。"""
        parse = _mod.PromptBuildPlugin._parse_seq_from_tags
        assert parse(["seq:5-12"]) == (5, 12)
        assert parse(["seq:7"]) == (7, 7)
        assert parse(["seq:ab-cd"]) == (0, 0)  # ValueError → 保持 (0,0)
        assert parse(["seq:5"]) == (5, 5)  # 无 '-' 单值
        assert parse(["seq:1-2", "seq:9-10"]) == (9, 10)  # 后者覆盖前者
        assert parse(["seq:xyz"]) == (0, 0)  # 单值非数字 → ValueError → (0,0)
        assert parse([123]) == (0, 0)  # 非 str 跳过
        assert parse([]) == (0, 0)


# ═══════════════════════════════════════════════════════════
# 状态快照
# ═══════════════════════════════════════════════════════════


class TestStateSnapshot:
    def test_snapshot_no_backend_empty(self) -> None:
        """无后端 → 空列表。"""
        _mod._memory_backend = None
        plugin = make_plugin()
        assert _run(plugin._load_state_snapshot_message(make_ctx({}), "p-1")) == []

    def test_snapshot_non_dict_items_skipped(self) -> None:
        """结果含非 dict / 无快照标签条目 → 返回空列表。"""
        plugin = make_plugin()
        backend = _Backend(
            [
                "junk",
                {"metadata": {"tags": ["L1", "pipeline:p-1"]}, "content": "x"},
                {"metadata": {"tags": "not-a-list"}, "content": "y"},
            ]
        )
        _mod._memory_backend = backend
        try:
            out = _run(plugin._load_state_snapshot_message(make_ctx({"pipeline_id": "p-1"}), "p-1"))
        finally:
            _mod._memory_backend = None
        assert out == []

    def test_snapshot_picks_first_match(self) -> None:
        """多条快照 → 返回最早匹配那条（顺序取首）。"""
        plugin = make_plugin()
        backend = _Backend(
            [
                _chunk("第一个", ["STATE_SNAPSHOT", "pipeline:p-1"]),
                _chunk("第二个", ["STATE_SNAPSHOT", "pipeline:p-1"]),
            ]
        )
        _mod._memory_backend = backend
        try:
            out = _run(plugin._load_state_snapshot_message(make_ctx({"user_id": "u"}), "p-1"))
        finally:
            _mod._memory_backend = None
        assert len(out) == 1
        assert "第一个" in out[0]["content"]

    def test_snapshot_other_pipeline_ignored(self) -> None:
        """其他管道的 STATE_SNAPSHOT → 空。"""
        plugin = make_plugin()
        backend = _Backend([_chunk("别的", ["STATE_SNAPSHOT", "pipeline:other"])])
        _mod._memory_backend = backend
        try:
            assert _run(plugin._load_state_snapshot_message(make_ctx({}), "p-1")) == []
        finally:
            _mod._memory_backend = None


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
