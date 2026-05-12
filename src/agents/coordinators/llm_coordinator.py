"""
LLM 协调器 - 负责 LLM 客户端的创建和管理

职责：
- 创建和管理 LLM 客户端
- 处理 LLM 客户端的生命周期
- 提供统一的 LLM 客户端获取接口
"""

import logging

from langchain_core.language_models import BaseChatModel

from src.agents.types import AgentConfig
from src.llm.base import LLMClient
from src.llm.factory import LLMFactory

logger = logging.getLogger(__name__)


class LLMCoordinator:
    """
    LLM 客户端协调器

    负责 LLM 客户端的创建、缓存和管理
    """

    def __init__(self, config: AgentConfig, llm_factory: LLMFactory | None = None):
        """
        初始化 LLM 协调器

        Args:
            config: Agent 配置
            llm_factory: LLM 工厂实例（可选，推荐通过 DI 注入）
        """
        self.config = config

        # 优先使用注入的 LLMFactory，否则从 DI 容器获取
        if llm_factory is not None:
            self.llm_factory = llm_factory
        else:
            from src.core.di import get_global_container

            container = get_global_container()
            self.llm_factory = container.get("llm_factory")

        self._llm_client: LLMClient | None = None
        self._langchain_llm: BaseChatModel | None = None

    def get_langchain_llm(self) -> BaseChatModel:
        """
        获取 LangChain 兼容的 LLM 客户端

        Returns:
            LangChain BaseChatModel 实例
        """
        if self._langchain_llm is not None:
            return self._langchain_llm

        # 优先使用自研客户端的适配器，以确保消息日志记录功能正常工作
        # LangChain 原生客户端不会调用我们的消息日志记录器
        logger.debug(
            f"[LLMCoordinator] 使用自研 LLM 客户端适配器 | model={self.config.model_name}"
        )
        self._langchain_llm = self._create_llm_adapter()
        return self._langchain_llm

    def get_native_llm(self) -> LLMClient:
        """
        获取原生 LLM 客户端

        Returns:
            原生 LLM 客户端实例
        """
        if self._llm_client is not None:
            return self._llm_client

        self._llm_client = self.llm_factory.get_client(self.config.model_name)
        return self._llm_client

    def _create_llm_adapter(self) -> BaseChatModel:
        """
        创建自研 LLM 客户端的 LangChain 适配器

        Returns:
            适配后的 LLM 客户端
        """
        from src.llm.adapters import LLMClientAdapter

        native_client = self.get_native_llm()
        return LLMClientAdapter(native_client)

    def cleanup(self) -> None:
        """清理 LLM 客户端资源"""
        self._llm_client = None
        self._langchain_llm = None
        logger.debug("[LLMCoordinator] LLM 客户端资源已清理")
