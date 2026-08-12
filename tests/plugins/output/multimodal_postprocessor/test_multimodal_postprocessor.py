"""MultimodalPostprocessor 单元测试——图片 URL 提取与状态写入。

覆盖：_extract_urls 的正则匹配（多扩展名/带查询串/去重/保序）、
execute 的 has_multimodal 门控、非字符串 raw_result 跳过、无 URL 空结果。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


# ============================================================
# 配置与基本属性
# ============================================================


class TestConfig:
    def test_属性(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        assert p.name == "multimodal_postprocessor"
        assert p.priority == 40
        assert p.route_signals == []

    def test_自定义优先级(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor(config={"priority": 7})
        assert p.priority == 7

    def test_error_policy为SKIP(self) -> None:
        from plugin import MultimodalPostprocessor
        from pipeline.types import ErrorPolicy

        assert MultimodalPostprocessor.error_policy == ErrorPolicy.SKIP


# ============================================================
# _extract_urls
# ============================================================


class TestExtractUrls:
    def test_提取单个png_url(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        urls = p._extract_urls("see https://example.com/a.png for detail")
        assert urls == ["https://example.com/a.png"]

    def test_支持多种扩展名(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        text = " ".join(
            [
                "http://x/1.jpg",
                "https://y/2.jpeg",
                "https://z/3.gif",
                "https://w/4.webp",
                "https://v/5.svg",
                "https://u/6.PNG",  # 大写
            ]
        )
        urls = p._extract_urls(text)
        assert len(urls) == 6

    def test_带查询串的url完整保留(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        urls = p._extract_urls("img https://cdn.x.com/p.png?w=100&h=200 end")
        assert urls == ["https://cdn.x.com/p.png?w=100&h=200"]

    def test_重复url去重(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        text = "https://x/a.png and https://x/a.png again"
        urls = p._extract_urls(text)
        assert urls == ["https://x/a.png"]

    def test_保持首次出现顺序(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        text = "https://b/2.png https://a/1.jpg https://c/3.gif"
        urls = p._extract_urls(text)
        assert urls == [
            "https://b/2.png",
            "https://a/1.jpg",
            "https://c/3.gif",
        ]

    def test_无图片url返回空列表(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        assert p._extract_urls("just text http://x.com/page.html no images") == []

    def test_空字符串返回空列表(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        assert p._extract_urls("") == []

    def test_非图片扩展名不命中(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        assert p._extract_urls("https://x.com/doc.pdf") == []
        assert p._extract_urls("https://x.com/page") == []


# ============================================================
# execute 端到端
# ============================================================


class TestExecute:
    @pytest.mark.asyncio
    async def test_无has_multimodal标记返回空(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(
            _ctx({StateKeys.RAW_RESULT: "https://x/a.png"})
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_has_multimodal但raw_result为空返回空(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(_ctx({"has_multimodal": True, StateKeys.RAW_RESULT: ""}))
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_raw_result非字符串返回空(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(
            _ctx({"has_multimodal": True, StateKeys.RAW_RESULT: {"url": "x"}})
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_has_multimodal但无图片url返回空(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(
            _ctx({"has_multimodal": True, StateKeys.RAW_RESULT: "no images here"})
        )
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_正常提取写入multimodal_output_urls(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(
            _ctx(
                {
                    "has_multimodal": True,
                    StateKeys.RAW_RESULT: "see https://a/x.png and https://b/y.jpg",
                }
            )
        )
        urls = result.state_updates["multimodal_output_urls"]
        assert urls == ["https://a/x.png", "https://b/y.jpg"]

    @pytest.mark.asyncio
    async def test_raw_result为None返回空(self) -> None:
        from plugin import MultimodalPostprocessor

        p = MultimodalPostprocessor()
        result = await p.execute(
            _ctx({"has_multimodal": True, StateKeys.RAW_RESULT: None})
        )
        assert result.state_updates == {}
