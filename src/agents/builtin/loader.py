"""
内置Agent配置加载器
"""

from pathlib import Path

import yaml

from src.agents.types import AgentConfig, AgentType


class BuiltinAgentLoader:
    """内置Agent配置加载器"""

    def __init__(self, config_dir: Path | None = None):
        """
        初始化加载器

        Args:
            config_dir: 配置文件目录，默认为 config/agents/
        """
        if config_dir is None:
            # 默认配置目录为 config/agents/，包含所有分类子目录
            config_dir = Path("config/agents")

        self.config_dir = Path(config_dir)
        self._configs_cache: dict[str, AgentConfig] | None = None

    def load_all(self) -> dict[str, AgentConfig]:
        """
        加载所有内置Agent配置

        递归加载 config/agents/ 及其所有子目录中的 YAML 配置文件

        Returns:
            Agent名称到配置的映射字典
        """
        if self._configs_cache is not None:
            return self._configs_cache

        configs = {}

        # 递归查找所有 YAML 文件
        if self.config_dir.exists():
            yaml_files = list(self.config_dir.rglob("*.yaml"))

            for yaml_file in yaml_files:
                # 跳过README
                if "README" in yaml_file.name:
                    continue

                try:
                    with open(yaml_file, encoding="utf-8") as f:
                        data = yaml.safe_load(f)

                    if not data or not isinstance(data, dict):
                        continue

                    # 处理agent_type（从字符串转为枚举）
                    if "agent_type" in data and isinstance(data["agent_type"], str):
                        # 处理特殊的 agent_type 值
                        agent_type_str = data["agent_type"].lower()
                        if agent_type_str == "main":
                            data["agent_type"] = AgentType.MAIN
                        elif agent_type_str == "subagent":
                            data["agent_type"] = AgentType.SUBAGENT
                        elif agent_type_str == "specialized":
                            data["agent_type"] = AgentType.SPECIALIZED
                        else:
                            data["agent_type"] = AgentType(agent_type_str)

                    # 创建配置对象（字段名已统一，无需映射）
                    config = AgentConfig(**data)
                    configs[config.name] = config

                except Exception as e:
                    print(f"加载配置文件失败: {yaml_file}, 错误: {e}")

        self._configs_cache = configs
        return configs

    def load(self, agent_name: str) -> AgentConfig | None:
        """
        加载指定Agent的配置

        Args:
            agent_name: Agent名称

        Returns:
            Agent配置对象，如果不存在返回None
        """
        configs = self.load_all()
        return configs.get(agent_name)

    def list_agents(self, agent_type: AgentType | None = None) -> list[str]:
        """
        列出所有Agent名称

        Args:
            agent_type: 可选的Agent类型过滤

        Returns:
            Agent名称列表
        """
        configs = self.load_all()

        if agent_type is not None:
            return [
                name
                for name, config in configs.items()
                if config.agent_type == agent_type
            ]

        return list(configs.keys())

    def get_system_agents(self) -> dict[str, AgentConfig]:
        """
        获取所有系统Agent配置

        Returns:
            系统Agent配置字典
        """
        configs = self.load_all()
        return {
            name: config
            for name, config in configs.items()
            if config.agent_type == AgentType.SYSTEM
        }

    def get_builtin_agents(self) -> dict[str, AgentConfig]:
        """
        获取所有内置Agent配置（非系统Agent）

        Returns:
            内置Agent配置字典
        """
        configs = self.load_all()
        return {
            name: config
            for name, config in configs.items()
            if config.agent_type in [AgentType.MAIN, AgentType.ATOMIC]
        }

    def get_agent_by_capability(self, capability: str) -> list[AgentConfig]:
        """
        根据能力获取Agent

        Args:
            capability: 能力标签

        Returns:
            具有该能力的Agent列表
        """
        configs = self.load_all()
        return [
            config
            for config in configs.values()
            if capability in config.metadata.get("capabilities", [])
        ]

    def reload(self) -> dict[str, AgentConfig]:
        """
        重新加载所有配置

        Returns:
            Agent名称到配置的映射字典
        """
        self._configs_cache = None
        return self.load_all()


# 全局加载器实例
_loader: BuiltinAgentLoader | None = None


def get_loader() -> BuiltinAgentLoader:
    """
    获取全局加载器实例

    Returns:
        加载器实例
    """
    global _loader
    if _loader is None:
        _loader = BuiltinAgentLoader()
    return _loader


def load_agent(agent_name: str) -> AgentConfig | None:
    """
    快捷方法：加载指定Agent配置

    Args:
        agent_name: Agent名称

    Returns:
        Agent配置对象
    """
    return get_loader().load(agent_name)


def load_all_agents() -> dict[str, AgentConfig]:
    """
    快捷方法：加载所有Agent配置

    Returns:
        Agent配置字典
    """
    return get_loader().load_all()


# 预定义的Agent名称常量
class AgentNames:
    """内置Agent名称常量"""

    # 系统Agent
    PLANNER = "planner_agent"
    EVALUATOR = "evaluator_agent"
    RECOVERY = "recovery_agent"

    # 内置Agent
    MAIN = "main_agent"
    CODE_ANALYZER = "code_analyzer"
    TEST_GENERATOR = "test_generator"

    # 进化Agent
    TASK_DECOMPOSER = "task_decomposer"
    TOOL_MAKER = "tool_maker"
    ERROR_ROUTER = "error_router"
    EXECUTION_AUDITOR = "execution_auditor"


if __name__ == "__main__":
    # 测试代码
    loader = BuiltinAgentLoader()

    print("=== 所有Agent ===")
    all_agents = loader.load_all()
    for name, config in all_agents.items():
        print(
            f"{name}: type={config.agent_type}, "
            f"model={config.model_name}, tools={len(config.tool_ids)}"
        )

    print("\n=== 系统Agent ===")
    system_agents = loader.get_system_agents()
    for name in system_agents:
        print(f"- {name}")

    print("\n=== 内置Agent ===")
    builtin_agents = loader.get_builtin_agents()
    for name in builtin_agents:
        print(f"- {name}")

    print("\n=== 按能力查找 ===")
    planning_agents = loader.get_agent_by_capability("planning")
    for config in planning_agents:
        print(f"- {config.name}")
