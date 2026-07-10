"""Agent 配置加载器。

从 YAML 文件加载 Agent 配置，支持单文件加载和目录递归加载。
处理嵌套数据结构的映射（如 static_vars → ContextConfig）。

典型用法::

    from agents.loader import AgentConfigLoader

    # 加载单个 YAML
    config = AgentConfigLoader.load_from_yaml("path/to/agent.yaml")

    # 加载目录下所有 YAML
    configs = AgentConfigLoader.load_from_directory("path/to/agents/")
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .types import (
    AgentConfig,
    AgentLevel,
    AgentPluginsConfig,
    AgentType,
    ContextConfig,
    ContextVarItem,
    DeliverableSpec,
    KnowledgeConfig,
    MetricRef,
    RuleReinforcement,
)

logger = logging.getLogger(__name__)

# 环境变量占位符模式：${VAR_NAME}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# agent_type 合法键集合（单一真相源）。
# config/schema.py 与本模块的 _resolve_agent_type 必须以此为准，
# 否则热重载校验会误拒 orchestrator/atomic 等编排/原子 Agent 配置。
# 注意：_resolve_agent_type 对未知值降级为 SPECIALIZED（宽容运行），
# 但 schema 校验应拒绝未知值（严格校验），故合法集合=映射键本身。
VALID_AGENT_TYPE_KEYS: frozenset[str] = frozenset({"main", "orchestrator", "specialized", "atomic", "system"})


def _substitute_env_vars(value: Any) -> Any:
    """递归替换字典/列表/字符串中的环境变量占位符。

    将 ``${ENV_VAR}`` 格式的占位符替换为 ``os.environ.get("ENV_VAR", "")``。
    若环境变量不存在，替换为空字符串（与 src/config/models.py 保持一致）。

    Args:
        value: 待替换的值（可能是 dict / list / str / 其他）。

    Returns:
        替换后的值，类型与输入一致。
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")

        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


