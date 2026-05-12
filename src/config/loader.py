"""
配置文件加载器

自动将 YAML 配置文件加载到数据库中。
支持 Agent、Workflow 配置的同步。
支持环境变量替换和 .env 文件加载。
"""

import ast
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConfigNotFoundError, EnvVarNotFoundError
from src.db.models import AgentConfig, ToolLibrary, Workflow

logger = logging.getLogger(__name__)


class ConfigLoader:
    """配置文件加载器"""

    # 环境变量替换正则：${VAR} 或 ${VAR:-default}
    ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

    def __init__(
        self,
        config_dir: str | Path = "config",
        env_file: str | Path | None = None,
    ):
        """
        初始化加载器

        Args:
            config_dir: 配置文件根目录
            env_file: .env 文件路径（可选）
        """
        self._config_dir = Path(config_dir)
        self._env_vars: dict[str, str] = {}

        # 加载 .env 文件
        if env_file:
            self._load_env_file(Path(env_file))

    def _load_env_file(self, env_file: Path) -> None:
        """
        加载 .env 文件到内部环境变量字典

        Args:
            env_file: .env 文件路径
        """
        if not env_file.exists():
            return

        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    self._env_vars[key.strip()] = value.strip()

    def _get_env_var(self, var_name: str, default: str | None = None) -> str:
        """
        获取环境变量值

        优先级：系统环境变量 > .env 文件

        Args:
            var_name: 变量名
            default: 默认值

        Returns:
            变量值

        Raises:
            EnvVarNotFoundError: 变量不存在且无默认值
        """
        # 优先从系统环境变量获取
        value = os.environ.get(var_name)
        if value is not None:
            return value

        # 其次从 .env 文件获取
        value = self._env_vars.get(var_name)
        if value is not None:
            return value

        # 使用默认值
        if default is not None:
            return default

        raise EnvVarNotFoundError(var_name)

    def _substitute_env_vars(self, value: Any) -> Any:
        """
        递归替换配置中的环境变量

        支持格式：
        - ${VAR}: 必需的环境变量
        - ${VAR:-default}: 带默认值的环境变量

        Args:
            value: 配置值（可以是字符串、字典、列表等）

        Returns:
            替换后的值
        """
        if isinstance(value, str):

            def replace_match(match):
                var_name = match.group(1)
                default = match.group(2)  # 可能为 None
                return self._get_env_var(var_name, default)

            return self.ENV_VAR_PATTERN.sub(replace_match, value)

        elif isinstance(value, dict):
            return {k: self._substitute_env_vars(v) for k, v in value.items()}

        elif isinstance(value, list):
            return [self._substitute_env_vars(item) for item in value]

        return value

    def load(self, filename: str) -> dict[str, Any]:
        """
        加载单个 YAML 配置文件

        Args:
            filename: 配置文件名（相对于 config_dir）

        Returns:
            配置字典

        Raises:
            ConfigNotFoundError: 文件不存在
        """
        file_path = self._config_dir / filename

        if not file_path.exists():
            raise ConfigNotFoundError(str(file_path))

        config = self._load_yaml(file_path)
        if config is None:
            return {}

        # 替换环境变量
        return self._substitute_env_vars(config)

    def load_all(self) -> dict[str, Any]:
        """
        加载配置目录下所有 YAML 文件

        Returns:
            合并后的配置字典，键为文件名（不含扩展名）
        """
        result = {}

        if not self._config_dir.exists():
            return result

        for yaml_file in self._config_dir.glob("*.yaml"):
            try:
                config = self.load(yaml_file.name)
                # 使用文件名（不含扩展名）作为键
                key = yaml_file.stem
                result[key] = config
            except Exception:
                continue

        return result

    async def load_agents(
        self,
        session: AsyncSession,
        agents_dir: str = "agents",
        include_builtin: bool = True,
    ) -> list[str]:
        """
        加载 Agent 配置到数据库

        Args:
            session: 数据库会话
            agents_dir: Agent 配置目录（相对于 config_dir）
            include_builtin: 是否包含 config/agents/ 目录及其子目录

        Returns:
            加载的 Agent config_id 列表
        """
        loaded = []

        # 收集所有配置目录
        config_paths = []
        agents_path = self._config_dir / agents_dir
        if agents_path.exists():
            config_paths.append(agents_path)

        # 添加内置 Agent 目录（config/agents/ 及其所有子目录）
        if include_builtin:
            builtin_path = Path("config/agents")
            if builtin_path.exists():
                config_paths.append(builtin_path)

        # 遍历所有配置目录
        for config_path in config_paths:
            # 递归查找所有 YAML 文件
            for yaml_file in config_path.rglob("*.yaml"):
                # 跳过 README
                if "README" in yaml_file.name:
                    continue

                try:
                    config = self._load_yaml(yaml_file)
                    if not config or "config_id" not in config:
                        continue

                    config_id = config["config_id"]

                    # 检查是否已存在
                    existing = await session.execute(
                        select(AgentConfig).where(AgentConfig.config_id == config_id).limit(1)
                    )
                    agent = existing.scalar_one_or_none()

                    if agent:
                        # 更新现有配置
                        self._update_agent(agent, config)
                    else:
                        # 创建新配置
                        agent = self._create_agent(config)
                        session.add(agent)

                    loaded.append(config_id)

                except Exception as e:
                    logger.debug(f"加载 Agent 配置失败 {yaml_file}: {e}")
                    continue

        await session.commit()
        return loaded

    async def load_workflows(
        self, session: AsyncSession, workflows_dir: str = "workflows"
    ) -> list[str]:
        """
        加载工作流配置到数据库

        Args:
            session: 数据库会话
            workflows_dir: 工作流配置目录

        Returns:
            加载的工作流 ID 列表
        """
        loaded = []
        workflows_path = self._config_dir / workflows_dir

        if not workflows_path.exists():
            return loaded

        for yaml_file in workflows_path.rglob("*.yaml"):
            try:
                config = self._load_yaml(yaml_file)
                if not config or "id" not in config:
                    continue

                workflow_id = config["id"]

                # 检查是否已存在
                existing = await session.execute(
                    select(Workflow).where(Workflow.name == workflow_id)
                )
                workflow = existing.scalar_one_or_none()

                if workflow:
                    # 更新现有配置
                    self._update_workflow(workflow, config)
                else:
                    # 创建新配置
                    workflow = self._create_workflow(config)
                    session.add(workflow)

                loaded.append(workflow_id)

            except Exception as e:
                logger.debug(f"加载工作流配置失败 {yaml_file}: {e}")
                continue

        await session.commit()
        return loaded

    async def load_tools(
        self, session: AsyncSession, tools_dir: str = "src/tools/builtin"
    ) -> list[str]:
        """
        加载工具到数据库

        扫描 Python 文件，提取带 @tool 装饰器的函数信息

        Args:
            session: 数据库会话
            tools_dir: 工具目录

        Returns:
            加载的工具名称列表
        """
        loaded = []
        tools_path = Path(tools_dir)

        if not tools_path.exists():
            return loaded

        for py_file in tools_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue

            try:
                source_code = py_file.read_text(encoding="utf-8")

                # 解析 AST 提取工具信息
                tool_info = self._extract_tool_info(source_code, py_file.name)
                if not tool_info:
                    continue

                tool_name = tool_info["name"]

                # 检查是否已存在
                existing = await session.execute(
                    select(ToolLibrary).where(ToolLibrary.name == tool_name).limit(1)
                )
                tool = existing.scalar_one_or_none()

                if tool:
                    # 更新现有工具
                    tool.description = tool_info.get("description")
                    tool.source_code = source_code
                    tool.schema = tool_info.get("schema")
                else:
                    # 创建新工具
                    tool = ToolLibrary(
                        name=tool_name,
                        description=tool_info.get("description"),
                        source_code=source_code,
                        schema=tool_info.get("schema"),
                        status="active",
                        version="1.0.0",
                    )
                    session.add(tool)

                loaded.append(tool_name)

            except Exception as e:
                logger.debug(f"加载工具失败 {py_file}: {e}")
                continue

        await session.commit()
        return loaded

    def _extract_tool_info(
        self, source_code: str, filename: str
    ) -> dict[str, Any] | None:
        """
        从源代码提取工具信息

        Args:
            source_code: Python 源代码
            filename: 文件名

        Returns:
            工具信息字典或 None
        """
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 检查是否有 @tool 装饰器
                for decorator in node.decorator_list:
                    decorator_name = None
                    decorator_args = {}

                    if isinstance(decorator, ast.Name):
                        decorator_name = decorator.id
                    elif isinstance(decorator, ast.Call):
                        if isinstance(decorator.func, ast.Name):
                            decorator_name = decorator.func.id
                        # 提取装饰器参数
                        for keyword in decorator.keywords:
                            if keyword.arg and isinstance(keyword.value, ast.Constant):
                                decorator_args[keyword.arg] = keyword.value.value

                    if decorator_name == "tool":
                        # 提取函数文档字符串
                        docstring = ast.get_docstring(node) or ""

                        # 工具名称：优先使用装饰器参数，否则使用函数名
                        tool_name = decorator_args.get("name", node.name)
                        description = decorator_args.get(
                            "description", docstring.split("\n")[0] if docstring else ""
                        )

                        # 构建参数 schema
                        schema = self._build_tool_schema(node, docstring)

                        return {
                            "name": tool_name,
                            "description": description,
                            "schema": schema,
                        }

        return None

    def _build_tool_schema(self, func_node, docstring: str) -> dict[str, Any]:
        """从函数定义构建参数 schema"""
        properties = {}
        required = []

        for arg in func_node.args.args:
            arg_name = arg.arg
            if arg_name == "self":
                continue

            # 获取类型注解
            arg_type = "string"
            if arg.annotation:
                if isinstance(arg.annotation, ast.Name):
                    type_map = {
                        "str": "string",
                        "int": "integer",
                        "float": "number",
                        "bool": "boolean",
                    }
                    arg_type = type_map.get(arg.annotation.id, "string")
                elif isinstance(arg.annotation, ast.Constant):
                    arg_type = str(arg.annotation.value)

            properties[arg_name] = {"type": arg_type}

            # 检查是否有默认值
            defaults_count = len(func_node.args.defaults)
            args_count = len(func_node.args.args)
            default_start = args_count - defaults_count
            arg_index = func_node.args.args.index(arg)

            if arg_index < default_start:
                required.append(arg_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def sync_all(self, session: AsyncSession) -> dict[str, list[str]]:
        """
        同步所有配置到数据库

        Args:
            session: 数据库会话

        Returns:
            同步结果 {"agents": [...], "workflows": [...], "tools": [...]}
        """
        return {
            "agents": await self.load_agents(session),
            "workflows": await self.load_workflows(session),
            "tools": await self.load_tools(session),
        }

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """加载 YAML 文件"""
        try:
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            return None

    def _create_agent(self, config: dict[str, Any]) -> AgentConfig:
        """从配置创建 AgentConfig"""
        return AgentConfig(
            config_id=config["config_id"],
            name=config.get("name", config["config_id"]),
            description=config.get("description"),
            agent_type=config.get("agent_type", "atomic"),
            model_name=config.get("model_name", "deepseek-chat"),
            model_params=config.get("model_params", {}),
            system_prompt=config.get("system_prompt", ""),
            tool_ids=config.get("tool_ids", []),
            hard_constraints=config.get("hard_constraints", []),
            soft_constraints=config.get("soft_constraints", []),
            # 四层提示词结构配置
            static_vars=config.get("static_vars", {}),
            dynamic_vars=config.get("dynamic_vars", {}),
            context_variables=config.get("context_variables", {}),
            input_schema=config.get("input_schema", {}),
            output_schema=config.get("output_schema", {}),
            version=config.get("version", "1.0.0"),
            is_active=config.get("is_active", True),
            max_iterations=config.get("max_iterations", 10),
            timeout_seconds=config.get("timeout_seconds", 300),
        )

    def _update_agent(self, agent: AgentConfig, config: dict[str, Any]) -> None:
        """更新 AgentConfig"""
        agent.name = config.get("name", agent.name)
        agent.description = config.get("description", agent.description)
        agent.agent_type = config.get("agent_type", agent.agent_type)
        agent.model_name = config.get("model_name", agent.model_name)
        agent.model_params = config.get("model_params", agent.model_params)
        agent.system_prompt = config.get("system_prompt", agent.system_prompt)
        agent.tool_ids = config.get("tool_ids", agent.tool_ids)
        agent.hard_constraints = config.get("hard_constraints", agent.hard_constraints)
        agent.soft_constraints = config.get("soft_constraints", agent.soft_constraints)
        # 四层提示词结构配置
        agent.static_vars = config.get("static_vars", agent.static_vars)
        agent.dynamic_vars = config.get("dynamic_vars", agent.dynamic_vars)
        agent.context_variables = config.get(
            "context_variables", agent.context_variables
        )
        agent.input_schema = config.get("input_schema", agent.input_schema)
        agent.output_schema = config.get("output_schema", agent.output_schema)
        agent.version = config.get("version", agent.version)
        agent.is_active = config.get("is_active", agent.is_active)
        agent.max_iterations = config.get("max_iterations", agent.max_iterations)
        agent.timeout_seconds = config.get("timeout_seconds", agent.timeout_seconds)

    def _create_workflow(self, config: dict[str, Any]) -> Workflow:
        """从配置创建 Workflow"""
        metadata = config.get("metadata", {})
        return Workflow(
            name=config["id"],
            description=metadata.get("description", config.get("description")),
            type=metadata.get("category", "user_defined"),
            source=metadata.get("source", "native"),
            definition=config,
            inputs_schema=self._build_schema(config.get("inputs", [])),
            outputs_schema=self._build_schema(config.get("outputs", [])),
            status=config.get("status", "active"),
        )

    def _update_workflow(self, workflow: Workflow, config: dict[str, Any]) -> None:
        """更新 Workflow"""
        metadata = config.get("metadata", {})
        workflow.description = metadata.get("description", config.get("description"))
        workflow.type = metadata.get("category", workflow.type)
        workflow.definition = config
        workflow.inputs_schema = self._build_schema(config.get("inputs", []))
        workflow.outputs_schema = self._build_schema(config.get("outputs", []))
        workflow.status = config.get("status", workflow.status)

    def _build_schema(self, items: list[dict]) -> dict[str, Any]:
        """构建 JSON Schema"""
        if not items:
            return {}

        properties = {}
        required = []

        for item in items:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    properties[name] = {
                        "type": item.get("type", "string"),
                        "description": item.get("description", ""),
                    }
                    if item.get("required", False):
                        required.append(name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


# 便捷函数
async def sync_configs(
    session: AsyncSession, config_dir: str = "config"
) -> dict[str, list[str]]:
    """
    同步配置文件到数据库

    Args:
        session: 数据库会话
        config_dir: 配置目录

    Returns:
        同步结果
    """
    loader = ConfigLoader(config_dir)
    return await loader.sync_all(session)
