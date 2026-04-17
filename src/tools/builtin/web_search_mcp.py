"""
Network Search Tool (Based on mcp-webgate)

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- WebSearchMCPConfig：WebSearchMCPConfig类
- WebSearchMCPTool：WebSearchMCPTool类

后端：mcp-webgate（Python MCP 服务器）
特性：BM25 重排序、HTML 去噪、URL 去重、上下文保护、纯 HTTP 抓取（无浏览器依赖）
"""

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from tools.mcp_loader import MCPServerConfig, MCPToolLoader
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


def _get_webgate_command() -> tuple[str, list[str]]:
    """获取 mcp-webgate 启动命令，优先 uvx，回退 pip 安装的模块"""
    if shutil.which("mcp-webgate"):
        return ("mcp-webgate", [])
    if shutil.which("uvx"):
        return ("uvx", ["mcp-webgate"])
    return (os.sys.executable or "python", ["-m", "mcp_webgate"])


@dataclass
class WebSearchMCPConfig:
    """Network Search MCP Config (mcp-webgate)"""

    max_results: int = 10
    timeout: int = 30

    enable_dedup: bool = True
    enable_filter: bool = True
    enable_ranking: bool = True
    similarity_threshold: float = 0.85

    blocked_domains: list[str] = field(default_factory=list)
    blocked_keywords: list[str] = field(default_factory=list)

    searxng_url: str = field(
        default_factory=lambda: os.environ.get(
            "WEBGATE_SEARXNG_URL", "http://localhost:8080"
        )
    )

    max_query_budget: int = 32000
    max_result_length: int = 8000
    search_timeout: int = 8


