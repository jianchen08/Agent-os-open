# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""tool_result_cache 单测——工具结果缓存核（input 查 / output 写单一真值源）。

覆盖：
- make_cache_key：OpenAI 形状（arguments JSON 串）与 dict 形状同参同键；
  异参异键；非法 arguments JSON 串参与键不抛错；
- put/is_excluded：默认排除集、自定义排除追加、enabled=False 不写；
- get：命中触碰 LRU、未命中 (False, None)、过期删除；
- evict_expired：TTL 清理 + LRU 超限淘汰；
- global_cache：模块内单例（多次调用同一字典）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agentos_plugin_sdk.tool_result_cache import (
    DEFAULT_EXCLUDE_TOOLS,
    ToolResultCache,
    evict_expired,
    global_cache,
    make_cache_key,
    namespace_from_state,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    global_cache().clear()
    yield
    global_cache().clear()


def test_make_cache_key_openai_and_dict_shapes_agree() -> None:
    """arguments JSON 串与 dict 两种生产形状，同参同键（键不分叉）。"""
    k1 = make_cache_key({"name": "web_search", "arguments": '{"query": "agentos"}'})
    k2 = make_cache_key({"name": "web_search", "args": {"query": "agentos"}})
    assert k1 == k2
    assert k1 != make_cache_key({"name": "web_search", "args": {"query": "other"}})
    assert k1 != make_cache_key({"name": "other_tool", "args": {"query": "agentos"}})


def test_make_cache_key_invalid_arguments_json_stable() -> None:
    """arguments 非法 JSON 串回退 _raw 包裹，不抛错且同串同键。"""
    k1 = make_cache_key({"name": "t", "arguments": "not-json{"})
    k2 = make_cache_key({"name": "t", "arguments": "not-json{"})
    assert k1 == k2


def test_put_excludes_default_and_custom() -> None:
    """默认排除集不写缓存；自定义 exclude_tools 追加不覆盖。"""
    core = ToolResultCache(config={"exclude_tools": ["my_tool"]})
    core.put({"name": "bash_execute", "args": {}}, "x")
    core.put({"name": "my_tool", "args": {}}, "x")
    assert len(global_cache()) == 0
    assert core.is_excluded("bash_execute")
    assert core.is_excluded("my_tool")
    assert "my_tool" not in DEFAULT_EXCLUDE_TOOLS

    core.put({"name": "web_search", "args": {"q": 1}}, "data")
    assert len(global_cache()) == 1


def test_put_disabled_no_op() -> None:
    """enabled=False 不写缓存。"""
    core = ToolResultCache(config={"enabled": False})
    core.put({"name": "web_search", "args": {}}, "x")
    assert global_cache() == {}
    assert core.enabled is False


def test_get_hit_touches_lru_and_miss_expires() -> None:
    """命中返回结果并刷新访问时间；过期删除并返回未命中。"""
    core = ToolResultCache(config={"default_ttl": 100})
    key = make_cache_key({"name": "t", "args": {"a": 1}})
    core.put({"name": "t", "args": {"a": 1}}, "v1", now=900.0)

    hit, result = core.get(key, now=999.9)
    assert hit is True
    assert result == "v1"
    # LRU 触碰：访问时间被刷新为 now
    assert global_cache()[key][2] == 999.9

    # 过期：TTL=100，写入于 900 → 过期时刻 1_000，now >= 过期时刻即淘汰
    hit, result = core.get(key, now=1_000.0)
    assert hit is False
    assert result is None
    assert key not in global_cache()


def test_get_unknown_key_miss() -> None:
    core = ToolResultCache()
    hit, result = core.get("missing")
    assert hit is False
    assert result is None


def test_evict_expired_and_lru_cap() -> None:
    """先清过期；仍超限按 LRU（最近访问时间）淘汰。"""
    now = time.time()
    cache = global_cache()
    cache["expired"] = ("v", now - 1.0, now - 1.0)  # 已过期
    cache["old"] = ("v", now + 9_999.0, now + 1.0)
    cache["new"] = ("v", now + 9_999.0, now + 2.0)
    evict_expired(cache, max_size=2)
    # expired 清掉后剩 2 条未超限
    assert set(cache) == {"old", "new"}

    cache["newest"] = ("v", now + 9_999.0, now + 3.0)
    evict_expired(cache, max_size=2)
    # 超限：访问时间最老的 old 被淘汰
    assert set(cache) == {"new", "newest"}


def test_global_cache_is_module_singleton() -> None:
    """global_cache() 多次调用返回同一字典对象。"""
    assert global_cache() is global_cache()


# ── namespace 身份隔离维度 ────────────────────────────────


@pytest.mark.parametrize("args", [{"query": "agentos"}, {"path": "ctx.md"}])
def test_make_cache_key_namespace_separates_identity(args: dict) -> None:
    """同参不同 namespace → 键不同；同 namespace → 键相同；None 与无参旧键一致。"""
    tc = {"name": "memory_retrieve", "args": args}
    key_none = make_cache_key(tc)
    key_user_a = make_cache_key(tc, namespace="pipe1\x1fuser_a")
    key_user_b = make_cache_key(tc, namespace="pipe2\x1fuser_b")
    assert key_user_a != key_user_b, "跨身份同参调用不得共享缓存键"
    assert make_cache_key(tc, namespace="pipe1\x1fuser_a") == key_user_a, "同身份键稳定"
    assert make_cache_key(tc, namespace=None) == key_none, "namespace=None 与旧键完全一致"


def test_put_get_namespace_isolation() -> None:
    """写入带 namespace 后：同 namespace 命中、跨 namespace 不命中、None 查不到。"""
    core = ToolResultCache()
    core.put({"name": "memory_list", "args": {}}, "user_a_data", namespace="pipe1\x1fuser_a")

    hit, _ = core.get(make_cache_key({"name": "memory_list", "args": {}}, namespace="pipe2\x1fuser_b"))
    assert hit is False, "用户 B 不得命中用户 A 写入的条目"

    hit, result = core.get(make_cache_key({"name": "memory_list", "args": {}}, namespace="pipe1\x1fuser_a"))
    assert hit is True and result == "user_a_data", "同身份正常命中"

    hit, _ = core.get(make_cache_key({"name": "memory_list", "args": {}}))
    assert hit is False, "无 namespace 的旧键查不到带 namespace 的条目"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"pipeline_id": "p1"}, "p1"),
        ({"pipeline_id": "p1", "user_id": "u1", "session_id": "s1"}, "p1\x1fu1\x1fs1"),
        ({"pipeline_id": "p1", "user_id": "", "session_id": "s1"}, "p1\x1fs1"),
        ({"user_id": "u1"}, "u1"),
        ({}, None),
        ({"pipeline_id": "", "user_id": None}, None),
    ],
)
def test_namespace_from_state(state: dict, expected: str | None) -> None:
    """state 身份键按 pipeline_id/user_id/session_id 序拼接；全缺失返回 None。"""
    assert namespace_from_state(state) == expected
