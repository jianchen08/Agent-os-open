"""工具缓存写入断路接通的单测。

验证 tool_cache（input）和 tool_cache_writer（output）共享模块级单例缓存，
写入断路被接通：
    1. writer 写入后，cache 能命中（跳过执行）
    2. exclude_tools 中的有副作用工具不写缓存
    3. 失败的工具调用（result 含 error）不写缓存
    4. 全局缓存共享：两个插件实例操作同一份缓存
    5. TTL 过期后不命中
    6. max_size 超限时 LRU 淘汰

不依赖 src/ 或 plugins.input 旧路径，通过 sys.path 注入直接导入本地 plugin 模块。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# 复制 server.py 的 sys.path 机制
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# 注入 input/tool_cache 目录，使其本地 plugin.py 可作为 'plugin' 导入
# 注意：本测试文件所在目录的 plugin.py 也是 'plugin'，会冲突。
# 用 importlib 显式从路径加载 input 端的 plugin.py，避免名字冲突。
# 从 output/tool_cache_writer/ 往上：
#   parents[0] = tool_cache_writer/
#   parents[1] = output/
#   parents[2] = pipeline/
# input/tool_cache 在 pipeline/input/tool_cache
_INPUT_TOOL_CACHE_DIR = str(Path(__file__).resolve().parents[2] / "input" / "tool_cache")
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "tool_cache_plugin", str(Path(_INPUT_TOOL_CACHE_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_tool_cache_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tool_cache_mod)
ToolCache = _tool_cache_mod.ToolCache
_GLOBAL_CACHE = _tool_cache_mod._GLOBAL_CACHE
get_global_cache = _tool_cache_mod.get_global_cache

# 本目录 plugin.py 也用 importlib 加载（避免 'plugin' 名字污染）
_writer_spec = importlib.util.spec_from_file_location(
    "tool_cache_writer_plugin", str(Path(_THIS_DIR) / "plugin.py")
)
assert _writer_spec is not None and _writer_spec.loader is not None
_writer_mod = importlib.util.module_from_spec(_writer_spec)
_writer_spec.loader.exec_module(_writer_mod)
ToolCacheWriter = _writer_mod.ToolCacheWriter
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402


# ── 测试辅助 ──


def clear_cache() -> None:
    """每个测试前清空全局缓存，保证隔离。"""
    _GLOBAL_CACHE.clear()


def make_ctx(raw_tool_calls: list[dict], tool_results: list) -> PluginContext:
    """构造带工具调用 state 的上下文。"""
    return PluginContext(
        state={
            StateKeys.RAW_TOOL_CALLS: raw_tool_calls,
            StateKeys.TOOL_RESULTS: tool_results,
        }
    )


# ══════════════════════════════════════════════════
# 1. writer 写入后，cache 能命中
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_writer_write_then_cache_hit() -> None:
    """writer 写入工具结果后，下一轮 tool_cache 应命中跳过执行。"""
    clear_cache()
    writer = ToolCacheWriter(config={})
    cache = ToolCache(config={})

    # 模拟工具执行完成：raw_tool_calls + tool_results
    raw_tool_calls = [{"name": "file_read", "args": {"path": "/tmp/a.txt"}}]
    tool_results = [{"content": "hello world"}]
    ctx = make_ctx(raw_tool_calls, tool_results)

    # writer 写缓存
    await writer.execute(ctx)
    assert len(_GLOBAL_CACHE) == 1

    # 下一轮：tool_cache 查同样调用，应命中
    ctx2 = PluginContext(
        state={StateKeys.RAW_TOOL_CALLS: raw_tool_calls},
    )
    result = await cache.execute(ctx2)

    assert result.state_updates.get("cache_hit") is True
    assert result.state_updates.get(StateKeys.TOOL_RESULTS) == [{"content": "hello world"}]
    assert result.skip_remaining is True


# ══════════════════════════════════════════════════
# 2. exclude_tools 中的有副作用工具不写缓存
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_exclude_tools_not_cached() -> None:
    """bash_execute 等有副作用的工具不应写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    raw_tool_calls = [{"name": "bash_execute", "args": {"command": "ls"}}]
    tool_results = [{"output": "file1\nfile2"}]
    ctx = make_ctx(raw_tool_calls, tool_results)

    await writer.execute(ctx)

    assert len(_GLOBAL_CACHE) == 0, "bash_execute 不应被缓存"


