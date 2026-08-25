"""音乐生成工具（0.2 迁移版）。

经 tool-executor capability 调用后端媒体服务（MediaProviderClient，
服务契约 media.generate）——调用方未注入时返回**显式错误**
（error_code=PROVIDER_UNAVAILABLE），**不降级空转**。

暴露接口：
- get_tool_definition() -> Tool：工具定义
- MusicGenerateTool：音乐生成工具类
"""

from __future__ import annotations

import logging
from typing import Any

from _media_core import (
    MediaProviderClient,
    MediaType,
    ProviderUnavailable,
)

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class MusicGenerateTool(BuiltinTool):
    """音乐生成工具。

    经 tool-executor capability 调用后端媒体服务（F-MEDIA-2 主路径）。

    Args:
        capability_caller: 能力调用 async 函数（可选，F-MEDIA-2 主路径）。
    """

    def __init__(
        self,
        capability_caller: Any | None = None,
    ) -> None:
        """初始化音乐生成工具。

        Args:
            capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`
                （经 tool-executor.invoke 调 media.generate；生产环境由插件注入）
        """
        self._capability_caller = capability_caller

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="music_generate",
            description=(
                "音乐生成工具。根据文本描述生成音乐内容，当前为基线版本。"
            ),
            when_to_use=[
                "需要根据文本描述生成音乐片段",
                "需要为视频或演示生成背景音乐",
                "需要快速创作特定风格的音乐",
            ],
            when_not_to_use=[
                "需要编辑已有音频文件（使用音频编辑工具）",
                "需要播放或转码音频（使用对应工具）",
                "需要实时音频流处理",
            ],
            caveats=[
                "音乐生成通常为异步任务，执行时间可能较长",
                "生成质量取决于 Provider 和模型能力",
                "需要先配置音乐生成 Provider 后才能使用",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "音乐内容描述，用于指导音乐生成（必填）",
                    },
                    "genre": {
                        "type": "string",
                        "description": "音乐风格（如 'pop', 'jazz', 'classical', 'rock'），默认由 Provider 决定",
                    },
                    "mood": {
                        "type": "string",
                        "description": "情绪氛围（如 'happy', 'sad', 'energetic', 'calm'），默认由 Provider 决定",
                    },
                    "duration_seconds": {
                        "type": "number",
                        "description": "音乐时长（秒），默认由 Provider 决定",
                    },
                    "tempo": {
                        "type": "integer",
                        "description": "节奏速度（BPM），默认由 Provider 决定",
                    },
                    "provider": {
                        "type": "string",
                        "description": "指定使用的音乐生成 Provider（不填则自动选择）",
                    },
                },
                "required": ["prompt"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["music", "generate", "media", "creative", "audio"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行音乐生成。

        F-MEDIA-2：经 capability 调用后端服务（未注入调用方时显式
        PROVIDER_UNAVAILABLE）。0.1 的 not_configured 成功态已废除（不降级空转）。

        Args:
            inputs: 工具输入参数，包含 prompt（必填）
                和 genre、mood、duration_seconds、tempo（可选）。

        Returns:
            ToolExecutionResult: 生成成功时包含文件路径和元数据；
                服务不可用时包含显式错误（error_code=PROVIDER_UNAVAILABLE）。
        """
        prompt = inputs.get("prompt", "").strip()
        if not prompt:
            return create_failure_result(
                error="prompt 参数不能为空，请提供音乐内容描述",
                error_code="MISSING_PROMPT",
            )

        kwargs = self._build_kwargs(inputs)

        # F-MEDIA-2 主路径：经 capability 调用后端服务
        return await self._execute_via_capability(prompt, inputs, kwargs)

    async def _execute_via_capability(
        self,
        prompt: str,
        inputs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> ToolExecutionResult:
        """F-MEDIA-2 主路径：经 tool-executor capability 调用后端服务。

        调用方未注入（无 capability_caller）时返回**显式错误**
        PROVIDER_UNAVAILABLE——不静默空转（产品决定：迁移依赖而非降级）。
        """
        if self._capability_caller is None:
            return self._provider_unavailable_result()

        client = MediaProviderClient(self._capability_caller)
        try:
            result = await client.execute_generate(
                MediaType.MUSIC,
                prompt,
                provider=inputs.get("provider"),
                **kwargs,
            )
        except ProviderUnavailable as e:
            logger.warning("[MusicGenerate] 媒体生成服务不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
            )
        return create_success_result(
            data={
                "file_path": str(result.file_path),
                "media_type": result.media_type.value,
                "duration_seconds": result.duration_seconds,
                "provider_name": result.provider_name,
                "metadata": result.metadata,
            },
            metadata={"action": "music_generate", "provider": result.provider_name},
        )

    @staticmethod
    def _provider_unavailable_result() -> ToolExecutionResult:
        """媒体生成服务不可用的显式错误结果（不降级空转）。"""
        return create_failure_result(
            error="媒体生成服务不可用（media.generate）：未注入 capability_caller",
            error_code="PROVIDER_UNAVAILABLE",
        )

    def _build_kwargs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """构建传递给 Provider 的可选参数，过滤掉 None 值。

        Args:
            inputs: 原始输入参数

        Returns:
            非空的可选参数字典
        """
        optional_keys = ("genre", "mood", "duration_seconds", "tempo")
        kwargs: dict[str, Any] = {}
        for key in optional_keys:
            value = inputs.get(key)
            if value is not None:
                kwargs[key] = value
        return kwargs
