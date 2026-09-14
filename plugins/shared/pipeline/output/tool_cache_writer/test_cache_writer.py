# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""工具缓存写入断路接通的单测。

验证 tool_cache（input）和 tool_cache_writer（output）共享 SDK
tool_result_cache 单例缓存，写入断路被接通：
    1. writer 写入后，cache 能命中（跳过执行）
    2. exclude_tools 中的有副作用工具不写缓存
    3. 失败的工具调用（result 含 error）不写缓存
    4. 全局缓存共享：两个插件实例操作同一份缓存
    5. TTL 过期后不命中
    6. max_size 超限时 LRU 淘汰

通过 sys.path 注入直接导入本地 plugin 模块；共享缓存直接从
agentos_plugin_sdk.tool_result_cache 访问。
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

import pytest

from agentos_plugin_sdk.tool_result_cache import ToolResultCache, make_cache_key

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
# （收集期绑定会随 pipeline 包实例更替失效）。
_INPUT_TOOL_CACHE_PLUGIN = str(Path(_INPUT_TOOL_CACHE_DIR) / "plugin.py")

# 本目录 plugin.py 也用 importlib 加载（避免 'plugin' 名字污染）
_writer_spec = importlib.util.spec_from_file_location(
    "tool_cache_writer_plugin", str(Path(_THIS_DIR) / "plugin.py")
)
assert _writer_spec is not None, "writer plugin.py spec 可解析"
assert _writer_spec.loader is not None, "writer spec 含 loader"
_writer_mod = importlib.util.module_from_spec(_writer_spec)
_writer_spec.loader.exec_module(_writer_mod)
ToolCacheWriter = _writer_mod.ToolCacheWriter
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402

# ── 测试辅助 ──
# 共享单例缓存在 SDK 模块上（sys.modules 去重保证同一字典），测试隔离
# 直接清空该字典。


def _cache_now() -> dict:
    from agentos_plugin_sdk import tool_result_cache as _trc

    return _trc.global_cache()


def _fresh_tool_cache(config: dict):
    """fresh 加载 input 端 ToolCache（绑定运行期当前 pipeline 实例）。"""
    spec = importlib.util.spec_from_file_location(
        "tool_cache_fresh_test", _INPUT_TOOL_CACHE_PLUGIN
    )
    assert spec is not None, "input tool_cache plugin.py spec 可解析"
    assert spec.loader is not None, "input spec 含 loader"
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


@pytest.mark.asyncio
async def test_global_cache_shared_across_instances() -> None:
    """多个缓存核实例共享同一份全局缓存。"""
    clear_cache()
    core1 = ToolResultCache(config={})
    core2 = ToolResultCache(config={})
    cache = _fresh_tool_cache(config={})

    # core1 写入（file_read 已排除，用纯查询工具验证共享性）
    core1.put({"name": "web_search", "args": {"query": "agentos"}}, "data1")

    # core2 与 input 端插件应看到同一份全局缓存
    assert len(_cache_now()) == 1
    assert core2.is_excluded("bash_execute") is True
    hit, result = core2.get(make_cache_key({"name": "web_search", "args": {"query": "agentos"}}))
    assert hit is True
    assert result == "data1"
    # input 端插件实例经同一缓存命中
    ctx = PluginContext(
        state={StateKeys.RAW_TOOL_CALLS: [{"name": "web_search", "arguments": '{"query": "agentos"}'}]},
    )
    res = await cache.execute(ctx)
    assert res.state_updates.get("cache_hit") is True


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


