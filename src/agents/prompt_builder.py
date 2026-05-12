"""
提示词构建器模块

提供四层结构的提示词构建功能：
1. 系统静态层（可缓存）- system_prompt + tools_description + static_vars
2. 压缩层 - L3关键词 + L2三元组 + L1八段压缩
3. 消息层 - L0原始对话 + 用户消息
4. 尾部动态层（每轮变化）- dynamic_vars
"""

import logging
from datetime import datetime
from typing import Any

from src.agents.types import (
    AgentConfig,
    DynamicVarsConfig,
    PromptStructureConfig,
    StaticVarsConfig,
)
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class SandwichPromptBuilder:
    """
    四层提示词构建器

    构建结构化的提示词，包含四层结构：
    1. 系统静态层（可缓存）- system_prompt + tools_description + static_vars
    2. 压缩层 - L3关键词 + L2三元组 + L1八段压缩
    3. 消息层 - L0原始对话 + 用户消息
    4. 尾部动态层（每轮变化）- dynamic_vars
    """

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry | None = None,
        structure_config: PromptStructureConfig | None = None,
    ):
        """
        初始化提示词构建器

        Args:
            config: Agent 配置
            tool_registry: 工具注册表
            structure_config: 提示词结构配置
        """
        self.config = config
        self.tool_registry = tool_registry
        self.structure_config = structure_config or PromptStructureConfig()
        self._cached_static_layer: str | None = None

    def build_static_vars(self) -> str:
        """
        构建首部静态变量

        从 static_vars 配置中加载静态知识库源内容。
        这部分属于系统静态层，可以被缓存。

        Returns:
            静态变量文本
        """
        static_config = self.config.static_vars or StaticVarsConfig()

        if not static_config.enabled:
            return ""

        if not static_config.sources:
            return ""

        parts = []
        for source in static_config.sources:
            source_type = source.get("type", "")
            source_path = source.get("path", "")
            source_mode = source.get("mode", "full")

            if not source_path:
                continue

            # 根据类型和模式构建内容
            if source_type == "knowledge":
                parts.append(f"## 知识库 ({source_mode})\n来源: {source_path}")
            elif source_type == "rule":
                parts.append(f"## 规则 ({source_mode})\n来源: {source_path}")
            else:
                parts.append(f"## 静态资源 ({source_mode})\n来源: {source_path}")

        return "\n\n".join(parts)

    def build_dynamic_vars(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        model_name: str | None = None,
        knowledge_content: str | None = None,
        retrieval_content: str | None = None,
    ) -> str:
        """
        构建尾部动态变量

        解析占位符如 {{Date}}, {{Time}}, {{Knowledge}}, {{Retrieval}}, {{Rules}}
        并注入对应的动态内容。这部分每轮都会变化。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            model_name: 模型名称
            knowledge_content: 知识库内容
            retrieval_content: 检索召回内容

        Returns:
            动态变量文本
        """
        dynamic_config = self.config.dynamic_vars or DynamicVarsConfig()

        if not dynamic_config.enabled:
            return ""

        if not dynamic_config.vars:
            return ""

        # 构建变量值映射
        var_values: dict[str, str] = {}

        now = datetime.now()

        for var_name in dynamic_config.vars:
            if var_name == "Date":
                var_values["Date"] = now.strftime("%Y-%m-%d")
            elif var_name == "Time":
                var_values["Time"] = now.strftime("%H:%M:%S")
            elif var_name == "Knowledge":
                var_values["Knowledge"] = knowledge_content or ""
            elif var_name == "Retrieval":
                var_values["Retrieval"] = retrieval_content or ""
            elif var_name == "Rules":
                var_values["Rules"] = self._build_rules_content(dynamic_config)
            elif var_name == "SessionId":
                var_values["SessionId"] = session_id or ""
            elif var_name == "UserId":
                var_values["UserId"] = user_id or ""
            elif var_name == "ModelName":
                var_values["ModelName"] = model_name or ""
            elif var_name == "AgentName":
                var_values["AgentName"] = self.config.name

        # 过滤空值
        var_values = {k: v for k, v in var_values.items() if v}

        if not var_values:
            return ""

        # 格式化为文本
        parts = []
        for key, value in var_values.items():
            if key in ["Knowledge", "Retrieval", "Rules"]:
                # 多行内容使用块格式
                parts.append(f"## {key}\n{value}")
            else:
                # 单行内容使用键值对格式
                parts.append(f"- {key}: {value}")

        return "\n\n".join(parts)

    def _build_rules_content(self, dynamic_config: DynamicVarsConfig) -> str:
        """
        构建规则内容

        从 dynamic_vars.rules 配置中提取规则内容。

        Args:
            dynamic_config: 动态变量配置

        Returns:
            规则文本
        """
        rules_config = dynamic_config.rules

        if not rules_config.get("enabled", False):
            return ""

        rules: list[str] = []

        # 添加硬约束
        hard_constraints = rules_config.get("hard_constraints", [])
        if hard_constraints:
            rules.extend(hard_constraints)

        # 添加配置中的硬约束
        if self.config.hard_constraints:
            rules.extend(self.config.hard_constraints)

        # 添加配置中的软约束
        if self.config.soft_constraints:
            rules.extend(self.config.soft_constraints)

        # 去重
        seen = set()
        unique_rules = []
        for rule in rules:
            rule_key = rule.strip()
            if rule_key and rule_key not in seen:
                seen.add(rule_key)
                unique_rules.append(rule)

        # 限制规则数量
        max_rules = rules_config.get("max_rules", 10)
        if len(unique_rules) > max_rules:
            unique_rules = unique_rules[:max_rules]

        if not unique_rules:
            return ""

        return "\n".join(f"• {rule}" for rule in unique_rules)

    def build_full_context(
        self,
        user_message: str,
        session_id: str | None = None,
        user_id: str | None = None,
        model_name: str | None = None,
        memory_l1: str | None = None,
        memory_l2: str | None = None,
        memory_l3: str | None = None,
        knowledge_content: str | None = None,
        retrieval_content: str | None = None,
        recent_messages: str | None = None,
    ) -> str:
        """
        构建完整上下文

        按照四层结构组装提示词：
        1. 系统静态层（可缓存）
        2. 压缩层
        3. 消息层
        4. 尾部动态层

        Args:
            user_message: 用户消息
            session_id: 会话 ID
            user_id: 用户 ID
            model_name: 模型名称
            memory_l1: L1 层记忆内容（八段压缩）
            memory_l2: L2 层记忆内容（三元组摘要）
            memory_l3: L3 层记忆内容（关键词索引）
            knowledge_content: 知识库内容
            retrieval_content: 检索召回内容
            recent_messages: 最近消息内容

        Returns:
            完整提示词文本
        """
        parts = []

        # 按层级顺序构建内容
        for layer in self.structure_config.layer_order:
            content = self._build_layer_content(
                layer=layer,
                user_message=user_message,
                session_id=session_id,
                user_id=user_id,
                model_name=model_name,
                memory_l1=memory_l1,
                memory_l2=memory_l2,
                memory_l3=memory_l3,
                knowledge_content=knowledge_content,
                retrieval_content=retrieval_content,
                recent_messages=recent_messages,
            )
            if content:
                parts.append(content)

        return "\n\n".join(parts)

    def _build_layer_content(
        self,
        layer: str,
        user_message: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        model_name: str | None = None,
        memory_l1: str | None = None,
        memory_l2: str | None = None,
        memory_l3: str | None = None,
        knowledge_content: str | None = None,
        retrieval_content: str | None = None,
        recent_messages: str | None = None,
    ) -> str:
        """
        构建单个层级的内容

        Args:
            layer: 层级名称
            user_message: 用户消息
            session_id: 会话 ID
            user_id: 用户 ID
            model_name: 模型名称
            memory_l1: L1 层记忆内容
            memory_l2: L2 层记忆内容
            memory_l3: L3 层记忆内容
            knowledge_content: 知识库内容
            retrieval_content: 检索召回内容
            recent_messages: 最近消息内容

        Returns:
            层级内容文本
        """
        # ===== 第1层：系统静态层（可缓存）=====
        if layer == "system_prompt":
            if self.structure_config.include_system_prompt:
                return self.config.system_prompt

        elif layer == "tools_description":
            if self.structure_config.include_tools_description:
                return self._build_tools_description()

        elif layer == "static_vars":
            if self.structure_config.include_static_vars:
                return self.build_static_vars()

        # ===== 第2层：压缩层 =====
        elif layer == "l3_memory":
            if self.structure_config.include_memory_l3 and memory_l3:
                return f"## 历史关键词\n{memory_l3}"

        elif layer == "l2_memory":
            if self.structure_config.include_memory_l2 and memory_l2:
                return f"## 历史摘要\n{memory_l2}"

        elif layer == "l1_memory":
            if self.structure_config.include_memory_l1 and memory_l1:
                return f"## 历史详情\n{memory_l1}"

        # ===== 第3层：消息层 =====
        elif layer == "recent_messages":
            if self.structure_config.include_recent_messages:
                parts = []
                if recent_messages:
                    parts.append(f"## 对话历史\n{recent_messages}")
                if user_message:
                    parts.append(f"## 用户消息\n{user_message}")
                return "\n\n".join(parts) if parts else ""

        # ===== 第4层：尾部动态层 =====
        elif layer == "dynamic_vars":
            if self.structure_config.include_dynamic_vars:
                return self.build_dynamic_vars(
                    session_id=session_id,
                    user_id=user_id,
                    model_name=model_name,
                    knowledge_content=knowledge_content,
                    retrieval_content=retrieval_content,
                )

        return ""

    def build_static_layer(self) -> str:
        """
        构建系统静态层

        包含 system_prompt、tools_description 和 static_vars。
        这部分内容可以被缓存，因为它不随对话变化。

        Returns:
            系统静态层文本
        """
        # 检查缓存
        if self._cached_static_layer is not None:
            return self._cached_static_layer

        parts = []

        # 系统提示词
        if self.structure_config.include_system_prompt:
            parts.append(self.config.system_prompt)

        # 工具描述
        if self.structure_config.include_tools_description and self.tool_registry:
            tool_desc = self._build_tools_description()
            if tool_desc:
                parts.append(tool_desc)

        # 静态变量
        if self.structure_config.include_static_vars:
            static_vars = self.build_static_vars()
            if static_vars:
                parts.append(static_vars)

        static_layer = "\n\n".join(parts)
        self._cached_static_layer = static_layer
        return static_layer

    def _build_tools_description(self) -> str:
        """
        构建工具描述

        Returns:
            工具描述文本
        """
        if not self.tool_registry or not self.config.tool_ids:
            return ""

        tools = []
        for tool_id in self.config.tool_ids:
            tool = self.tool_registry.get_tool(tool_id)
            if tool:
                tools.append(f"- {tool_id}: {getattr(tool, 'description', '无描述')}")

        if not tools:
            return ""

        return "## 可用工具\n" + "\n".join(tools)

    def get_cache_status(self) -> dict[str, Any]:
        """
        获取缓存状态

        Returns:
            缓存状态字典
        """
        return {
            "static_layer_cached": self._cached_static_layer is not None,
        }

    def clear_cache(self) -> None:
        """清除缓存"""
        self._cached_static_layer = None