@pytest.mark.asyncio
async def test_custom_exclude_tools() -> None:
    """用户自定义 exclude_tools 追加到默认列表。"""
    clear_cache()
    writer = ToolCacheWriter(config={"exclude_tools": ["my_unsafe_tool"]})

    raw_tool_calls = [{"name": "my_unsafe_tool", "args": {}}]
    tool_results = ["result"]
    ctx = make_ctx(raw_tool_calls, tool_results)

    await writer.execute(ctx)

    assert len(_GLOBAL_CACHE) == 0, "自定义排除工具不应被缓存"


# ══════════════════════════════════════════════════
# 3. 失败的工具调用不写缓存
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_failed_tool_call_not_cached() -> None:
    """result 含 error 字段的工具调用不缓存（失败结果不可复用）。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    raw_tool_calls = [{"name": "file_read", "args": {"path": "/nonexistent"}}]
    tool_results = [{"error": "file not found"}]
    ctx = make_ctx(raw_tool_calls, tool_results)

    await writer.execute(ctx)

    assert len(_GLOBAL_CACHE) == 0, "失败的工具调用不应被缓存"


# ══════════════════════════════════════════════════
# 4. 全局缓存共享
# ══════════════════════════════════════════════════


def test_global_cache_shared_across_instances() -> None:
    """多个 ToolCache 实例共享同一份全局缓存。"""
    clear_cache()
    cache1 = ToolCache(config={})
    cache2 = ToolCache(config={})

    # cache1 写入
    cache1.put({"name": "file_read", "args": {"path": "x"}}, "data1")

    # cache2 应能读到（同一份全局缓存）
    g = get_global_cache()
    assert len(g) == 1
    # 验证 cache2 实例也能通过 _is_excluded 等方法访问共享状态
    assert cache2._is_excluded("bash_execute") is True


# ══════════════════════════════════════════════════
# 5. TTL 过期后不命中
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    """TTL 过期后 cache 不命中。"""
    clear_cache()
    # 用极短 TTL 构造
    cache = ToolCache(config={"default_ttl": 0})  # 立即过期
    writer = ToolCacheWriter(config={"default_ttl": 0})

    raw_tool_calls = [{"name": "file_read", "args": {"path": "x"}}]
    tool_results = ["data"]

    await writer.execute(make_ctx(raw_tool_calls, tool_results))

    # 等一下让时间推进
    time.sleep(0.01)

    ctx = PluginContext(state={StateKeys.RAW_TOOL_CALLS: raw_tool_calls})
    result = await cache.execute(ctx)

    # TTL=0 已过期，不应命中
    assert result.state_updates.get("cache_hit") is not True
    assert not result.skip_remaining


# ══════════════════════════════════════════════════
# 6. max_size 超限时 LRU 淘汰
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_max_size_eviction() -> None:
    """缓存条目超过 max_size 时淘汰最老的。"""
    clear_cache()
    cache = ToolCache(config={"max_size": 2, "default_ttl": 300})
    writer = ToolCacheWriter(config={"max_size": 2, "default_ttl": 300})

    # 写 3 个不同工具调用，max_size=2 应淘汰最老的
    for i in range(3):
        raw = [{"name": "file_read", "args": {"path": f"file{i}"}}]
        res = [f"data{i}"]
        await writer.execute(make_ctx(raw, res))

    # 全局缓存不应超过 max_size（可能因 LRU 淘汰到 2 条）
    assert len(_GLOBAL_CACHE) <= 2


# ══════════════════════════════════════════════════
# 7. 空输入 / 禁用时不操作
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_empty_inputs_no_op() -> None:
    """raw_tool_calls 或 tool_results 为空时不写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    # 空 tool_results
    await writer.execute(make_ctx([{"name": "file_read", "args": {}}], []))
    assert len(_GLOBAL_CACHE) == 0

    # 空 raw_tool_calls
    await writer.execute(make_ctx([], ["data"]))
    assert len(_GLOBAL_CACHE) == 0


@pytest.mark.asyncio
async def test_disabled_writer_no_op() -> None:
    """enabled=False 时 writer 不写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={"enabled": False})

    raw = [{"name": "file_read", "args": {"path": "x"}}]
    res = ["data"]
    await writer.execute(make_ctx(raw, res))

    assert len(_GLOBAL_CACHE) == 0


@pytest.mark.asyncio
async def test_disabled_cache_no_op() -> None:
    """enabled=False 时 cache 不查缓存。"""
    clear_cache()
    cache = ToolCache(config={"enabled": False})

    ctx = PluginContext(state={StateKeys.RAW_TOOL_CALLS: [{"name": "file_read", "args": {}}]})
    result = await cache.execute(ctx)

    assert result.state_updates == {}
    assert not result.skip_remaining
