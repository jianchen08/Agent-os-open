# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""prompt_build 主流程（execute/_do_work）、系统内容组装、
目录遍历、嵌套路由与 MCP server.py 适配层的行为测试。

覆盖既有测试未触达的行为面：
    - execute/_do_work 全链路（动态变量、超时降级；压缩块经序列持久化，
      本插件读路径零压缩装配——ADR 2026-08-28 compression-block-pointer-indirection）
    - _build_system_content：语言指令（已知/未知）、tools 开关、static_vars 开关、占位符
    - _read_dir_entries：非目录、OSError、扩展名过滤、超大文件跳过、读取失败、空目录
    - _resolve_target_path：空路径
    - _resolve_placeholders 占位符解析超时降级
    - _resolve_routed_var：无 route_key、嵌套 path/retrieval/content dict、路径缺失、None 值规范化
    - server.py：工具注册、execute 工具、on_load/on_unload 生命周期

外部依赖（文件系统/超时控制）用真实实现或标准库级 patch；
内部异步方法一律真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
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

from pipeline.plugin import PluginContext, PluginResult  # noqa: E402

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _THIS_DIR 显式路径加载（与 test_prompt_build.py 的 _load_plugin_module 同范式）。
_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_workflow_test", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_workflow_test"] = _mod
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


# ═══════════════════════════════════════════════════════════
# 插件配置接口
# ═══════════════════════════════════════════════════════════


class TestPluginConfig:
    def test_priority_from_config(self) -> None:
        """priority 从 config 读取，默认 50。"""
        assert PromptBuildPlugin({"priority": 77}).priority == 77
        assert make_plugin().priority == 50

    def test_name_and_max_depth(self) -> None:
        """name 固定；placeholder_max_depth 缺省 5、支持配置覆盖。"""
        assert make_plugin().name == "prompt_build"
        assert make_plugin({"placeholder_max_depth": 0})._placeholder_max_depth == 0
        assert make_plugin()._placeholder_max_depth == 5


# ═══════════════════════════════════════════════════════════
# execute / _do_work 主流程
# ═══════════════════════════════════════════════════════════


class TestExecuteWorkflow:
    def test_execute_full_flow(self) -> None:
        """execute 产出 system_message + dynamic_vars（压缩块经序列持久化，
        不在本插件装配面——ADR 2026-08-28 compression-block-pointer-indirection）。"""
        plugin = make_plugin({"dynamic_vars": [{"type": "session", "name": "会话"}]})
        ctx = make_ctx(
            {
                "pipeline_id": "p-1",
                "context.system_prompt": "你是助手",
                "context.session_id": "s-1",
            }
        )
        result = _run(plugin.execute(ctx))
        assert isinstance(result, PluginResult)
        updates = result.state_updates
        assert updates["system_message"] == {"role": "system", "content": "你是助手"}
        assert "compression_messages" not in updates
        assert "- 会话: s-1" in updates["prompt.dynamic_vars"]["content"]

    def test_do_work_never_produces_compression_messages(self) -> None:
        """任何配置下都不产出 compression_messages（读路径零压缩装配）。"""
        plugin = make_plugin({"include_compressed_layers": True})
        ctx = make_ctx({"context.system_prompt": "P", "pipeline_id": "p-1"})
        updates = _run(plugin._do_work(ctx))
        assert updates["system_message"]["content"] == "P"
        assert "compression_messages" not in updates
        assert "prompt.dynamic_vars" not in updates

    def test_do_work_dynamic_vars_timeout_injects_degrade_marker(self, monkeypatch) -> None:
        """动态变量构建超时(30s) → 注入 [上下文降级] 标记，不静默缺席。"""
        import warnings

        real_wait_for = asyncio.wait_for

        def fake_wait_for(awaitable: Any, timeout: float) -> Any:
            if timeout == _mod.DYNAMIC_VARS_BUILD_TIMEOUT_S:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    awaitable.close()
                raise asyncio.TimeoutError("dynamic vars stuck")
            return real_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        plugin = make_plugin({"dynamic_vars": [{"type": "session", "name": "会话"}]})
        ctx = make_ctx({"context.session_id": "s-2"})
        updates = _run(plugin._do_work(ctx))
        msg = updates["prompt.dynamic_vars"]
        assert isinstance(msg, dict)
        assert msg["role"] == "user"
        assert "[上下文降级]" in msg["content"]
        assert "超时" in msg["content"]

    def test_do_work_dynamic_vars_normal_flow_untouched(self) -> None:
        """对照：正常构建（零兜底，无声明 → 不产出）不受降级标记影响。"""
        plugin = make_plugin()
        ctx = make_ctx({"context.system_prompt": "P"})
        updates = _run(plugin._do_work(ctx))
        assert "prompt.dynamic_vars" not in updates


