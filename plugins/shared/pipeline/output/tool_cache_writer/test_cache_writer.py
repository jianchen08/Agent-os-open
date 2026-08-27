# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
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

# input 端 plugin.py 由 _fresh_tool_cache 在测试执行时点 fresh 加载
# （见下方测试辅助段注释——收集期绑定会随 pipeline 包实例更替失效）。
_INPUT_TOOL_CACHE_PLUGIN = str(Path(_INPUT_TOOL_CACHE_DIR) / "plugin.py")

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
# 车道中央裸名逐出机制（tests/plugins conftest）会在会话中重建 `pipeline`
# 包实例，而 tool_cache 的全局缓存挂在 pipeline 包属性上、writer.execute
# 每次执行都 fresh 加载 input 端并绑定【当时】的实例。因此涉及缓存的
# 断言/清空一律按运行期当前实例解析（收集期模块级绑定会指向被换掉的
# 旧实例，造成 len==0 假失败或"不缓存"断言恒过的假绿）。


def _pkg_now():
    """运行期当前的 pipeline 包实例。"""
    import pipeline

    if not hasattr(pipeline, "_tool_result_cache"):
        pipeline._tool_result_cache = {}
    return pipeline


def _cache_now() -> dict:
    return _pkg_now()._tool_result_cache


