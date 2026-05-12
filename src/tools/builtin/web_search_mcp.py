"""
Network Search Tool (Based on Web Search MCP)

Uses Web Search MCP server for network search, providing:
- Multi-engine search (Bing > Brave > DuckDuckGo)
- Auto dedup and noise filtering (URL dedup, content similarity, domain filter)
- Concurrent content extraction
- Smart request strategy (browser priority, axios fallback)
"""

from dataclasses import dataclass, field
from typing import Any

from src.tools.mcp_loader import MCPServerConfig, MCPToolLoader
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


@dataclass
class WebSearchMCPConfig:
    """Network Search MCP Config"""

    # Web Search MCP server path
    mcp_server_path: str = "D:/Jianguoyun/Agent/web-search-mcp/dist/index.js"

    # Search parameters
    max_results: int = 10
    timeout: int = 30

    # Result processing config (maintain compatibility with original web_search.py)
    enable_dedup: bool = True
    enable_filter: bool = True
    enable_ranking: bool = True
    similarity_threshold: float = 0.85

    # Filter config
    blocked_domains: list[str] = field(default_factory=list)
    blocked_keywords: list[str] = field(default_factory=list)


class WebSearchMCPTool:
    """
    Network Search Tool (Based on Web Search MCP)

    Features:
    - Uses Web Search MCP server for search
    - Supports multiple search modes (full search, summary, content extraction)
    - Auto dedup and noise filtering (built-in to Web Search MCP)
    - Result quality scoring
    """

    def __init__(self, config: WebSearchMCPConfig | None = None):
        """
        Initialize search tool

        Args:
            config: Search config
        """
        self.config = config or WebSearchMCPConfig()
        self.loader = MCPToolLoader()
        self._mcp_client: Any = None

    async def _get_mcp_client(self):
        """
        Get MCP client (lazy initialization)
        """
        if self._mcp_client is None:
            # Configure Web Search MCP server
            server_config = MCPServerConfig(
                name="web-search",
                command="node",
                args=[self.config.mcp_server_path],
                env={
                    "MAX_CONTENT_LENGTH": "500000",
                    "DEFAULT_TIMEOUT": str(self.config.timeout * 1000),
                    "MAX_BROWSERS": "3",
                    "ENABLE_RELEVANCE_CHECKING": "true"
                    if self.config.enable_ranking
                    else "false",
                    "RELEVANCE_THRESHOLD": str(
                        0.3 if self.config.enable_ranking else 0.0
                    ),
                    "FORCE_MULTI_ENGINE_SEARCH": "false",
                    "BROWSER_HEADLESS": "true",
                },
            )

            # Connect to MCP server
            self._mcp_client = await self.loader._connect_server(server_config)

        return self._mcp_client

    async def cleanup(self):
        """Cleanup resources"""
        if self._mcp_client and hasattr(self._mcp_client, "close"):
            try:
                await self._mcp_client.close()
            except Exception:
                pass
            self._mcp_client = None

    @staticmethod
    def get_tool_definition() -> Tool:
        """Get tool definition"""
        return Tool(
            name="web_search",
            description="搜索互联网信息，基于Web Search MCP实现。支持多引擎搜索（Bing/Brave/DuckDuckGo），自动去重和噪声过滤，返回高质量结果。提供三种搜索模式：完整搜索（获取摘要和页面内容）、摘要模式（仅获取搜索结果摘要）、内容提取（提取指定URL的页面内容）。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。在full和summary模式下为搜索查询词，在content_only模式下为要提取内容的URL地址",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数量，范围1-10，默认为10",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 10,
                    },
                    "search_mode": {
                        "type": "string",
                        "description": "搜索模式：full=完整搜索（获取搜索结果并提取页面内容），summary=摘要模式（仅获取搜索结果摘要），content_only=内容提取（将query作为URL提取页面内容）",
                        "enum": ["full", "summary", "content_only"],
                        "default": "full",
                    },
                },
                "required": ["query"],
            },
            source=ToolSource.MCP,
            category=ToolCategory.SEARCH,
            level=ToolLevel.USER,
            requires_approval=False,
            tags=["web", "search", "internet", "mcp"],
            metadata={
                "backend": "web-search-mcp",
                "version": "0.3.2",
                "features": [
                    "multi_engine_search",
                    "url_dedup",
                    "content_dedup",
                    "domain_filter",
                    "noise_filter",
                    "concurrent_extraction",
                    "playwright_browser",
                ],
            },
            isolation_required=False,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """
        Execute search

        Args:
            inputs: Input parameters

        Returns:
            Search results
        """
        query = inputs.get("query", "").strip()
        if not query:
            return create_failure_result(
                error="Search keyword cannot be empty",
                error_code="EMPTY_QUERY",
            )

        max_results = inputs.get("max_results", self.config.max_results)
        search_mode = inputs.get("search_mode", "full")

        # Limit max results
        max_results = min(max(max_results, 1), 10)

        try:
            client = await self._get_mcp_client()

            # Select tool based on search mode
            if search_mode == "full":
                # Use full-web-search tool (full search + content extraction)
                result = await client.call_tool(
                    "full-web-search",
                    {
                        "query": query,
                        "limit": max_results,
                    },
                )
            elif search_mode == "summary":
                # Use get-web-search-summaries tool (summary only)
                result = await client.call_tool(
                    "get-web-search-summaries",
                    {
                        "query": query,
                        "limit": max_results,
                    },
                )
            elif search_mode == "content_only":
                # Use get-single-web-page-content tool (content extraction)
                result = await client.call_tool(
                    "get-single-web-page-content",
                    {
                        "url": query,  # query as URL
                    },
                )
            else:
                return create_failure_result(
                    error=f"Unsupported search mode: {search_mode}",
                    error_code="INVALID_MODE",
                )

            # Parse MCP return result
            if isinstance(result, dict):
                # Web Search MCP returns structured result
                search_results = self._parse_mcp_result(result, search_mode)
            elif isinstance(result, str):
                # Some cases return string
                search_results = {
                    "query": query,
                    "results": [],
                    "total": 0,
                    "raw_result": result,
                    "mode": search_mode,
                }
            else:
                search_results = {
                    "query": query,
                    "results": [],
                    "total": 0,
                    "raw_result": str(result),
                    "mode": search_mode,
                }

            return create_success_result(
                data=search_results,
                metadata={
                    "backend": "web-search-mcp",
                    "search_mode": search_mode,
                    "mcp_server": "web-search",
                },
            )

        except Exception as e:
            import logging

            logging.exception("Web Search MCP call failed")
            return create_failure_result(
                error=f"Search failed: {str(e)}",
                error_code="MCP_CALL_FAILED",
            )

    def _parse_mcp_result(
        self, result: dict[str, Any], search_mode: str
    ) -> dict[str, Any]:
        """
        Parse Web Search MCP return result

        Args:
            result: MCP return raw result
            search_mode: Search mode

        Returns:
            Formatted search results
        """
        # Try to parse different result formats
        if "results" in result:
            # Standard search result format
            raw_results = result.get("results", [])
        elif "pages" in result:
            # Content extraction result format
            raw_results = result.get("pages", [])
        elif "summaries" in result:
            # Summary result format
            raw_results = result.get("summaries", [])
        elif "content" in result:
            # Single page content format
            raw_results = [{"content": result.get("content", "")}]
        else:
            # Unknown format, try to extract result
            raw_results = []

        # Convert to unified format
        formatted_results = []
        for i, item in enumerate(raw_results):
            if search_mode == "content_only" and "content" in item:
                formatted_results.append(
                    {
                        "url": item.get("url", ""),
                        "title": "Extracted Content",
                        "snippet": item.get("content", "")[:500],
                        "source": "web-search-mcp",
                        "index": i,
                    }
                )
            elif isinstance(item, dict):
                formatted_results.append(
                    {
                        "url": item.get("url", ""),
                        "title": item.get("title", item.get("name", "")),
                        "snippet": item.get(
                            "snippet", item.get("description", item.get("content", ""))
                        )[:500],
                        "source": "web-search-mcp",
                        "index": i,
                        "metadata": item.get("metadata", {}),
                    }
                )
            elif isinstance(item, str):
                formatted_results.append(
                    {
                        "url": "",
                        "title": f"Result {i + 1}",
                        "snippet": item[:500],
                        "source": "web-search-mcp",
                        "index": i,
                    }
                )

        return {
            "query": result.get("query", ""),
            "results": formatted_results,
            "total": len(formatted_results),
            "raw_total": len(raw_results),
            "processed": True,  # Web Search MCP already has built-in processing
            "mode": search_mode,
        }


# Convenience function
async def web_search_mcp(
    query: str,
    max_results: int = 10,
    search_mode: str = "full",
) -> dict[str, Any]:
    """
    Network search convenience function (based on Web Search MCP)

    Args:
        query: Search keyword
        max_results: Max result count
        search_mode: Search mode (full/summary/content_only)

    Returns:
        Search results
    """
    config = WebSearchMCPConfig(
        max_results=max_results,
    )

    tool = WebSearchMCPTool(config)
    try:
        result = await tool.execute(
            {
                "query": query,
                "max_results": max_results,
                "search_mode": search_mode,
            }
        )
        return result.data if result.success else {"error": result.error}
    finally:
        await tool.cleanup()