# ═══════════════════════════════════════════════════════════
# 系统内容组装（_build_system_content）
# ═══════════════════════════════════════════════════════════


class TestBuildSystemContent:
    def test_language_known_and_unknown(self) -> None:
        """已知语言 → 标准指令；未知语言 → 通用 '请使用X' 句式。"""
        plugin = make_plugin({"language": "zh-CN"})
        ctx = make_ctx({"context.system_prompt": "P"})
        out = _run(plugin._build_system_content(ctx))
        assert out.startswith("P")
        assert "请使用中文（简体）思考和回复" in out

        plugin2 = make_plugin({"language": "xx"})
        out2 = _run(plugin2._build_system_content(ctx))
        assert "请使用xx思考和回复" in out2

    def test_tools_description_toggle(self) -> None:
        """include_tools_description_in_prompt 开启且 state 有描述 → 拼入；默认关闭。"""
        ctx = make_ctx(
            {"context.system_prompt": "P", "prompt.tool_descriptions": "工具说明"}
        )
        out = _run(make_plugin()._build_system_content(ctx))
        assert "工具说明" not in out  # 默认关闭

        out2 = _run(
            make_plugin({"include_tools_description_in_prompt": True})._build_system_content(ctx)
        )
        assert "工具说明" in out2

    def test_static_vars_toggle(self) -> None:
        """include_static_vars=False → 静态变量不拼入。"""
        ctx = make_ctx(
            {
                "context.system_prompt": "P",
                "context.static_vars": [{"type": "session", "name": "s"}],
                "context.session_id": "s-3",
            }
        )
        out = _run(make_plugin({"include_static_vars": False})._build_system_content(ctx))
        assert "静态变量" not in out

    def test_system_prompt_placeholder_resolved(self) -> None:
        """system_prompt 含 {{session}} 占位符 → 先解析再拼入。"""
        ctx = make_ctx({"context.system_prompt": "Hi {{session}}", "context.session_id": "s-4"})
        out = _run(make_plugin()._build_system_content(ctx))
        assert "Hi s-4" in out

    def test_empty_system_prompt(self) -> None:
        """无 system_prompt → 其他段正常拼入。"""
        ctx = make_ctx({"context.static_vars": [{"type": "session", "name": "s"}], "context.session_id": "s-5"})
        out = _run(make_plugin()._build_system_content(ctx))
        assert "静态变量" in out


# ═══════════════════════════════════════════════════════════
# 目录读取（_read_dir_entries）
# ═══════════════════════════════════════════════════════════


