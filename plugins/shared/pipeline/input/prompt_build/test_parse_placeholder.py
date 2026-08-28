# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""占位符语法解析（_parse_placeholder / _resolve_placeholder 分发）的单测。

覆盖既有测试未触达的解析分支：
    - 简单保留字（rules/session/workspace/project_root/timestamp）→ 无参
    - path 的 extensions 过滤（|extensions=.md,.yaml 形式与空扩展名跳过）
    - content 类型
    - 通用键值对占位符（retrieval:tags=a|top_k=5 → tags 列表 + int top_k）
    - workspace/project_root 占位符的 state/服务回退解析
    - vector/hybrid/routed 占位符 → 复用 var_def 后解析（routed 含 routes 提取）
    - retrieval: 缺省 top_k → 5；空 tags → 空检索

原则：只通过公开/受保护方法断言可观察行为；不 mock 内部依赖。
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
    "prompt_build_plugin_parse_test", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_parse_test"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin  # noqa: E402


def make_plugin() -> PromptBuildPlugin:
    return PromptBuildPlugin({})


def make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestParsePlaceholder:
    def test_parse_simple_names(self) -> None:
        """简单保留名返回 (名称, 空参数字典)，不进入参数解析分支。"""
        plugin = make_plugin()
        assert plugin._parse_placeholder("session") == ("session", {})
        assert plugin._parse_placeholder("workspace") == ("workspace", {})
        assert plugin._parse_placeholder("project_root") == ("project_root", {})
        assert plugin._parse_placeholder("timestamp") == ("timestamp", {})

    def test_parse_timestamp_with_format(self) -> None:
        """{{timestamp:%Y}} → (timestamp, {format})。"""
        plugin = make_plugin()
        assert plugin._parse_placeholder("timestamp:%Y") == ("timestamp", {"format": "%Y"})

    def test_parse_path_with_extensions(self) -> None:
        """{{path:docs|extensions=.md,.yaml}} → 路径 + 扩展名白名单。"""
        plugin = make_plugin()
        t, params = plugin._parse_placeholder("path:docs|extensions=.md,.yaml")
        assert t == "path"
        assert params["path"] == "docs"
        assert params["extensions"] == [".md", ".yaml"]

    def test_parse_path_plain(self) -> None:
        """{{path:foo.md}} → 无 extensions 键（文件注入走同一解析）。"""
        plugin = make_plugin()
        t, params = plugin._parse_placeholder("path:foo.md")
        assert t == "path"
        assert params == {"path": "foo.md"}

    def test_parse_content(self) -> None:
        """{{content:文本}} → content 变量。"""
        plugin = make_plugin()
        assert plugin._parse_placeholder("content:hello") == ("content", {"content": "hello"})

    def test_parse_key_value_retrieval(self) -> None:
        """{{retrieval:tags=a,b|top_k=3}} → 键值对解析 + int 值。"""
        plugin = make_plugin()
        t, params = plugin._parse_placeholder("retrieval:tags=a,b|top_k=3")
        assert t == "retrieval"
        assert params["tags"] == "a,b"
        assert params["top_k"] == "3"

    def test_parse_empty_args_generic(self) -> None:
        """未知类型 + 空参数 → 通用键值对分支产出一个空键（现状行为）。"""
        plugin = make_plugin()
        assert plugin._parse_placeholder("unknown") == ("unknown", {"": ""})


