"""Agent 配置注册表。

提供 Agent 配置的注册、查找和筛选功能。
支持按 config_id、层级、类型、分类、标签和工具进行查询。
支持懒加载：当 get() 找不到 config_id 时，自动从配置目录扫描并加载。

典型用法::

    from agents.registry import AgentRegistry
    from agents.loader import AgentConfigLoader

    registry = AgentRegistry()
    count = registry.load_directory("config/agents/")

    # 按 ID 查找（未命中时自动从磁盘懒加载）
    config = registry.get("main_agent")

    # 按层级筛选
    l1_agents = registry.find_by_level(AgentLevel.L1_MAIN)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from .types import AgentConfig, AgentLevel, AgentType

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Agent 配置注册表，管理所有已加载的 AgentConfig 实例。

    支持懒加载：首次 get() 未命中时，自动从 _config_dir 扫描对应的
    YAML 文件并注册到内存，无需重启服务。

    Attributes:
        _configs: 以 config_id 为键的配置字典。
        _config_dir: 配置文件根目录（load_directory 时记录）。
        _scanned_files: 已扫描过的 YAML 文件路径集合，避免重复扫描。
    """

    def __init__(self) -> None:
        """初始化空的 Agent 注册表。"""
        self._configs: dict[str, AgentConfig] = {}
        self._config_dir: Path | None = None
        self._scanned_files: set[str] = set()

    def register(self, config: AgentConfig) -> None:
        """注册 Agent 配置。

        如果 config_id 已存在，将覆盖旧配置并记录警告。

        Args:
            config: AgentConfig 实例。

        Raises:
            ValueError: config_id 为空。
        """
        if not config.config_id:
            raise ValueError("AgentConfig.config_id 不能为空")
        if config.config_id in self._configs:
            logger.warning("覆盖已存在的 Agent 配置: %s", config.config_id)
        self._configs[config.config_id] = config

    def get(self, config_id: str) -> AgentConfig | None:
        """按 config_id 查找 Agent 配置。

        内存中未命中时，自动从 _config_dir 懒加载对应的 YAML 文件。
        支持两种文件名匹配策略：
        1. 文件名等于 config_id（如 research_agent.yaml）
        2. 文件内容中 config_id 字段匹配（遍历未扫描的文件）

        Args:
            config_id: 配置唯一标识。

        Returns:
            AgentConfig 实例，未找到返回 None。
        """
        config = self._configs.get(config_id)
        if config is not None:
            return config

        if self._config_dir is None or not self._config_dir.exists():
            return None

        loaded = self._lazy_load(config_id)
        return loaded

    def _lazy_load(self, config_id: str) -> AgentConfig | None:
        """从配置目录懒加载指定 config_id 的 Agent 配置。

        查找策略：
        1. 先按文件名 rglob("{config_id}.yaml") 快速匹配
        2. 未找到则遍历所有未扫描的 YAML 文件，按内容 config_id 字段匹配
        3. 找到后通过 AgentConfigLoader 解析并注册到内存

        Args:
            config_id: 配置唯一标识。

        Returns:
            成功加载返回 AgentConfig，否则返回 None。
        """
        from .loader import AgentConfigLoader  # noqa: PLC0415

        yaml_path = self._find_yaml_by_filename(config_id)
        if yaml_path is None:
            yaml_path = self._find_yaml_by_content(config_id)

        if yaml_path is None:
            logger.debug(
                "懒加载未找到 Agent 配置: config_id=%s (搜索目录: %s)",
                config_id,
                self._config_dir,
            )
            return None

        try:
            config = AgentConfigLoader.load_from_yaml(yaml_path)
            self.register(config)
            self._scanned_files.add(str(yaml_path))
            logger.info(
                "懒加载 Agent 配置成功: %s (from %s)",
                config.config_id,
                yaml_path,
            )
            return config
        except (ValueError, Exception) as e:
            logger.warning("懒加载 Agent 配置失败: %s (from %s): %s", config_id, yaml_path, e)
            return None

    def _find_yaml_by_filename(self, config_id: str) -> Path | None:
        """按文件名查找 YAML 配置文件。

        Args:
            config_id: 配置唯一标识，同时作为文件名（不含扩展名）。

        Returns:
            匹配的 YAML 文件路径，未找到返回 None。
        """
        assert self._config_dir is not None
        for p in self._config_dir.rglob(f"{config_id}.yaml"):
            return p
        return None

    # _find_yaml_by_content 最大扫描文件数，防止目录过大时阻塞
    _MAX_SCAN_FILES = 200

    def _find_yaml_by_content(self, config_id: str) -> Path | None:
        """按 YAML 内容中的 config_id 字段查找配置文件。

        遍历所有未扫描过的 YAML 文件，读取其 config_id 字段进行匹配。
        匹配成功的文件路径会被缓存到 _scanned_files 中。
        设置最大扫描数量限制，避免大目录阻塞。

        Args:
            config_id: 配置唯一标识。

        Returns:
            匹配的 YAML 文件路径，未找到返回 None。
        """
        assert self._config_dir is not None
        scanned_count = 0
        for p in self._config_dir.rglob("*.yaml"):
            if scanned_count >= self._MAX_SCAN_FILES:
                logger.warning(
                    "已达到最大扫描文件数限制 (%d)，停止扫描: config_id=%s",
                    self._MAX_SCAN_FILES,
                    config_id,
                )
                break
            p_str = str(p)
            if p_str in self._scanned_files:
                continue
            scanned_count += 1
            try:
                with open(p, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    file_config_id = data.get("config_id", "")
                    self._scanned_files.add(p_str)
                    if file_config_id == config_id:
                        return p
            except Exception:
                continue
        return None

    def find_by_level(self, level: AgentLevel) -> list[AgentConfig]:
        """按层级筛选 Agent 配置。

        Args:
            level: Agent 层级。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.level == level]

    def find_by_type(self, agent_type: AgentType) -> list[AgentConfig]:
        """按类型筛选 Agent 配置。

        Args:
            agent_type: Agent 类型。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.agent_type == agent_type]

    def find_by_category(self, category: str) -> list[AgentConfig]:
        """按分类筛选 Agent 配置。

        Args:
            category: Agent 分类名称。

        Returns:
            匹配的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if c.category == category]

    def find_by_tag(self, tag: str) -> list[AgentConfig]:
        """按标签筛选 Agent 配置。

        Args:
            tag: 标签名称。

        Returns:
            包含该标签的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if tag in c.tags]

    def find_by_tool(self, tool_id: str) -> list[AgentConfig]:
        """按工具筛选 Agent 配置。

        Args:
            tool_id: 工具 ID。

        Returns:
            绑定了该工具的 AgentConfig 列表。
        """
        return [c for c in self._configs.values() if tool_id in c.tool_ids]

    def list_all(self) -> list[AgentConfig]:
        """列出所有已注册的 Agent 配置。

        Returns:
            所有 AgentConfig 列表。
        """
        return list(self._configs.values())

    def load_directory(self, dir_path: str | Path) -> int:
        """从目录批量加载 Agent 配置并注册。

        同时记录配置目录路径，后续 get() 未命中时可触发懒加载。

        使用 strict=False：单个坏配置文件（含 YAML 语法错误）会被跳过并记录
        warning，不影响其余 agent 加载，避免单个坏配置拖垮整个引擎初始化。

        Args:
            dir_path: YAML 配置目录路径。

        Returns:
            成功加载的配置数量。
        """
        from .loader import AgentConfigLoader  # noqa: PLC0415

        dir_path = Path(dir_path)
        self._config_dir = dir_path

        configs = AgentConfigLoader.load_from_directory(dir_path, strict=False)
        for config in configs:
            try:
                self.register(config)
                if hasattr(config, "__yaml_path__"):
                    self._scanned_files.add(str(config.__yaml_path__))
            except ValueError as e:
                logger.warning("跳过无效配置 %s: %s", config.config_id, e)

        for p in dir_path.rglob("*.yaml"):
            self._scanned_files.add(str(p))

        logger.info(
            "AgentRegistry: 从 %s 加载了 %d 个配置 (支持后续懒加载)",
            dir_path,
            len(configs),
        )
        return len(configs)

    def unregister(self, config_id: str) -> bool:
        """注销 Agent 配置。

        Args:
            config_id: 配置唯一标识。

        Returns:
            是否成功注销（True=已移除，False=不存在）。
        """
        if config_id in self._configs:
            del self._configs[config_id]
            return True
        return False

    def reload_agent(self, config_id: str) -> AgentConfig | None:
        """从磁盘重新加载指定 Agent 的配置。

        Args:
            config_id: 配置唯一标识。

        Returns:
            更新后的 AgentConfig 实例，未找到返回 None。
        """
        if self._config_dir is None or not self._config_dir.exists():
            return None

        yaml_path = self._find_yaml_by_filename(config_id)
        if yaml_path is None:
            yaml_path = self._find_yaml_by_content(config_id)

        if yaml_path is None:
            return None

        from .loader import AgentConfigLoader  # noqa: PLC0415

        try:
            config = AgentConfigLoader.load_from_yaml(yaml_path)
            self.register(config)
            self._scanned_files.add(str(yaml_path))
            logger.info("热更新 Agent 配置: %s (from %s)", config.config_id, yaml_path)
            return config
        except (FileNotFoundError, ValueError) as e:
            # 捕获具体异常（backend_rules §5.3 禁止 except Exception），
            # 加载失败通常是文件缺失或 YAML 解析错误，记录完整堆栈供排查。
            logger.warning(
                "热更新 Agent 配置失败: %s (from %s): %s",
                config_id,
                yaml_path,
                e,
                exc_info=True,
            )
            return None

    def count(self) -> int:
        """返回已注册的 Agent 配置数量。

        Returns:
            配置数量。
        """
        return len(self._configs)

    # ---- 异步方法 ----

    async def get_async(self, config_id: str) -> AgentConfig | None:
        """异步版本的 get，将懒加载中的同步 I/O 卸载到线程池。"""
        config = self._configs.get(config_id)
        if config is not None:
            return config
        if self._config_dir is None or not self._config_dir.exists():
            return None
        return await asyncio.to_thread(self._lazy_load, config_id)

    async def load_directory_async(self, dir_path: str | Path) -> int:
        """异步版本的 load_directory，将同步 I/O 卸载到线程池。"""
        return await asyncio.to_thread(self.load_directory, dir_path)
