# @feature: FP-0.2.〇 管道引擎 | @ci: pytest-plugins
"""压缩块检索 tags 过滤测试（D1 演进：裁剪已与记忆库解耦后的两个存量消费点）。

2026-11-16 修复后 _trim_covered_messages 不再检索记忆库（证据源 =
历史已应用压缩块 metadata.compression_ref.seq_range，孤儿 chunk 不驱动
删除）。仍检索后端的消费点剩两处——clean_if_window_changed（window 变更
检测）与 _estimate_assembled_tokens（压缩块拼接估算），它们的检索必须带
``tags=[f"pipeline:{pipeline_id}"], tags_match="any"`` 服务端过滤（D1 既有
保护意图）：缺 tags 时向量召回混入同 thread 旧代 pipeline 块，l1 估算
失真 / window 判定错块。

另含本事故回归：孤儿 chunk（落库后回写前被打断的覆盖声明）不影响裁剪
决策——trim 零检索、零删除。

memory backend 属外部依赖，用 stub 替身（按 hindsight 服务端 tags
过滤契约模拟：带 tags 只回本管道块，不带 tags 回全量）；断行为不断实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/（_libs 依赖）
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_SDK_SRC = str(_PLUGIN_DIR.parents[1] / "sdk" / "src")
if _SDK_SRC not in sys.path:
    sys.path.insert(0, _SDK_SRC)


def _load_guard_module() -> Any:
    """动态加载 guard plugin.py（唯一模块名，避免裸名串扰）。"""
    mod_name = "cwg_trim_tags_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, str(_PLUGIN_DIR / "plugin.py"))
    assert spec is not None and spec.loader is not None
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


def _l1_chunk(tags: list[str], content: str = "L" * 40) -> dict[str, Any]:
    """构造 backend 检索结果里的 L1 块条目（sequence 信息在 metadata.tags）。"""
    return {
        "id": "c-l1",
        "content": content,
        "memory_type": "chunk",
        "metadata": {"tags": ["L1", *tags]},
    }


class _TagsAwareBackend:
    """按 hindsight 服务端 tags 过滤契约模拟的伪记忆后端。

    search 全量入参记录到 search_calls；带 tags 参数时按服务端过滤语义
    只返回 tags 命中本管道的块（模拟旧代块被服务端滤掉），不带 tags 时
    返回全量（含旧代 pipeline 块——即 bug 场景下向量召回的真实返回面）。
    """

    def __init__(
        self,
        own_chunks: list[dict[str, Any]],
        stale_chunks: list[dict[str, Any]],
    ) -> None:
        self.own_chunks = own_chunks
        self.stale_chunks = stale_chunks
        self.search_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        tags = kwargs.get("tags")
        if tags:
            wanted = set(tags)
            matched = [
                c
                for c in [*self.own_chunks, *self.stale_chunks]
                if wanted & set(c["metadata"]["tags"])
            ]
            return matched
        return [*self.own_chunks, *self.stale_chunks]


def _make_ctx(mod: Any, pipeline_id: str) -> Any:
    return mod._make_minimal_ctx(pipeline_id=pipeline_id)


class TestCleanSearchTagsFilter:
    """clean_if_window_changed 检索必须带 pipeline tags 服务端过滤。"""

    def test_clean_if_window_changed_passes_pipeline_tags(self) -> None:
        """检索调用带 tags=["pipeline:<id>"], tags_match="any"（契约断言）。"""
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-2", "ctx:64000"])],
            stale_chunks=[],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = _make_ctx(mod, "p")
        _run(
            plugin.clean_if_window_changed(
                [{"role": "user", "content": "x"}], 128_000, ctx
            )
        )
        assert len(backend.search_calls) == 1
        call = backend.search_calls[0]
        assert call["tags"] == ["pipeline:p"]
        assert call["tags_match"] == "any"


class TestEstimateSearchTagsFilter:
    """_estimate_assembled_tokens 检索必须带 pipeline tags 服务端过滤。"""

    def test_estimate_passes_pipeline_tags_contract(self) -> None:
        """检索调用带 tags=["pipeline:<id>"], tags_match="any"（契约断言）。"""
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-2"])],
            stale_chunks=[],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = _make_ctx(mod, "p")
        _run(plugin._estimate_assembled_tokens(ctx, []))
        assert len(backend.search_calls) == 1
        call = backend.search_calls[0]
        assert call["tags"] == ["pipeline:p"]
        assert call["tags_match"] == "any"

    def test_stale_pipeline_chunks_excluded_from_estimate(self) -> None:
        """有区分度行为断言：tags 过滤生效 → 旧代大块不入估算。

        本管道块 seq:1-2（40 字符 → l1 估算 20）+ 旧代块 seq:1-1000；
        消息仅 seq 5 一条（"hello" → recent 估算 2）。带 tags 时服务端滤掉
        旧代块：total = 20 + 2 = 22；若缺 tags（bug 形态）全量召回两块：
        l1 40 + max_end=1000 → recent 0 → total 40。两组输入几何可辨。
        """
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-2"])],
            stale_chunks=[_l1_chunk(["pipeline:p-old-gen", "seq:1-1000"])],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = _make_ctx(mod, "p")
        messages = [{"role": "user", "content": "hello", "seq": 5}]
        estimated = _run(plugin._estimate_assembled_tokens(ctx, messages))
        assert estimated == 22, (
            f"估算必须只计本管道块（l1=20 + recent=2），实际 {estimated}"
        )


class TestOrphanChunksDoNotAffectTrim:
    """本事故回归（2026-11-16）：孤儿 chunk 不影响裁剪决策。"""

    def test_orphan_chunks_do_not_drive_trim(self) -> None:
        """仅记忆库裸 chunk、历史无已应用块 → trim 零检索、零删除。"""
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-50"])],
            stale_chunks=[],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        messages = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 61)
        ]
        result = _run(plugin._trim_covered_messages(messages))
        assert result is messages, "孤儿 chunk 不得驱动删除"
        assert backend.search_calls == [], "trim 已与记忆库解耦（零检索）"
