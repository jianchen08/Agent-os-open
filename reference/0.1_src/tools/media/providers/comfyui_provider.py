"""ComfyUI 图像生成 Provider。

通过 ComfyUI API 实现图像生成，支持：
- Prompt 模式：使用内置默认文生图工作流模板
- 工作流模板模式：加载预定义工作流并替换参数

ComfyUI API 交互流程：
1. POST /prompt 提交工作流
2. 轮询 /history/{prompt_id} 等待完成
3. 从 /view 获取输出图片
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType

logger = logging.getLogger(__name__)

# 默认配置常量
DEFAULT_API_URL = "http://127.0.0.1:8188"
DEFAULT_CHECKPOINT = "v1-5-pruned-emaonly.safetensors"
DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 512
DEFAULT_STEPS = 20
DEFAULT_CFG_SCALE = 7.0
DEFAULT_SEED = -1  # -1 表示随机种子
DEFAULT_NEGATIVE_PROMPT = ""
POLL_INTERVAL = 1  # 轮询间隔（秒）
POLL_TIMEOUT = 120  # 轮询超时（秒）


class ComfyUIProvider(MediaProvider):
    """ComfyUI 图像生成 Provider。

    通过 ComfyUI REST API 提交工作流，异步轮询等待结果，
    下载并保存生成的图像文件。

    Attributes:
        api_url: ComfyUI API 地址
        output_dir: 输出目录路径
        workflow_dir: 工作流模板目录路径
        default_checkpoint: 默认模型检查点名称
    """

    def __init__(self, config: MediaProviderConfig) -> None:
        """初始化 ComfyUI Provider。

        Args:
            config: Provider 配置，config 字段支持：
                - api_url: ComfyUI API 地址（默认 http://127.0.0.1:8188）
                - output_dir: 输出目录（默认 ./output/images）
                - workflow_dir: 工作流模板目录
                - default_checkpoint: 默认模型检查点
        """
        super().__init__(
            provider_name="comfyui",
            media_type=MediaType.IMAGE,
            config=config,
        )
        cfg = config.config
        self._api_url: str = cfg.get("api_url", DEFAULT_API_URL)
        self._output_dir: Path = Path(cfg.get("output_dir", "./output/images"))
        self._workflow_dir: Path | None = Path(cfg["workflow_dir"]) if "workflow_dir" in cfg else None
        self._default_checkpoint: str = cfg.get("default_checkpoint", DEFAULT_CHECKPOINT)

    @property
    def api_url(self) -> str:
        """ComfyUI API 地址。"""
        return self._api_url

    async def is_available(self) -> bool:
        """检查 ComfyUI API 是否可连接。

        尝试访问 ComfyUI 的系统信息接口，如果返回 200 则认为可用。

        Returns:
            True 表示 API 可连接，False 表示不可连接
        """
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    f"{self._api_url}/system_stats",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp,
            ):
                return resp.status == 200
        except Exception:
            logger.debug("[ComfyUI] API 连接失败: %s", self._api_url)
            return False

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成图像。

        支持两种模式：
        - Prompt 模式（默认）：使用内置默认文生图模板
        - 工作流模板模式：通过 workflow_template 指定模板名称

        Args:
            prompt: 图像生成提示词
            **kwargs: 可选参数：
                - negative_prompt: 负面提示词
                - width: 图像宽度（默认 512）
                - height: 图像高度（默认 512）
                - steps: 采样步数（默认 20）
                - cfg_scale: CFG 引导系数（默认 7.0）
                - seed: 随机种子（-1 为随机）
                - style: 图像风格
                - workflow_template: 工作流模板名称

        Returns:
            MediaResult 包含生成的图像文件路径和元数据

        Raises:
            FileNotFoundError: 工作流模板不存在
            RuntimeError: ComfyUI API 调用失败
            TimeoutError: 生成超时
        """
        # 提取参数
        workflow_template = kwargs.get("workflow_template")
        params = self._build_params(prompt, **kwargs)

        # 加载并渲染工作流模板
        template_name = workflow_template or "default_txt2img"
        workflow = self._load_and_render_template(template_name, params)

        # 确保输出目录存在
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 提交工作流
        logger.info("[ComfyUI] 提交工作流: template=%s, prompt=%r", template_name, prompt)
        prompt_id = await self._submit_workflow(workflow)

        # 轮询等待结果
        logger.info("[ComfyUI] 等待结果: prompt_id=%s", prompt_id)
        history = await self._poll_result(prompt_id)

        # 提取输出图片信息
        output_images = self._extract_output_images(history)
        if not output_images:
            raise RuntimeError("ComfyUI 工作流完成但未生成图片")

        # 下载第一张图片
        image_info = output_images[0]
        file_path = await self._download_image(image_info)

        # 构建 metadata
        metadata: dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": params.get("negative_prompt", ""),
            "width": params.get("width", DEFAULT_WIDTH),
            "height": params.get("height", DEFAULT_HEIGHT),
            "steps": params.get("steps", DEFAULT_STEPS),
            "cfg_scale": params.get("cfg_scale", DEFAULT_CFG_SCALE),
            "seed": params.get("seed", DEFAULT_SEED),
            "workflow_template": template_name,
        }
        if "style" in kwargs and kwargs["style"]:
            metadata["style"] = kwargs["style"]

        logger.info("[ComfyUI] 图像生成完成: %s", file_path)
        return MediaResult(
            file_path=file_path,
            media_type=MediaType.IMAGE,
            metadata=metadata,
            provider_name=self.provider_name,
        )

    def _build_params(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """构建工作流模板参数。

        Args:
            prompt: 提示词
            **kwargs: 用户传入的参数

        Returns:
            完整的模板参数字典
        """
        return {
            "prompt": prompt,
            "negative_prompt": kwargs.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT),
            "width": kwargs.get("width", DEFAULT_WIDTH),
            "height": kwargs.get("height", DEFAULT_HEIGHT),
            "steps": kwargs.get("steps", DEFAULT_STEPS),
            "cfg_scale": kwargs.get("cfg_scale", DEFAULT_CFG_SCALE),
            "seed": kwargs.get("seed", DEFAULT_SEED),
            "checkpoint": self._default_checkpoint,
        }

    def _load_and_render_template(self, template_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """加载并渲染工作流模板。

        Args:
            template_name: 模板名称（不含扩展名）
            params: 模板参数

        Returns:
            渲染后的工作流字典

        Raises:
            FileNotFoundError: 模板文件不存在
        """
        raw_template = self._load_template(template_name)
        rendered = self._render_template(raw_template, params)
        return json.loads(rendered)

    def _load_template(self, template_name: str) -> str:
        """加载工作流模板文件。

        Args:
            template_name: 模板名称（不含 .json 扩展名）

        Returns:
            模板文件内容字符串

        Raises:
            FileNotFoundError: 模板文件不存在
        """
        # 搜索路径：1) 配置的 workflow_dir  2) config/media_workflows/
        search_paths: list[Path] = []
        if self._workflow_dir:
            search_paths.append(self._workflow_dir)

        # 默认路径：相对于项目根目录
        project_root = Path(__file__).parent.parent.parent.parent.parent
        search_paths.append(project_root / "config" / "media_workflows")

        for search_dir in search_paths:
            template_path = search_dir / f"{template_name}.json"
            if template_path.exists():
                content = template_path.read_text(encoding="utf-8")
                logger.debug("[ComfyUI] 加载模板: %s", template_path)
                return content

        raise FileNotFoundError(f"工作流模板不存在: {template_name}（搜索路径: {search_paths}）")

    def _render_template(self, template_str: str, params: dict[str, Any]) -> str:
        """渲染模板，替换占位符。

        将 {{placeholder}} 格式的占位符替换为参数值。
        字符串值会自动加引号，数值类型不加引号。

        Args:
            template_str: 模板字符串
            params: 替换参数

        Returns:
            渲染后的字符串
        """
        # 对字符串类型的值添加引号，数值类型直接替换
        result = template_str
        for key, value in params.items():
            placeholder = "{{" + key + "}}"
            if isinstance(value, str):
                # 字符串已在 JSON 模板中用引号包裹
                replacement = value.replace("\\", "\\\\").replace('"', '\\"')
                result = result.replace(placeholder, replacement)
            else:
                result = result.replace(placeholder, str(value))
        return result

    async def _submit_workflow(self, workflow: dict[str, Any]) -> str:
        """提交工作流到 ComfyUI。

        Args:
            workflow: 工作流定义字典

        Returns:
            prompt_id 字符串

        Raises:
            RuntimeError: 提交失败
        """
        payload = {"prompt": workflow}
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self._api_url}/prompt",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"ComfyUI 提交工作流失败 (status={resp.status}): {error_text}")
            result = await resp.json()
            prompt_id: str = result.get("prompt_id", "")
            if not prompt_id:
                raise RuntimeError("ComfyUI 返回无效的 prompt_id")
            return prompt_id

    async def _poll_result(self, prompt_id: str) -> dict[str, Any]:
        """轮询等待 ComfyUI 工作流完成。

        Args:
            prompt_id: 工作流 ID

        Returns:
            工作流执行历史记录

        Raises:
            TimeoutError: 轮询超时
            RuntimeError: 工作流执行失败
        """
        start_time = time.time()
        url = f"{self._api_url}/history/{prompt_id}"

        async with aiohttp.ClientSession() as session:
            while True:
                elapsed = time.time() - start_time
                if elapsed > POLL_TIMEOUT:
                    raise TimeoutError(f"ComfyUI generation timed out after {POLL_TIMEOUT} seconds")

                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        history: dict[str, Any] = await resp.json()
                        # 检查是否有该 prompt_id 的记录
                        prompt_history = history.get(prompt_id, {})

                        if prompt_history:
                            # 检查是否完成
                            status = prompt_history.get("status", {})
                            if status.get("completed", False) or status.get("status_str", "") in ("success", "error"):
                                if status.get("status_str") == "error":
                                    error_msg = status.get("messages", "Unknown error")
                                    raise RuntimeError(f"ComfyUI 工作流执行失败: {error_msg}")
                                return prompt_history

                await asyncio.sleep(POLL_INTERVAL)

    def _extract_output_images(self, history: dict[str, Any]) -> list[dict[str, Any]]:
        """从工作流历史中提取输出图片信息。

        Args:
            history: 工作流历史记录

        Returns:
            输出图片信息列表，每项包含 filename, subfolder, type
        """
        images: list[dict[str, Any]] = []
        outputs = history.get("outputs", {})

        for _node_id, node_output in outputs.items():
            if "images" in node_output:
                for img in node_output["images"]:
                    images.append(img)

        return images

    async def _download_image(self, image_info: dict[str, Any]) -> Path:
        """从 ComfyUI 下载输出图片。

        Args:
            image_info: 图片信息字典（filename, subfolder, type）

        Returns:
            下载后的本地文件路径

        Raises:
            RuntimeError: 下载失败
        """
        filename = image_info.get("filename", "output.png")
        subfolder = image_info.get("subfolder", "")
        image_type = image_info.get("type", "output")

        params: dict[str, str] = {
            "filename": filename,
            "type": image_type,
        }
        if subfolder:
            params["subfolder"] = subfolder

        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{self._api_url}/view",
                params=params,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp,
        ):
            if resp.status != 200:
                raise RuntimeError(f"下载图片失败 (status={resp.status}): {filename}")
            content = await resp.read()

        # 保存到本地
        output_path = self._output_dir / filename
        output_path.write_bytes(content)
        logger.debug("[ComfyUI] 图片已保存: %s (%d bytes)", output_path, len(content))
        return output_path
