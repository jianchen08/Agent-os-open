"""MiniMax 图像生成 Provider。

通过 MiniMax API (api.minimaxi.com) 实现图像生成，
支持 image-01 模型的文生图功能。

暴露接口：
- MiniMaxImageProvider：MiniMax 图像生成 Provider
"""

from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.minimaxi.com/v1/image_generation"
DEFAULT_MODEL = "image-01"


class MiniMaxImageProvider(MediaProvider):
    """MiniMax 图像生成 Provider。

    通过 MiniMax REST API 生成图像，支持：
    - 文生图（text-to-image）
    - 自定义宽高比
    - prompt 自动优化
    - 批量生成（1-9张）

    Attributes:
        _api_url: MiniMax API 地址
        _api_key: API 密钥
        _model: 模型名称
        _output_dir: 输出目录
    """

    def __init__(self, config: MediaProviderConfig) -> None:
        """初始化 MiniMax Provider。

        Args:
            config: Provider 配置，config 字段支持：
                - api_url: API 地址（默认 https://api.minimaxi.com/v1/image_generation）
                - api_key: API 密钥（必填）
                - model: 模型名称（默认 image-01）
                - output_dir: 输出目录（默认 ./output/images）
                - prompt_optimizer: 是否优化 prompt（默认 true）
        """
        super().__init__(
            provider_name="minimax",
            media_type=MediaType.IMAGE,
            config=config,
        )
        cfg = config.config
        self._api_url: str = cfg.get("api_url", DEFAULT_API_URL)
        self._api_key: str = cfg.get("api_key", "")
        self._model: str = cfg.get("model", DEFAULT_MODEL)
        self._output_dir: Path = Path(cfg.get("output_dir", "./output/images"))
        self._prompt_optimizer: bool = cfg.get("prompt_optimizer", True)

    async def is_available(self) -> bool:
        """检查 MiniMax API 是否可用（API Key 已配置）。"""
        return bool(self._api_key)

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成图像。

        Args:
            prompt: 图像生成提示词
            **kwargs: 可选参数：
                - width: 图像宽度
                - height: 图像高度
                - aspect_ratio: 宽高比
                - seed: 随机种子
                - n: 生成数量
                - style: 风格（附加到 prompt）

        Returns:
            MediaResult 包含生成的图像文件路径和元数据

        Raises:
            RuntimeError: API 调用失败
            ValueError: API Key 未配置
        """
        if not self._api_key:
            raise ValueError("MiniMax API Key 未配置")

        payload = self._build_payload(prompt, **kwargs)

        logger.info("[MiniMax] 提交图像生成: prompt=%s, model=%s", prompt[:50], self._model)
        response_data = await self._call_api(payload)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        file_path = await self._save_image(response_data)

        metadata: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "provider": "minimax",
        }
        if "aspect_ratio" in payload:
            metadata["aspect_ratio"] = payload["aspect_ratio"]
        if "width" in payload:
            metadata["width"] = payload["width"]
            metadata["height"] = payload["height"]

        logger.info("[MiniMax] 图像生成完成: %s", file_path)
        return MediaResult(
            file_path=file_path,
            media_type=MediaType.IMAGE,
            metadata=metadata,
            provider_name=self.provider_name,
        )

    def _build_payload(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """构建 API 请求参数。

        Args:
            prompt: 提示词
            **kwargs: 可选生成参数

        Returns:
            MiniMax API 请求体字典
        """
        style = kwargs.get("style")
        if style:
            prompt = f"{prompt}, {style} style"

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt[:1500],
            "response_format": "url",
            "prompt_optimizer": self._prompt_optimizer,
        }

        aspect_ratio = kwargs.get("aspect_ratio")
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        width = kwargs.get("width")
        height = kwargs.get("height")
        if width and height:
            w = max(512, min(2048, (int(width) // 8) * 8))
            h = max(512, min(2048, (int(height) // 8) * 8))
            payload["width"] = w
            payload["height"] = h

        seed = kwargs.get("seed")
        if seed is not None and int(seed) != -1:
            payload["seed"] = int(seed)

        n = kwargs.get("n", 1)
        if n and 1 <= int(n) <= 9:
            payload["n"] = int(n)

        return payload

    async def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调用 MiniMax API。

        Args:
            payload: 请求体字典

        Returns:
            API 响应 JSON 字典

        Raises:
            RuntimeError: HTTP 错误或业务错误
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                self._api_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp,
        ):
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"MiniMax API 调用失败 (status={resp.status}): {error_text}")
            result = await resp.json()

        base_resp = result.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax API 业务错误: {base_resp.get('status_msg', 'unknown')}")

        return result

    async def _save_image(self, response_data: dict[str, Any]) -> Path:
        """从响应中下载并保存图片。

        优先从 image_urls 下载，备选从 image_base64 解码。

        Args:
            response_data: MiniMax API 响应体

        Returns:
            保存后的本地文件路径

        Raises:
            RuntimeError: 响应中无图片数据或下载失败
        """
        image_urls = response_data.get("data", {}).get("image_urls", [])
        if image_urls:
            url = image_urls[0]
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp,
            ):
                if resp.status != 200:
                    raise RuntimeError(f"下载图片失败 (status={resp.status})")
                content = await resp.read()
        else:
            base64_list = response_data.get("data", {}).get("image_base64", [])
            if not base64_list:
                raise RuntimeError("MiniMax 响应中没有图片数据")
            content = base64.b64decode(base64_list[0])

        filename = f"minimax_{uuid.uuid4().hex[:8]}.png"
        output_path = self._output_dir / filename
        output_path.write_bytes(content)
        return output_path


__all__ = ["MiniMaxImageProvider"]
