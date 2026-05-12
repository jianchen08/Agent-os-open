"""
隔离配置

定义隔离系统的配置选项和策略
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.isolation.types import IsolationLevel

logger = logging.getLogger(__name__)


@dataclass
class IsolationConfig:
    """隔离配置

    控制隔离系统的行为，包括全局开关、降级策略、工具隔离策略等
    """

    # ==================== 全局控制 ====================

    enabled: bool = True
    """全局开关：是否启用隔离功能"""

    enable_fallback: bool = True
    """降级开关：隔离失败时是否自动降级到宿主机"""

    # ==================== 工具策略 ====================

    # 工具白名单（这些工具不隔离）
    # 适用于只读、安全的工具
    whitelist: set[str] = field(
        default_factory=lambda: {
            "read",
            "write",
            "edit",
            "glob",
            "grep",
            "search",
            "web_search",
            "web_fetch",
            "memory_retrieve",
            "resource_search",
        }
    )

    # 工具黑名单（这些工具强制隔离）
    # 适用于危险操作、代码执行等工具
    blacklist: set[str] = field(
        default_factory=lambda: {
            "bash",
            "shell_execute",
            "python_execute",
            "desktop_control",
            "isolation_execute",
        }
    )

    # 工具特定策略（工具名 -> 隔离级别）
    # 允许为特定工具指定精确的隔离级别
    tool_policies: dict[str, str] = field(default_factory=dict)

    # ==================== 默认配置 ====================

    default_level: str = "host"
    """默认隔离级别：sandbox | container | host"""

    # ==================== 环境管理 ====================

    reuse_environment: bool = True
    """是否复用环境：同一会话的工具执行复用同一环境"""

    environment_ttl: int = 3600
    """环境生存时间（秒）：超过此时间的空闲环境将被清理"""

    max_environments: int = 10
    """最大环境数量：限制同时存在的隔离环境数量"""

    # ==================== 提供者配置 ====================

    providers: dict[str, dict] = field(default_factory=dict)
    """各提供者的配置

    示例：
    {
        "host": {"enabled": True},
        "cua": {"enabled": False, "image": "python:3.11-slim"},
        "e2b": {"enabled": False, "template": "base-python"}
    }
    """

    # ==================== 分类策略 ====================

    # 需要隔离的工具分类
    isolated_categories: set[str] = field(
        default_factory=lambda: {
            "code_execution",
            "system",
            "network",
            "dangerous",
        }
    )

    # 不需要隔离的工具分类
    safe_categories: set[str] = field(
        default_factory=lambda: {
            "query",
            "read",
            "analysis",
        }
    )

    # ==================== 资源限制 ====================

    # 默认资源限制（用于容器/沙箱）
    default_memory_limit: str = "512m"
    """默认内存限制"""

    default_cpu_limit: str = "1"
    """默认 CPU 限制"""

    default_timeout: int = 300
    """默认执行超时（秒）"""

    # ==================== 高级选项 ====================

    # 环境预热
    preload_environments: bool = False
    """是否在启动时预热环境"""

    # 异步清理
    async_cleanup: bool = True
    """是否异步清理环境"""

    # 清理延迟（秒）
    cleanup_delay: int = 60
    """环境在任务结束后延迟清理的时间"""

    @classmethod
    def from_file(cls, path: str) -> "IsolationConfig":
        """从配置文件加载

        Args:
            path: 配置文件路径（YAML 格式）

        Returns:
            IsolationConfig 实例
        """
        import yaml

        config_file = Path(path)
        if not config_file.exists():
            logger.warning(f"[IsolationConfig] 配置文件不存在: {path}，使用默认配置")
            return cls()

        try:
            with open(config_file, encoding="utf-8") as f:
                full_data = yaml.safe_load(f) or {}

            # 提取 coordinator 部分（新格式）
            data = full_data.get("coordinator", {})

            # 如果没有 coordinator 部分，可能是旧格式，直接使用全部数据
            if not data:
                data = full_data

            # 提取 resource_limits（如果存在）
            if "resource_limits" in data:
                limits = data.pop("resource_limits", {})
                # 将嵌套的 resource_limits 映射到顶层字段
                if "max_environments" in limits:
                    data["max_environments"] = limits["max_environments"]
                if "environment_ttl" in limits:
                    data["environment_ttl"] = limits["environment_ttl"]
                if "default_timeout" in limits:
                    data["default_timeout"] = limits["default_timeout"]

            # 存储提供者配置（不传递给 dataclass）
            providers = full_data.get("providers", {})

            # 创建配置实例
            config = cls(**data)
            config.providers = providers

            logger.info(f"[IsolationConfig] 从配置文件加载成功: {path}")
            return config

        except Exception as e:
            logger.error(f"[IsolationConfig] 加载配置文件失败: {e}，使用默认配置")
            return cls()

    def to_file(self, path: str) -> None:
        """保存配置到文件

        Args:
            path: 配置文件路径（YAML 格式）
        """
        import yaml

        config_file = Path(path)
        config_file.parent.mkdir(parents=True, exist_ok=True)

        with open(config_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self.__dict__, f, allow_unicode=True, default_flow_style=False
            )

        logger.info(f"[IsolationConfig] 配置已保存到: {path}")

    def get_tool_policy(self, tool_name: str) -> IsolationLevel | None:
        """获取工具的隔离策略

        Args:
            tool_name: 工具名称

        Returns:
            隔离级别，如果工具没有特定策略则返回 None
        """
        level_str = self.tool_policies.get(tool_name)
        if level_str:
            try:
                return IsolationLevel(level_str)
            except ValueError:
                logger.warning(f"[IsolationConfig] 无效的隔离级别: {level_str}")

        return None

    def is_tool_whitelisted(self, tool_name: str) -> bool:
        """检查工具是否在白名单中（不隔离）

        Args:
            tool_name: 工具名称

        Returns:
            是否在白名单中
        """
        return tool_name in self.whitelist

    def is_tool_blacklisted(self, tool_name: str) -> bool:
        """检查工具是否在黑名单中（强制隔离）

        Args:
            tool_name: 工具名称

        Returns:
            是否在黑名单中
        """
        return tool_name in self.blacklist

    def should_isolate_category(self, category: str) -> bool:
        """判断工具分类是否需要隔离

        Args:
            category: 工具分类

        Returns:
            是否需要隔离
        """
        # 优先检查明确标记的分类
        if category in self.isolated_categories:
            return True
        if category in self.safe_categories:
            return False

        # 默认：未知分类不隔离
        return False

    def validate(self) -> bool:
        """验证配置是否有效

        Returns:
            配置是否有效
        """
        # 检查隔离级别
        try:
            IsolationLevel(self.default_level)
        except ValueError:
            logger.error(f"[IsolationConfig] 无效的默认隔离级别: {self.default_level}")
            return False

        # 检查环境 TTL
        if self.environment_ttl <= 0:
            logger.error(
                f"[IsolationConfig] 环境 TTL 必须大于 0: {self.environment_ttl}"
            )
            return False

        # 检查最大环境数
        if self.max_environments <= 0:
            logger.error(
                f"[IsolationConfig] 最大环境数必须大于 0: {self.max_environments}"
            )
            return False

        # 检查资源限制
        if self.default_timeout <= 0:
            logger.error(
                f"[IsolationConfig] 超时时间必须大于 0: {self.default_timeout}"
            )
            return False

        return True

    def __post_init__(self):
        """初始化后处理"""
        # 转换集合类型
        if not isinstance(self.whitelist, set):
            self.whitelist = set(self.whitelist)
        if not isinstance(self.blacklist, set):
            self.blacklist = set(self.blacklist)
        if not isinstance(self.isolated_categories, set):
            self.isolated_categories = set(self.isolated_categories)
        if not isinstance(self.safe_categories, set):
            self.safe_categories = set(self.safe_categories)

        # 验证配置
        if not self.validate():
            logger.warning("[IsolationConfig] 配置验证失败，使用默认值")


# ==================== 默认配置实例 ====================


def get_default_config() -> IsolationConfig:
    """获取默认隔离配置

    Returns:
        默认配置实例
    """
    return IsolationConfig()


def load_config(path: str | None = None) -> IsolationConfig:
    """加载隔离配置

    Args:
        path: 配置文件路径，如果为 None 则尝试从默认位置加载

    Returns:
        隔离配置实例
    """
    # 默认配置文件路径
    default_paths = [
        "config/isolation/isolation_config.yaml",
        "config/isolation.yaml",
    ]

    if path:
        return IsolationConfig.from_file(path)

    # 尝试从默认位置加载
    for default_path in default_paths:
        if Path(default_path).exists():
            logger.info(f"[IsolationConfig] 从默认位置加载配置: {default_path}")
            return IsolationConfig.from_file(default_path)

    # 使用默认配置
    logger.info("[IsolationConfig] 未找到配置文件，使用默认配置")
    return get_default_config()
