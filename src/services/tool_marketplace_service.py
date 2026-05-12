"""
工具市场服务

提供工具注册、搜索、安装、评级和推荐功能
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from src.config.settings import get_settings
from src.core.event_bus.base import EventBusBase

logger = logging.getLogger(__name__)


class ToolCategory(Enum):
    """工具分类"""

    DEVELOPMENT = "development"
    SYSTEM = "system"
    FILE = "file"
    NETWORK = "network"
    DATA = "data"
    AI = "ai"
    UTILITY = "utility"
    CUSTOM = "custom"


@dataclass
class MarketplaceTool:
    """市场工具定义"""

    id: str
    name: str
    description: str
    category: ToolCategory
    version: str
    author: str
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    price: float = 0.0
    rating: float = 0.0
    download_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRating:
    """工具评级"""

    id: str
    tool_id: str
    user_id: str
    rating: int
    comment: str = ""
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ToolInstallation:
    """工具安装记录"""

    id: str
    tool_id: str
    user_id: str
    status: str
    installed_at: datetime = field(default_factory=datetime.now)
    version: str = "1.0.0"


@dataclass
class ToolSearchFilter:
    """工具搜索过滤器"""

    category: ToolCategory | None = None
    tags: list[str] = field(default_factory=list)
    min_rating: float = 0.0
    max_price: float = float("inf")
    author: str | None = None
    free_only: bool = False


class ToolMarketplaceService:
    """
    工具市场服务

    提供工具的注册、搜索、安装、评级和推荐功能
    """

    def __init__(self, event_bus: EventBusBase = None):
        self.tools: dict[str, MarketplaceTool] = {}
        self.ratings: dict[str, list[ToolRating]] = {}
        self.installations: dict[str, list[ToolInstallation]] = {}
        self.event_bus = event_bus
        self.settings = get_settings()

    async def register_tool(self, tool: MarketplaceTool) -> bool:
        """
        注册工具到市场

        Args:
            tool: 工具定义

        Returns:
            注册是否成功
        """
        try:
            if not self._validate_tool(tool):
                return False

            if not tool.id:
                tool.id = str(uuid4())

            tool.created_at = datetime.now()
            tool.updated_at = datetime.now()

            self.tools[tool.id] = tool

            if tool.id not in self.ratings:
                self.ratings[tool.id] = []
            if tool.id not in self.installations:
                self.installations[tool.id] = []

            from src.core.event_bus.types import EventType, ExecutionEvent

            event = ExecutionEvent(
                event_type=EventType.TOOL_REGISTERED,
                session_id="system",
                data={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "category": tool.category.value,
                    "author": tool.author,
                },
            )
            await self.event_bus.publish(event)

            logger.info(f"工具已注册到市场: {tool.name} ({tool.id})")
            return True

        except Exception as e:
            logger.error(f"注册工具失败 {tool.name}: {e}")
            return False

    async def get_tool(self, tool_id: str) -> MarketplaceTool | None:
        """
        获取工具信息

        Args:
            tool_id: 工具ID

        Returns:
            工具信息，如果不存在返回None
        """
        return self.tools.get(tool_id)

    async def search_tools(
        self,
        query: str = "",
        filters: ToolSearchFilter | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MarketplaceTool]:
        """
        搜索工具

        Args:
            query: 搜索关键词
            filters: 搜索过滤器
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            匹配的工具列表
        """
        try:
            results = []
            query_lower = query.lower()

            for tool in self.tools.values():
                if not tool.enabled:
                    continue

                if query and not self._matches_query(tool, query_lower):
                    continue

                if filters and not self._matches_filters(tool, filters):
                    continue

                results.append(tool)

            results.sort(key=lambda t: (t.rating, t.download_count), reverse=True)

            return results[offset : offset + limit]

        except Exception as e:
            logger.error(f"搜索工具失败: {e}")
            return []

    async def install_tool(self, tool_id: str, user_id: str) -> ToolInstallation | None:
        """
        安装工具

        Args:
            tool_id: 工具ID
            user_id: 用户ID

        Returns:
            安装记录，失败返回None
        """
        try:
            tool = self.tools.get(tool_id)
            if not tool:
                logger.error(f"工具不存在: {tool_id}")
                return None

            if await self.is_tool_installed(tool_id, user_id):
                logger.warning(f"工具已安装: {tool_id} for user {user_id}")
                return None

            installation = ToolInstallation(
                id=str(uuid4()),
                tool_id=tool_id,
                user_id=user_id,
                status="installed",
                version=tool.version,
            )

            if tool_id not in self.installations:
                self.installations[tool_id] = []
            self.installations[tool_id].append(installation)

            tool.download_count += 1

            from src.core.event_bus.types import EventType, ExecutionEvent

            event = ExecutionEvent(
                event_type=EventType.TOOL_INSTALLED,
                session_id="system",
                data={
                    "tool_id": tool_id,
                    "user_id": user_id,
                    "tool_name": tool.name,
                },
            )
            await self.event_bus.publish(event)

            logger.info(f"工具已安装: {tool.name} for user {user_id}")
            return installation

        except Exception as e:
            logger.error(f"安装工具失败 {tool_id}: {e}")
            return None

    async def uninstall_tool(self, tool_id: str, user_id: str) -> bool:
        """
        卸载工具

        Args:
            tool_id: 工具ID
            user_id: 用户ID

        Returns:
            卸载是否成功
        """
        try:
            installations = self.installations.get(tool_id, [])
            for installation in installations:
                if (
                    installation.user_id == user_id
                    and installation.status == "installed"
                ):
                    installation.status = "uninstalled"

                    from src.core.event_bus.types import EventType, ExecutionEvent

                    event = ExecutionEvent(
                        event_type=EventType.TOOL_UNINSTALLED,
                        session_id="system",
                        data={
                            "tool_id": tool_id,
                            "user_id": user_id,
                        },
                    )
                    await self.event_bus.publish(event)

                    logger.info(f"工具已卸载: {tool_id} for user {user_id}")
                    return True

            return False

        except Exception as e:
            logger.error(f"卸载工具失败 {tool_id}: {e}")
            return False

    async def is_tool_installed(self, tool_id: str, user_id: str) -> bool:
        """
        检查工具是否已安装

        Args:
            tool_id: 工具ID
            user_id: 用户ID

        Returns:
            是否已安装
        """
        installations = self.installations.get(tool_id, [])
        for installation in installations:
            if installation.user_id == user_id and installation.status == "installed":
                return True
        return False

    async def rate_tool(
        self, tool_id: str, user_id: str, rating: int, comment: str = ""
    ) -> ToolRating | None:
        """
        评级工具

        Args:
            tool_id: 工具ID
            user_id: 用户ID
            rating: 评分 (1-5)
            comment: 评论

        Returns:
            评级记录，失败返回None
        """
        try:
            if not 1 <= rating <= 5:
                logger.error(f"无效评分: {rating}")
                return None

            if tool_id not in self.tools:
                logger.error(f"工具不存在: {tool_id}")
                return None

            rating_record = ToolRating(
                id=str(uuid4()),
                tool_id=tool_id,
                user_id=user_id,
                rating=rating,
                comment=comment,
            )

            if tool_id not in self.ratings:
                self.ratings[tool_id] = []
            self.ratings[tool_id].append(rating_record)

            await self._update_tool_rating(tool_id)

            from src.core.event_bus.types import EventType, ExecutionEvent

            event = ExecutionEvent(
                event_type=EventType.TOOL_RATED,
                session_id="system",
                data={
                    "tool_id": tool_id,
                    "user_id": user_id,
                    "rating": rating,
                },
            )
            await self.event_bus.publish(event)

            logger.info(f"工具已评级: {tool_id} - {rating}星")
            return rating_record

        except Exception as e:
            logger.error(f"评级工具失败 {tool_id}: {e}")
            return None

    async def get_tool_ratings(self, tool_id: str) -> list[ToolRating]:
        """
        获取工具评级

        Args:
            tool_id: 工具ID

        Returns:
            评级列表
        """
        return self.ratings.get(tool_id, [])

    async def get_popular_tools(self, limit: int = 10) -> list[MarketplaceTool]:
        """
        获取热门工具

        Args:
            limit: 返回数量限制

        Returns:
            热门工具列表
        """
        tools = [tool for tool in self.tools.values() if tool.enabled]
        tools.sort(key=lambda t: (t.download_count, t.rating), reverse=True)
        return tools[:limit]

    async def get_recommended_tools(
        self, user_id: str, limit: int = 5
    ) -> list[MarketplaceTool]:
        """
        获取推荐工具

        Args:
            user_id: 用户ID
            limit: 返回数量限制

        Returns:
            推荐工具列表
        """
        try:
            installed_tools = await self.get_user_tools(user_id)
            installed_ids = {tool.id for tool in installed_tools}

            categories = {tool.category for tool in installed_tools}

            recommendations = []
            for tool in self.tools.values():
                if not tool.enabled or tool.id in installed_ids:
                    continue

                if tool.category in categories:
                    recommendations.append(tool)

            recommendations.sort(key=lambda t: t.rating, reverse=True)
            return recommendations[:limit]

        except Exception as e:
            logger.error(f"获取推荐工具失败: {e}")
            return []

    async def get_user_tools(self, user_id: str) -> list[MarketplaceTool]:
        """
        获取用户已安装工具

        Args:
            user_id: 用户ID

        Returns:
            已安装工具列表
        """
        user_tools = []

        for tool_id, installations in self.installations.items():
            for installation in installations:
                if (
                    installation.user_id == user_id
                    and installation.status == "installed"
                ):
                    tool = self.tools.get(tool_id)
                    if tool:
                        user_tools.append(tool)
                    break

        return user_tools

    async def update_tool(self, tool: MarketplaceTool) -> bool:
        """
        更新工具信息

        Args:
            tool: 更新后的工具信息

        Returns:
            更新是否成功
        """
        try:
            if tool.id not in self.tools:
                return False

            tool.updated_at = datetime.now()

            self.tools[tool.id] = tool

            from src.core.event_bus.types import EventType, ExecutionEvent

            event = ExecutionEvent(
                event_type=EventType.TOOL_UPDATED,
                session_id="system",
                data={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                },
            )
            await self.event_bus.publish(event)

            logger.info(f"工具已更新: {tool.name}")
            return True

        except Exception as e:
            logger.error(f"更新工具失败 {tool.id}: {e}")
            return False

    def _validate_tool(self, tool: MarketplaceTool) -> bool:
        """验证工具信息"""
        if not tool.name or not tool.description:
            return False
        if not tool.author:
            return False
        if not tool.version:
            return False
        return True

    def _matches_query(self, tool: MarketplaceTool, query: str) -> bool:
        """检查工具是否匹配查询"""
        return (
            query in tool.name.lower()
            or query in tool.description.lower()
            or query in tool.author.lower()
            or any(query in tag.lower() for tag in tool.tags)
        )

    def _matches_filters(
        self, tool: MarketplaceTool, filters: ToolSearchFilter
    ) -> bool:
        """检查工具是否匹配过滤器"""
        if filters.category and tool.category != filters.category:
            return False

        if filters.tags and not any(tag in tool.tags for tag in filters.tags):
            return False

        if tool.rating < filters.min_rating:
            return False

        if tool.price > filters.max_price:
            return False

        if filters.author and tool.author != filters.author:
            return False

        if filters.free_only and tool.price > 0:
            return False

        return True

    async def _update_tool_rating(self, tool_id: str):
        """更新工具平均评分"""
        ratings = self.ratings.get(tool_id, [])
        if ratings:
            avg_rating = sum(r.rating for r in ratings) / len(ratings)
            self.tools[tool_id].rating = round(avg_rating, 1)


_tool_marketplace_service = None


def get_tool_marketplace_service() -> ToolMarketplaceService:
    """获取工具市场服务实例"""
    global _tool_marketplace_service
    if _tool_marketplace_service is None:
        _tool_marketplace_service = ToolMarketplaceService()
    return _tool_marketplace_service
