"""
MCP Registry 平台适配器

通过 MCP 官方注册表 API 搜索已注册的 MCP Server。
API 端点: GET https://registry.modelcontextprotocol.io/v0/servers?search={query}
文档: https://modelcontextprotocol.info/tools/registry/
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from tools.builtin.external_resource_search import PlatformAdapter

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"
_TIMEOUT = 10


class MCPRegistryAdapter(PlatformAdapter):
    """
    MCP Registry 平台适配器

    通过 MCP 官方注册表 REST API 搜索已注册的 MCP Server。
    API 使用 cursor 分页，搜索参数为 search。

    Args:
        base_url: API 基地址
    """

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        """初始化 MCP Registry 适配器"""
        self._base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        搜索 MCP Registry 中的 Server 资源

        Args:
            query: 搜索关键词
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            标准化资源列表
        """
        url = f"{self._base_url}/v0/servers"
        params: dict[str, str | int] = {"search": query, "limit": limit}

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.warning(
                        "[mcp_registry] 搜索请求失败: status=%d url=%s",
                        resp.status,
                        resp.url,
                    )
                    return []

                body = await resp.json()

        except aiohttp.ClientError as e:
            logger.warning("[mcp_registry] 网络请求异常: %s", e)
            return []
        except Exception as e:
            logger.warning("[mcp_registry] 未知异常: %s", e)
            return []

        servers = self._extract_servers(body)
        if not servers:
            logger.debug(
                "[mcp_registry] 未找到 servers 列表，原始响应 keys: %s",
                list(body.keys()) if isinstance(body, dict) else type(body).__name__,
            )
            return []

        results: list[dict[str, Any]] = []
        for server in servers[:limit]:
            parsed = self._parse_server(server)
            if parsed:
                results.append(parsed)

        logger.info(
            "[mcp_registry] 搜索完成: query=%s 返回 %d 条结果",
            query,
            len(results),
        )
        return results

    def _extract_servers(self, body: Any) -> list[dict[str, Any]]:
        """
        从 API 响应体中提取 servers 列表

        MCP Registry 返回 {"servers": [...], "metadata": {...}}

        Args:
            body: API 响应体

        Returns:
            server 字典列表
        """
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("servers", "data", "results", "items"):
                val = body.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _parse_server(self, server: dict[str, Any]) -> dict[str, Any] | None:
        """
        解析单个 server 条目为标准化资源格式

        MCP Registry v0 响应格式：
        { "server": { "name": "...", "description": "..." }, "_meta": {...} }

        Args:
            server: 原始 server 数据

        Returns:
            标准化资源字典，解析失败返回 None
        """
        server_data = server.get("server", server)

        name = server_data.get("name") or server_data.get("id", "")
        if not name:
            return None

        description = server_data.get("description", "")

        schema: dict[str, Any] = {}
        packages = server_data.get("packages")
        if isinstance(packages, list) and packages:
            first_pkg = packages[0] if isinstance(packages[0], dict) else {}
            schema = first_pkg.get("inputSchema", {})
        elif isinstance(server_data.get("inputSchema"), dict):
            schema = server_data["inputSchema"]

        return {
            "name": str(name),
            "description": str(description),
            "schema": schema,
            "source_platform": "mcp_registry",
        }