class TestReadDirEntries:
    def test_non_dir_returns_empty(self, tmp_path) -> None:
        """目标不是目录 → 空串。"""
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        assert _run(make_plugin()._read_dir_entries(f)) == ""

    def test_iter_error_warns(self, tmp_path, monkeypatch, caplog) -> None:
        """目录遍历 OSError → warning + 空串。"""
        def boom_iters(self: Any) -> Any:
            raise OSError("denied")

        monkeypatch.setattr(Path, "iterdir", boom_iters)
        with caplog.at_level(logging.WARNING):
            out = _run(make_plugin()._read_dir_entries(tmp_path))
        assert out == ""
        assert any("目录遍历失败" in r.getMessage() for r in caplog.records)

    def test_extensions_filter(self, tmp_path) -> None:
        """扩展名白名单过滤非匹配文件。"""
        (tmp_path / "a.md").write_text("M", encoding="utf-8")
        (tmp_path / "b.txt").write_text("T", encoding="utf-8")
        out = _run(make_plugin()._read_dir_entries(tmp_path, extensions=[".md"]))
        assert "a.md" in out and "M" in out
        assert "b.txt" not in out

    def test_oversize_file_skipped(self, tmp_path, monkeypatch, caplog) -> None:
        """超过 10MB 上限的文件跳过并 debug 留痕；正常文件照常读取。"""
        import os
        import stat as stat_mod

        (tmp_path / "big.bin").write_text("x", encoding="utf-8")
        (tmp_path / "ok.md").write_text("OK", encoding="utf-8")

        orig_stat = Path.stat

        def selective_stat(self: Any, **kwargs: Any) -> Any:
            if self.name == "big.bin":
                return os.stat_result(
                    (stat_mod.S_IFREG, 0, 0, 0, 0, 0, 11 * 1024 * 1024, 0, 0, 0)
                )
            return orig_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", selective_stat)
        with caplog.at_level(logging.DEBUG):
            out = _run(make_plugin()._read_dir_entries(tmp_path))
        assert "OK" in out  # 正常文件仍注入
        assert "big.bin" not in out  # 超大文件被跳过
        assert any("跳过超大文件" in r.getMessage() for r in caplog.records)

    def test_read_error_skipped(self, tmp_path, monkeypatch, caplog) -> None:
        """文件读取失败（解码错误）→ 跳过该文件继续。"""
        (tmp_path / "bad.txt").write_bytes(b"\xff\xfe")
        (tmp_path / "good.txt").write_text("GOOD", encoding="utf-8")
        out = _run(make_plugin()._read_dir_entries(tmp_path))
        assert "GOOD" in out
        assert "bad.txt" not in out  # 读取失败的文件不进结果

    def test_empty_dir_returns_empty(self, tmp_path) -> None:
        """空目录 → 空串。"""
        assert _run(make_plugin()._read_dir_entries(tmp_path)) == ""


# ═══════════════════════════════════════════════════════════
# 目标路径解析
# ═══════════════════════════════════════════════════════════


