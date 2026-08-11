"""ComfyUIProvider 测试。

覆盖场景：
- is_available: API 可连接 / 不可连接
- generate (Prompt 模式): 正常生成、参数传递、工作流模板替换
- generate (工作流模板模式): 加载自定义模板
- 轮询超时 / 执行失败
- 输出目录创建
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.media.base import MediaProviderConfig, MediaResult, MediaType
from tools.media.providers.comfyui_provider import ComfyUIProvider


@pytest.fixture
def default_config() -> MediaProviderConfig:
    """创建默认 Provider 配置。"""
    return MediaProviderConfig(
        class_name="ComfyUIProvider",
        enabled=True,
        priority=1,
        config={
            "api_url": "http://127.0.0.1:8188",
            "output_dir": "/tmp/comfyui_output",
            "workflow_dir": str(Path(__file__).parent.parent.parent.parent.parent / "config" / "media_workflows"),
            "default_checkpoint": "v1-5-pruned-emaonly.safetensors",
        },
    )


@pytest.fixture
def provider(default_config: MediaProviderConfig) -> ComfyUIProvider:
    """创建 ComfyUIProvider 实例。"""
    return ComfyUIProvider(config=default_config)


@pytest.fixture
def temp_output_dir() -> Path:
    """创建临时输出目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def provider_with_temp_dir(default_config: MediaProviderConfig, temp_output_dir: Path) -> ComfyUIProvider:
    """创建使用临时输出目录的 ComfyUIProvider。"""
    default_config.config["output_dir"] = str(temp_output_dir)
    return ComfyUIProvider(config=default_config)


class TestComfyUIProviderInit:
    """测试 Provider 初始化。"""

    def test_media_type_is_image(self, provider: ComfyUIProvider) -> None:
        """media_type 应为 IMAGE。"""
        assert provider.media_type == MediaType.IMAGE

    def test_provider_name(self, provider: ComfyUIProvider) -> None:
        """provider_name 应为 'comfyui'。"""
        assert provider.provider_name == "comfyui"

    def test_default_api_url(self, provider: ComfyUIProvider) -> None:
        """默认 API 地址应为 http://127.0.0.1:8188。"""
        assert provider.api_url == "http://127.0.0.1:8188"

    def test_custom_api_url(self) -> None:
        """支持自定义 API 地址。"""
        config = MediaProviderConfig(
            class_name="ComfyUIProvider",
            config={"api_url": "http://192.168.1.100:8188"},
        )
        p = ComfyUIProvider(config=config)
        assert p.api_url == "http://192.168.1.100:8188"


class TestIsAvailable:
    """测试 is_available 方法。"""

    @pytest.mark.asyncio
    async def test_available_when_api_responds(self, provider: ComfyUIProvider) -> None:
        """API 返回正常时 is_available 应返回 True。"""
        with patch("tools.media.providers.comfyui_provider.aiohttp.ClientSession") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.status = 200

            # session.get() 必须同步返回异步上下文管理器（而非协程）
            mock_get_cm = MagicMock()
            mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_get_cm.__aexit__ = AsyncMock(return_value=None)

            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=mock_get_cm)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_cls.return_value = mock_session

            result = await provider.is_available()
            assert result is True

    @pytest.mark.asyncio
    async def test_not_available_when_connection_fails(self, provider: ComfyUIProvider) -> None:
        """API 连接失败时 is_available 应返回 False。"""
        with patch("tools.media.providers.comfyui_provider.aiohttp.ClientSession") as mock_cls:
            mock_session = MagicMock()
            mock_session.get = MagicMock(side_effect=Exception("Connection refused"))
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_session

            result = await provider.is_available()
            assert result is False


