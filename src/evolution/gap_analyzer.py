"""能力缺口分析与四层筛选模块。

负责识别 Agent 缺失的能力，并通过四层筛选策略确定最优实现方案：
1. TOOL 层：搜索已有工具是否能满足需求
2. CONFIG 层：检查是否可通过配置变更满足
3. PLUGIN 层：检查是否有可安装的插件满足
4. CORE 层：需要核心代码修改（最高成本）

暴露接口：
- analyze_gap(required_capability, context) -> CapabilityGap
- four_layer_filter(gap, tool_registry, config_store, plugin_registry) -> FilterResult
- GapAnalyzer: 能力缺口分析器类
"""

from __future__ import annotations

import logging
from typing import Any

from evolution.types import CapabilityGap, FilterLayer, FilterResult

logger = logging.getLogger(__name__)


class GapAnalyzer:
    """能力缺口分析器。

    分析 Agent 缺失的能力，并通过四层筛选确定最优实现方案。
    每一层如果不满足则自动进入下一层，返回最终推荐层。

    Attributes:
        _tool_registry: 工具注册中心（可选）
        _config_store: 配置存储（可选）
        _plugin_registry: 插件注册中心（可选）
    """

    def __init__(
        self,
        tool_registry: Any | None = None,
        config_store: Any | None = None,
        plugin_registry: Any | None = None,
    ) -> None:
        """初始化能力缺口分析器。

        Args:
            tool_registry: 工具注册中心实例（需实现 search 方法）
            config_store: 配置存储实例
            plugin_registry: 插件注册中心实例
        """
        self._tool_registry = tool_registry
        self._config_store = config_store
        self._plugin_registry = plugin_registry

    def analyze_gap(
        self,
        required_capability: str,
        context: dict[str, Any] | None = None,
    ) -> CapabilityGap:
        """分析能力缺口。

        根据需求描述和上下文，构造一个 CapabilityGap 对象。

        Args:
            required_capability: 需要的能力描述
            context: 附加上下文信息

        Returns:
            能力缺口对象
        """
        context = context or {}
        priority = context.get("priority", 5)
        required_by = context.get("required_by", "unknown")

        # 根据能力描述推断建议的筛选层
        suggested_layer = self._infer_suggested_layer(required_capability, context)

        gap = CapabilityGap(
            missing_capability=required_capability,
            required_by=required_by,
            priority=min(10, max(1, priority)),
            suggested_layer=suggested_layer,
            context=context,
        )

        logger.info(
            "[GapAnalyzer] 能力缺口分析: capability='%s', priority=%d, suggested_layer=%s",
            required_capability,
            gap.priority,
            suggested_layer.value,
        )
        return gap

    def four_layer_filter(
        self,
        gap: CapabilityGap,
        tool_registry: Any | None = None,
        config_store: Any | None = None,
        plugin_registry: Any | None = None,
    ) -> FilterResult:
        """执行四层筛选。

        按顺序从低到高执行四层筛选，每层不满足则进入下一层。
        筛选顺序：TOOL → CONFIG → PLUGIN → CORE

        Args:
            gap: 能力缺口
            tool_registry: 工具注册中心（覆盖实例属性）
            config_store: 配置存储（覆盖实例属性）
            plugin_registry: 插件注册中心（覆盖实例属性）

        Returns:
            四层筛选结果，包含每层检查结果和最终推荐
        """
        registry = tool_registry or self._tool_registry
        config = config_store or self._config_store
        plugins = plugin_registry or self._plugin_registry

        result = FilterResult(gap=gap)

        # 第一层：TOOL 层 - 搜索已有工具
        tool_matched = self._check_tool_layer(gap, registry, result)
        if tool_matched:
            return result

        # 第二层：CONFIG 层 - 检查配置变更
        config_matched = self._check_config_layer(gap, config, result)
        if config_matched:
            return result

        # 第三层：PLUGIN 层 - 检查可用插件
        plugin_matched = self._check_plugin_layer(gap, plugins, result)
        if plugin_matched:
            return result

        # 第四层：CORE 层 - 核心代码修改
        result.core_layer_result = (
            f"需要核心代码修改来满足 '{gap.missing_capability}' 能力"
        )
        result.recommended_layer = FilterLayer.CORE
        result.recommended_action = (
            f"生成核心代码以实现 '{gap.missing_capability}' 能力"
        )

        logger.info(
            "[GapAnalyzer] 四层筛选完成: capability='%s', recommended_layer=%s",
            gap.missing_capability,
            result.recommended_layer.value,
        )
        return result

    def _check_tool_layer(
        self,
        gap: CapabilityGap,
        registry: Any | None,
        result: FilterResult,
    ) -> bool:
        """检查 TOOL 层：搜索已有工具是否能满足需求。

        遍历 tool_registry 中的工具描述，检查是否有匹配的工具。

        Args:
            gap: 能力缺口
            registry: 工具注册中心
            result: 筛选结果（会就地更新）

        Returns:
            是否在工具层找到匹配
        """
        if registry is None:
            result.tool_layer_result = "未提供工具注册中心，跳过工具层检查"
            return False

        try:
            # 使用注册中心的 search 方法搜索匹配工具
            matched_tools = registry.search(gap.missing_capability)

            if matched_tools:
                tool_names = [t.name for t in matched_tools]
                result.tool_layer_result = (
                    f"找到匹配工具: {', '.join(tool_names)}"
                )
                result.recommended_layer = FilterLayer.TOOL
                result.recommended_action = (
                    f"使用已有工具 '{tool_names[0]}' 满足需求"
                )
                logger.info(
                    "[GapAnalyzer] 工具层匹配: capability='%s', tools=%s",
                    gap.missing_capability,
                    tool_names,
                )
                return True

            result.tool_layer_result = (
                f"未找到匹配 '{gap.missing_capability}' 的已有工具"
            )
        except Exception as exc:
            logger.warning("[GapAnalyzer] 工具层检查异常: %s", exc)
            result.tool_layer_result = f"工具层检查异常: {exc}"

        return False

    def _check_config_layer(
        self,
        gap: CapabilityGap,
        config_store: Any | None,
        result: FilterResult,
    ) -> bool:
        """检查 CONFIG 层：是否可通过配置变更满足需求。

        检查配置存储中是否有相关配置项可以调整。

        Args:
            gap: 能力缺口
            config_store: 配置存储
            result: 筛选结果（会就地更新）

        Returns:
            是否在配置层找到解决方案
        """
        if config_store is None:
            result.config_layer_result = "未提供配置存储，跳过配置层检查"
            return False

        try:
            # 检查配置存储中是否有相关配置
            if hasattr(config_store, "search"):
                config_matches = config_store.search(gap.missing_capability)
                if config_matches:
                    result.config_layer_result = (
                        f"找到相关配置项: {len(config_matches)} 项"
                    )
                    result.recommended_layer = FilterLayer.CONFIG
                    result.recommended_action = "通过调整配置满足需求"
                    return True

            # 尝试通过 get 方法检查常见配置键
            config_keys = gap.context.get("config_keys", [])
            for key in config_keys:
                if hasattr(config_store, "get") and config_store.get(key) is not None:
                    result.config_layer_result = f"找到配置项: {key}"
                    result.recommended_layer = FilterLayer.CONFIG
                    result.recommended_action = f"通过调整配置 '{key}' 满足需求"
                    return True

            result.config_layer_result = (
                f"未找到可通过配置变更满足 '{gap.missing_capability}' 的方案"
            )
        except Exception as exc:
            logger.warning("[GapAnalyzer] 配置层检查异常: %s", exc)
            result.config_layer_result = f"配置层检查异常: {exc}"

        return False

    def _check_plugin_layer(
        self,
        gap: CapabilityGap,
        plugin_registry: Any | None,
        result: FilterResult,
    ) -> bool:
        """检查 PLUGIN 层：是否有可安装的插件满足需求。

        Args:
            gap: 能力缺口
            plugin_registry: 插件注册中心
            result: 筛选结果（会就地更新）

        Returns:
            是否在插件层找到解决方案
        """
        if plugin_registry is None:
            result.plugin_layer_result = "未提供插件注册中心，将生成新插件"
            # 检查能力描述是否含"核心"/"core"关键词，含则需降级到 CORE 层
            capability_lower = gap.missing_capability.lower()
            if "核心" in gap.missing_capability or "core" in capability_lower:
                result.plugin_layer_result = "能力描述含核心关键词，需核心代码修改"
                return False
            result.recommended_layer = FilterLayer.PLUGIN
            result.recommended_action = (
                f"生成新插件以实现 '{gap.missing_capability}' 能力"
            )
            return True

        try:
            # 搜索已有插件
            if hasattr(plugin_registry, "search"):
                matched_plugins = plugin_registry.search(gap.missing_capability)
                if matched_plugins:
                    plugin_names = [p.get("name", str(p)) for p in matched_plugins]
                    result.plugin_layer_result = (
                        f"找到可安装的插件: {', '.join(plugin_names)}"
                    )
                    result.recommended_layer = FilterLayer.PLUGIN
                    result.recommended_action = (
                        f"安装插件 '{plugin_names[0]}' 满足需求"
                    )
                    return True

            # 未找到已有插件，但可以在 PLUGIN 层生成新插件
            result.plugin_layer_result = "未找到已有插件，将生成新插件"
            result.recommended_layer = FilterLayer.PLUGIN
            result.recommended_action = (
                f"生成新插件以实现 '{gap.missing_capability}' 能力"
            )
            return True
        except Exception as exc:
            logger.warning("[GapAnalyzer] 插件层检查异常: %s", exc)
            result.plugin_layer_result = f"插件层检查异常: {exc}"
            result.recommended_layer = FilterLayer.PLUGIN
            result.recommended_action = (
                f"生成新插件以实现 '{gap.missing_capability}' 能力"
            )
            return True

    def _infer_suggested_layer(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> FilterLayer:
        """根据能力描述推断建议的筛选层。

        Args:
            capability: 能力描述
            context: 上下文信息

        Returns:
            建议的筛选层
        """
        # 如果上下文明确指定了层级
        if "suggested_layer" in context:
            layer_str = context["suggested_layer"]
            try:
                return FilterLayer(layer_str)
            except ValueError:
                pass

        # 基于关键词的简单推断
        capability_lower = capability.lower()

        # 常见工具关键词 → 从 TOOL 层开始
        tool_keywords = [
            "file", "search", "read", "write", "copy", "move",
            "delete", "list", "execute", "web", "http", "api",
        ]
        if any(kw in capability_lower for kw in tool_keywords):
            return FilterLayer.TOOL

        # 配置相关关键词 → 从 CONFIG 层开始
        config_keywords = ["config", "setting", "parameter", "option", "enable"]
        if any(kw in capability_lower for kw in config_keywords):
            return FilterLayer.CONFIG

        # 默认从 PLUGIN 层开始
        return FilterLayer.PLUGIN