def test_per_pipeline_max_evicts_oldest_full_result():
    """per_pipeline_max=1：同管道第二条全文入缓存时逐出第一条（用户裁定：一管道一条）。"""
    from agentos_plugin_sdk.tool_result_cache import ToolResultCache, make_cache_key

    cache = ToolResultCache({
        "enabled": True,
        "default_ttl": 300,
        "max_size": 100,
        "per_pipeline_max": 1,
    })
    tc1 = {"name": "doc_search", "arguments": "{\"q\": \"a\"}"}
    tc2 = {"name": "doc_search", "arguments": "{\"q\": \"b\"}"}

    cache.put(tc1, {"content": "全文一"}, pipeline_id="pipe_1")
    assert cache.get(make_cache_key(tc1))[0] is True, "第一条在（未超限）"

    cache.put(tc2, {"content": "全文二"}, pipeline_id="pipe_1")
    assert cache.get(make_cache_key(tc1))[0] is False, "同管道超限逐出最旧"
    hit, result = cache.get(make_cache_key(tc2))
    assert hit, "最新一条存活"
    assert result == {"content": "全文二"}, "最新一条内容正确"

    # 不同管道互不挤占
    tc3 = {"name": "doc_search", "arguments": "{\"q\": \"c\"}"}
    cache.put(tc3, {"content": "全文三"}, pipeline_id="pipe_2")
    assert cache.get(make_cache_key(tc2))[0] is True, "pipe_2 入场不影响 pipe_1 现有条目"


def test_per_pipeline_max_two_keeps_two_newest_fifo():
    """per_pipeline_max=2：四连写滚动逐出写入序最旧、任意时刻存活恰为最新 2 条。

    max=1 下任何逐出策略结果相同；max=2 多条存活才能区分"按写入序逐最旧"
    （FIFO 契约）与 LRU/逐最新等策略。
    """
    from agentos_plugin_sdk.tool_result_cache import ToolResultCache, make_cache_key

    cache = ToolResultCache({
        "enabled": True,
        "default_ttl": 300,
        "max_size": 100,
        "per_pipeline_max": 2,
    })
    tcs = [{"name": "doc_search", "arguments": f'{{"q": "{i}"}}'} for i in range(4)]

    for i in (0, 1):
        cache.put(tcs[i], {"n": i}, pipeline_id="pipe_x")
    assert all(cache.get(make_cache_key(tcs[i]))[0] for i in (0, 1)), "未超限全在"

    cache.put(tcs[2], {"n": 2}, pipeline_id="pipe_x")
    assert cache.get(make_cache_key(tcs[0]))[0] is False, "超限逐出写入序最旧(#0)"
    assert cache.get(make_cache_key(tcs[1]))[0] is True, "#1 仍存活"
    assert cache.get(make_cache_key(tcs[2]))[0] is True, "#2 存活"

    # 中途触碰过 #1（上面 get 更新访问序）后再写入 #3：仍逐 #1——
    # 逐出按写入序而非访问序（与 get 的 LRU 触碰解耦）。
    cache.put(tcs[3], {"n": 3}, pipeline_id="pipe_x")
    assert cache.get(make_cache_key(tcs[1]))[0] is False, "被触碰过的 #1 仍按写入序逐出"
    alive = [i for i in range(4) if cache.get(make_cache_key(tcs[i]))[0]]
    assert alive == [2, 3], "任意时刻存活恰为最新 per_pipeline_max 条（封顶性质）"


def test_per_pipeline_eviction_spares_key_held_by_other_pipeline():
    """同一 tool_call 被两条管道先后写入：一条管道超限逐出该键时，
    另一管道仍持有 → 全局条目必须存活（共享键防御分支）。"""
    from agentos_plugin_sdk.tool_result_cache import ToolResultCache, make_cache_key

    cache = ToolResultCache({
        "enabled": True,
        "default_ttl": 300,
        "max_size": 100,
        "per_pipeline_max": 1,
    })
    shared = {"name": "doc_search", "arguments": "{\"q\": \"shared\"}"}
    cache.put(shared, {"v": 1}, pipeline_id="p1")
    cache.put(shared, {"v": 2}, pipeline_id="p2")  # 同 cache_key，p1/p2 各持一条目序
    hit, result = cache.get(make_cache_key(shared))
    assert hit, "同键后写覆盖，两管道均持有该键"
    assert result == {"v": 2}, "同键后写覆盖：读到最新值"

    other = {"name": "doc_search", "arguments": "{\"q\": \"other\"}"}
    cache.put(other, {"v": 3}, pipeline_id="p1")  # p1 超限，逐出的恰是共享键
    hit, result = cache.get(make_cache_key(shared))
    assert hit, "p1 逐出共享键但 p2 仍持有 → 条目不得删除"
    assert result == {"v": 2}, "共享键内容不被误删后重写"
    hit, result = cache.get(make_cache_key(other))
    assert hit, "p1 最新条存活"
    assert result == {"v": 3}, "p1 最新条内容正确"