def _fresh_tool_cache(config: dict):
    """fresh 加载 input 端 ToolCache（绑定运行期当前 pipeline 实例）。"""
    spec = importlib.util.spec_from_file_location(
        "tool_cache_fresh_test", _INPUT_TOOL_CACHE_PLUGIN
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tool_cache_fresh_test"] = mod
    spec.loader.exec_module(mod)
    return mod.ToolCache(config=config)


def clear_cache() -> None:
    """每个测试前清空全局缓存，保证隔离。"""
    _cache_now().clear()


def make_ctx(executed_calls: list[dict], tool_results: list) -> PluginContext:
    """构造带工具调用快照 state 的上下文。

    writer 读 ``_executed_tool_calls``（tool_core 执行后 raw_tool_calls
    被清空，执行前的调用列表快照在该键），tool_results 按下标配对。
    """
    return PluginContext(
        state={
            "_executed_tool_calls": executed_calls,
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
    cache = _fresh_tool_cache(config={})

    # 模拟工具执行完成：_executed_tool_calls + tool_results（2026-08-22 起
    # file_read 按路径读类型排除出缓存，示例改用纯查询工具 web_search；
    # 调用形状贴近生产：llm_core 产出 OpenAI 风格 {name, arguments}）
    executed_calls = [{"name": "web_search", "arguments": '{"query": "agentos"}'}]
    tool_results = [{"content": "hello world"}]
    ctx = make_ctx(executed_calls, tool_results)

    # writer 写缓存
    await writer.execute(ctx)
    assert len(_cache_now()) == 1

    # 下一轮：tool_cache 查同样调用，应命中
    ctx2 = PluginContext(
        state={StateKeys.RAW_TOOL_CALLS: [{"name": "web_search", "arguments": '{"query": "agentos"}'}]},
    )
    result = await cache.execute(ctx2)

    assert result.state_updates.get("cache_hit") is True
    assert result.state_updates.get(StateKeys.TOOL_RESULTS) == [{"content": "hello world"}]
    assert result.skip_remaining is True


@pytest.mark.asyncio
async def test_writer_noop_when_raw_tool_calls_cleared() -> None:
    """tool_core 执行后 raw_tool_calls 被清空：无 _executed_tool_calls 时 writer 空转。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    ctx = PluginContext(
        state={
            # 生产 post 链形状：raw_tool_calls 已被 tool_core 清空为 []
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.TOOL_RESULTS: [{"content": "hello world"}],
        }
    )
    await writer.execute(ctx)
    assert len(_cache_now()) == 0


# ══════════════════════════════════════════════════
# 2. exclude_tools 中的有副作用工具不写缓存
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_exclude_tools_not_cached() -> None:
    """bash_execute 等有副作用的工具不应写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    executed_calls = [{"name": "bash_execute", "args": {"command": "ls"}}]
    tool_results = [{"output": "file1\nfile2"}]
    ctx = make_ctx(executed_calls, tool_results)

    await writer.execute(ctx)

    assert len(_cache_now()) == 0, "bash_execute 不应被缓存"


@pytest.mark.asyncio
async def test_custom_exclude_tools() -> None:
    """用户自定义 exclude_tools 追加到默认列表。"""
    clear_cache()
    writer = ToolCacheWriter(config={"exclude_tools": ["my_unsafe_tool"]})

    executed_calls = [{"name": "my_unsafe_tool", "args": {}}]
    tool_results = ["result"]
    ctx = make_ctx(executed_calls, tool_results)

    await writer.execute(ctx)

    assert len(_cache_now()) == 0, "自定义排除工具不应被缓存"


# ══════════════════════════════════════════════════
# 3. 失败的工具调用不写缓存
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_failed_tool_call_not_cached() -> None:
    """result 含 error 字段的工具调用不缓存（失败结果不可复用）。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    executed_calls = [{"name": "file_read", "args": {"path": "/nonexistent"}}]
    tool_results = [{"error": "file not found"}]
    ctx = make_ctx(executed_calls, tool_results)

    await writer.execute(ctx)

    assert len(_cache_now()) == 0, "失败的工具调用不应被缓存"


# ══════════════════════════════════════════════════
# 4. 全局缓存共享
# ══════════════════════════════════════════════════


def test_global_cache_shared_across_instances() -> None:
    """多个 ToolCache 实例共享同一份全局缓存。"""
    clear_cache()
    cache1 = _fresh_tool_cache(config={})
    cache2 = _fresh_tool_cache(config={})

    # cache1 写入（file_read 已排除，用纯查询工具验证共享性）
    cache1.put({"name": "web_search", "args": {"query": "agentos"}}, "data1")

    # cache2 应能读到（同一份全局缓存）
    g = _cache_now()
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
    cache = _fresh_tool_cache(config={"default_ttl": 0})  # 立即过期
    writer = ToolCacheWriter(config={"default_ttl": 0})

    executed_calls = [{"name": "file_read", "args": {"path": "x"}}]
    tool_results = ["data"]

    await writer.execute(make_ctx(executed_calls, tool_results))

    # 等一下让时间推进
    time.sleep(0.01)

    ctx = PluginContext(state={StateKeys.RAW_TOOL_CALLS: executed_calls})
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
    writer = ToolCacheWriter(config={"max_size": 2, "default_ttl": 300})

    # 写 3 个不同工具调用，max_size=2 应淘汰最老的
    for i in range(3):
        executed = [{"name": "file_read", "args": {"path": f"file{i}"}}]
        res = [f"data{i}"]
        await writer.execute(make_ctx(executed, res))

    # 全局缓存不应超过 max_size（可能因 LRU 淘汰到 2 条）
    assert len(_cache_now()) <= 2


# ══════════════════════════════════════════════════
# 7. 空输入 / 禁用时不操作
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_empty_inputs_no_op() -> None:
    """_executed_tool_calls 或 tool_results 为空时不写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={})

    # 空 tool_results
    await writer.execute(make_ctx([{"name": "file_read", "args": {}}], []))
    assert len(_cache_now()) == 0

    # 空 _executed_tool_calls
    await writer.execute(make_ctx([], ["data"]))
    assert len(_cache_now()) == 0


@pytest.mark.asyncio
async def test_disabled_writer_no_op() -> None:
    """enabled=False 时 writer 不写缓存。"""
    clear_cache()
    writer = ToolCacheWriter(config={"enabled": False})

    raw = [{"name": "file_read", "args": {"path": "x"}}]
    res = ["data"]
    await writer.execute(make_ctx(raw, res))

    assert len(_cache_now()) == 0


@pytest.mark.asyncio
async def test_disabled_cache_no_op() -> None:
    """enabled=False 时 cache 不查缓存。"""
    clear_cache()
    cache = _fresh_tool_cache(config={"enabled": False})

    ctx = PluginContext(state={StateKeys.RAW_TOOL_CALLS: [{"name": "file_read", "args": {}}]})
    result = await cache.execute(ctx)

    assert result.state_updates == {}
    assert not result.skip_remaining


@pytest.mark.asyncio
async def test_file_read_not_cached() -> None:
    """file_read 不写缓存（2026-08-22 裁决）。

    路径型读工具排除：同内容合法两次独立读（读→改→再读）在同一 pipeline 的
    TTL 窗口内会被内容键误合并，第二次读返回改前内容——文件内容变了缓存不失效。
    """
    clear_cache()
    writer = ToolCacheWriter(config={})

    executed_calls = [{"name": "file_read", "args": {"path": "src/a.py"}}]
    tool_results = [{"output": "# 第一版内容"}]
    ctx = make_ctx(executed_calls, tool_results)

    await writer.execute(ctx)

    assert len(_cache_now()) == 0, "file_read 不应被缓存（读→写→再读会命中陈旧内容）"


@pytest.mark.asyncio
async def test_cache_hit_appends_tool_result_messages() -> None:
    """命中缓存时补 messages 配对（role=tool），避免 LLM 重发调用死循环。"""
    clear_cache()
    cache = _fresh_tool_cache(config={})

    # 预置缓存：web_search(query=agentos) → "cached answer"
    cache.put(
        {"name": "web_search", "args": {"query": "agentos"}},
        {"content": "cached answer"},
    )

    # 模拟 llm_core 已 append assistant(tool_calls) 后进入 core step：
    # raw_tool_calls 含 id（OpenAI 风格）
    ctx = PluginContext(
        state={
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "call_abc", "name": "web_search", "arguments": '{"query": "agentos"}'}
            ],
        }
    )
    result = await cache.execute(ctx)

    assert result.state_updates.get("cache_hit") is True
    assert result.state_updates.get(StateKeys.TOOL_RESULTS) == [{"content": "cached answer"}]
    # raw_tool_calls 清空（工具已消费，防 post 路由再派 tool_execute 死循环）
    assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS) == []
    # messages 配对：role=tool + tool_call_id 与调用 id 一致 + tool_result envelope
    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1
    msg = ops[0]["msg"]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_abc"
    assert msg["tool_result"]["tool_name"] == "web_search"
    assert msg["tool_result"]["success"] is True
    assert result.skip_remaining is True
