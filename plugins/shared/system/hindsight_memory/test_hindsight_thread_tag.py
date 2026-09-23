# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""A4 thread-tag 记忆召回 TDD（BUG-76 用户裁定：recall 不做会话隔离，
把「跨 bank 召回」转化为 tag 过滤）。

行为锚点：
- 写入侧：会话 bank（thread-*）retain 自动注入 thread tag（bank id 本身），
  每条记忆带会话标记；非 thread bank 不注入（现行为不变）。
- 召回侧：tags 含 thread tag（"thread-X" 原生 / "session:thread-X" memory
  工具注入形态）→ 跨 bank 扇出（自身 bank → default → 其余 thread-*，上限
  封顶），服务端 tag_groups 布尔过滤（thread tag 双形态 any_strict 组 AND
  其余过滤组），跨 bank 合并按 scores.final 降序截断 top_k。
- 不带 thread tag → 维持现行为（单 bank 单次 recall；含 pipeline:{id} 等
  非 thread 业务 tag 也不触发扇出）。

测试不依赖真实 hindsight 包——mock 模块级 _client（兄弟文件同款）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module() -> Any:
    """动态加载 server.py 模块（独立模块名，避免跨文件模块级状态污染）。"""
    mod_name = "hindsight_memory_server_thread_tag"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    """调用插件工具并 await 协程结果（新建事件循环，避免 pytest-asyncio 冲突）。"""
    td = module.plugin._tools[tool_name]
    result = td.handler(**kwargs)
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()
    return result


def _item(item_id: str, score: float) -> Any:
    """构造 RecallResult 条目替身（真实形态：text 主字段 + 嵌套 scores.final）。"""
    return MagicMock(
        model_dump=lambda: {
            "id": item_id,
            "text": f"内容 {item_id}",
            "scores": {"final": score},
        }
    )


def _set_banks(client: MagicMock, bank_ids: list[str]) -> None:
    """配置 banks.list_banks 替身（真实 API 条目形态 = {"bank_id": ...}，
    hindsight_list_banks 输出串列表——test_hindsight_server 同款契约）。"""
    client.banks.list_banks = AsyncMock(
        return_value={
            "banks": [{"bank_id": b} for b in bank_ids],
            "total": len(bank_ids),
        }
    )


def _recall_by_bank(
    responses: dict[str, list[Any]], strict: bool = True
) -> AsyncMock:
    """按 bank_id 分响应的 arecall 替身；strict 时未配置 bank 即失败（扇出越界暴露）。"""

    async def _recall(**kwargs: Any) -> Any:
        bank = str(kwargs.get("bank_id", ""))
        if bank not in responses:
            if strict:
                raise AssertionError(f"unexpected arecall bank: {bank}")
            return SimpleNamespace(results=[], chunks=[], source_facts=[])
        return SimpleNamespace(results=responses[bank], chunks=[], source_facts=[])

    return AsyncMock(side_effect=_recall)


@pytest.fixture
def mod() -> Any:
    module = _load_module()
    module._client = None
    return module


@pytest.fixture
def mock_client(mod: Any) -> MagicMock:
    client = MagicMock()
    client.aretain = AsyncMock(return_value=MagicMock(id="mem-1", stored=True))
    client.arecall = AsyncMock(
        return_value=SimpleNamespace(results=[], chunks=[], source_facts=[])
    )
    _set_banks(client, [])
    mod._client = client
    return client


# ═══════════════════════════════════════════════════════════
# 写入侧：会话 bank retain 自动注入 thread tag
# ═══════════════════════════════════════════════════════════


