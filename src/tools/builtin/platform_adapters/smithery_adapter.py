"""
Smithery 平台适配器

通过 Smithery Registry API 搜索已注册的 MCP Server。
API 端点: GET https://api.smithery.ai/servers?q={query}
文档: https://www.smithery.ai/docs/concepts/registry_search_servers
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from tools.builtin.external_resource_search import PlatformAdapter

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.smithery.ai"
_TIMEOUT = 10


class SmitheryAdapter(PlatformAdapter):
    """
    Smithery 平台适配器

    通过 Smithery Registry REST API 搜索 MCP Server。
    支持语义搜索，参数为 q。

    Args:
        base_url: API 基地址
        api_key: Smithery API Key（可选，无 Key 有速率限制）
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        """初始化 Smithery 适配器"""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def search(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        搜索 Smithery 中的 Server 资源

        Args:
            query: 搜索关键词（支持语义搜索）
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            标准化资源列表
        """
        url = f"{self._base_url}/servers"
        params: dict[str, str | int] = {"q": query, "pageSize": limit}

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.warning(
                        "[smithery] 搜索请求失败: status=%d url=%s",
                        resp.status,
                        resp.url,
                    )
                    return []

                body = await resp.json()

        except aiohttp.ClientError as e:
            logger.warning("[smithery] 网络请求异常: %s", e)
            return []
        except Exception as e:
            logger.warning("[smithery] 未知异常: %s", e)
            return []

        servers = self._extract_servers(body)
        if not servers:
            logger.debug(
                "[smithery] 未找到 servers 列表，原始响应 keys: %s",
                list(body.keys()) if isinstance(body, dict) else type(body).__name__,
            )
            return []

        results: list[dict[str, Any]] = []
        for server in servers[:limit]:
            parsed = self._parse_server(server)
            if parsed:
                results.append(parsed)

        logger.info(
            "[smithery] 搜索完成: query=%s 返回 %d 条结果",
            query,
            len(results),
        )
        return results

    def _extract_servers(self, body: Any) -> list[dict[str, Any]]:
        """
        从 API 响应体中提取 servers 列表

        Smithery 返回 {"servers": [...], "pagination": {...}}

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

        Smithery 响应字段: qualifiedName / displayName / description / useCount

        Args:
            server: 原始 server 数据

        Returns:
            标准化资源字典，解析失败返回 None
        """
        name = server.get("qualifiedName") or server.get("displayName") or server.get("name") or server.get("id", "")
        if not name:
            return None

        description = server.get("description", "")

        schema: dict[str, Any] = {}
        if isinstance(server.get("inputSchema"), dict):
            schema = server["inputSchema"]
        elif isinstance(server.get("input_schema"), dict):
            schema = server["input_schema"]

        return {
            "name": str(name),
            "description": str(description),
            "schema": schema,
            "source_platform": "smithery",
        }
