"""多模态后处理 Output 插件。

处理 LLM 返回的多模态结果（如图片URL），将结果持久化存储。

State 命名空间：
    - multimodal_output_urls : 从LLM输出中提取的图片URL列表
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

# 图片URL正则：匹配 http(s)://...jpg/png/gif/webp/svg
_IMAGE_URL_PATTERN = re.compile(
    r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp|svg)(?:\?\S*)?)",
    re.IGNORECASE,
)


class MultimodalPostprocessor(IOutputPlugin):
    """多模态后处理 Output 插件。

    当管道状态标记包含多模态内容时，从 LLM 返回的原始结果中
    提取图片URL并写入管道状态，供后续持久化存储使用。

    优先级：40（后处理级）
    错误策略：SKIP（提取失败不影响管道执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化多模态后处理插件。

        Args:
            config: 插件配置字典，支持以下键：
                - priority: 插件优先级（默认 40）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "multimodal_postprocessor"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 40)

    @property
    def route_signals(self) -> list[str]:
        """本插件不产出路由信号。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行多模态后处理。

        当管道状态中存在多模态标记时，从 LLM 原始输出中
        提取图片URL列表并写入状态。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含多模态输出URL状态更新的输出结果
        """
        state = ctx.state

        if not state.get("has_multimodal"):
            return OutputResult()

        raw_result = state.get(StateKeys.RAW_RESULT, "")
        if not raw_result or not isinstance(raw_result, str):
            return OutputResult()

        multimodal_urls = self._extract_urls(raw_result)
        if not multimodal_urls:
            return OutputResult()

        return OutputResult(
            state_updates={
                "multimodal_output_urls": multimodal_urls,
            }
        )

    def _extract_urls(self, text: str) -> list[str]:
        """从文本中提取图片URL。

        使用正则匹配 http(s) 协议的图片URL，
        自动去重并保持原始顺序。

        Args:
            text: 待提取的文本

        Returns:
            去重后的图片URL列表
        """
        seen: set[str] = set()
        urls: list[str] = []
        for match in _IMAGE_URL_PATTERN.finditer(text):
            url = match.group(1)
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls
