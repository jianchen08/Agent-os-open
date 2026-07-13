"""
平台适配器模块

提供外部资源平台的适配器实现，每个适配器封装特定平台的搜索 API。
"""

from tools.builtin.platform_adapters.langchain_hub_adapter import LangChainHubAdapter
from tools.builtin.platform_adapters.mcp_registry_adapter import MCPRegistryAdapter
from tools.builtin.platform_adapters.smithery_adapter import SmitheryAdapter

# 平台名称到适配器类的映射，供配置动态加载使用
PLATFORM_ADAPTER_MAP: dict[str, type] = {
    "mcp_registry": MCPRegistryAdapter,
    "smithery": SmitheryAdapter,
    "langchain_hub": LangChainHubAdapter,
}

__all__ = [
    "MCPRegistryAdapter",
    "SmitheryAdapter",
    "LangChainHubAdapter",
    "PLATFORM_ADAPTER_MAP",
]
