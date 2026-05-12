"""
上下文配置模块

提供统一的上下文层级配置管理，支持动态扩展新的上下文类型。

使用示例：
    # 加载配置
    config = ContextWindowConfig.load()

    # 获取层级模板
    template = config.get_template("system_prompt")

    # 获取层级顺序
    order = config.get_layer_order()

    # 获取预算
    budget = config.get_budget("l3_memory", context_window=8000)

    # 运行时扩展新层级
    config.register_layer("custom_layer", "## 自定义\n\n{content}", stability="dynamic")
"""

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class ContextWindowConfig:
    """上下文窗口配置

    统一管理所有上下文层级的配置，支持：
    - 从配置文件加载
    - 运行时注册新层级
    - 获取层级顺序和模板
    - 预算计算
    """

    # 版本
    version: str = "1.0"

    # 压缩触发比例
    compress_trigger_ratio: float = 0.5

    # 预算分配
    budgets: dict[str, float] = field(default_factory=dict)

    # 格式模板（层级ID -> 模板）
    templates: dict[str, str] = field(default_factory=dict)

    # 稳定性（层级ID -> stability）
    stability: dict[str, str] = field(default_factory=dict)

    # 预算键映射（层级ID -> 预算键，默认等于层级ID）
    budget_mapping: dict[str, str] = field(default_factory=dict)

    # 层级顺序
    layer_order: list[str] = field(default_factory=list)

    # 动态变量配置
    dynamic_variables: dict[str, Any] = field(default_factory=dict)

    # 检索配置
    retrieval: dict[str, Any] = field(default_factory=dict)

    # 压缩配置
    compression: dict[str, Any] = field(default_factory=dict)

    # 自定义层级（运行时扩展）
    custom_layers: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: str | None = None) -> "ContextWindowConfig":
        """从配置文件加载

        Args:
            config_path: 配置文件路径，None 使用默认路径

        Returns:
            配置对象
        """
        if config_path is None:
            config_path = "config/system/context_window_config.yaml"

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 加载自定义层级到 templates 和 stability
        templates = data.get("templates", {})
        stability = data.get("stability", {})
        budget_mapping = data.get("budget_mapping", {})

        # 强制要求 layer_order 配置必须存在且非空
        if "layer_order" not in data:
            raise KeyError(
                f"配置文件中缺少必需的 'layer_order' 字段。"
                f"请在配置文件 {config_path} 中添加 'layer_order' 字段，"
                f"指定上下层层级的顺序列表。"
            )
        layer_order = list(data["layer_order"])
        if not layer_order:
            raise ValueError(
                f"'layer_order' 字段不能为空列表。"
                f"请在配置文件 {config_path} 中配置有效的层级顺序，"
                f"至少包含一个层级标识符。"
            )

        for layer_id, layer_data in data.get("custom_layers", {}).items():
            # 添加模板
            if "template" in layer_data:
                templates[layer_id] = layer_data["template"]

            # 添加稳定性
            if "stability" in layer_data:
                stability[layer_id] = layer_data["stability"]

            # 添加预算映射
            if "budget_key" in layer_data:
                budget_mapping[layer_id] = layer_data["budget_key"]

            # 插入到指定位置
            insert_pos = layer_data.get("insert_position")
            if insert_pos and layer_id not in layer_order:
                position, target = insert_pos.split(":")
                if target in layer_order:
                    target_idx = layer_order.index(target)
                    if position == "after":
                        layer_order.insert(target_idx + 1, layer_id)
                    elif position == "before":
                        layer_order.insert(target_idx, layer_id)
            elif layer_id not in layer_order:
                layer_order.append(layer_id)

        return cls(
            version=data.get("version", "1.0"),
            compress_trigger_ratio=data.get("compress_trigger_ratio", 0.5),
            budgets=data.get("budgets", {}),
            templates=templates,
            stability=stability,
            budget_mapping=budget_mapping,
            layer_order=layer_order,
            dynamic_variables=data.get("dynamic_variables", {}),
            retrieval=data.get("retrieval", {}),
            compression=data.get("compression", {}),
            custom_layers=data.get("custom_layers", {}),
        )

    def register_layer(
        self,
        layer_id: str,
        template: str,
        stability: str = "dynamic",
        budget_key: str | None = None,
        insert_position: str | None = None,
    ) -> None:
        """注册新的上下文层级

        用于运行时动态扩展新的上下文类型。

        Args:
            layer_id: 层级标识符
            template: 格式模板
            stability: 稳定性 (stable/semi_stable/dynamic)
            budget_key: 预算键，None 表示使用 layer_id
            insert_position: 插入位置，如 "after:retrieval" 或 "before:user_message"
        """
        # 注册模板
        self.templates[layer_id] = template

        # 注册稳定性
        self.stability[layer_id] = stability

        # 注册预算映射
        if budget_key:
            self.budget_mapping[layer_id] = budget_key

        # 插入到顺序列表
        if layer_id not in self.layer_order:
            if insert_position:
                position, target = insert_position.split(":")
                if target in self.layer_order:
                    target_idx = self.layer_order.index(target)
                    if position == "after":
                        self.layer_order.insert(target_idx + 1, layer_id)
                    elif position == "before":
                        self.layer_order.insert(target_idx, layer_id)
                else:
                    self.layer_order.append(layer_id)
            else:
                self.layer_order.append(layer_id)

    def get_template(self, layer_id: str) -> str | None:
        """获取层级模板

        Args:
            layer_id: 层级标识符

        Returns:
            模板字符串，不存在返回 None
        """
        return self.templates.get(layer_id)

    def format_layer(self, layer_id: str, **kwargs) -> str | None:
        """格式化层级内容

        Args:
            layer_id: 层级标识符
            **kwargs: 模板变量

        Returns:
            格式化后的字符串，模板不存在返回 None
        """
        template = self.get_template(layer_id)
        if not template:
            return None
        try:
            return template.format(**kwargs)
        except KeyError:
            # 缺少变量时保留原样
            return template

    def get_layer_order(self) -> list[str]:
        """获取层级顺序

        Returns:
            层级标识符列表（只返回有模板的层级）
        """
        return [lid for lid in self.layer_order if lid in self.templates]

    def get_stability(self, layer_id: str) -> str:
        """获取层级稳定性

        Args:
            layer_id: 层级标识符

        Returns:
            稳定性 (stable/semi_stable/dynamic)，默认 dynamic
        """
        return self.stability.get(layer_id, "dynamic")

    def get_budget(self, layer_id: str, context_window: int) -> int:
        """获取层级的 Token 预算

        Args:
            layer_id: 层级标识符
            context_window: 上下文窗口大小

        Returns:
            Token 预算，未配置返回 0
        """
        # 获取预算键（默认等于层级ID）
        budget_key = self.budget_mapping.get(layer_id, layer_id)

        # 获取预算比例
        ratio = self.budgets.get(budget_key, 0)

        return int(context_window * ratio)

    def get_layers_by_stability(self, stability: str) -> list[str]:
        """按稳定性获取层级

        Args:
            stability: 稳定性: stable | semi_stable | dynamic

        Returns:
            层级标识符列表
        """
        return [lid for lid in self.layer_order
               if self.stability.get(lid) == stability and lid in self.templates]


# 全局配置实例（单例模式）
_context_config: ContextWindowConfig | None = None


def get_context_window_config() -> ContextWindowConfig:
    """获取全局上下文窗口配置

    Returns:
        配置对象（首次调用时自动加载）
    """
    global _context_config
    if _context_config is None:
        _context_config = ContextWindowConfig.load()
    return _context_config


def reload_context_window_config() -> ContextWindowConfig:
    """重新加载配置

    Returns:
        新的配置对象
    """
    global _context_config
    _context_config = ContextWindowConfig.load()
    return _context_config
