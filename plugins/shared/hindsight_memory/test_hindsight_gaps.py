# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""hindsight_memory 缺口补测（官方车道 coverage.xml 2026-09-14 靶单）。

覆盖面（按行为分组）：
- ``HindsightBackend._map_hindsight_results``（438-441）：条目顶层无 list 型
  tags 时回退解析 ``metadata["tags"]`` JSON 串——合法 JSON 列表 → 采用；
  非法 JSON（``ValueError``）与不可解析类型（``TypeError``）→ 空列表
  （不抛、不把垃圾直透给 prompt_build 的 list 消费面）；
- ``knowledge_base`` 模块级共享根注入（58）：``_SHARED_ROOT`` 不在 sys.path
  时插入（真实 import 时序下执行，非 mock）；import 前先移除以保证真实执行；
- ``knowledge_base.upload_document``：decode 后纯空白（空白字符形态）→
  KBError(400, KNB_VAL_7004) 拒绝入库且 bank 不被触碰；
- ``routes_memory._document_to_memory``（98-99）：文档 tags 无 ``type:*``
  前缀时回落 ``document_metadata.memory_type``（取到值 / 缺失空串两态）。

不可达说明（逐条）：
- ``knowledge_base`` 58 行仅在「共享根不在 sys.path」时执行；同进程内其他测试
  可能已插入该路径 → 本文件在加载前先移除再加载，保证真实执行（非豁免）。
- 其余目标行均常态可达，无留白。

外部依赖：capability_caller（memory_backend）与 hindsight client（knowledge_base）
均为外部能力边界，用替身注入；文件系统用真实 tmp_path。禁止真实网络。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_ROOT = str(_PLUGIN_DIR.parents[0])


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# memory_backend._map_hindsight_results：metadata.tags JSON 串回退
# ═══════════════════════════════════════════════════════════