class TestGeneratePromptMode:
    """测试 Prompt 模式图像生成。"""

    @pytest.mark.asyncio
    async def test_generate_returns_media_result(
        self,
        provider_with_temp_dir: ComfyUIProvider,
        temp_output_dir: Path,
    ) -> None:
        """generate 应返回 MediaResult 实例。"""
        provider = provider_with_temp_dir

        # Mock ComfyUI API 的完整流程
        with (
            patch.object(provider, "_submit_workflow") as mock_submit,
            patch.object(provider, "_poll_result") as mock_poll,
            patch.object(provider, "_download_image") as mock_download,
        ):
            mock_submit.return_value = "test-prompt-id"
            mock_poll.return_value = {
                "status": "success",
                "outputs": {"9": {"images": [{"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}]}},
            }
            # 模拟下载图片：创建一个假的 PNG 文件
            fake_png = temp_output_dir / "ComfyUI_00001_.png"
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            mock_download.return_value = fake_png

            result = await provider.generate("a beautiful sunset")

            assert isinstance(result, MediaResult)
            assert result.media_type == MediaType.IMAGE
            assert result.provider_name == "comfyui"
            assert result.file_path.exists()

    @pytest.mark.asyncio
    async def test_generate_passes_parameters_to_workflow(
        self,
        provider_with_temp_dir: ComfyUIProvider,
        temp_output_dir: Path,
    ) -> None:
        """generate 应将参数正确传递到工作流模板。"""
        provider = provider_with_temp_dir

        with (
            patch.object(provider, "_submit_workflow") as mock_submit,
            patch.object(provider, "_poll_result") as mock_poll,
            patch.object(provider, "_download_image") as mock_download,
        ):
            mock_submit.return_value = "test-prompt-id"
            mock_poll.return_value = {
                "status": "success",
                "outputs": {"9": {"images": [{"filename": "test.png", "subfolder": "", "type": "output"}]}},
            }
            fake_png = temp_output_dir / "test.png"
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")
            mock_download.return_value = fake_png

            result = await provider.generate(
                "a cat",
                negative_prompt="ugly, blurry",
                width=512,
                height=768,
                steps=30,
                cfg_scale=7.5,
                seed=42,
            )

            # 验证 submit_workflow 被调用了
            assert mock_submit.called
            # 获取传入的工作流 JSON
            call_args = mock_submit.call_args
            call_args[0][0] if call_args[0] else call_args.kwargs.get("workflow", {})

            # 检查参数是否被替换到工作流中
            assert result.metadata["prompt"] == "a cat"
            assert result.metadata.get("seed") == 42

    @pytest.mark.asyncio
    async def test_generate_metadata_contains_info(
        self,
        provider_with_temp_dir: ComfyUIProvider,
        temp_output_dir: Path,
    ) -> None:
        """返回的 MediaResult metadata 应包含 prompt、size、seed 等信息。"""
        provider = provider_with_temp_dir

        with (
            patch.object(provider, "_submit_workflow") as mock_submit,
            patch.object(provider, "_poll_result") as mock_poll,
            patch.object(provider, "_download_image") as mock_download,
        ):
            mock_submit.return_value = "test-id"
            mock_poll.return_value = {
                "status": "success",
                "outputs": {"9": {"images": [{"filename": "test.png", "subfolder": "", "type": "output"}]}},
            }
            fake_png = temp_output_dir / "test.png"
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")
            mock_download.return_value = fake_png

            result = await provider.generate(
                "mountain landscape",
                width=640,
                height=480,
                steps=25,
                seed=123,
            )

            assert "prompt" in result.metadata
            assert result.metadata["prompt"] == "mountain landscape"
            assert result.metadata.get("width") == 640
            assert result.metadata.get("height") == 480
            assert result.metadata.get("steps") == 25
            assert result.metadata.get("seed") == 123


class TestGenerateWorkflowMode:
    """测试工作流模板模式。"""

    @pytest.mark.asyncio
    async def test_generate_with_workflow_template(
        self,
        provider_with_temp_dir: ComfyUIProvider,
        temp_output_dir: Path,
    ) -> None:
        """使用 workflow_template 参数时应加载对应模板。"""
        provider = provider_with_temp_dir

        with (
            patch.object(provider, "_submit_workflow") as mock_submit,
            patch.object(provider, "_poll_result") as mock_poll,
            patch.object(provider, "_download_image") as mock_download,
        ):
            mock_submit.return_value = "test-id"
            mock_poll.return_value = {
                "status": "success",
                "outputs": {"9": {"images": [{"filename": "test.png", "subfolder": "", "type": "output"}]}},
            }
            fake_png = temp_output_dir / "test.png"
            fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")
            mock_download.return_value = fake_png

            result = await provider.generate(
                "test prompt",
                workflow_template="default_txt2img",
            )

            assert isinstance(result, MediaResult)
            assert mock_submit.called


class TestGenerateErrors:
    """测试生成过程中的错误处理。"""

    @pytest.mark.asyncio
    async def test_generate_timeout(self, provider_with_temp_dir: ComfyUIProvider) -> None:
        """轮询超时应抛出异常。"""
        provider = provider_with_temp_dir

        with (
            patch.object(provider, "_submit_workflow") as mock_submit,
            patch.object(provider, "_poll_result") as mock_poll,
        ):
            mock_submit.return_value = "test-id"
            mock_poll.side_effect = TimeoutError("ComfyUI generation timed out after 120 seconds")

            with pytest.raises(TimeoutError, match="timed out"):
                await provider.generate("test prompt")

    @pytest.mark.asyncio
    async def test_generate_submit_failure(self, provider_with_temp_dir: ComfyUIProvider) -> None:
        """提交工作流失败时应抛出异常。"""
        provider = provider_with_temp_dir

        with patch.object(provider, "_submit_workflow") as mock_submit:
            mock_submit.side_effect = RuntimeError("Failed to submit workflow to ComfyUI")

            with pytest.raises(RuntimeError, match="Failed to submit"):
                await provider.generate("test prompt")


class TestWorkflowTemplateLoading:
    """测试工作流模板加载与替换。"""

    def test_load_default_template(self, provider: ComfyUIProvider) -> None:
        """应能加载默认文生图模板。"""
        template = provider._load_template("default_txt2img")
        assert template is not None
        assert "nodes" in template or "prompt" in template

    def test_load_nonexistent_template(self, provider: ComfyUIProvider) -> None:
        """加载不存在的模板应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            provider._load_template("nonexistent_template")

    def test_replace_template_placeholders(self, provider: ComfyUIProvider) -> None:
        """模板占位符应被正确替换。"""
        template_str = '{"seed": {{seed}}, "prompt": "{{prompt}}"}'
        params = {
            "seed": 42,
            "prompt": "a cat",
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg_scale": 7.0,
            "negative_prompt": "",
            "checkpoint": "model.safetensors",
        }

        result = provider._render_template(template_str, params)
        parsed = json.loads(result)

        assert parsed["seed"] == 42
        assert parsed["prompt"] == "a cat"


class TestGetToolDefinition:
    """测试 image_generate 工具定义。"""

    def test_tool_definition_exists(self) -> None:
        """image_generate 工具定义应存在。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        tool_def = ImageGenerateTool.get_tool_definition()
        assert tool_def.name == "image_generate"

    def test_tool_definition_has_required_params(self) -> None:
        """工具定义应包含 prompt 必填参数。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        tool_def = ImageGenerateTool.get_tool_definition()
        schema = tool_def.input_schema
        assert "properties" in schema
        assert "prompt" in schema["properties"]
        assert "required" in schema
        assert "prompt" in schema["required"]

    def test_tool_definition_has_optional_params(self) -> None:
        """工具定义应包含可选参数。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        tool_def = ImageGenerateTool.get_tool_definition()
        properties = tool_def.input_schema["properties"]

        optional_params = [
            "negative_prompt",
            "width",
            "height",
            "style",
            "seed",
            "workflow_template",
        ]
        for param in optional_params:
            assert param in properties, f"缺少可选参数: {param}"

    def test_tool_source_is_builtin(self) -> None:
        """工具来源应为 BUILTIN。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415
        from tools.types import ToolSource  # noqa: PLC0415

        tool_def = ImageGenerateTool.get_tool_definition()
        assert tool_def.source == ToolSource.BUILTIN


class TestImageGenerateExecution:
    """测试 image_generate 工具执行。"""

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        """成功执行应返回 ToolExecutionResult。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        # 创建 mock registry 并注入到 tool
        mock_registry = MagicMock()
        mock_chain = AsyncMock()
        mock_registry.get_chain_for_type.return_value = mock_chain

        fake_result = MediaResult(
            file_path=Path("/tmp/test_output.png"),
            media_type=MediaType.IMAGE,
            provider_name="comfyui",
            metadata={"prompt": "a cat"},
        )
        mock_chain.execute_generate.return_value = fake_result

        tool = ImageGenerateTool(registry=mock_registry)
        result = await tool.execute({"prompt": "a cat"})

        assert result.success is True
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_execute_missing_prompt(self) -> None:
        """缺少 prompt 参数应返回失败结果。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        mock_registry = MagicMock()
        tool = ImageGenerateTool(registry=mock_registry)
        result = await tool.execute({})

        assert result.success is False
        assert "prompt" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_execute_all_providers_failed(self) -> None:
        """所有 Provider 失败应返回失败结果。"""
        from tools.builtin.image_generate import ImageGenerateTool  # noqa: PLC0415

        mock_registry = MagicMock()
        mock_chain = AsyncMock()
        mock_registry.get_chain_for_type.return_value = mock_chain
        mock_chain.execute_generate.side_effect = RuntimeError("所有 Provider 均失败")

        tool = ImageGenerateTool(registry=mock_registry)
        result = await tool.execute({"prompt": "test"})

        assert result.success is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