class AgentConfigLoader:
    """Agent 配置加载器，从 YAML 文件加载 AgentConfig。"""

    @staticmethod
    def _parse_context_var_item(data: dict[str, Any] | str) -> ContextVarItem:
        """解析上下文变量项。

        Args:
            data: 原始 YAML 字典，或占位符字符串（如 "{{rules}}"）。

        Returns:
            ContextVarItem 实例。
        """
        # 字符串形式：占位符语法，整体作为 name 存储
        if isinstance(data, str):
            return ContextVarItem(
                name=data,
                type="placeholder",
                path="",
                tags=[],
                inject_type="",
                top_k=5,
                content="",
                memory_type="",
                memory_layer="",
                route_key="",
                routes={},
                extensions=[],
            )
        return ContextVarItem(
            name=data.get("name", ""),
            type=data.get("type", ""),
            path=data.get("path", ""),
            tags=data.get("tags", []),
            inject_type=data.get("inject_type", ""),
            top_k=data.get("top_k", 5),
            content=data.get("content", ""),
            memory_type=data.get("memory_type", ""),
            memory_layer=data.get("memory_layer", ""),
            route_key=data.get("route_key", ""),
            routes=data.get("routes", {}),
            extensions=data.get("extensions", []),
        )

    @staticmethod
    def _parse_context_config(data: dict[str, Any] | None) -> ContextConfig:
        """解析上下文配置。

        Args:
            data: 原始 YAML 字典，可能为 None。

        Returns:
            ContextConfig 实例。
        """
        if data is None:
            return ContextConfig()
        items = [AgentConfigLoader._parse_context_var_item(item) for item in (data.get("items") or [])]
        return ContextConfig(enabled=data.get("enabled", True), items=items)

    @staticmethod
    def _parse_knowledge_config(data: dict[str, Any] | None) -> KnowledgeConfig:
        """解析知识库配置。

        Args:
            data: 原始 YAML 字典，可能为 None。

        Returns:
            KnowledgeConfig 实例。
        """
        if data is None:
            return KnowledgeConfig()
        return KnowledgeConfig(
            mode=data.get("mode", "compressed"),
            max_tokens=data.get("max_tokens", 1000),
            top_k=data.get("top_k", 3),
            score_threshold=data.get("score_threshold", 0.7),
        )

    @staticmethod
    def _parse_rule_reinforcement(data: dict[str, Any] | None) -> RuleReinforcement:
        """解析规则强化配置。

        Args:
            data: 原始 YAML 字典，可能为 None。

        Returns:
            RuleReinforcement 实例。
        """
        if data is None:
            return RuleReinforcement()
        return RuleReinforcement(
            enabled=data.get("enabled", True),
            include_hard_constraints=data.get("include_hard_constraints", True),
            include_soft_constraints=data.get("include_soft_constraints", False),
            include_system_prompt_rules=data.get("include_system_prompt_rules", True),
            extraction_markers=data.get("extraction_markers", ["【重要】", "【必须】", "必须"]),
            custom_rules=data.get("custom_rules", []),
            template=data.get("template", ""),
            max_rules=data.get("max_rules", 10),
        )

    @staticmethod
    def _parse_deliverable(data: dict[str, Any]) -> DeliverableSpec:
        """解析产出物定义。

        Args:
            data: 原始 YAML 字典。

        Returns:
            DeliverableSpec 实例。
        """
        return DeliverableSpec(
            name=data.get("name", ""),
            description=data.get("description", ""),
            output_path=data.get("output_path", ""),
            type=data.get("type", "markdown"),
            template_source=data.get("template_source", ""),
            template_name=data.get("template_name", ""),
            required=data.get("required", True),
        )

    @staticmethod
    def _parse_metric_ref(data: dict[str, Any]) -> MetricRef:
        """解析评估指标引用。

        Args:
            data: 原始 YAML 字典。

        Returns:
            MetricRef 实例。
        """
        return MetricRef(
            metric_id=data.get("metric_id", ""),
            default_params=data.get("default_params", {}),
        )

    @staticmethod
    def _parse_plugins_config(data: dict[str, Any] | None) -> AgentPluginsConfig:
        """解析插件覆盖配置。

        Args:
            data: 原始 YAML 字典，可能为 None。

        Returns:
            AgentPluginsConfig 实例。
        """
        if data is None:
            return AgentPluginsConfig()
        return AgentPluginsConfig(
            disabled=data.get("disabled", []),
            enabled=data.get("enabled", {}),
        )

    @staticmethod
    def _resolve_agent_type(raw: str) -> AgentType:
        """解析 Agent 类型字符串。

        旧文件中使用 "main"/"orchestrator"/"system" 等，
        需要映射到 AgentType 枚举值。

        Args:
            raw: YAML 中的 agent_type 值。

        Returns:
            对应的 AgentType 枚举值。
        """
        mapping = {
            "main": AgentType.MAIN,
            "orchestrator": AgentType.SPECIALIZED,
            "specialized": AgentType.SPECIALIZED,
            "atomic": AgentType.SPECIALIZED,
            "system": AgentType.SYSTEM,
        }
        return mapping.get(raw, AgentType.SPECIALIZED)

    @staticmethod
    def _resolve_agent_level(raw: str) -> AgentLevel:
        """解析 Agent 层级字符串。

        Args:
            raw: YAML 中的 level 值（"L1"/"L2"/"L3"）。

        Returns:
            对应的 AgentLevel 枚举值。

        Raises:
            ValueError: 如果 level 值无法识别。
        """
        mapping = {
            "L1": AgentLevel.L1_MAIN,
            "L2": AgentLevel.L2_SUBTASK,
            "L3": AgentLevel.L3_ATOMIC,
        }
        if raw in mapping:
            return mapping[raw]
        raise ValueError(f"无法识别的 Agent 层级: {raw!r}")

    @classmethod
    def load_from_yaml(cls, path: str | Path) -> AgentConfig:
        """从单个 YAML 文件加载 Agent 配置。

        Args:
            path: YAML 文件路径。

        Returns:
            AgentConfig 实例。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: YAML 解析失败或必填字段缺失。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Agent 配置文件不存在: {path}")

        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML 解析失败 ({path}): {e}") from e

        if not isinstance(data, dict):
            raise ValueError(f"YAML 内容不是字典类型 ({path})")

        # 环境变量替换：${ENV_VAR} → os.environ.get("ENV_VAR", "")
        data = _substitute_env_vars(data)

        # 必填字段检查
        if not data.get("config_id"):
            raise ValueError(f"缺少必填字段 config_id ({path})")

        # 解析层级和类型
        raw_level = data.get("level", "L3")
        try:
            level = cls._resolve_agent_level(raw_level)
        except ValueError as e:
            raise ValueError(f"层级解析失败 ({path}): {e}") from e

        raw_agent_type = data.get("agent_type", "specialized")
        agent_type = cls._resolve_agent_type(raw_agent_type)

        # 解析嵌套结构
        static_vars = cls._parse_context_config(data.get("static_vars"))
        dynamic_vars = cls._parse_context_config(data.get("dynamic_vars"))
        knowledge = cls._parse_knowledge_config(data.get("knowledge"))
        rule_reinforcement = cls._parse_rule_reinforcement(data.get("rule_reinforcement"))
        plugins = cls._parse_plugins_config(data.get("plugins"))
        deliverables = [cls._parse_deliverable(d) for d in data.get("deliverables", [])]
        recommended_metrics = [cls._parse_metric_ref(m) for m in data.get("recommended_metrics", [])]

        return AgentConfig(
            config_id=data.get("config_id", ""),
            name=data.get("name", ""),
            display_name=data.get("display_name", data.get("name", "")),
            description=data.get("description", ""),
            agent_type=agent_type,
            category=data.get("category", ""),
            level=level,
            model_name=data.get("model_name", ""),
            model_tier=data.get("model_tier", ""),
            system_prompt=data.get("system_prompt", ""),
            tool_ids=data.get("tool_ids", []),
            static_vars=static_vars,
            dynamic_vars=dynamic_vars,
            context_variables=data.get("context_variables", {}),
            knowledge=knowledge,
            hard_constraints=data.get("hard_constraints", []),
            soft_constraints=data.get("soft_constraints", []),
            rule_reinforcement=rule_reinforcement,
            deliverables=deliverables,
            recommended_metrics=recommended_metrics,
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            version=data.get("version", "1.0.0"),
            is_active=data.get("is_active", True),
            status=data.get("status", "active"),
            max_iterations=data.get("max_iterations", 100),
            max_reminders=data.get("max_reminders", 3),
            timeout_seconds=data.get("timeout_seconds", -1),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
            plugins=plugins,
        )

    @classmethod
    def load_from_directory(cls, dir_path: str | Path, *, strict: bool = True) -> list[AgentConfig]:
        """从目录递归加载所有 YAML Agent 配置。

        单个文件的失败默认不中断整体加载（字段缺失等校验错误会被跳过）。
        YAML **语法错误**默认上抛——这是经过 ``tests/test_yaml_error_chain.py``
        守护的契约，便于发现配置写坏。

        装配层（如 ``AgentRegistry.load_directory``）应传 ``strict=False``：
        将语法错误也按文件隔离，避免单个坏配置拖垮整个引擎初始化。

        Args:
            dir_path: 目录路径。
            strict: True（默认）时 YAML 语法错误上抛 ValueError，中断加载；
                False 时任何单文件错误均跳过并记录 warning。

        Returns:
            AgentConfig 列表。

        Raises:
            FileNotFoundError: 目录不存在。
            ValueError: ``strict=True`` 时遇到 YAML 语法错误。
        """
        dir_path = Path(dir_path)
        if not dir_path.exists():
            raise FileNotFoundError(f"Agent 配置目录不存在: {dir_path}")
        if not dir_path.is_dir():
            raise ValueError(f"路径不是目录: {dir_path}")

        configs: list[AgentConfig] = []
        for yaml_file in sorted(dir_path.rglob("*.yaml")):
            try:
                config = cls.load_from_yaml(yaml_file)
                configs.append(config)
                logger.debug("已加载 Agent 配置: %s (from %s)", config.config_id, yaml_file)
            except ValueError as e:
                # strict=True：YAML 语法错误（load_from_yaml 已包装为 ValueError，
                # 通过 __cause__ 链识别）需上抛，保留 fail-fast 契约。
                # strict=False：装配层使用，任何单文件错误（含语法错误）一律隔离。
                if strict and isinstance(e.__cause__, yaml.YAMLError):
                    raise
                logger.warning("跳过无效配置文件 %s: %s", yaml_file, e)
        return configs

    # ---- 异步加载方法 ----

    @classmethod
    async def load_from_yaml_async(cls, path: str | Path) -> AgentConfig:
        """异步版本的 load_from_yaml，将同步 I/O 卸载到线程池。

        Args:
            path: YAML 文件路径。

        Returns:
            AgentConfig 实例。
        """
        return await asyncio.to_thread(cls.load_from_yaml, path)

    @classmethod
    async def load_from_directory_async(cls, dir_path: str | Path, *, strict: bool = True) -> list[AgentConfig]:
        """异步版本的 load_from_directory，将同步 I/O 卸载到线程池。

        Args:
            dir_path: 目录路径。
            strict: 透传给 ``load_from_directory``，详见该方法说明。

        Returns:
            AgentConfig 列表。
        """
        return await asyncio.to_thread(cls.load_from_directory, dir_path, strict=strict)
