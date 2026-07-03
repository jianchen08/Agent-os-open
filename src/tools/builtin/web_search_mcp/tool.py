"""Network Search Tool (Based on mcp-webgate)"""

import importlib.util
import logging
import sys
from dataclasses import dataclass
from typing import Any

from tools.builtin.base import BuiltinTool
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

logger = logging.getLogger(__name__)


def _resolve_bing_server():
    """向上搜索找到 mcp-servers/bing-search/server.py"""
    from pathlib import Path as _P  # noqa: N814,PLC0415

    # 1. 从当前模块向上搜索（适用于宿主机和容器）
    try:
        here = _P(__file__).resolve()
        for _ in range(10):
            here = here.parent
            p = here / "mcp-servers" / "bing-search" / "server.py"
            if p.exists():
                return str(p)
    except Exception:
        pass
    # 2. 容器内固定路径（/app/mcp-servers/ 由 docker-compose volume 挂载）
    p = _P("/app/mcp-servers/bing-search/server.py")
    if p.exists():
        return str(p)
    # 3. CWD 相对路径
    p = _P("mcp-servers/bing-search/server.py")
    if p.exists():
        return str(p)
    return None


def _get_search_command() -> tuple[str, list[str]]:
    """获取搜索引擎 MCP server 启动命令。"""
    import shutil  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: N814,PLC0415

    # 容器内全局安装路径(优先,所有用户可访问)
    global_bin = "/usr/local/bin/mcp-webgate"
    if _P(global_bin).exists():
        return (global_bin, [])

    # 容器内 pip --user 安装路径(后退)
    user_bin = "/home/appuser/.local/bin/mcp-webgate"
    if _P(user_bin).exists():
        return (user_bin, [])

    # 宿主机: python -m (需要有 __main__.py, 3.14 可行)
    if importlib.util.find_spec("mcp_webgate") is not None:
        return (sys.executable, ["-m", "mcp_webgate"])

    # PATH 上的 mcp-webgate
    if shutil.which("mcp-webgate"):
        return ("mcp-webgate", [])

    # 兜底: bing-search
    bing = _resolve_bing_server()
    if bing is not None:
        return (sys.executable, [bing])

    return (sys.executable, ["-m", "mcp_webgate"])


@dataclass
class WebSearchMCPConfig:
    """搜索 MCP 配置"""

    max_results: int = 10
    mcp_overall_timeout: float = 90.0
    fetch_timeout: int = 8
    searxng_url: str = "http://localhost:8080"


