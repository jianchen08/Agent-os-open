"""
能力适配器基类

暴露接口：
- CapabilityAdapterBase：共享回退逻辑、MCP 连接管理、内容提取
"""

import json
import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.mcp_loader import MCPToolLoader
from tools.types import ToolExecutionResult, create_failure_result

from ._config import BackendConfig, CapabilityAdapterConfig

logger = logging.getLogger(__name__)


class CapabilityAdapterBase(BuiltinTool):
    """能力适配器基类，封装 MCP 后端回退逻辑。

    子类只需实现：
    - _adapter_name: str — 匹配 YAML 配置中的 key
    - get_tool_definition() -> Tool — 稳定接口定义
    - execute(inputs) -> ToolExecutionResult — 业务逻辑
    """

    _adapter_name: str = ""

    def _get_backends(self) -> list[BackendConfig]:
        """获取当前适配器的后端链（按优先级排序）"""
        config = CapabilityAdapterConfig.load()
        return config.get(self._adapter_name, [])

    async def _call_backend(
        self,
        backend: BackendConfig,
        mcp_tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """创建独立 loader，调用单个 MCP 工具，断开连接。"""
        if backend.server is None:
            raise ValueError(f"Backend '{backend.name}' has no server config")

        loader = MCPToolLoader()
        try:
            return await loader.call_tool(
                server_config=backend.server,
                tool_name=mcp_tool_name,
                arguments=arguments,
                timeout=backend.timeout,
                overall_timeout=backend.overall_timeout,
            )
        finally:
            await loader.disconnect_all()

    async def _call_backend_multi_step(
        self,
        backend: BackendConfig,
        steps: list[tuple[str, dict[str, Any]]],
    ) -> list[Any]:
        """多步 MCP 调用（保持同一连接），返回每步结果。"""
        if backend.server is None:
            raise ValueError(f"Backend '{backend.name}' has no server config")

        loader = MCPToolLoader()
        try:
            results = []
            for mcp_tool_name, arguments in steps:
                result = await loader.call_tool(
                    server_config=backend.server,
                    tool_name=mcp_tool_name,
                    arguments=arguments,
                    timeout=backend.timeout,
                    overall_timeout=backend.overall_timeout,
                )
                results.append(result)
            return results
        finally:
            await loader.disconnect_all()

    async def _call_with_fallback(
        self,
        mcp_tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[Any, BackendConfig]:
        """按优先级尝试后端，返回第一个成功的结果。"""
        backends = self._get_backends()
        if not backends:
            raise RuntimeError(
                f"适配器 '{self._adapter_name}' 无可用后端。请检查 config/tools/capability_adapters.yaml"
            )

        last_error: Exception | None = None
        for backend in backends:
            if not backend.available:
                continue
            try:
                result = await self._call_backend(backend, mcp_tool_name, arguments)
                return result, backend
            except Exception as e:
                logger.warning(
                    "[CapabilityAdapter] 后端 '%s' 失败 | adapter=%s | error=%s",
                    backend.name,
                    self._adapter_name,
                    e,
                )
                last_error = e

        raise RuntimeError(f"适配器 '{self._adapter_name}' 所有后端均失败: {last_error}")

    @staticmethod
    def _extract_mcp_content(result: Any) -> Any:
        """从 MCP 标准返回格式中提取实际数据。"""
        if not isinstance(result, dict):
            return result

        if result.get("isError"):
            content_list = result.get("content", [])
            error_texts = []
            for item in content_list:
                if isinstance(item, dict) and item.get("type") == "text":
                    error_texts.append(item.get("text", ""))
            error_msg = "\n".join(error_texts).strip() or "MCP 后端返回未知错误"
            return {"error": True, "message": error_msg}

        content_list = result.get("content", [])
        if content_list and isinstance(content_list, list):
            texts = []
            for item in content_list:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))

            combined = "\n".join(texts).strip()
            if combined:
                try:
                    return json.loads(combined)
                except (json.JSONDecodeError, ValueError):
                    return combined

        return result

    def _fail_no_backends(self) -> ToolExecutionResult:
        """返回"无可用后端"的标准错误。"""
        return create_failure_result(
            error=(
                f"适配器 '{self._adapter_name}' 无可用后端。请在 config/tools/capability_adapters.yaml 中配置后端。"
            ),
            error_code="NO_BACKEND_CONFIGURED",
        )
