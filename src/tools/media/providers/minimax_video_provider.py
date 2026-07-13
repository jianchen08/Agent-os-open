"""MiniMax 视频生成 Provider。

通过 MiniMax API (api.minimaxi.com) 实现视频生成，
支持 MiniMax-Hailuo 系列模型的文生视频功能，采用异步任务轮询模式。

暴露接口：
- MiniMaxVideoProvider：MiniMax 视频生成 Provider
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
DEFAULT_MODEL = "MiniMax-Hailuo-2.3"
DEFAULT_POLL_INTERVAL = 5
DEFAULT_TIMEOUT = 600


class MiniMaxVideoProvider(MediaProvider):
    """MiniMax 视频生成 Provider。

    通过 MiniMax REST API 生成视频，支持：
    - 文生视频（text-to-video）
    - 多种模型选择（MiniMax-Hailuo-2.3、T2V-01-Director 等）
    - 自定义分辨率和时长
    - prompt 自动优化
    - 异步任务轮询等待完成

    Attributes:
        _api_base: MiniMax API 基础地址
        _api_key: API 密钥
        _model: 模型名称
        _output_dir: 输出目录
        _poll_interval: 轮询间隔（秒）
        _timeout: 超时时间（秒）
        _prompt_optimizer: 是否优化 prompt
    """

    def __init__(self, config: MediaProviderConfig) -> None:
        """初始化 MiniMaxVideoProvider。

        Args:
            config: Provider 配置，config 字段支持：
                - api_key: API 密钥（必填）
                - model: 模型名称（默认 MiniMax-Hailuo-2.3）
                - output_dir: 输出目录（默认 ./output/video）
                - poll_interval: 轮询间隔秒数（默认 5）
                - timeout: 超时秒数（默认 600）
                - prompt_optimizer: 是否优化 prompt（默认 true）
        """
        super().__init__(
            provider_name="minimax_video",
            media_type=MediaType.VIDEO,
            config=config,
        )
        cfg = config.config
        self._api_base: str = cfg.get("api_base", DEFAULT_API_BASE)
        self._api_key: str = cfg.get("api_key", "")
        self._model: str = cfg.get("model", DEFAULT_MODEL)
        self._output_dir: Path = Path(cfg.get("output_dir", "./output/video"))
        self._poll_interval: int = cfg.get("poll_interval", DEFAULT_POLL_INTERVAL)
        self._timeout: int = cfg.get("timeout", DEFAULT_TIMEOUT)
        self._prompt_optimizer: bool = cfg.get("prompt_optimizer", True)

    async def is_available(self) -> bool:
        """检查 MiniMax API 是否可用（API Key 已配置）。"""
        return bool(self._api_key)

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成视频。

        提交异步任务后轮询等待完成，下载视频文件保存到本地。

        Args:
            prompt: 视频描述提示词，最长 2000 字符
            **kwargs: 可选参数：
                - duration: 视频时长（秒，6 或 10，默认 6）
                - resolution: 分辨率（720P/768P/1080P，默认 1080P）
                - prompt_optimizer: 是否优化 prompt

        Returns:
            MediaResult 包含生成的视频文件路径和元数据

        Raises:
            RuntimeError: API 调用失败或任务超时
            ValueError: API Key 未配置
        """
        if not self._api_key:
            raise ValueError("MiniMax API Key 未配置")

        payload = self._build_payload(prompt, **kwargs)

        logger.info("[MiniMax Video] 提交视频生成: prompt=%s, model=%s", prompt[:50], self._model)
        task_id = await self._submit_task(payload)
        logger.info("[MiniMax Video] 任务已提交: task_id=%s", task_id)

        file_id = await self._poll_task(task_id)
        logger.info("[MiniMax Video] 视频生成完成，开始下载")

        self._output_dir.mkdir(parents=True, exist_ok=True)
        file_path = await self._download_video(file_id)

        metadata: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "provider": "minimax_video",
            "task_id": task_id,
        }
        if kwargs.get("duration"):
            metadata["duration"] = kwargs["duration"]
        if kwargs.get("resolution"):
            metadata["resolution"] = kwargs["resolution"]

        logger.info("[MiniMax Video] 文件已保存: %s", file_path)
        return MediaResult(
            file_path=file_path,
            media_type=MediaType.VIDEO,
            metadata=metadata,
            provider_name=self.provider_name,
        )

    def _build_payload(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """构建视频生成 API 请求参数。

        Args:
            prompt: 视频描述提示词
            **kwargs: 可选生成参数

        Returns:
            MiniMax API 请求体字典
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt[:2000],
            "prompt_optimizer": kwargs.get("prompt_optimizer", self._prompt_optimizer),
        }

        duration = kwargs.get("duration")
        if duration is not None:
            payload["duration"] = int(duration)

        resolution = kwargs.get("resolution")
        if resolution:
            payload["resolution"] = resolution

        return payload

    async def _submit_task(self, payload: dict[str, Any]) -> str:
        """提交视频生成异步任务。

        Args:
            payload: 请求体字典

        Returns:
            异步任务 ID

        Raises:
            RuntimeError: HTTP 错误或业务错误
        """
        url = f"{self._api_base}/video_generation"
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
                raise RuntimeError(f"MiniMax 视频生成任务提交失败 (status={resp.status}): {error_text}")
            result = await resp.json()

        base_resp = result.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax 视频生成业务错误: {base_resp.get('status_msg', 'unknown')}")

        task_id = result.get("task_id")
        if not task_id:
            raise RuntimeError(f"MiniMax 视频生成响应中缺少 task_id: {result}")

        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询异步任务状态直到完成。

        Args:
            task_id: 异步任务 ID

        Returns:
            生成的视频文件 ID

        Raises:
            RuntimeError: 任务失败或超时
        """
        url = f"{self._api_base}/query/video_generation"
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
                        raise RuntimeError(f"查询视频生成任务失败 (status={resp.status}): {error_text}")
                    result = await resp.json()

                status = result.get("status", "processing")
                if status == "success":
                    file_id = result.get("file_id")
                    if not file_id:
                        raise RuntimeError(f"视频生成完成但响应中缺少 file_id: {result}")
                    return file_id

                if status == "failed":
                    raise RuntimeError(f"视频生成任务失败: {result.get('base_resp', {}).get('status_msg', 'unknown')}")

                logger.debug(
                    "[MiniMax Video] 任务 %s 状态: %s，等待 %ds",
                    task_id,
                    status,
                    self._poll_interval,
                )
                await asyncio.sleep(self._poll_interval)
                elapsed += self._poll_interval

        raise RuntimeError(f"视频生成任务超时 (task_id={task_id}, timeout={self._timeout}s)")

    async def _download_video(self, file_id: str) -> Path:
        """通过 file_id 下载视频文件。

        Args:
            file_id: MiniMax 文件 ID

        Returns:
            保存后的本地文件路径

        Raises:
            RuntimeError: 下载失败
        """
        url = f"{self._api_base}/files/retrieve"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                params={"file_id": file_id, "purpose": "video_generation"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp,
        ):
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"下载视频文件失败 (status={resp.status}): {error_text}")
            content = await resp.read()

        filename = f"minimax_video_{uuid.uuid4().hex[:8]}.mp4"
        output_path = self._output_dir / filename
        output_path.write_bytes(content)
        return output_path


__all__ = ["MiniMaxVideoProvider"]
