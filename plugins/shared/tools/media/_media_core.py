"""媒体 Provider 核心类型（0.2 自包含版）。

0.1 的 ``tools.media`` 包（归档于 reference/0.1_src/tools/media/，src/ 已删）
在 0.2 未迁移，本模块就地重建 image/tts/video/music 四个生成工具共用的
最小类型面（对应 0.1 的 base.py / fallback.py / provider_registry.py）：

- ``MediaType``：媒体类型枚举
- ``FallbackStrategy`` / ``ProviderChain``：Fallback 链（最小可用实现，
  按序尝试每个 Provider，全部失败抛 RuntimeError——语义与 0.1 对齐）
- ``MediaProviderRegistry``：注册表类型面（0.1 完整实现未迁移；运行时实例
  由外部注入，duck-typing，本类仅用于类型注解与空默认行为）
- ``ProviderUnavailable`` / ``MediaResult`` / ``MediaProviderClient``：
  F-MEDIA-2 新增——provider 依赖迁移：0.1 的 infrastructure.service_provider
  （全局服务注册表）已删，0.2 等价机制是**经 tool-executor capability 调用
  后端服务**（参考 hindsight_memory/memory_backend.py 的 HindsightBackend：
  capability_caller 注入 + tool-executor.invoke 模式）。

0.2 媒体服务契约（F-MEDIA-2，本模块为权威定义）：
- 调用方式：``tool-executor.invoke``（params 形如 ``{"tool_name": ..., "args": ...}``）
- 服务名：``media.generate``（约定名——由媒体生成后端服务插件提供；
  0.2 尚未实现时内核返回 ``{"success": false, "error": "tool execution failed: ..."}``
  → 本类抛 ``ProviderUnavailable``，调用方明确得知服务未配置/不可达）
- args 形态：``{media_type: "image"|"tts"|"video"|"music", prompt|text: 主内容,
  provider?: 指定 provider, ...生成参数}``
- 成功响应形态（dict）：``{file_path, media_type, provider_name,
  metadata?, duration_seconds?}``
- 失败响应形态（dict）：``{success: false, error}``

产品决定：**迁移依赖，不降级空转**——调用失败/服务不可达时抛
``ProviderUnavailable`` 显式错误，绝不静默返回空结果。

本模块自包含（仅标准库），由 media 插件目录以平铺模块方式导入。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# capability_caller 类型：(method: str, params: dict) -> Awaitable[Any]
# （与 hindsight_memory/memory_backend.py 的 CapabilityCaller 对齐；
# 生产环境由插件把 tool-executor 能力句柄的 call 方法注入进来，测试传 AsyncMock）
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ProviderUnavailable(Exception):
    """媒体 Provider/后端服务不可用（F-MEDIA-2 显式错误）。

    与 0.1 的「Provider 未配置返回空/提示」的静默降级相反：调用方未注入
    capability_caller，或经 tool-executor.invoke 调用后端服务失败（服务未
    配置/不可达/返回失败）时抛出，让调用方明确知道服务不可用。
    """


class MediaType(str, Enum):
    """媒体类型枚举（与 0.1 tools.media.base.MediaType 对齐）。"""

    TTS = "tts"
    IMAGE = "image"
    VIDEO = "video"
    MUSIC = "music"


class FallbackStrategy(str, Enum):
    """Fallback 策略枚举（与 0.1 tools.media.fallback.FallbackStrategy 对齐）。"""

    SEQUENTIAL = "sequential"  # 按优先级顺序
    RANDOM = "random"  # 随机选择
    WEIGHTED = "weighted"  # 按权重选择


@dataclass
class MediaResult:
    """媒体生成结果（F-MEDIA-2 capability 路径统一形态）。

    字段与 0.1 MediaResult 对齐（file_path / media_type / provider_name /
    metadata / duration_seconds），供四工具成功分支统一消费。
    """

    file_path: str
    media_type: MediaType
    provider_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None


class MediaProviderClient:
    """媒体生成后端客户端——经 tool-executor capability 调用后端服务。

    F-MEDIA-2：0.1 的 provider 调用走进程内 service_provider 注册表直调；
    0.2 改为经 tool-executor.invoke 调用约定服务 ``media.generate``
    （契约见模块 docstring）。与 HindsightBackend（memory_backend.py）同款
    模式：唯一外部依赖是注入的 capability_caller，构造时传入，便于测试 mock。

    与 ProviderChain 语义的关键区别：调用失败**不降级空转**——抛
    ``ProviderUnavailable`` 显式错误（产品决定：迁移依赖而非优雅降级）。
    """

    SERVICE_TOOL_NAME = "media.generate"

    def __init__(self, capability_caller: CapabilityCaller) -> None:
        """初始化客户端。

        Args:
            capability_caller: 注入的能力调用 async 函数
                `(method, params) -> Any`（生产环境由插件传入 tool-executor
                能力句柄的 call 方法，测试传 AsyncMock）

        Raises:
            ValueError: capability_caller 为 None（必须注入，便于测试与解耦）。
        """
        if capability_caller is None:
            raise ValueError(
                "capability_caller 必须注入（生产环境由插件传入 tool-executor "
                "能力句柄的 call 方法）"
            )
        self._call = capability_caller

    async def execute_generate(
        self,
        media_type: MediaType | str,
        prompt: str,
        provider: str | None = None,
        **kwargs: Any,
    ) -> MediaResult:
        """执行生成（image/video/music），经 media.generate 服务契约。"""
        args: dict[str, Any] = {
            "media_type": _media_type_str(media_type),
            "prompt": prompt,
        }
        return await self._invoke(args, media_type, provider, kwargs)

    async def execute_synthesize(
        self,
        media_type: MediaType | str,
        text: str,
        provider: str | None = None,
        **kwargs: Any,
    ) -> MediaResult:
        """执行合成（tts），经 media.generate 服务契约（主内容键为 text）。"""
        args: dict[str, Any] = {
            "media_type": _media_type_str(media_type),
            "text": text,
        }
        return await self._invoke(args, media_type, provider, kwargs)

    async def _invoke(
        self,
        args: dict[str, Any],
        media_type: MediaType | str,
        provider: str | None,
        extra: dict[str, Any],
    ) -> MediaResult:
        """组装参数并调用 tool-executor.invoke；失败抛 ProviderUnavailable。"""
        if provider:
            args["provider"] = provider
        for key, value in extra.items():
            if value is not None:
                args[key] = value
        params = {"tool_name": self.SERVICE_TOOL_NAME, "args": args}
        try:
            result = await self._call("tool-executor.invoke", params)
        except ProviderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[MediaProviderClient] 媒体生成服务调用失败 | error=%s", e
            )
            raise ProviderUnavailable(
                f"媒体生成服务不可达（{self.SERVICE_TOOL_NAME}）: {e}"
            ) from e
        return self._map_result(result, media_type)

    @staticmethod
    def _map_result(
        result: Any, media_type: MediaType | str
    ) -> MediaResult:
        """把后端响应映射为统一 MediaResult；契约不满足/失败时抛异常。"""
        if isinstance(result, dict) and result.get("success") is False:
            error = result.get("error") or "媒体生成服务返回失败"
            raise ProviderUnavailable(
                f"媒体生成服务调用失败（{MediaProviderClient.SERVICE_TOOL_NAME}）: {error}"
            )
        if not isinstance(result, dict):
            raise ProviderUnavailable(
                f"媒体生成服务返回异常形态（{MediaProviderClient.SERVICE_TOOL_NAME}）: {result!r}"
            )
        file_path = result.get("file_path")
        if not file_path:
            raise ProviderUnavailable(
                f"媒体生成服务返回结果缺少 file_path（{MediaProviderClient.SERVICE_TOOL_NAME}）"
            )
        mt = result.get("media_type")
        try:
            media_type_enum = MediaType(mt) if mt else _to_media_type(media_type)
        except ValueError:
            raise ProviderUnavailable(
                f"媒体生成服务返回未知 media_type: {mt!r}"
            ) from None
        return MediaResult(
            file_path=str(file_path),
            media_type=media_type_enum,
            provider_name=str(result.get("provider_name") or "media"),
            metadata=dict(result.get("metadata") or {}),
            duration_seconds=result.get("duration_seconds"),
        )


def _media_type_str(media_type: MediaType | str) -> str:
    """MediaType 枚举 → 契约字符串。"""
    return media_type.value if isinstance(media_type, MediaType) else str(media_type)


def _to_media_type(media_type: MediaType | str) -> MediaType:
    """入参媒体类型归一为 MediaType 枚举。"""
    if isinstance(media_type, MediaType):
        return media_type
    return MediaType(str(media_type))


class ProviderChain:
    """按优先级顺序尝试 Provider 的 Fallback 链（0.2 最小实现）。

    语义与 0.1 tools.media.fallback.ProviderChain 对齐：
    依次尝试每个 Provider（先 ``is_available()`` 探测，异常视为不可用跳过），
    全部失败抛 ``RuntimeError``。

    Attributes:
        providers: Provider 列表（保持传入顺序）
        strategy: Fallback 策略
    """

    def __init__(
        self,
        providers: list[Any] | None = None,
        strategy: FallbackStrategy = FallbackStrategy.SEQUENTIAL,
    ) -> None:
        """初始化 Fallback 链。

        Args:
            providers: Provider 列表（duck-typing，需提供 is_available /
                execute_generate 或 execute_synthesize 方法）
            strategy: Fallback 策略
        """
        self._providers: list[Any] = list(providers or [])
        self._strategy = strategy

    @property
    def providers(self) -> list[Any]:
        """获取 Provider 列表。"""
        return self._providers

    @property
    def strategy(self) -> FallbackStrategy:
        """获取 Fallback 策略。"""
        return self._strategy

    async def execute_generate(self, prompt: str, **kwargs: Any) -> Any:
        """按 Fallback 策略执行生成（image/video/music）。"""
        return await self._execute("execute_generate", prompt, **kwargs)

    async def execute_synthesize(self, text: str, **kwargs: Any) -> Any:
        """按 Fallback 策略执行合成（tts）。"""
        return await self._execute("execute_synthesize", text, **kwargs)

    async def _execute(self, method_name: str, primary: str, **kwargs: Any) -> Any:
        """依次尝试每个 Provider，直到成功或全部失败。"""
        if not self._providers:
            raise RuntimeError("没有可用的 Provider")

        errors: list[str] = []
        for provider in self._providers:
            # 可用性探测（异常视为不可用，跳过——与 0.1 语义一致）
            try:
                available = await provider.is_available()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[ProviderChain] Provider '%s' 可用性检查异常: %s",
                    getattr(provider, "provider_name", "?"),
                    e,
                )
                available = False
            if not available:
                continue

            # 执行（失败则记录并尝试下一个）
            try:
                method = getattr(provider, method_name)
                return await method(primary, **kwargs)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{getattr(provider, 'provider_name', '?')}: {e}")
                logger.warning("[ProviderChain] Provider 执行失败: %s", errors[-1])

        detail = "; ".join(errors) if errors else "所有 Provider 不可用"
        raise RuntimeError(f"所有 Provider 均失败: {detail}")


class MediaProviderRegistry:
    """媒体 Provider 注册表（0.2 类型面）。

    0.1 tools.media.provider_registry.MediaProviderRegistry 完整实现未迁移；
    本类仅提供类型面与空默认行为（供类型注解 / duck-typing 参考）。
    运行时注册表实例由外部注入（工具构造参数，或未来 0.2 媒体插件），
    工具直接调用注入实例的同名方法（get / list_by_type / get_chain_for_type）。

    注（F-MEDIA-2）：0.2 主路径已改为经 capability 调用后端服务
    （MediaProviderClient）；本类型面保留用于注入式兼容与 schema 丰富。
    """

    def get(self, provider_name: str) -> Any:
        """按名称获取 Provider（无注入时恒 None）。"""
        return None

    def list_by_type(self, media_type: MediaType) -> list[Any]:
        """列出指定媒体类型的 Provider（无注入时恒空列表）。"""
        return []

    def get_chain_for_type(
        self,
        media_type: MediaType,
        strategy: FallbackStrategy = FallbackStrategy.SEQUENTIAL,
    ) -> ProviderChain | None:
        """获取指定媒体类型的 ProviderChain（无注入时恒 None）。"""
        return None


__all__ = [
    "CapabilityCaller",
    "FallbackStrategy",
    "MediaProviderClient",
    "MediaProviderRegistry",
    "MediaResult",
    "MediaType",
    "ProviderChain",
    "ProviderUnavailable",
]