# ══════════════════════════════════════════════════
# 8. namespace 身份隔离：跨会话同参调用不互命中
# ══════════════════════════════════════════════════


_IDENTITY_A = {"pipeline_id": "pipe_aaa", "user_id": "user_a", "session_id": "sess_a"}
_IDENTITY_B = {"pipeline_id": "pipe_bbb", "user_id": "user_b", "session_id": "sess_b"}


def _identity_ctx(identity: dict, executed_calls: list[dict], tool_results: list) -> PluginContext:
    """带身份键的 writer 上下文（写端 namespace 与 state 身份键同源）。"""
    return PluginContext(
        state={
            **identity,
            "_executed_tool_calls": executed_calls,
            StateKeys.TOOL_RESULTS: tool_results,
        }
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "result_payload"),
    [
        ("memory_retrieve", '{"query": "登录密码规则"}', {"content": "用户A的私有记忆"}),
        ("memory_list", '{}', {"items": ["只有A能看的列表"]}),
    ],
)
@pytest.mark.asyncio
async def test_cross_identity_same_args_never_hit(tool_name: str, arguments: str, result_payload: dict) -> None:
    """用户 B 同参调用不得命中用户 A 写入的缓存条目（真实执行不跳过）。"""
    clear_cache()
    writer = ToolCacheWriter(config={})
    cache = _fresh_tool_cache(config={})

    call = [{"name": tool_name, "arguments": arguments}]
    await writer.execute(_identity_ctx(_IDENTITY_A, call, [result_payload]))
    assert len(_cache_now()) == 1, "写入端正常落一条（带身份维度）"

    ctx_b = PluginContext(
        state={**_IDENTITY_B, StateKeys.RAW_TOOL_CALLS: [{"name": tool_name, "arguments": arguments}]},
    )
    result = await cache.execute(ctx_b)
    assert result.state_updates.get("cache_hit") is not True, "跨身份不得命中"
    assert not result.skip_remaining, "跨身份不得跳过真实执行"


@pytest.mark.asyncio
async def test_same_identity_hits() -> None:
    """同身份同参调用正常命中（隔离不误伤本会话复用）。"""
    clear_cache()
    writer = ToolCacheWriter(config={})
    cache = _fresh_tool_cache(config={})

    call = [{"name": "memory_retrieve", "arguments": '{"query": "偏好"}'}]
    await writer.execute(_identity_ctx(_IDENTITY_A, call, [{"content": "A的记忆"}]))

    ctx_a2 = PluginContext(
        state={**_IDENTITY_A, StateKeys.RAW_TOOL_CALLS: [{"name": "memory_retrieve", "arguments": '{"query": "偏好"}'}]},
    )
    result = await cache.execute(ctx_a2)
    assert result.state_updates.get("cache_hit") is True
    assert result.state_updates.get(StateKeys.TOOL_RESULTS) == [{"content": "A的记忆"}]
    assert result.skip_remaining is True


