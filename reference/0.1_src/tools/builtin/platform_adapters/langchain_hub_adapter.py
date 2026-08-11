"""
LangChain Hub 平台适配器

通过 LangSmith API 搜索提示词模板和工具。
API 端点: GET https://api.smith.langchain.com/v1/repo/search?query={query}&repo_type=prompt
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from tools.builtin.external_resource_search import PlatformAdapter

logger = logging.getLogger(__name__)

# 默认 API 基地址
_DEFAULT_BASE_URL = "https://api.smith.langchain.com"
# HTTP 请求超时（秒）
_TIMEOUT = 10


class LangChainHubAdapter(PlatformAdapter):
    """
    LangChain Hub 平台适配器

    通过 LangSmith REST API 搜索提示词模板和工具，
    返回标准化资源列表。

    Args:
        base_url: API 基地址，默认为 LangSmith 官方地址
        api_key: LangSmith API Key（可选，部分端点可能需要认证）
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        """初始化 LangChain Hub 适配器"""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def search(
        self,
        query: str,
        resource_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        搜索 LangChain Hub 中的提示词模板或工具资源

        Args:
            query: 搜索关键词
            resource_type: 资源类型（tool / skill）
            limit: 最大返回数量

        Returns:
            标准化资源列表，每项包含 name / description / schema / source_platform
        """
        url = f"{self._base_url}/v1/repo/search"
        # resource_type 映射：tool -> tool 类型，其他默认 prompt
        repo_type = "tool" if resource_type == "tool" else "prompt"
        params: dict[str, str | int] = {
            "query": query,
            "repo_type": repo_type,
        }

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key

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
                        "[langchain_hub] 搜索请求失败: status=%d url=%s",
                        resp.status,
                        resp.url,
                    )
                    return []

                body = await resp.json()

        except aiohttp.ClientError as e:
            logger.warning("[langchain_hub] 网络请求异常: %s", e)
            return []
        except Exception as e:
            logger.warning("[langchain_hub] 未知异常: %s", e)
            return []

        # 解析响应
        repos = self._extract_repos(body)
        if not repos:
            logger.debug(
                "[langchain_hub] 未找到仓库列表，原始响应 keys: %s",
                list(body.keys()) if isinstance(body, dict) else type(body).__name__,
            )
            return []

        results: list[dict[str, Any]] = []
        for repo in repos[:limit]:
            parsed = self._parse_repo(repo)
            if parsed:
                results.append(parsed)

        logger.info(
            "[langchain_hub] 搜索完成: query=%s repo_type=%s 返回 %d 条结果",
            query,
            repo_type,
            len(results),
        )
        return results

    def _extract_repos(self, body: Any) -> list[dict[str, Any]]:
        """
        从 API 响应体中提取仓库列表

        兼容多种可能的响应格式：
        - {"repos": [...]}
        - {"results": [...]}
        - 直接为列表

        Args:
            body: API 响应体

        Returns:
            仓库字典列表
        """
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("repos", "results", "data", "items"):
                val = body.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _parse_repo(self, repo: dict[str, Any]) -> dict[str, Any] | None:
        """
        解析单个仓库条目为标准化资源格式

        Args:
            repo: 原始仓库数据

        Returns:
            标准化资源字典，解析失败返回 None
        """
        name = repo.get("name") or repo.get("repo_id", "")
        if not name:
            return None

        description = repo.get("description", "")

        # LangChain Hub 通常不返回 JSON Schema，构造一个基本描述
        schema: dict[str, Any] = {}
        if isinstance(repo.get("input_schema"), dict):
            schema = repo["input_schema"]
        elif isinstance(repo.get("inputSchema"), dict):
            schema = repo["inputSchema"]
        else:
            # 使用元数据构造简单 schema
            tags = repo.get("tags", [])
            schema = {
                "type": "object",
                "description": f"LangChain Hub 资源: {name}",
                "tags": tags if isinstance(tags, list) else [],
            }

        return {
            "name": str(name),
            "description": str(description),
            "schema": schema,
            "source_platform": "langchain_hub",
        }