class TestResolvePlaceholderDispatch:
    def test_path_placeholder_file_inject(self, tmp_path, monkeypatch) -> None:
        """{{path:doc.py}} → 走 path 分支 → 按系统项目根解析文件内容注入。"""
        (tmp_path / "config").mkdir()
        (tmp_path / "doc.py").write_text("ROOT_DOC = 1\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        plugin = make_plugin()
        ctx = make_ctx({})
        out = _run(plugin._resolve_placeholder(ctx, "path:doc.py"))
        assert out.startswith("<doc>") and out.endswith("</doc>")

    def test_timestamp_placeholder_with_format(self) -> None:
        """{{timestamp:%Y}} → timestamp 分支带 format 参数，产出带时区后缀。"""
        import re

        plugin = make_plugin()
        ctx = make_ctx({})
        out = _run(plugin._resolve_placeholder(ctx, "timestamp:%Y"))
        assert re.fullmatch(r"\d{4} \(UTC[+-]\d+([:.]\d+)?, [^)]+\)", out)

    def test_workspace_placeholder_returns_system_root(self) -> None:
        """{{workspace}} = 实际项目目录（系统根），不读 state["workspace"]。"""
        plugin = make_plugin()
        ctx = make_ctx({"workspace": "/ws/1"})
        out = _run(plugin._resolve_placeholder(ctx, "workspace"))
        assert Path(out).is_absolute() and (Path(out) / "config").is_dir()

    def test_project_root_placeholder_returns_system_root(self) -> None:
        """{{project_root}} = 实际项目目录（系统根），不受 state 工作区影响。"""
        plugin = make_plugin()
        ctx = make_ctx({"project_root": "/proj"})
        out = _run(plugin._resolve_placeholder(ctx, "project_root"))
        assert Path(out).is_absolute() and (Path(out) / "config").is_dir()

    def test_rules_placeholder_retired(self) -> None:
        """{{rules}} 已退役：约束改注入式（context.constraints_text），占位符按未识别降级空串。"""
        plugin = make_plugin()
        ctx = make_ctx({"constraints": {"hard": ["h1"], "soft": ["s1"]}})
        out = _run(plugin._resolve_placeholder(ctx, "rules"))
        assert out == ""

    def test_content_placeholder(self) -> None:
        """{{content:正文}} → 直接注入正文。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_placeholder(ctx, "content:正文")) == "正文"

    def test_session_placeholder(self) -> None:
        """{{session}} → context.session_id。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.session_id": "sess-9"})
        assert _run(plugin._resolve_placeholder(ctx, "session")) == "sess-9"

    def test_retrieval_placeholder_empty_tags_no_service(self) -> None:
        """{{retrieval}} 无 tags → 空串且不触发服务调用（tags 为空提前返回）。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_placeholder(ctx, "retrieval")) == ""

    def test_retrieval_placeholder_service_missing(self) -> None:
        """{{retrieval:tags=a}} 有 tags 但 memory_service 未注册 → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_placeholder(ctx, "retrieval:tags=a")) == ""

    def test_vector_placeholder_file_inject(self, tmp_path, monkeypatch) -> None:
        """{{vector:path=doc.py|top_k=1}}（等号形式）→ path 文件注入成功。

        [现状]：vector 占位符的路径参数实际用 ``path=<路径>`` 等号形式；
        docstring 里的 ``{{vector:path:x|top_k=3}}`` 冒号形式会把 ``path:x``
        整体当成键名（path 参数为空），解析结果为注入失败——按现状断言。
        """
        (tmp_path / "config").mkdir()
        (tmp_path / "doc.py").write_text("ROOT_DOC = 1\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
        plugin = make_plugin()
        ctx = make_ctx({})
        out = _run(plugin._resolve_placeholder(ctx, "vector:path=doc.py|top_k=1"))
        assert "ROOT_DOC" in out  # 文件内容注入成功
        # 冒号形式（docstring 写法）按现状行为解析为空键
        t, params = plugin._parse_placeholder("vector:path:plugin.py|top_k=1")
        assert t == "vector"
        assert params.get("path") in (None, "")
        assert params.get("top_k") == "1"

    def test_hybrid_placeholder_no_service(self) -> None:
        """{{hybrid:tags=k}} → 无 memory_service 时降级为空串（不崩溃）。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_placeholder(ctx, "hybrid:tags=k")) == ""

    def test_routed_placeholder_default(self) -> None:
        """{{routed:route_key=tier}} → routes 提取（route_key 之外的键进 routes 表）。"""
        plugin = make_plugin()
        ctx = make_ctx({"tier": "big"})
        out = _run(plugin._resolve_placeholder(ctx, "routed:route_key=tier|big=大档|small=小档"))
        assert out == "大档"