class TestResolveTargetPath:
    def test_empty_path_returns_none(self) -> None:
        """空串/纯空白路径 → None。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert plugin._resolve_target_path(ctx, "") is None
        assert plugin._resolve_target_path(ctx, "   ") is None


# ═══════════════════════════════════════════════════════════
# 占位符解析超时
# ═══════════════════════════════════════════════════════════


class TestPlaceholderTimeout:
    def test_resolve_placeholder_timeout_skips(self, monkeypatch) -> None:
        """单个占位符解析超时(30s) → 替换为空串继续，不挂起。"""
        import warnings

        real_wait_for = asyncio.wait_for

        def fake_wait_for(awaitable: Any, timeout: float) -> Any:
            if timeout == 30.0:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    awaitable.close()
                raise asyncio.TimeoutError("placeholder stuck")
            return real_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        plugin = make_plugin()
        ctx = make_ctx({"context.session_id": "s"})
        out = _run(plugin._resolve_placeholders(ctx, "A {{session}} B"))
        assert out == "A  B"  # 超时占位符替换为空


# ═══════════════════════════════════════════════════════════
# 路由变量嵌套定义
# ═══════════════════════════════════════════════════════════


class TestRoutedNestedDict:
    def test_no_route_key_or_routes_empty(self) -> None:
        """缺 route_key 或 routes 空 → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({})
        assert _run(plugin._resolve_routed_var(ctx, {"route_key": ""})) == ""
        assert _run(plugin._resolve_routed_var(ctx, {"route_key": "k", "routes": {}})) == ""

    def test_nested_path_dict_reads_file(self, tmp_path) -> None:
        """routes 值 {type: path} → 读取文件内容。"""
        f = tmp_path / "doc.md"
        f.write_text("嵌套文件内容", encoding="utf-8")
        plugin = make_plugin()
        ctx = make_ctx({"k": "v"})
        var_def = {"route_key": "k", "routes": {"v": {"type": "path", "path": str(f)}}}
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "嵌套文件内容"

    def test_nested_path_dict_missing_path(self) -> None:
        """{type: path} 但 path 为空 → 空串。"""
        plugin = make_plugin()
        ctx = make_ctx({"k": "v"})
        var_def = {"route_key": "k", "routes": {"v": {"type": "path", "path": ""}}}
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == ""

    def test_nested_path_read_failure_warns(self, tmp_path, monkeypatch, caplog) -> None:
        """嵌套 path 文件读取失败 → warning + 空串。"""
        f = tmp_path / "doc.md"
        f.write_text("x", encoding="utf-8")

        def boom_read_text(self: Any, *a: Any, **k: Any) -> str:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", boom_read_text)
        plugin = make_plugin()
        ctx = make_ctx({"k": "v"})
        var_def = {"route_key": "k", "routes": {"v": {"type": "path", "path": str(f)}}}
        with caplog.at_level(logging.WARNING):
            out = _run(plugin._resolve_routed_var(ctx, var_def))
        assert out == ""
        assert any("路由嵌套变量文件读取失败" in r.getMessage() for r in caplog.records)

    def test_nested_retrieval_dict(self) -> None:
        """routes 值含 tags → 走 _retrieve_by_tags。"""
        plugin = make_plugin()
        ctx = make_ctx({"k": "v"}, services={"memory_service": type("S", (), {"retrieve": _retrieve})()})
        var_def = {"route_key": "k", "routes": {"v": {"tags": ["a"]}}}
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "知识内容"

    def test_nested_plain_dict_uses_content(self) -> None:
        """routes 值无 type/tags → 取 content 字段。"""
        plugin = make_plugin()
        ctx = make_ctx({"k": "v"})
        var_def = {"route_key": "k", "routes": {"v": {"content": "普通内容"}}}
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "普通内容"

    def test_state_value_none_normalized(self) -> None:
        """state 键值为 None → 规范化空串，命中 '' 路由。"""
        plugin = make_plugin()
        ctx = make_ctx({"k": None})
        var_def = {"route_key": "k", "routes": {"": "空值路由", "_default": "兜底"}}
        assert _run(plugin._resolve_routed_var(ctx, var_def)) == "空值路由"


async def _retrieve(self: Any, **kwargs: Any) -> list[Any]:
    return [_Req("知识内容")]


class _Req:
    def __init__(self, content: str) -> None:
        self.content = content


# ═══════════════════════════════════════════════════════════
# server.py 适配层
# ═══════════════════════════════════════════════════════════


