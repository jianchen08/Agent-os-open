# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""tool_cache_writer（output 写缓存）行为契约测试。

断输入→输出/副作用，不 mock 插件内部协作：
- priority：缺省 25（config 可覆盖）。
- 缓存停用（enabled=False）→ 零写入。
- 数据源缺失（无 _executed_tool_calls / 无 tool_results）→ 零写入。
- 成功结果写入共享单例缓存 → input 端（tool_cache）可命中（真往返）。
- 失败结果（error 非空）→ 跳过不写。
- 排除表工具（bash/file_write 等有副作用工具）→ 跳过不写——含本插件
  构造期并集的 bash 缺省排除（ADR 2026-11-07 T2 改名补偿：SDK 缺省表
  仍含退役名 bash_execute，改名后的工具名在插件层兜底）。

缓存实现（agentos_plugin_sdk.tool_result_cache）是真依赖，共享单例在
用例间清空（global_cache.clear）保证互不串扰。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "tool_cache_writer"
_SHARED_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared"
for _d in (str(_PLUGIN_DIR), str(_SHARED_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 裸名 plugin 多插件共跑会被别的 plugin.py 抢注：按显式路径加载独享实例
# （tests/test_tool_cache_gaps.py 同款纪律）。
_w_spec = importlib.util.spec_from_file_location(
    "tool_cache_writer_under_test", str(_PLUGIN_DIR / "plugin.py")
)
assert _w_spec is not None
assert _w_spec.loader is not None
w_plugin = importlib.util.module_from_spec(_w_spec)
sys.modules["tool_cache_writer_under_test"] = w_plugin
_w_spec.loader.exec_module(w_plugin)

from agentos_plugin_sdk.tool_result_cache import global_cache  # noqa: E402
from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, _services={})


def _state(results: list[Any], calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        StateKeys.TOOL_RESULTS: results,
        "_full_tool_results": results,
        "_executed_tool_calls": calls if calls is not None else [
            {"id": f"c{i}", "name": "web_search", "args": {"q": f"q{i}"}}
            for i in range(len(results))
        ],
        "pipeline_id": "pipe-w",
    }


def _run(writer: Any, state: dict[str, Any]) -> Any:
    return asyncio.run(writer.execute(_ctx(state)))


@pytest.fixture(autouse=True)
def _clear_cache():
    global_cache().clear()
    yield
    global_cache().clear()


class TestToolCacheWriterContract:
    def test_name_and_default_priority(self) -> None:
        writer = w_plugin.ToolCacheWriter()
        assert writer.name == "tool_cache_writer"
        assert writer.priority == 25

    def test_disabled_writer_writes_nothing(self) -> None:
        writer = w_plugin.ToolCacheWriter(config={"enabled": False})
        result = _run(writer, _state([{"answer": 42}]))
        assert result.state_updates == {}
        assert not global_cache()

    def test_missing_sources_yields_no_output(self) -> None:
        writer = w_plugin.ToolCacheWriter()
        for state in (
            {StateKeys.TOOL_RESULTS: [], "_executed_tool_calls": []},
            {"_executed_tool_calls": [{"id": "c1", "name": "web_search", "args": {}}]},
            {StateKeys.TOOL_RESULTS: [{"answer": 1}]},
        ):
            assert _run(writer, state).state_updates == {}

    def test_successful_result_written_and_input_side_hits(self) -> None:
        """写端 put → input 端（tool_cache 插件同款 ToolResultCache）可命中。

        两端 namespace 同源（namespace_from_state 读同一 state 身份键），
        查询端用 make_cache_key + get 复算同一键。
        """
        from agentos_plugin_sdk.tool_result_cache import (
            ToolResultCache,
            make_cache_key,
            namespace_from_state,
        )

        writer = w_plugin.ToolCacheWriter()
        state = _state([{"stdout": "hello"}])
        result = _run(writer, state)
        assert result.state_updates == {}

        # input 端同源查询（SDK 单一缓存、同一 namespace 维度）
        cache = ToolResultCache({})
        call = {"id": "c0", "name": "web_search", "args": {"q": "q0"}}
        key = make_cache_key(call, namespace=namespace_from_state(state))
        hit, entry = cache.get(key)
        assert hit, "写端成功结果应可被 input 端命中"
        assert entry == {"stdout": "hello"}

    def test_failed_result_skipped(self) -> None:
        writer = w_plugin.ToolCacheWriter()
        _run(writer, _state([{"error": "boom"}]))
        assert not global_cache()

    def test_excluded_side_effect_tools_skipped(self) -> None:
        """bash/file_write 等有副作用工具不写缓存（构造期并集含 bash 缺省排除）。"""
        writer = w_plugin.ToolCacheWriter()
        calls = [
            {"id": "c0", "name": "bash", "args": {"command": "echo hi"}},
            {"id": "c1", "name": "file_write", "args": {"path": "x"}},
        ]
        results = [{"stdout": "hi"}, {"written": True}]
        _run(writer, _state(results, calls))
        assert not global_cache(), "有副作用工具的结果不可复用，绝不入缓存"

    def test_user_exclude_tools_config_union(self) -> None:
        """用户 exclude_tools 与缺省 bash 排除并集（用户扩展不被覆盖）。"""
        writer = w_plugin.ToolCacheWriter(config={"exclude_tools": ["web_search"]})
        calls = [
            {"id": "c0", "name": "web_search", "args": {"q": "x"}},
            {"id": "c1", "name": "file_read", "args": {"path": "x"}},
        ]
        results = [{"answer": 1}, {"content": "..."}]
        _run(writer, _state(results, calls))
        assert not global_cache(), "web_search 被用户排除；file_read 命中 SDK 缺省排除"
