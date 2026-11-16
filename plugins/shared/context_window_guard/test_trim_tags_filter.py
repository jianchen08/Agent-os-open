# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""压缩块检索 tags 过滤测试（D1：裁剪放弃根因修复）。

_trim_covered_messages 与 clean_if_window_changed 的检索必须带
``tags=[f"pipeline:{pipeline_id}"], tags_match="any"`` 服务端过滤
（对齐 _estimate_assembled_tokens 正确口径）：缺 tags 时向量召回混入
同 thread 旧代 pipeline 压缩块，max_end 被历史大值抬高 → 误判当前消息
全部已压缩 → 触发 <10% 防护放弃裁剪。

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


class TestTrimSearchTagsFilter:
    """_trim_covered_messages 检索必须带 pipeline tags 服务端过滤。"""

    def test_search_passes_pipeline_tags_contract(self) -> None:
        """检索调用带 tags=["pipeline:<id>"], tags_match="any"（契约断言）。"""
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-2"])],
            stale_chunks=[],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        messages = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 5)
        ]
        _run(plugin._trim_covered_messages(_make_ctx(mod, "p"), messages))
        assert len(backend.search_calls) == 1
        call = backend.search_calls[0]
        assert call["tags"] == ["pipeline:p"]
        assert call["tags_match"] == "any"

    def test_stale_pipeline_chunks_excluded_from_max_end(self) -> None:
        """有区分度行为断言：tags 过滤生效 → 旧代大 seq 块不入 max_end。

        本管道块 seq:1-4 + 旧代块 seq:1-1000；消息 seq 1..8。带 tags 时
        服务端滤掉旧代块，max_end=4，保留 seq 5..8；若缺 tags（bug 形态）
        全量召回 max_end=1000 → 全裁触发防护放弃。两组输入几何不同：
        本组验证「部分裁剪生效」，反向组验证防护告警取证字段。
        """
        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-4"])],
            stale_chunks=[_l1_chunk(["pipeline:p-old-gen", "seq:1-1000"])],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        messages = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 9)
        ]
        result = _run(plugin._trim_covered_messages(_make_ctx(mod, "p"), messages))
        kept_seqs = [m["seq"] for m in result if m.get("seq") is not None]
        assert kept_seqs == [5, 6, 7, 8]

    def test_clean_if_window_changed_passes_pipeline_tags(self) -> None:
        """clean_if_window_changed 检索同样带 pipeline tags（第二处同病）。"""
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


class TestProtectionAlertCarriesRecallTags:
    """<10% 防护告警须附召回块的 pipeline tag（取证字段）。"""

    def test_protection_alert_includes_pipeline_tags(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """裁剪过激触发防护 → error 告警含召回块 pipeline tag。"""
        import logging

        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-10"])],
            stale_chunks=[],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = _make_ctx(mod, "p")
        messages = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 12)
        ]
        with caplog.at_level(logging.ERROR):
            result = _run(plugin._trim_covered_messages(ctx, messages))
        assert result is messages  # 防护生效，放弃裁剪
        alert = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert alert, "应有一条 <10% 防护 error 告警"
        assert any("pipeline:p" in r.getMessage() for r in alert)

    def test_protection_alert_lists_mixed_pipeline_tags(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """召回块携带多 pipeline 标签时，告警应全部列出（取证可辨）。

        旧代块以双标签形态命中服务端过滤（残留历史管道 tag + 本管道
        tag），seq:1-50 混入抬高 max_end=50 → 防护触发，告警须同时给出
        本管道与遗留管道两个 tag，取证时可直接定位错位来源。
        """
        import logging

        mod = _load_guard_module()
        backend = _TagsAwareBackend(
            own_chunks=[_l1_chunk(["pipeline:p", "seq:1-10"])],
            stale_chunks=[
                _l1_chunk(["pipeline:p", "pipeline:p-legacy", "seq:1-50"])
            ],
        )
        mod.set_memory_backend(backend)
        plugin = mod.ContextWindowGuardPlugin()
        ctx = _make_ctx(mod, "p")
        messages = [
            {"role": "user", "content": f"m{i}", "seq": i} for i in range(1, 12)
        ]
        with caplog.at_level(logging.ERROR):
            result = _run(plugin._trim_covered_messages(ctx, messages))
        assert result is messages
        alert_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "pipeline:p-legacy" in alert_text