def _load_backend() -> Any:
    """动态加载 memory_backend.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_gaps_memory_backend_under_test"
    path = _PLUGIN_DIR / "memory_backend.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load memory_backend.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("raw_meta_tags", "expected"),
    [
        (json.dumps(["review_id:r1", "report"]), ["review_id:r1", "report"]),
        ("[]", []),
        ("not-json-at-all", []),          # ValueError 分支
        (None, []),                       # 非 str → parsed=None → []（无异常）
        ({"nested": "dict"}, []),         # 非 str 非 list → []
    ],
)
def test_map_results_parses_metadata_tags_json_fallback(
    raw_meta_tags: Any, expected: list[str]
) -> None:
    """顶层无 list 型 tags → 回退解析 metadata.tags JSON 串；失败一律空列表。

    契约（retain 侧把 list 序列化为 str 落库）：解析成功必须还原为 list，
    非法 JSON 与不可解析类型必须降级为空列表——调用方（prompt_build 压缩块
    过滤）按 list 消费，垃圾直透必被拒。
    """
    be = _load_backend()
    result = {
        "results": [
            {
                "id": "m1",
                "content": "c",
                "metadata": {"tags": raw_meta_tags, "memory_type": "semantic"},
            }
        ]
    }

    mapped = be.HindsightBackend._map_hindsight_results(result)

    assert mapped[0]["tags"] == expected
    assert isinstance(mapped[0]["tags"], list)  # 性质断言：恒为 list


def test_map_results_prefers_top_level_list_tags_over_metadata(
) -> None:
    """区分度输入（正例）：顶层 tags 为 list → 优先采用，不解析 metadata 串。"""
    be = _load_backend()
    result = [
        {
            "id": "m2",
            "content": "c",
            "tags": ["native-tag"],
            "metadata": {"tags": json.dumps(["stale-from-meta"])},
        }
    ]

    mapped = be.HindsightBackend._map_hindsight_results(result)

    assert mapped[0]["tags"] == ["native-tag"]


def test_map_results_filters_falsy_and_coerces_tags_to_str() -> None:
    """tags 元素过滤假值并强转 str（None/空串/0 不出现；数字转字符串）。"""
    be = _load_backend()
    result = {
        "results": [
            {"id": "m3", "content": "c", "tags": ["a", "", None, 0, 7]},
        ]
    }

    mapped = be.HindsightBackend._map_hindsight_results(result)

    assert mapped[0]["tags"] == ["a", "7"]


def test_map_results_non_dict_result_returns_empty_list() -> None:
    """信封既非 dict（含 results）也非 list → 空列表（降级不抛）。"""
    be = _load_backend()

    assert be.HindsightBackend._map_hindsight_results("raw-string") == []
    assert be.HindsightBackend._map_hindsight_results(None) == []


async def test_search_applies_knowledge_name_filter_and_json_tag_fallback() -> None:
    """端到端（真实 search 装配 + 真实 JSON 串）：knowledge_name 客户端过滤生效。

    经注入 capability caller 驱动真实 search 链路（唯一外部边界替身），
    验证 tags 回退解析结果参与下游消费。
    """
    be = _load_backend()
    caller = AsyncMock()
    caller.return_value = {
        "results": [
            {
                "id": "hit",
                "content": "match",
                "metadata": {
                    "tags": json.dumps(["knowledge_name:kb-1"]),
                    "knowledge_name": "kb-1",
                },
            },
            {
                "id": "miss",
                "content": "other",
                "metadata": {"tags": "broken-json", "knowledge_name": "kb-2"},
            },
        ]
    }
    backend = be.HindsightBackend(caller)

    mapped = await backend.search(query="q", user_id="u", knowledge_name="kb-1")

    assert [m["id"] for m in mapped] == ["hit"]
    assert mapped[0]["tags"] == ["knowledge_name:kb-1"]


# ═══════════════════════════════════════════════════════════
# knowledge_base：共享根注入 / 空白拒绝 / uploads 解析回退
# ═══════════════════════════════════════════════════════════


def _load_kb_fresh() -> Any:
    """加载 knowledge_base.py（唯一模块名；真实执行模块级共享根注入）。"""
    mod_name = "hindsight_gaps_knowledge_base_under_test"
    path = _PLUGIN_DIR / "knowledge_base.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load knowledge_base.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_module_level_shared_root_is_added_to_sys_path() -> None:
    """模块级共享根自举（58 行）：不在 sys.path 时导入即插入（真实副作用）。

    先移除该路径使自举行真实执行；导入后断言已存在（幂等契约）。
    """
    while _SHARED_ROOT in sys.path:
        sys.path.remove(_SHARED_ROOT)

    module = _load_kb_fresh()

    assert _SHARED_ROOT in sys.path
    assert module._SHARED_ROOT == _SHARED_ROOT


def test_whitespace_only_upload_is_rejected_before_bank_touch() -> None:
    """解码后纯空白文本 → KBError(400, KNB_VAL_7004)。

    客户端切块面已随原生摄取管线切换移除（单次 aretain，切块归上游）；空白
    拒绝由 upload_document 的解码后 strip 检查直接承载，bank 不被触碰。
    """
    module = _load_kb_fresh()
    client = MagicMock()
    client.aretain = AsyncMock(return_value=MagicMock(operation_id="c", accepted=True))
    client.acreate_bank = AsyncMock(return_value=None)
    module.set_client(client)

    with pytest.raises(module.KBError) as ei:
        _run(module.upload_document(
            "doc.md", "  \n  ".encode(), "text/markdown",
            headers={"x-agentos-tenant": "default", "x-agentos-user": "admin"},
        ))

    assert ei.value.status_code == 400
    assert ei.value.error_code == "KNB_VAL_7004"
    client.aretain.assert_not_called()


# ═══════════════════════════════════════════════════════════
# routes_memory._document_to_memory：memory_type 回落
# ═══════════════════════════════════════════════════════════


def _load_routes() -> Any:
    mod_name = "hindsight_gaps_routes_memory_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "routes_memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("doc", "expected_type"),
    [
        # 「type:*」标签缺失 → 回落 document_metadata.memory_type
        (
            {"id": "d1", "original_text": "t", "document_metadata": {"memory_type": "episode"}},
            "episode",
        ),
        # 「type:*」标签存在 → 优先取标签（不回落到 metadata）
        (
            {
                "id": "d2",
                "original_text": "t",
                "tags": ["type:procedural"],
                "document_metadata": {"memory_type": "episode"},
            },
            "procedural",
        ),
        # 两处都缺 → 空串（不伪造类型）
        ({"id": "d3", "original_text": "t"}, ""),
        # document_metadata 存在但 memory_type 为空串 → 空串（不伪造类型）
        ({"id": "d5", "original_text": "t", "document_metadata": {"memory_type": ""}}, ""),
    ],
)
def test_document_to_memory_memory_type_resolution(doc: dict[str, Any], expected_type: str) -> None:
    """memory_type 解析优先级：type:* 标签 > document_metadata.memory_type > 空串。

    契约（_document_to_memory docstring）：tags 剔除内部 ``type:*`` 前缀
    （实现细节不外泄），score 恒 0（列举无排序语义，不伪造评分）。
    """
    rm = _load_routes()

    out = rm._document_to_memory(doc)

    assert out["memory_type"] == expected_type
    assert out["score"] == 0.0
    assert all(not t.startswith("type:") for t in out["tags"])


def test_document_to_memory_content_and_created_at_mapping() -> None:
    """content ← original_text、created_at 透传（documents 面保有原文的契约）。"""
    rm = _load_routes()

    out = rm._document_to_memory(
        {
            "id": 42,
            "original_text": "原文内容",
            "created_at": "2026-09-14T00:00:00Z",
            "tags": ["type:semantic", "topic:x"],
        }
    )

    assert out["id"] == "42"  # 数字 id 强转 str
    assert out["content"] == "原文内容"
    assert out["created_at"] == "2026-09-14T00:00:00Z"
    assert out["tags"] == ["topic:x"]
