"""LLM chat completions HTTP 客户端（压缩专用精简版）。

从 plugins/shared/system/memory/embedding_client.py 精简而来——compress 只需要
chat_completion，不需要 embedding，故删除了 embedding 相关代码与对 ports 的依赖。

从内核注入的 config（config_refs: ["models"]）解析 GLM 模型配置，
直接用 requests 调 OpenAI 兼容端点。独立于 src/，sidecar 内部完成 env 展开。

config 形状（loader 原样传入，${ENV} 字面量未展开）：
    config["models"]["models"]["<model_id>"]   # 模型定义
    config["models"]["providers"]["<provider>"]  # provider + api_key
    config["models"]["defaults"]["chat"]          # 默认 chat 模型 ID

暴露接口：
- LLMClient: HTTP 客户端（chat_available / chat_completion）
  chat_completion 入参 prompt 支持字符串（包成单条 user 消息）或
  消息列表（原样发送，fork 压缩用）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_DEFAULT_TIMEOUT = 30


def _resolve_env(value: str) -> str:
    """展开字符串中的 ${ENV_VAR} 占位符。

    Args:
        value: 可能含 ${ENV} 占位符的字符串

    Returns:
        展开后的字符串；未定义的环境变量替换为空字符串
    """
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


class LLMClient:
    """GLM LLM HTTP 客户端（chat completions，压缩专用）。

    从 models config 解析 chat 模型与 API key，
    提供 chat_completion 同步方法（在 async 调用方用 to_thread 包裹）。
    默认按 ``default_role`` 从 ``defaults`` 选模型（chat / compression）。

    Attributes:
        chat_api_base: chat 端点 base url
        chat_model: chat 模型名
        chat_api_key: chat API key
    """

    def __init__(self, config: dict[str, Any], default_role: str = "chat") -> None:
        """从 models config 解析配置。

        Args:
            config: 内核注入的插件 config（config_refs 含 "models"）
            default_role: 从 defaults 中读取的角色键，默认 "chat"；
                压缩场景传 "compression"（读 defaults.compression → minimax-m3）
        """
        models_cfg = config.get("models", {}) if config else {}

        # 按角色解析；chat 默认 deepseek-v4-flash，compression 默认 minimax-m3，回退 glm-5.2
        fallback = "glm-5.2"
        default_id = "minimax-m3" if default_role == "compression" else "deepseek-v4-flash"
        self.chat_api_base, self.chat_model, self.chat_api_key = self._resolve_model(
            models_cfg, role=default_role, default_id=default_id, fallback_id=fallback
        )

    def _resolve_model(
        self,
        models_cfg: dict[str, Any],
        role: str,
        default_id: str,
        fallback_id: str | None = None,
    ) -> tuple[str, str, str]:
        """解析指定角色的模型配置。

        Args:
            models_cfg: models 配置块
            role: defaults 中的角色键（如 "chat" / "compression"）
            default_id: defaults 中指定的模型 ID
            fallback_id: 默认 ID 不可用时的回退 ID

        Returns:
            (api_base, model_name, api_key)
        """
        defaults = models_cfg.get("defaults", {})
        model_id = defaults.get(role, default_id)
        models = models_cfg.get("models", {})
        providers = models_cfg.get("providers", {})

        model_def = models.get(model_id)
        if model_def is None and fallback_id:
            model_id = fallback_id
            model_def = models.get(model_id)

        if model_def is None:
            logger.warning("[LLMClient] 角色 %s 的模型 %s 未配置", role, model_id)
            return "", "", ""

        api_base = _resolve_env(str(model_def.get("api_base", "")))
        model_name = str(model_def.get("model_name", model_id))

        api_key = self._lookup_api_key(model_def, providers)
        return api_base, model_name, api_key

    def _lookup_api_key(self, model_def: dict[str, Any], providers: dict[str, Any]) -> str:
        """按 provider 查找 API key。

        Args:
            model_def: 模型定义（含 provider 字段）
            providers: providers 配置块

        Returns:
            展开后的 API key；未找到返回空字符串
        """
        provider_name = model_def.get("provider", "")
        provider = providers.get(provider_name, {})
        keys = provider.get("keys", [])
        if keys:
            raw_key = keys[0].get("api_key", "")
            return _resolve_env(str(raw_key))
        return ""

    @property
    def chat_available(self) -> bool:
        """chat 是否可用（api_base/model/key 均非空）。"""
        return bool(self.chat_api_base and self.chat_model and self.chat_api_key)

    def chat_completion(
        self,
        prompt: str | list[dict[str, Any]],
        max_tokens: int = 800,
    ) -> str:
        """调用 chat completions 生成文本（用于压缩摘要）。

        Args:
            prompt: 用户 prompt 字符串，或完整消息列表（fork 压缩：
                system + messages + 末尾指令原样发送，复用 provider 前缀 cache）
            max_tokens: 最大输出 token 数

        Returns:
            模型生成的文本

        Raises:
            RuntimeError: 配置缺失或 HTTP 调用失败
        """
        if not self.chat_available:
            raise RuntimeError("chat 配置缺失（api_base/model/api_key）")

        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = list(prompt)

        url = f"{self.chat_api_base.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self.chat_api_key}"}

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"chat HTTP 调用失败: {e}") from e

        try:
            choices = resp.json().get("choices", [])
            if not choices:
                return ""
            return choices[0].get("message", {}).get("content", "")
        except ValueError as e:
            raise RuntimeError(f"chat 响应解析失败: {e}") from e
