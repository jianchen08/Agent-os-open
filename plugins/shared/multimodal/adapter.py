"""
多模态模型适配器

适配器契约：convert() 把文本 + 附件转成各提供商的消息块格式；
get_capability() 报告该适配器面向的模型能力。经 DefaultAdapter
转换的附件会被全部丢弃——它恒以 degraded=True 标记，调用方据此感知
附件未随消息送达。

暴露接口：
- MultimodalAdapter：多模态适配器抽象基类
- OpenAIVisionAdapter：OpenAI Vision 格式适配器
- ClaudeVisionAdapter：Anthropic Claude Vision 格式适配器
- DefaultAdapter：纯文本降级适配器（丢弃附件，degraded=True）
"""

from abc import ABC, abstractmethod

from mm_types import AttachmentInfo, MediaType, ModelCapability


class MultimodalAdapter(ABC):
    """
    多模态适配器抽象基类

    定义多模态消息转换的通用接口，不同LLM提供商需要实现具体的转换逻辑。

    子类需要实现:
        - convert(): 将内容和附件转换为模型特定格式
        - get_capability(): 返回模型的多模态能力

    Attributes:
        degraded: True 表示该适配器会丢弃附件（降级面），调用方必须可感知。
    """

    #: 子类置 True 表示附件将被丢弃（见 DefaultAdapter）
    degraded: bool = False

    @abstractmethod
    def convert(self, content: str, attachments: list[AttachmentInfo]) -> list[dict]:
        """转换为模型特定格式"""
        pass

    @abstractmethod
    def get_capability(self) -> ModelCapability:
        """获取模型能力"""
        pass


class OpenAIVisionAdapter(MultimodalAdapter):
    """
    OpenAI Vision 适配器

    将附件转换为OpenAI GPT-4V/GPT-4o的消息格式。

    输出格式示例:
        [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
        ]

    支持的模型:
        - gpt-4-vision-preview
        - gpt-4o
        - gpt-4o-mini
        - gpt-4-turbo
    """

    def convert(self, content: str, attachments: list[AttachmentInfo]) -> list[dict]:
        """转换为OpenAI Vision格式"""
        messages: list[dict] = [{"type": "text", "text": content}]

        for attachment in attachments:
            # 只处理图片类型
            if attachment.media_type == MediaType.IMAGE:
                # 优先使用base64数据
                if attachment.base64_data:
                    image_url = f"data:{attachment.mime_type};base64,{attachment.base64_data}"
                    messages.append({"type": "image_url", "image_url": {"url": image_url}})
                # 如果有URL，直接使用
                elif attachment.url:
                    messages.append({"type": "image_url", "image_url": {"url": attachment.url}})

        return messages

    def get_capability(self) -> ModelCapability:
        """获取OpenAI Vision模型能力"""
        return ModelCapability(
            model_name="gpt-4o",
            supports_image=True,
            supports_audio=True,
            supports_video=False,
            supported_image_types=["image/jpeg", "image/png", "image/gif", "image/webp"],
            max_image_size=20 * 1024 * 1024,  # 20MB
            max_audio_size=25 * 1024 * 1024,  # 25MB
        )


class ClaudeVisionAdapter(MultimodalAdapter):
    """
    Claude Vision 适配器

    将附件转换为Anthropic Claude-3系列的消息格式。

    输出格式示例:
        [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}
        ]

    支持的模型:
        - claude-3-opus
        - claude-3-sonnet
        - claude-3-haiku
        - claude-3-5-sonnet
        - claude-3-7-sonnet
    """

    def convert(self, content: str, attachments: list[AttachmentInfo]) -> list[dict]:
        """转换为Claude Vision格式"""
        messages: list[dict] = [{"type": "text", "text": content}]

        for attachment in attachments:
            # 只处理图片类型
            if attachment.media_type == MediaType.IMAGE:  # noqa: SIM102
                # 使用base64数据
                if attachment.base64_data:
                    messages.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": attachment.mime_type,
                                "data": attachment.base64_data,
                            },
                        }
                    )

        return messages

    def get_capability(self) -> ModelCapability:
        """获取Claude Vision模型能力"""
        return ModelCapability(
            model_name="claude-3-opus",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=["image/jpeg", "image/png", "image/gif", "image/webp"],
            max_image_size=20 * 1024 * 1024,  # 20MB
        )


class DefaultAdapter(MultimodalAdapter):
    """
    默认适配器

    用于不支持多模态的模型，仅返回文本内容，忽略所有附件。

    附件丢弃必须可感知：本适配器 degraded 恒为 True，
    经 get_adapter() 取得的实例携带该标记。

    适用场景:
        - 纯文本模型（如 deepseek-chat、gpt-3.5-turbo）
        - 不支持多模态的模型
        - 未知模型的降级处理
    """

    degraded = True

    def convert(self, content: str, attachments: list[AttachmentInfo]) -> list[dict]:
        """转换为纯文本格式"""
        return [{"type": "text", "text": content}]

    def get_capability(self) -> ModelCapability:
        """获取默认模型能力"""
        return ModelCapability(
            model_name="default",
            supports_image=False,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[],
        )