class TestRetainThreadTag:
    def test_thread_bank_injects_bank_id_tag(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """会话 bank（thread-*）写入自动带 bank id thread tag——每条记忆带会话
        标记，agent 按自身 thread tag 跨 bank 召回的写入侧依据。"""
        _call_tool(mod, "hindsight.retain", bank_id="thread-abc", content="hello")

        tags = mock_client.aretain.call_args.kwargs["tags"]
        assert "thread-abc" in tags
        assert "type:semantic" in tags

    def test_thread_tag_not_duplicated(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """调用方 tags 已含同值（metadata JSON 串提升）时不重复注入。"""
        _call_tool(
            mod,
            "hindsight.retain",
            bank_id="thread-abc",
            content="hello",
            metadata={"tags": json.dumps(["thread-abc", "topic-x"])},
        )

        tags = mock_client.aretain.call_args.kwargs["tags"]
        assert tags.count("thread-abc") == 1
        assert "topic-x" in tags

    def test_non_thread_bank_untagged(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """非会话 bank（default/review 等）不注入 thread tag（现行为不变）。"""
        for bank in ("default", "review"):
            _call_tool(mod, "hindsight.retain", bank_id=bank, content="hello")
            tags = mock_client.aretain.call_args.kwargs["tags"]
            assert bank not in tags


# ═══════════════════════════════════════════════════════════
# 召回侧：thread tag → 跨 bank tag 过滤
# ═══════════════════════════════════════════════════════════


class TestRecallThreadTagCrossBank:
    def test_thread_tag_recalls_across_banks(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """带 thread tag → 跨 bank 召回：X 会话记忆无论落在哪个 bank 都命中；
        review bank 不进扇出（thread-* + default 约定）。"""
        mock_client.arecall = _recall_by_bank(
            {
                "thread-abc": [_item("m1", 0.9)],
                "default": [_item("m2", 0.95)],
                "thread-other": [],
            }
        )
        _set_banks(mock_client, ["thread-other", "thread-abc", "review", "default"])

        result = _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc"],
        )

        called = [c.kwargs["bank_id"] for c in mock_client.arecall.await_args_list]
        assert called == ["thread-abc", "default", "thread-other"]
        ids = [r["id"] for r in result["results"]]
        assert ids == ["m2", "m1"]  # 跨 bank 合并按 scores.final 降序
        assert result["total"] == 2

    def test_thread_tag_request_uses_boolean_tag_groups(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """thread-tag 扇出的服务端过滤形状：tag_groups 布尔组（thread tag 双形态
        any_strict 组 AND 其余过滤组），不传平铺 tags。"""
        mock_client.arecall = _recall_by_bank({"thread-abc": [], "default": []}, strict=False)
        _set_banks(mock_client, ["thread-abc", "default"])

        _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc", "topic-x"], tags_match="any",
        )

        kwargs = mock_client.arecall.await_args_list[0].kwargs
        assert "tags" not in kwargs
        groups = kwargs["tag_groups"]
        assert groups[0] == {
            "tags": ["thread-abc", "session:thread-abc"],
            "match": "any_strict",
        }
        assert groups[1] == {"tags": ["topic-x"], "match": "any"}

    def test_session_prefixed_tag_triggers_cross_bank(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """session:thread-X 形态（memory 工具 filter.session_id 注入路径）同样
        触发跨 bank 召回并归一到双形态过滤。"""
        mock_client.arecall = _recall_by_bank({"thread-abc": [], "default": []}, strict=False)
        _set_banks(mock_client, ["thread-abc", "default"])

        _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["session:thread-abc"],
        )

        kwargs = mock_client.arecall.await_args_list[0].kwargs
        assert kwargs["tag_groups"][0]["tags"] == ["thread-abc", "session:thread-abc"]

    def test_merged_results_truncate_to_top_k(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """跨 bank 合并后统一按 scores.final 降序截断 top_k（检索数量契约）。"""
        mock_client.arecall = _recall_by_bank(
            {
                "thread-abc": [_item("m1", 0.9)],
                "default": [_item("m2", 0.95), _item("m3", 0.5)],
            }
        )
        _set_banks(mock_client, ["thread-abc", "default"])

        result = _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc"], top_k=2,
        )

        assert [r["id"] for r in result["results"]] == ["m2", "m1"]
        assert result["total"] == 2

    def test_fan_out_bounded_by_cap(self, mod: Any, mock_client: MagicMock) -> None:
        """扇出 bank 数封顶（自身 bank 必在扇出集且优先）——无上限会随会话 bank
        积累击穿工具面客户端死线。"""
        mock_client.arecall = _recall_by_bank({}, strict=False)
        _set_banks(
            mock_client, [f"thread-{i:03d}" for i in range(30)] + ["default"]
        )

        _call_tool(
            mod, "hindsight.recall", bank_id="thread-007", query="q",
            tags=["thread-007"],
        )

        called = [c.kwargs["bank_id"] for c in mock_client.arecall.await_args_list]
        assert len(called) == mod._RECALL_BANK_FANOUT_CAP
        assert called[0] == "thread-007"
        assert len(set(called)) == len(called)  # 扇出集无重复 bank

    def test_bank_discovery_failure_degrades_to_own_bank(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """bank 发现失败（list_banks 报错）→ 降级自身 bank 召回，结果照常返回。"""
        mock_client.arecall = _recall_by_bank({"thread-abc": [_item("m1", 0.9)]})
        mock_client.banks.list_banks = AsyncMock(
            return_value={"banks": [], "error": "discovery boom"}
        )

        result = _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc"],
        )

        assert mock_client.arecall.await_count == 1
        assert [r["id"] for r in result["results"]] == ["m1"]

    def test_client_without_banks_api_degrades_to_own_bank(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """client 无 banks API（旧形态）→ 降级自身 bank 召回。"""
        mock_client.arecall = _recall_by_bank({"thread-abc": [_item("m1", 0.9)]})
        mock_client.banks = None

        result = _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc"],
        )

        assert mock_client.arecall.await_count == 1
        assert [r["id"] for r in result["results"]] == ["m1"]

    def test_per_bank_failure_propagates_as_error(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """扇出中单 bank arecall 失败 → 诚实报 error（不吞成假成功）。"""

        async def _fail(**kwargs: Any) -> Any:
            if kwargs.get("bank_id") == "thread-bad":
                raise RuntimeError("bank boom")
            return SimpleNamespace(results=[], chunks=[], source_facts=[])

        mock_client.arecall = AsyncMock(side_effect=_fail)
        _set_banks(mock_client, ["thread-abc", "thread-bad"])

        result = _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            tags=["thread-abc"],
        )

        assert result["results"] == []
        assert "bank boom" in result["error"]


class TestRecallWithoutThreadTag:
    """不带 thread tag → 维持现行为（单 bank）。"""

    def test_no_tags_single_bank_unchanged(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        mock_client.arecall = _recall_by_bank(
            {"thread-abc": [_item("m1", 0.9)]}, strict=False
        )
        _set_banks(mock_client, ["thread-abc", "default", "thread-other"])

        result = _call_tool(mod, "hindsight.recall", bank_id="thread-abc", query="q")

        assert mock_client.arecall.await_count == 1
        kwargs = mock_client.arecall.await_args_list[0].kwargs
        assert kwargs["bank_id"] == "thread-abc"
        assert "tags" not in kwargs
        assert "tag_groups" not in kwargs
        assert [r["id"] for r in result["results"]] == ["m1"]

    def test_non_thread_tags_keep_single_bank(
        self, mod: Any, mock_client: MagicMock
    ) -> None:
        """非 thread 业务 tag（如 pipeline:{id} 压缩块过滤）不触发扇出，
        平铺 tags 过滤面保持原语义。"""
        mock_client.arecall = _recall_by_bank({"thread-abc": []}, strict=False)
        _set_banks(mock_client, ["thread-abc", "default"])

        _call_tool(
            mod, "hindsight.recall", bank_id="thread-abc", query="q",
            memory_type="chunk", tags=["pipeline:p1"],
        )

        assert mock_client.arecall.await_count == 1
        kwargs = mock_client.arecall.await_args_list[0].kwargs
        assert kwargs["bank_id"] == "thread-abc"
        assert kwargs["tags"] == ["type:chunk", "pipeline:p1"]
        assert kwargs["tags_match"] == "any"
        assert "tag_groups" not in kwargs
