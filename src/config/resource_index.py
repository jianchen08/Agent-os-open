"""
资源索引系统

提供高效的资源搜索和查找功能
"""

import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ResourceType(str, Enum):
    """资源类型"""

    AGENT = "agent"
    WORKFLOW = "workflow"
    TOOL = "tool"


class ResourceMetadata(BaseModel):
    """资源元数据"""

    id: str  # 唯一标识
    type: ResourceType  # agent/workflow/tool
    name: str  # 显示名称
    category: str  # 分类
    level: str | None = None  # L1/L2/L3
    capabilities: list[str] = []  # 能力列表
    tags: list[str] = []  # 标签
    description: str = ""  # 描述
    file_path: str  # 文件路径
    version: str = "1.0.0"  # 版本
    dependencies: list[str] = []  # 依赖
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResourceIndex:
    """资源索引管理器"""

    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)

        # 主索引
        self.agents: dict[str, ResourceMetadata] = {}
        self.workflows: dict[str, ResourceMetadata] = {}
        self.tools: dict[str, ResourceMetadata] = {}

        # 分类索引
        self.by_category: dict[str, list[str]] = {}
        self.by_level: dict[str, list[str]] = {}
        self.by_capability: dict[str, list[str]] = {}
        self.by_tag: dict[str, list[str]] = {}

        # 全文搜索索引（倒排索引）
        self.search_index: dict[str, set[str]] = {}

    async def build_index(self):
        """构建索引"""
        logger.info("🔨 构建资源索引...")

        # 扫描 Agent 配置
        await self._scan_agents()

        # 扫描 Workflow 配置
        await self._scan_workflows()

        # 构建搜索索引
        self._build_search_index()

        logger.debug("✅ 索引构建完成:")
        logger.debug(f"  - Agents: {len(self.agents)}")
        logger.debug(f"  - Workflows: {len(self.workflows)}")
        logger.debug(f"  - Tools: {len(self.tools)}")

    async def _scan_agents(self):
        """扫描 Agent 配置"""
        agents_dir = self.config_dir / "agents"

        if not agents_dir.exists():
            return

        # 递归扫描所有 .yaml 文件
        for yaml_file in agents_dir.rglob("*.yaml"):
            if yaml_file.name == ".gitkeep":
                continue

            try:
                metadata = await self._extract_agent_metadata(yaml_file)
                self.agents[metadata.id] = metadata

                # 更新分类索引
                self._update_category_index(metadata)
                self._update_level_index(metadata)
                self._update_capability_index(metadata)
                self._update_tag_index(metadata)

            except Exception as e:
                logger.debug(f"⚠️  解析 Agent 配置失败: {yaml_file}, 错误: {e}")

    async def _scan_workflows(self):
        """扫描 Workflow 配置"""