@pytest.mark.asyncio
async def test_no_identity_keys_legacy_hit() -> None:
    """state 无任何身份键时退化为旧键语义（None namespace）读写互通。"""
    clear_cache()
    writer = ToolCacheWriter(config={})
    cache = _fresh_tool_cache(config={})

    call = [{"name": "web_search", "arguments": '{"query": "legacy"}'}]
    await writer.execute(_identity_ctx({}, call, [{"content": "legacy data"}]))

    ctx = PluginContext(state={StateKeys.RAW_TOOL_CALLS: call})
    result = await cache.execute(ctx)
    assert result.state_updates.get("cache_hit") is True, "无身份键保持旧命中行为"
    assert result.skip_remaining is True


# ══════════════════════════════════════════════════
# 7. spill 定位符回读（2026-09-14 _full_tool_results 定位符化配套）
# ══════════════════════════════════════════════════


def _write_spill(tmp_path, pipeline_id: str, key: str, text: str, compress: bool) -> Path:
    """按 spill_store 布局（{base}/{pipeline_id}/{key}）写一个存档文件。"""
    base = tmp_path / "spill"
    target = base / pipeline_id / key
    target.parent.mkdir(parents=True)
    raw = text.encode("utf-8")
    if compress:
        target.write_bytes(gzip.compress(raw))
    else:
        target.write_bytes(raw)
    return base


@pytest.mark.asyncio
async def test_writer_resolves_spilled_locator_to_full_result(tmp_path, monkeypatch) -> None:
    """__spilled__ 定位符条目：回读 spill 存档（gzip），缓存收到完整 ToolResult。"""
    clear_cache()
    full = {
        "tool_name": "web_search",
        "success": True,
        "error": None,
        "data": {"output": "line 000000: 全文正文\nline 000001: 尾部"},
        "metadata": None,
    }
    base = _write_spill(
        tmp_path, "pipe-1", "call_x_full", json.dumps(full, ensure_ascii=False), compress=True
    )
    monkeypatch.setenv("AGENTOS_SPILL_BASE", str(base))
    writer = ToolCacheWriter(config={})
    executed = [{"name": "web_search", "arguments": '{"query": "agentos"}'}]
    tool_results = [
        {
            "__spilled__": True,
            "tool_call_id": "call_x",
            "locator": "pipe-1/call_x_full",
            "original_bytes": 123,
            "compressed": True,
        }
    ]
    await writer.execute(make_ctx(executed, tool_results))

    entries = list(_cache_now().values())
    assert len(entries) == 1, "定位符回读成功 → 正常入缓存"
    assert entries[0][0] == full, "缓存收到的是存档里的完整 ToolResult"


@pytest.mark.asyncio
async def test_writer_skips_when_spill_file_missing(tmp_path, monkeypatch) -> None:
    """存档缺失：跳过该条且不崩（缓存宁可缺不可存截断版）。"""
    clear_cache()
    monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path / "empty"))
    writer = ToolCacheWriter(config={})
    executed = [{"name": "web_search", "arguments": "{}"}]
    tool_results = [{"__spilled__": True, "locator": "pipe-1/gone_full"}]
    await writer.execute(make_ctx(executed, tool_results))
    assert len(_cache_now()) == 0, "回读失败 → 不写缓存"


@pytest.mark.asyncio
async def test_writer_skips_oversize_resolved_result(tmp_path, monkeypatch) -> None:
    """回读后超单条缓存上限（1MB）：不入缓存，防常驻内存被大结果占据。"""
    clear_cache()
    big = {
        "tool_name": "web_search",
        "success": True,
        "error": None,
        "data": {"output": "x" * 1_100_000},
        "metadata": None,
    }
    base = _write_spill(tmp_path, "pipe-1", "call_big_full", json.dumps(big), compress=False)
    monkeypatch.setenv("AGENTOS_SPILL_BASE", str(base))
    writer = ToolCacheWriter(config={})
    executed = [{"name": "web_search", "arguments": "{}"}]
    tool_results = [{"__spilled__": True, "locator": "pipe-1/call_big_full"}]
    await writer.execute(make_ctx(executed, tool_results))
    assert len(_cache_now()) == 0, "超大结果不入常驻缓存"