class WebSearchMCPTool(BuiltinTool):
    """网络搜索工具。"""

    def __init__(self, config: WebSearchMCPConfig | None = None):
        """初始化搜索工具"""
        self.config = config or WebSearchMCPConfig()

    def _build_server_config(self) -> MCPServerConfig:
        """构建 MCP 服务器配置，自动适配容器/宿主机环境"""
        cmd, args = _get_search_command()
        # 容器内用 Docker 服务名，宿主机用 localhost
        bing = _resolve_bing_server()
        searxng_host = "searxng" if (bing and bing.startswith("/app/")) else "localhost"
        searxng_url = f"http://{searxng_host}:8080"
        env_vars = {
            "WEBGATE_DEFAULT_BACKEND": "searxng",
            "WEBGATE_SEARXNG_URL": searxng_url,
            "WEBGATE_SEARCH_TIMEOUT": str(self.config.fetch_timeout),
            "WEBGATE_RESULTS_PER_QUERY": str(self.config.max_results),
            "WEBGATE_MAX_QUERY_BUDGET": "32000",
        }
        return MCPServerConfig(name="web-search", command=cmd, args=args, env=env_vars)

    async def cleanup(self):
        """清理资源（保留接口兼容，不再持有共享 loader）"""
        pass

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
                error="搜索关键词不能为空",  # 统一中文错误信息
                error_code="EMPTY_QUERY",
            )

        max_results = inputs.get("max_results", self.config.max_results)
        # 安全值转换：LLM 可能传字符串类型
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = self.config.max_results
        max_results = min(max(max_results, 1), 10)
        search_mode = str(inputs.get("search_mode", "full"))

        server_config = self._build_server_config()

        loader = MCPToolLoader()
        try:
            return await self._do_search(loader, server_config, query, max_results, search_mode)
        finally:
            await loader.disconnect_all()

    async def _do_search(  # noqa: PLR0911,PLR0912
        self,
        loader: MCPToolLoader,
        server_config: MCPServerConfig,
        query: str,
        max_results: int,
        search_mode: str,
    ) -> ToolResult:
        """执行搜索，mcp-webgate 失败时自动降级到 bing-search"""
        import json  # noqa: F401,PLC0415
        import traceback  # noqa: F401,PLC0415

        is_webgate = "mcp_webgate" in str(server_config.args) or "mcp-webgate" in str(server_config.command)
        original_backend = "mcp-webgate" if is_webgate else "bing-search"

        try:
            if is_webgate:
                if search_mode in ("full", "summary"):
                    tool_name = "webgate_query"
                    mcp_args = {"queries": query, "num_results_per_query": max_results}
                elif search_mode == "content_only":
                    tool_name = "webgate_fetch"
                    mcp_args = {"url": query}
                else:
                    return create_failure_result(error=f"不支持的模式: {search_mode}", error_code="INVALID_MODE")
            else:
                if search_mode == "content_only":
                    return create_failure_result(
                        error="content_only 需要 mcp-webgate 后端（pip install mcp-webgate + Docker SearXNG）",
                        error_code="UNSUPPORTED_MODE",
                    )
                tool_name = "web_search"
                mcp_args = {"query": query, "max_results": max_results}

            result = await loader.call_tool(
                server_config,
                tool_name,
                mcp_args,
                timeout=60.0,
                overall_timeout=self.config.mcp_overall_timeout,
            )
            parsed = self._extract_mcp_content(result)

            if is_webgate and isinstance(parsed, dict):
                search_results = self._parse_webgate_minimal(parsed, query, search_mode)
                # 后端不可用检测：fetched=0 且 failed=0 → SearXNG 没响应
                stats = search_results.get("stats", {})
                if search_results.get("total", 0) == 0 and stats.get("fetched", 0) == 0 and stats.get("failed", 0) == 0:
                    logger.warning("mcp-webgate 后端无响应（fetched=0 failed=0），降级到 bing-search")
                    return await self._fallback_bing_search(loader, query, max_results, search_mode)
            elif isinstance(parsed, dict):
                search_results = {
                    "query": parsed.get("query", query),
                    "results": parsed.get("results", []),
                    "total": parsed.get("total", 0),
                    "mode": search_mode,
                }
            else:
                search_results = {"query": query, "results": [], "total": 0, "mode": search_mode}

            if search_results.get("total", 0) == 0:
                return create_failure_result(error=f"未找到结果（关键词: {query}）", error_code="NO_RESULTS")

            return create_success_result(
                data=search_results,
                metadata={"backend": original_backend, "search_mode": search_mode},
            )

        except Exception as e:
            logger.warning(f"搜索失败 | backend={original_backend} | query={query[:60]} | error={e}")
            # ── 自动降级：mcp-webgate 挂了就用 bing-search ──
            if is_webgate:
                logger.info("mcp-webgate 不可用，降级到 bing-search")
                return await self._fallback_bing_search(loader, query, max_results, search_mode)
            return create_failure_result(error=f"搜索失败: {e}", error_code="SEARCH_FAILED")

    async def _fallback_bing_search(
        self,
        loader: MCPToolLoader,
        query: str,
        max_results: int,
        search_mode: str,
    ) -> ToolResult:
        """降级到 bing-search（纯 Python，零依赖）"""
        # 向上搜索项目根，找到 mcp-servers/bing-search/server.py
        server_py = _resolve_bing_server()
        if server_py is None:
            return create_failure_result(error="[fallback] bing-search server.py 未找到", error_code="FALLBACK_MISSING")

        fallback_cfg = MCPServerConfig(name="bing-fallback", command=sys.executable, args=[server_py])
        try:
            result = await loader.call_tool(
                fallback_cfg,
                "web_search",
                {"query": query, "max_results": max_results},
                timeout=30.0,
                overall_timeout=45.0,
            )
            parsed = self._extract_mcp_content(result)
            if isinstance(parsed, dict):
                search_results = {
                    "query": parsed.get("query", query),
                    "results": parsed.get("results", []),
                    "total": parsed.get("total", 0),
                    "mode": search_mode,
                }
            else:
                search_results = {"query": query, "results": [], "total": 0, "mode": search_mode}

            if search_results.get("total", 0) == 0:
                return create_failure_result(error=f"[fallback] 未找到结果（关键词: {query}）", error_code="NO_RESULTS")
            return create_success_result(
                data=search_results,
                metadata={"backend": "bing-search(fallback)", "search_mode": search_mode},
            )
        except Exception as e2:
            logger.exception("bing-search 降级也失败")
            # 携带 error 类型名，便于区分 UnicodeDecodeError（编码）/ TimeoutError
            # （超时）/ MCPConnectionError（子进程）等不同根因
            err_type = type(e2).__name__
            return create_failure_result(
                error=f"[fallback] 搜索失败 ({err_type}): {e2}",
                error_code="SEARCH_FAILED",
            )

    # ── 精简版 webgate 结果解析（仅提取 sources + snippets → 统一 results 格式）──

    @staticmethod
    def _parse_webgate_minimal(result: dict[str, Any], query: str, mode: str) -> dict[str, Any]:
        """将 mcp-webgate 的 {sources, snippet_pool, stats} 转统一格式"""
        results: list[dict[str, Any]] = []
        # sources（已抓取+清洗的页面）
        for i, src in enumerate(result.get("sources", []) or []):
            content = src.get("content", "")
            results.append(
                {
                    "title": src.get("title", ""),
                    "url": src.get("url", ""),
                    "snippet": src.get("snippet", content[:200] if content else ""),
                    "content": content if not src.get("truncated") else content + "...",
                    "index": i,
                }
            )
        # snippet_pool（未抓取的补充摘要）
        offset = len(results)
        for i, snip in enumerate(result.get("snippet_pool", []) or []):
            results.append(
                {
                    "title": snip.get("title", ""),
                    "url": snip.get("url", ""),
                    "snippet": snip.get("snippet", ""),
                    "index": offset + i,
                }
            )
        return {
            "query": result.get("queries", query) if isinstance(result.get("queries"), str) else query,
            "results": results,
            "total": len(results),
            "mode": mode,
            "stats": result.get("stats", {}),
        }

    @staticmethod
    def _extract_mcp_content(result: Any) -> Any:
        """从 MCP 标准返回格式中提取实际数据"""
        import json  # noqa: PLC0415

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

    @staticmethod
    def _smart_truncate(text: str, max_len: int = 500) -> str:
        """智能截断文本，在最近的句子/标点处截断"""
        if len(text) <= max_len:
            return text
        truncated = text[:max_len]
        for sep in ("。", ".", "！", "!", "\n", "；", ";"):
            last = truncated.rfind(sep)
            if last > max_len * 0.5:
                return truncated[: last + 1] + "..."
        return truncated + "..."


async def web_search_mcp(
    query: str,
    max_results: int = 10,
    search_mode: str = "full",
) -> dict[str, Any]:
    """Network search convenience function (based on mcp-webgate)"""
    config = WebSearchMCPConfig(max_results=max_results)

    tool = WebSearchMCPTool(config)
    result = await tool.execute(
        {
            "query": query,
            "max_results": max_results,
            "search_mode": search_mode,
        }
    )
    return result.output if result.success else {"error": result.error}