class WebSearchMCPTool:
    """
    Network Search Tool (Based on mcp-webgate)

    mcp-webgate 核心能力：
    - BM25 关键词重排序（始终启用，零成本）
    - HTML 去噪：移除菜单、脚本、广告、页脚，只保留正文
    - URL 去重 + 二进制文件过滤
    - 上下文保护：硬限制输出大小，防止上下文洪泛
    - 并行抓取 + 指数退避重试
    - 纯 HTTP 抓取，无 Playwright 浏览器依赖

    工具映射：
    - full/search 模式 → webgate_query（搜索+抓取+清洗+BM25排序）
    - content_only 模式 → webgate_fetch（单页抓取+清洗）
    """

    def __init__(self, config: WebSearchMCPConfig | None = None):
        """初始化搜索工具"""
        self.config = config or WebSearchMCPConfig()
        self.loader = MCPToolLoader()

    def _build_server_config(self) -> MCPServerConfig:
        """构建 MCP 服务器配置"""
        cmd, base_args = _get_webgate_command()

        env_vars = {
            "WEBGATE_DEFAULT_BACKEND": "searxng",
            "WEBGATE_SEARXNG_URL": self.config.searxng_url,
            "WEBGATE_MAX_QUERY_BUDGET": str(self.config.max_query_budget),
            "WEBGATE_MAX_RESULT_LENGTH": str(self.config.max_result_length),
            "WEBGATE_RESULTS_PER_QUERY": str(self.config.max_results),
            "WEBGATE_SEARCH_TIMEOUT": str(self.config.search_timeout),
            "WEBGATE_MAX_TOTAL_RESULTS": "20",
            "WEBGATE_OVERSAMPLING_FACTOR": "2",
        }

        if self.config.blocked_domains:
            env_vars["WEBGATE_BLOCKED_DOMAINS"] = ",".join(self.config.blocked_domains)

        return MCPServerConfig(
            name="webgate",
            command=cmd,
            args=base_args,
            env=env_vars,
        )

    async def cleanup(self):
        """清理资源"""
        await self.loader.disconnect_all()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="web_search",
            description="搜索互联网信息，基于 mcp-webgate 实现。支持多引擎搜索（SearXNG 聚合），"
            "自动 BM25 重排序、HTML 去噪、URL 去重，返回高质量结果。"
            "提供三种搜索模式：完整搜索（搜索+抓取+清洗+排序）、"
            "摘要模式（仅搜索结果摘要）、内容提取（提取指定 URL 的页面内容）。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。在 full 和 summary 模式下为搜索查询词，"
                        "在 content_only 模式下为要提取内容的 URL 地址",
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
                        "description": "搜索模式：full=完整搜索（搜索+抓取+清洗+BM25排序），"
                        "summary=摘要模式（仅获取搜索结果摘要），"
                        "content_only=内容提取（将 query 作为 URL 提取页面内容）",
                        "enum": ["full", "summary", "content_only"],
                        "default": "full",
                    },
                },
                "required": ["query"],
            },
            source=ToolSource.MCP,
            category=ToolCategory.SEARCH,
            level=ToolLevel.USER,
            tags=["web", "search", "internet", "mcp"],
            metadata={
                "backend": "mcp-webgate",
                "version": "0.1.0",
                "features": [
                    "bm25_reranking",
                    "html_denoising",
                    "url_dedup",
                    "domain_filter",
                    "context_protection",
                    "parallel_fetch",
                    "http_only",
                ],
            },
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行搜索"""
        query = inputs.get("query", "").strip()
        if not query:
            return create_failure_result(
                error="Search keyword cannot be empty",
                error_code="EMPTY_QUERY",
            )

        max_results = inputs.get("max_results", self.config.max_results)
        search_mode = inputs.get("search_mode", "full")
        max_results = min(max(max_results, 1), 10)

        server_config = self._build_server_config()

        try:
            try:

                async def _call_mcp_tool() -> Any:
                    """通过 MCPToolLoader 统一调用（自动检测连接 + 失败重连）"""
                    if search_mode in ("full", "summary"):
                        num_results = (
                            max_results
                            if search_mode == "full"
                            else min(max_results, 5)
                        )
                        return await self.loader.call_tool(
                            server_config,
                            "webgate_query",
                            {
                                "queries": query,
                                "num_results_per_query": num_results,
                            },
                            timeout=60.0,
                        )
                    elif search_mode == "content_only":
                        return await self.loader.call_tool(
                            server_config,
                            "webgate_fetch",
                            {
                                "url": query,
                                "max_chars": self.config.max_query_budget,
                            },
                            timeout=60.0,
                        )
                    else:
                        return None

                result = await asyncio.wait_for(_call_mcp_tool(), timeout=90.0)

                if result is None:
                    return create_failure_result(
                        error=f"Unsupported search mode: {search_mode}",
                        error_code="INVALID_MODE",
                    )
            except TimeoutError:
                return create_failure_result(
                    error=f"Web 搜索超时（90秒），MCP 服务器可能无响应 | query={query}",
                    error_code="MCP_TIMEOUT",
                )

            parsed_result = self._extract_mcp_content(result)
            if isinstance(parsed_result, dict):
                search_results = self._parse_webgate_result(
                    parsed_result, query, search_mode
                )
            else:
                search_results = {
                    "query": query,
                    "results": [],
                    "total": 0,
                    "raw_result": str(parsed_result),
                    "mode": search_mode,
                }

            return create_success_result(
                data=search_results,
                metadata={
                    "backend": "mcp-webgate",
                    "search_mode": search_mode,
                    "mcp_server": "webgate",
                },
            )

        except Exception as e:
            import logging

            logging.getLogger(__name__).exception("Web Search MCP call failed")

            error_msg = str(e)
            if "MCP_CONNECTION_ERROR" in error_msg or "连接失败" in error_msg:
                error_msg = (
                    f"MCP 服务器连接失败，请检查：\n"
                    f"1. uvx 是否已安装（pip install uv）\n"
                    f"2. mcp-webgate 是否可用（uvx mcp-webgate --help）\n"
                    f"3. SearXNG 是否运行（{self.config.searxng_url}）\n"
                    f"原始错误: {str(e)}"
                )
            elif "MCP 初始化失败" in error_msg:
                error_msg = (
                    f"MCP 服务器初始化失败，可能是协议版本不兼容\n"
                    f"原始错误: {str(e)}"
                )

            return create_failure_result(
                error=f"Search failed: {error_msg}",
                error_code="MCP_CALL_FAILED",
            )

    @staticmethod
    def _extract_mcp_content(result: Any) -> Any:
        """从 MCP 标准返回格式中提取实际数据"""
        import json

        if not isinstance(result, dict):
            return result

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

    def _parse_webgate_result(
        self, result: dict[str, Any], query: str, search_mode: str
    ) -> dict[str, Any]:
        """解析 mcp-webgate 返回结果"""
        formatted_results = []

        if search_mode == "content_only":
            url = result.get("url", query)
            title = result.get("title", "")
            text = result.get("text", "")
            char_count = result.get("char_count", len(text))
            truncated = result.get("truncated", False)

            return {
                "query": query,
                "results": [
                    {
                        "url": url,
                        "title": title,
                        "snippet": text[:500],
                        "content": text,
                        "char_count": char_count,
                        "truncated": truncated,
                        "source": "mcp-webgate",
                        "index": 0,
                    }
                ],
                "total": 1,
                "raw_total": 1,
                "processed": True,
                "mode": search_mode,
            }

        if "summary" in result:
            summary = result.get("summary", "")
            citations = result.get("citations", [])
            stats = result.get("stats", {})

            citation_results = []
            for i, cit in enumerate(citations):
                citation_results.append(
                    {
                        "url": cit.get("url", ""),
                        "title": cit.get("title", ""),
                        "snippet": summary[:500] if i == 0 else cit.get("title", ""),
                        "source": "mcp-webgate",
                        "index": i,
                    }
                )

            return {
                "query": query,
                "results": citation_results,
                "total": len(citation_results),
                "raw_total": stats.get("fetched", 0),
                "processed": True,
                "mode": search_mode,
            }

        sources = result.get("sources", [])
        for i, source in enumerate(sources):
            if isinstance(source, dict):
                content = source.get("content", "")
                formatted_results.append(
                    {
                        "url": source.get("url", ""),
                        "title": source.get("title", ""),
                        "snippet": (
                            content[:500] if content else source.get("title", "")
                        ),
                        "content": content,
                        "truncated": source.get("truncated", False),
                        "source": "mcp-webgate",
                        "index": i,
                    }
                )

        snippet_pool = result.get("snippet_pool", [])
        for i, snip in enumerate(snippet_pool):
            if isinstance(snip, dict):
                formatted_results.append(
                    {
                        "url": snip.get("url", ""),
                        "title": snip.get("title", ""),
                        "snippet": snip.get("snippet", "")[:500],
                        "source": "mcp-webgate-snippet",
                        "index": len(sources) + i,
                    }
                )

        stats = result.get("stats", {})

        return {
            "query": query,
            "results": formatted_results,
            "total": len(formatted_results),
            "raw_total": stats.get("fetched", len(sources)),
            "processed": True,
            "mode": search_mode,
            "stats": stats,
        }


async def web_search_mcp(
    query: str,
    max_results: int = 10,
    search_mode: str = "full",
) -> dict[str, Any]:
    """Network search convenience function (based on mcp-webgate)"""
    config = WebSearchMCPConfig(max_results=max_results)

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
