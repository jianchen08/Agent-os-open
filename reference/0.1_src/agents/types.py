"""Agent 配置系统数据类型定义。

包含 Agent 配置的所有枚举、数据类和工厂函数，
供加载器、注册表、上下文构建器和 Schema 验证器共同使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentLevel(Enum):
    """Agent 层级枚举。"""

    L1_MAIN = "L1"
    L2_SUBTASK = "L2"
    L3_ATOMIC = "L3"


class AgentType(Enum):
    """Agent 类型枚举。"""

    MAIN = "main"
    SPECIALIZED = "specialized"
    SYSTEM = "system"


@dataclass
class ContextVarItem:
    """上下文变量项。

    Attributes:
        name: 变量名称。
        type: 变量类型（rules/path/folder/timestamp/session/agent/model/retrieval/routed）。
        path: 文件路径（type=path 时使用）或文件夹路径（type=folder 时使用）。
        tags: 标签列表（用于知识库检索）。
        inject_type: 注入方式（full/summary/retrieval）。
        top_k: 检索数量。
        content: 直接内容（内联注入）。
        memory_type: 记忆类型。
        memory_layer: 记忆层级。
        route_key: 路由键名（type=routed 时使用），从管道 state 中取对应值作为路由依据。
        routes: 路由表（type=routed 时使用），键为可能的 state 值，值为注入内容（字符串或嵌套变量定义）。
        extensions: 文件扩展名过滤列表（type=folder 时使用），如 [".py", ".md"]。为空则加载所有文件。
    """

    name: str = ""
    type: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)
    inject_type: str = ""
    top_k: int = 5
    content: str = ""
    memory_type: str = ""
    memory_layer: str = ""
    route_key: str = ""
    routes: dict[str, Any] = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)


@dataclass
class ContextConfig:
    """上下文配置（静态变量 + 动态变量）。

    Attributes:
        enabled: 是否启用上下文注入。
        items: 上下文变量项列表。
    """

    enabled: bool = True
    items: list[ContextVarItem] = field(default_factory=list)


@dataclass
class KnowledgeConfig:
    """知识库配置。

    Attributes:
        mode: 知识库模式（compressed/full）。
        max_tokens: 最大 Token 数。
        top_k: 检索数量。
        score_threshold: 相似度阈值。
    """

    mode: str = "compressed"
    max_tokens: int = 1000
    top_k: int = 3
    score_threshold: float = 0.7


@dataclass
class RuleReinforcement:
    """规则强化配置。

    Attributes:
        enabled: 是否启用规则强化。
        include_hard_constraints: 是否包含硬约束。
        include_soft_constraints: 是否包含软约束。
        include_system_prompt_rules: 是否包含系统提示词中的规则。
        extraction_markers: 规则提取标记。
        custom_rules: 自定义规则列表。
        template: 规则模板。
        max_rules: 最大规则数量。
    """

    enabled: bool = True
    include_hard_constraints: bool = True
    include_soft_constraints: bool = False
    include_system_prompt_rules: bool = True
    extraction_markers: list[str] = field(default_factory=lambda: ["【重要】", "【必须】", "必须"])
    custom_rules: list[str] = field(default_factory=list)
    template: str = ""
    max_rules: int = 10


@dataclass
class DeliverableSpec:
    """产出物定义。

    Attributes:
        name: 产出物名称。
        description: 产出物描述。
        output_path: 输出路径。
        type: 产出物类型（markdown/json/yaml）。
        template_source: 模板来源（knowledge/path）。
        template_name: 模板名称。
        required: 是否必须。
    """

    name: str = ""
    description: str = ""
    output_path: str = ""
    type: str = "markdown"
    template_source: str = ""
    template_name: str = ""
    required: bool = True


@dataclass
class MetricRef:
    """评估指标引用。

    Attributes:
        metric_id: 指标 ID。
        default_params: 默认参数。
    """

    metric_id: str = ""
    default_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentPluginsConfig:
    """Agent 插件覆盖配置。

    Agent 通过此配置覆盖 Pipeline 默认插件列表：
    - disabled: 禁用的默认插件名称列表
    - enabled: 启用的非默认插件及其参数

    Attributes:
        disabled: 要禁用的默认插件名称列表。
        enabled: 要启用的非默认插件及其参数，键为插件名，值为配置字典。
    """

    disabled: list[str] = field(default_factory=list)
    enabled: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Agent 完整配置。

    Agent 是插件的参数配置集 + 行为描述，它向 Pipeline 提供配置，但不控制执行流程。

    Attributes:
        config_id: 配置唯一标识。
        name: Agent 名称。
        display_name: 显示名称。
        description: Agent 描述。
        agent_type: Agent 类型。
        category: Agent 分类。
        level: Agent 层级。
        system_prompt: 系统提示词。
        tool_ids: 绑定的工具 ID 列表。
        static_vars: 静态变量配置（第1层，会话级不变）。
        dynamic_vars: 动态变量配置（第4层，每轮变化）。
        context_variables: 上下文变量配置。
        knowledge: 知识库配置。
        hard_constraints: 硬约束列表。
        soft_constraints: 软约束列表。
        rule_reinforcement: 规则强化配置。
        deliverables: 产出物定义列表。
        recommended_metrics: 推荐评估指标列表。
        input_schema: 输入 Schema。
        output_schema: 输出 Schema。
        version: 版本号。
        is_active: 是否启用。
        status: 状态。
        max_iterations: 最大迭代次数。
        max_reminders: 最大提醒次数。
        timeout_seconds: 超时时间（秒），-1 表示不限。
        tags: 标签列表。
        metadata: 元数据。
        plugins: 插件覆盖配置（disabled/enabled）。
        model_name: 指定 LLM 模型标识（如 minimax-m2.7、glm-4.7），覆盖 pipeline 默认模型。
            优先级高于 model_tier。
        model_tier: 模型分级标识（large/medium/small），从 llm.yaml defaults.tiers 解析为 model_name。
            优先级低于 model_name。
    """

    config_id: str = ""
    name: str = ""
    display_name: str = ""
    description: str = ""
    agent_type: AgentType = AgentType.SPECIALIZED
    category: str = ""
    level: AgentLevel = AgentLevel.L3_ATOMIC
    model_name: str = ""
    model_tier: str = ""
    system_prompt: str = ""
    tool_ids: list[str] = field(default_factory=list)
    static_vars: ContextConfig = field(default_factory=ContextConfig)
    dynamic_vars: ContextConfig = field(default_factory=ContextConfig)
    context_variables: dict[str, Any] = field(default_factory=dict)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    hard_constraints: list[str] = field(default_factory=list)
    soft_constraints: list[str] = field(default_factory=list)
    rule_reinforcement: RuleReinforcement = field(default_factory=RuleReinforcement)
    deliverables: list[DeliverableSpec] = field(default_factory=list)
    recommended_metrics: list[MetricRef] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    is_active: bool = True
    status: str = "active"
    max_iterations: int = 100
    max_reminders: int = 3
    timeout_seconds: int = -1
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    plugins: AgentPluginsConfig = field(default_factory=AgentPluginsConfig)

    def to_state(self) -> dict[str, Any]:
        """将 Agent 配置转换为管道 state 注入字典。

        包含 system_prompt、tool_ids、constraints、static_vars、dynamic_vars
        和 plugin_configs，供 PipelineEngine 在运行时注入到管道 state 中。

        静态变量通过 context.static_vars 传递给 PromptBuildPlugin，
        由插件在构建系统提示词时动态加载（支持文件读取、向量检索等模式）。
        动态变量通过 context.dynamic_vars 传递，由 LLMCore 追加到消息列表末尾。

        Returns:
            包含 Agent 参数的状态字典
        """
        state: dict[str, Any] = {}

        # 将 Agent 层级写入 state，供 ParamInjectPlugin 注入 parent_agent_level
        state["agent_level"] = self.level.value

        if self.config_id:
            state["agent_config_id"] = self.config_id

        if self.system_prompt:
            state["system_prompt"] = self._build_full_system_prompt()

        if self.tool_ids:
            state["tool_ids"] = self.tool_ids

        state["constraints"] = {
            "hard": self.hard_constraints or [],
            "soft": self.soft_constraints or [],
        }

        if self.static_vars.enabled and self.static_vars.items:
            sv_items = [
                {
                    "name": item.name,
                    "type": item.type,
                    "path": item.path,
                    "content": item.content,
                    "tags": item.tags,
                    "inject_type": item.inject_type,
                    "top_k": item.top_k,
                    "memory_type": item.memory_type,
                    "memory_layer": item.memory_layer,
                    "route_key": item.route_key,
                    "routes": item.routes,
                    "extensions": item.extensions,
                }
                for item in self.static_vars.items
            ]
            if self.config_id:
                sv_items.append(
                    {
                        "name": "agent_self_memory",
                        "tags": [self.config_id],
                        "inject_type": "retrieval",
                        "top_k": 5,
                    }
                )
            state["context.static_vars"] = sv_items
        elif self.config_id:
            state["context.static_vars"] = [
                {
                    "name": "agent_self_memory",
                    "tags": [self.config_id],
                    "inject_type": "retrieval",
                    "top_k": 5,
                }
            ]

        if self.dynamic_vars.enabled and self.dynamic_vars.items:
            state["context.dynamic_vars"] = [
                {
                    "name": item.name,
                    "type": item.type,
                    "path": item.path,
                    "content": item.content,
                    "tags": item.tags,
                    "inject_type": item.inject_type,
                    "top_k": item.top_k,
                    "memory_type": item.memory_type,
                    "memory_layer": item.memory_layer,
                    "route_key": item.route_key,
                    "routes": item.routes,
                    "extensions": item.extensions,
                }
                for item in self.dynamic_vars.items
            ]

        if self.max_iterations:
            state["max_iterations"] = self.max_iterations

        if self.max_reminders:
            state["max_reminders"] = self.max_reminders

        # 允许 timeout_seconds=-1（无限制）通过
        if self.timeout_seconds is not None and self.timeout_seconds != 0:
            state["timeout_seconds"] = self.timeout_seconds

        plugin_configs = self.get_plugin_configs()
        if plugin_configs:
            state["plugin_configs"] = plugin_configs

        return state

    def get_plugin_configs(self) -> dict[str, Any]:
        """获取合并后的插件配置字典。

        将 plugins.enabled 中的配置合并为一个字典，
        供各插件从 ctx.state["plugin_configs"] 读取。

        Returns:
            插件名到配置字典的映射
        """
        configs: dict[str, Any] = {}
        for name, config in self.plugins.enabled.items():
            configs[name] = {"enabled": True, **config}
        for name in self.plugins.disabled:
            if name not in configs:
                configs[name] = {"enabled": False}
        return configs

    def _build_full_system_prompt(self) -> str:
        """构建核心系统提示词。

        仅组装 system_prompt 原文 + 硬约束 + 软约束。
        静态上下文（模板、规则、文件内容等）由 PromptBuildPlugin
        在运行时通过 _load_static_vars() 动态加载并拼入系统提示词。

        Returns:
            核心系统提示词字符串
        """
        parts: list[str] = []

        if self.system_prompt:
            parts.append(self.system_prompt.strip())

        if self.hard_constraints:
            parts.append("\n## 硬约束（必须遵守）\n")
            for i, c in enumerate(self.hard_constraints, 1):
                parts.append(f"{i}. {c}")

        if self.soft_constraints:
            parts.append("\n## 软约束（尽量遵守）\n")
            for i, c in enumerate(self.soft_constraints, 1):
                parts.append(f"{i}. {c}")

        parts.append(self._environment_context_block())
        return "\n".join(parts)

    @staticmethod
    def _environment_context_block() -> str:
        """运行时探测并生成环境信息块，拼入系统提示词。

        让 Agent 知道当前运行的宿主环境（OS/架构/shell）和容器执行环境，
        从而写出符合当前环境的命令——而非依赖命令翻译。
        容器执行环境恒为 Linux（cua 镜像基于 python:slim），跨机器一致；
        宿主环境按运行时探测，部署到不同机器自动适配。
        结果按进程缓存（环境不会在进程内变化）。
        """
        cached = getattr(AgentConfig, "_env_block_cache", None)  # noqa: PLC0415
        if cached is not None:
            return cached

        import platform  # noqa: PLC0415
        import shutil  # noqa: PLC0415

        os_name = platform.system() or "unknown"
        machine = platform.machine() or "unknown"
        # 识别 shell：优先探测，失败则按 OS 给默认值
        if os_name == "Windows":
            shell = "cmd" if shutil.which("cmd") else "powershell"
        else:
            shell = "bash" if shutil.which("bash") else "sh"

        block = (
            "\n## 当前运行环境\n"
            f"- 宿主操作系统: {os_name} {machine}\n"
            f"- 宿主 Shell: {shell}\n"
            "- 容器执行环境: Linux (sh -c), 容器内请写 POSIX 命令\n"
            "- 请写符合当前环境的命令; 宿主机操作写宿主命令, 容器内操作写 POSIX 命令\n"
        )
        AgentConfig._env_block_cache = block  # type: ignore[attr-defined]
        return block