def _load_server_module() -> Any:
    # 先逐出陈旧裸名 `plugin`：server.py 顶层 `from plugin import` 走
    # sys.modules 缓存，共跑车道里其他插件测试（收集期导入）会把自身
    # plugin.py 装进裸名缓存，不逐出则劫持到错误实现。
    sys.modules.pop("plugin", None)
    mod_name = "prompt_build_server_test"
    module_path = Path(_THIS_DIR) / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestServerAdapter:
    def test_get_instance_returns_plugin(self) -> None:
        """get_instance 返回 PromptBuildPlugin 实例（懒构建缓存）。"""
        server = _load_server_module()
        inst = server.get_instance()
        assert isinstance(inst, server.PromptBuildPlugin)
        assert inst.name == "prompt_build"

    def test_on_unload_clears_cache(self) -> None:
        """on_unload 清空单例缓存（之后 get_instance 仍可重建）。"""
        server = _load_server_module()
        _run(server._on_unload({}))
        inst = server.get_instance()
        assert isinstance(inst, server.PromptBuildPlugin)

    def test_on_load_injects_memory_backend(self, monkeypatch) -> None:
        """on_load 注入记忆后端（build_memory_backend 返回 backend 时）。"""
        server = _load_server_module()

        class FakeBackend:
            pass

        monkeypatch.setattr(server, "build_memory_backend", lambda plugin: FakeBackend())
        _run(server._on_load({}))
        # server.py 内部 import 的 plugin 模块（sys.modules["plugin"]）应持有注入的后端
        assert sys.modules["plugin"]._memory_backend is not None
        sys.modules["plugin"]._memory_backend = None

    def test_on_load_no_backend_warns(self, monkeypatch, caplog) -> None:
        """build_memory_backend 返回 None → warning 留痕（功能降级不崩溃）。"""
        server = _load_server_module()
        monkeypatch.setattr(server, "build_memory_backend", lambda plugin: None)
        with caplog.at_level(logging.WARNING):
            _run(server._on_load({}))
        assert any("记忆后端未注入" in r.getMessage() for r in caplog.records)

    def test_execute_tool_state_updates(self) -> None:
        """execute 工具：state 驱动，返回 state_updates 数据。"""
        server = _load_server_module()
        data = _run(
            server.execute(
                {"context.system_prompt": "你是助手", "pipeline_id": "p-9", "user_id": "u-9"}
            )
        )
        assert data["state_updates"]["system_message"] == {"role": "system", "content": "你是助手"}
        assert "route_signal" not in data
        assert "skip_remaining" not in data

    def test_execute_tool_with_config(self, monkeypatch) -> None:
        """execute 工具经 get_instance 构建的插件配置生效（语言指令）。"""
        server = _load_server_module()
        plugin = server.PromptBuildPlugin(config={"language": "en"})
        monkeypatch.setattr(server, "get_instance", lambda: plugin)
        data = _run(server.execute({"context.system_prompt": "P"}, config={}))
        content = data["state_updates"]["system_message"]["content"]
        assert "Please think and respond in English" in content

    def test_execute_tool_serializes_skip(self, monkeypatch) -> None:
        """execute 工具把 PluginResult 的 state_updates/skip_remaining 序列化进 data。"""
        from pipeline.plugin import PluginResult

        server = _load_server_module()

        class StubPlugin:
            async def execute(self, ctx: Any) -> PluginResult:
                return PluginResult(
                    state_updates={"system_message": {"role": "system", "content": "P"}},
                    skip_remaining=True,
                )

        monkeypatch.setattr(server, "get_instance", lambda: StubPlugin())
        data = _run(server.execute({"context.system_prompt": "P"}))
        assert data["state_updates"]["system_message"]["content"] == "P"
        assert "route_signal" not in data
        assert data["skip_remaining"] is True

    def test_execute_tool_dict_result_passthrough(self, monkeypatch) -> None:
        """execute 插件返回 dict 时原样透传（Core 插件形态）。"""
        server = _load_server_module()

        class DictPlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"state_updates": {"system_message": {"role": "system", "content": "D"}}}

        monkeypatch.setattr(server, "get_instance", lambda: DictPlugin())
        data = _run(server.execute({"context.system_prompt": "P"}))
        assert data == {"state_updates": {"system_message": {"role": "system", "content": "D"}}}

    def test_main_guard_runs_plugin(self, monkeypatch) -> None:
        """``if __name__ == "__main__"`` 守卫执行 plugin.run()（进程入口）。"""
        import runpy

        server = _load_server_module()
        ran: list[bool] = []
        monkeypatch.setattr(server.AgentOSPlugin, "run", lambda self: ran.append(True))
        runpy.run_path(str(Path(_THIS_DIR) / "server.py"), run_name="__main__")
        assert ran == [True]
