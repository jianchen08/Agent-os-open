"""MiniMax 音乐生成 Provider。

通过 MiniMax API (api.minimaxi.com) 实现音乐生成，
支持 music-2.6 模型的文生音乐功能，采用异步任务轮询模式。

暴露接口：
- MiniMaxMusicProvider：MiniMax 音乐生成 Provider
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "music-2.6"
DEFAULT_POLL_INTERVAL = 3
DEFAULT_TIMEOUT = 300


class MiniMaxMusicProvider(MediaProvider):
    """MiniMax 音乐生成 Provider。

    通过 MiniMax REST API 生成音乐，支持：
    - 文生音乐（text-to-music）
    - 可选歌词输入
    - 自定义时长
    - 异步任务轮询等待完成

    Attributes:
        _api_base: MiniMax API 基础地址
        _api_key: API 密钥
        _model: 模型名称
        _output_dir: 输出目录
        _poll_interval: 轮询间隔（秒）
        _timeout: 超时时间（秒）
    """

    def __init__(self, config: MediaProviderConfig) -> None:
        """初始化 MiniMaxMusicProvider。

        Args:
            config: Provider 配置，config 字段支持：
                - api_key: API 密钥（必填）
                - model: 模型名称（默认 music-2.6）
                - output_dir: 输出目录（默认 ./output/music）
                - poll_interval: 轮询间隔秒数（默认 3）
                - timeout: 超时秒数（默认 300）
        """
        super().__init__(
            provider_name="minimax_music",
            media_type=MediaType.MUSIC,
            config=config,
        )
        cfg = config.config
        self._api_base: str = cfg.get("api_base", DEFAULT_API_BASE)
        self._api_key: str = cfg.get("api_key", "")
        self._model: str = cfg.get("model", DEFAULT_MODEL)
        self._output_dir: Path = Path(cfg.get("output_dir", "./output/music"))
        self._poll_interval: int = cfg.get("poll_interval", DEFAULT_POLL_INTERVAL)
        self._timeout: int = cfg.get("timeout", DEFAULT_TIMEOUT)

    async def is_available(self) -> bool:
        """检查 MiniMax API 是否可用（API Key 已配置）。"""
        return bool(self._api_key)

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成音乐。

        提交异步任务后轮询等待完成，下载音频文件保存到本地。

        Args:
            prompt: 音乐描述提示词
            **kwargs: 可选参数：
                - lyrics: 歌词文本（可选）
                - duration: 音乐时长（秒，默认 30）

        Returns:
            MediaResult 包含生成的音频文件路径和元数据

        Raises:
            RuntimeError: API 调用失败或任务超时
            ValueError: API Key 未配置
        """
        if not self._api_key:
            raise ValueError("MiniMax API Key 未配置")

        payload = self._build_payload(prompt, **kwargs)

        logger.info("[MiniMax Music] 提交音乐生成: prompt=%s, model=%s", prompt[:50], self._model)
        task_id = await self._submit_task(payload)
        logger.info("[MiniMax Music] 任务已提交: task_id=%s", task_id)

        audio_url = await self._poll_task(task_id)
        logger.info("[MiniMax Music] 音乐生成完成，开始下载")

        self._output_dir.mkdir(parents=True, exist_ok=True)
        file_path = await self._download_audio(audio_url)

        metadata: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "provider": "minimax_music",
            "task_id": task_id,
        }
        if kwargs.get("lyrics"):
            metadata["lyrics"] = kwargs["lyrics"]
        if kwargs.get("duration"):
            metadata["duration"] = kwargs["duration"]

        logger.info("[MiniMax Music] 文件已保存: %s", file_path)
        return MediaResult(
            file_path=file_path,
            media_type=MediaType.MUSIC,
            metadata=metadata,
            provider_name=self.provider_name,
        )

    def _build_payload(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """构建音乐生成 API 请求参数。

        Args:
            prompt: 音乐描述提示词
            **kwargs: 可选生成参数

        Returns:
            MiniMax API 请求体字典
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "lyrics": kwargs.get("lyrics", ""),
            "response_format": "url",
        }

        duration = kwargs.get("duration")
        if duration is not None:
            payload["duration"] = int(duration)

        return payload

    async def _submit_task(self, payload: dict[str, Any]) -> str:
        """提交音乐生成异步任务。

        Args:
            payload: 请求体字典

        Returns:
            异步任务 ID

        Raises:
            RuntimeError: HTTP 错误或业务错误
        """
        url = f"{self._api_base}/music_generation"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp,
        ):
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"MiniMax 音乐生成任务提交失败 (status={resp.status}): {error_text}")
            result = await resp.json()

        base_resp = result.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax 音乐生成业务错误: {base_resp.get('status_msg', 'unknown')}")

        task_id = result.get("task_id")
        if not task_id:
            raise RuntimeError(f"MiniMax 音乐生成响应中缺少 task_id: {result}")

        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询异步任务状态直到完成。

        Args:
            task_id: 异步任务 ID

        Returns:
            生成的音频文件 URL

        Raises:
            RuntimeError: 任务失败或超时
        """
        url = f"{self._api_base}/query/music_generation"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        elapsed = 0

        async with aiohttp.ClientSession() as session:
            while elapsed < self._timeout:
                async with session.get(
                    url,
                    params={"task_id": task_id},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"查询音乐生成任务失败 (status={resp.status}): {error_text}")
                    result = await resp.json()

                status = result.get("status", "processing")
                if status == "success":
                    audio_url = result.get("data", {}).get("audio_url")
                    if not audio_url:
                        raise RuntimeError(f"音乐生成完成但响应中缺少 audio_url: {result}")
                    return audio_url

                if status == "failed":
                    raise RuntimeError(f"音乐生成任务失败: {result.get('base_resp', {}).get('status_msg', 'unknown')}")

                logger.debug(
                    "[MiniMax Music] 任务 %s 状态: %s，等待 %ds",
                    task_id,
                    status,
                    self._poll_interval,
                )
                await asyncio.sleep(self._poll_interval)
                elapsed += self._poll_interval

        raise RuntimeError(f"音乐生成任务超时 (task_id={task_id}, timeout={self._timeout}s)")

    async def _download_audio(self, audio_url: str) -> Path:
        """从 URL 下载音频文件。

        Args:
            audio_url: 音频文件 URL

        Returns:
            保存后的本地文件路径

        Raises:
            RuntimeError: 下载失败
        """
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                audio_url,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp,
        ):
            if resp.status != 200:
                raise RuntimeError(f"下载音乐文件失败 (status={resp.status})")
            content = await resp.read()

        filename = f"minimax_music_{uuid.uuid4().hex[:8]}.mp3"
        output_path = self._output_dir / filename
        output_path.write_bytes(content)
        return output_path


__all__ = ["MiniMaxMusicProvider"]
