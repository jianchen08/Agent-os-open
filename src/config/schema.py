"""配置 Schema 校验系统。

统一校验 Pipeline 和 Agent 配置的完整性，不引入第三方 jsonschema 库。
采用简化校验策略，与 M7 SchemaValidator 思路一致。

典型用法::

    from config import ConfigSchemaValidator

    validator = ConfigSchemaValidator()
    errors = validator.validate_pipeline_config(data)
    if errors:
        print("Pipeline 配置校验失败:", errors)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from agents.loader import VALID_AGENT_TYPE_KEYS

logger = logging.getLogger(__name__)

# Agent 合法枚举值
_VALID_AGENT_LEVELS: set[str] = {"L1", "L2", "L3"}
# agent_type 合法集合与 loader._resolve_agent_type 的映射键保持一致，
# 防止热重载校验误拒 orchestrator/atomic 等编排/原子 Agent（单一真相源）。
_VALID_AGENT_TYPES: set[str] = set(VALID_AGENT_TYPE_KEYS)

# Model 合法必填字段
_MODEL_REQUIRED_FIELDS: set[str] = {"provider", "model_name"}
_EMBEDDING_REQUIRED_FIELDS: set[str] = {"provider", "model_name"}


class ConfigSchemaValidator:
    """统一配置 Schema 校验器。

    支持：
    - Pipeline 配置校验（name、input_routes、output_routes 必填）
    - Agent 配置校验（config_id、name 必填，level/agent_type 合法值）
    - Model 配置校验（models 必填，每个模型需 provider + model_name）
    - 通用 YAML 校验（语法正确性、必填字段）
    - 目录批量校验
    """

    def validate_model_config(self, data: dict[str, Any]) -> list[str]:
        """校验 Model 配置数据。

        检查项：
        - ``models`` 必填且为字典
        - 每个模型必须有 ``provider`` 和 ``model_name`` 字段
        - ``defaults`` 若存在，必须为字典
        - ``providers`` 若存在，必须为字典

        Args:
            data: Model 配置字典（llm.yaml 解析后的完整数据）。

        Returns:
            错误列表，空列表表示校验通过。
        """
        errors: list[str] = []

        # models 必填
        if "models" not in data:
            errors.append("缺少必填字段: models")
        elif not isinstance(data["models"], dict):
            errors.append("models 必须为字典")
        else:
            # 校验每个模型条目
            for model_id, model_conf in data["models"].items():
                if not isinstance(model_conf, dict):
                    errors.append(f"模型 {model_id!r} 配置必须为字典")
                    continue
                for field_name in _MODEL_REQUIRED_FIELDS:
                    if field_name not in model_conf:
                        errors.append(f"模型 {model_id!r} 缺少必填字段: {field_name}")
                    elif not isinstance(model_conf[field_name], str) or not model_conf[field_name].strip():
                        errors.append(f"模型 {model_id!r} 的 {field_name} 必须为非空字符串")

        # defaults 若存在必须为字典
        if "defaults" in data and not isinstance(data["defaults"], dict):
            errors.append("defaults 必须为字典")

        # providers 若存在必须为字典
        if "providers" in data and not isinstance(data["providers"], dict):
            errors.append("providers 必须为字典")

        return errors

    def validate_pipeline_config(self, data: dict[str, Any]) -> list[str]:
        """校验 Pipeline 配置数据。

        检查项：
        - ``name`` 必填且为字符串
        - ``input_routes`` 必填且为列表
        - ``output_routes`` 必填且为列表

        Args:
            data: Pipeline 配置字典。

        Returns:
            错误列表，空列表表示校验通过。
        """
        errors: list[str] = []

        # name 必填
        if "name" not in data:
            errors.append("缺少必填字段: name")
        elif not isinstance(data["name"], str) or not data["name"].strip():
            errors.append("name 必须为非空字符串")

        # input_routes 必填
        if "input_routes" not in data:
            errors.append("缺少必填字段: input_routes")
        elif not isinstance(data["input_routes"], list):
            errors.append("input_routes 必须为列表")

        # output_routes 必填
        if "output_routes" not in data:
            errors.append("缺少必填字段: output_routes")
        elif not isinstance(data["output_routes"], list):
            errors.append("output_routes 必须为列表")

        return errors

    def validate_agent_config(self, data: dict[str, Any]) -> list[str]:
        """校验 Agent 配置数据。

        检查项：
        - ``config_id`` 必填且为字符串
        - ``name`` 必填且为字符串
        - ``level`` 若存在，必须为合法值（L1/L2/L3）
        - ``agent_type`` 若存在，必须为合法值（与 loader 一致：main/orchestrator/specialized/atomic/system）

        Args:
            data: Agent 配置字典。

        Returns:
            错误列表，空列表表示校验通过。
        """
        errors: list[str] = []

        # config_id 必填
        if "config_id" not in data:
            errors.append("缺少必填字段: config_id")
        elif not isinstance(data["config_id"], str) or not data["config_id"].strip():
            errors.append("config_id 必须为非空字符串")

        # name 必填
        if "name" not in data:
            errors.append("缺少必填字段: name")
        elif not isinstance(data["name"], str) or not data["name"].strip():
            errors.append("name 必须为非空字符串")

        # level 合法值
        if "level" in data:
            level = data["level"]
            if isinstance(level, str) and level not in _VALID_AGENT_LEVELS:
                errors.append(f"level 值 {level!r} 不合法，应为 {sorted(_VALID_AGENT_LEVELS)}")

        # agent_type 合法值
        if "agent_type" in data:
            agent_type = data["agent_type"]
            if isinstance(agent_type, str) and agent_type not in _VALID_AGENT_TYPES:
                errors.append(f"agent_type 值 {agent_type!r} 不合法，应为 {sorted(_VALID_AGENT_TYPES)}")

        return errors

    def validate_yaml_file(self, path: str | Path, config_type: str = "auto") -> list[str]:
        """校验单个 YAML 配置文件。

        先检查 YAML 语法正确性，再根据配置类型进行字段校验。

        Args:
            path: YAML 文件路径。
            config_type: 配置类型，可选 ``pipeline``、``agent``、``auto``。
                ``auto`` 根据文件路径自动判断。

        Returns:
            错误列表，空列表表示校验通过。
        """
        path = Path(path)
        errors: list[str] = []

        # 文件存在性检查
        if not path.exists():
            return [f"文件不存在: {path}"]

        # YAML 语法检查
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            return [f"YAML 语法错误: {exc}"]

        if not isinstance(data, dict):
            return [f"YAML 内容应为字典，实际为 {type(data).__name__}"]

        # 确定配置类型
        resolved_type = config_type
        if config_type == "auto":
            resolved_type = self._detect_config_type(path, data)

        # 按类型校验
        if resolved_type == "pipeline":
            errors.extend(self.validate_pipeline_config(data))
        elif resolved_type == "agent":
            errors.extend(self.validate_agent_config(data))
        elif resolved_type == "model":
            errors.extend(self.validate_model_config(data))
        else:
            # unknown 类型仅做基础校验
            logger.debug("未知配置类型，跳过字段校验 | path=%s", path)

        return errors

    def validate_directory(self, dir_path: str | Path, config_type: str = "auto") -> dict[str, list[str]]:
        """批量校验目录下的所有 YAML 配置文件。

        Args:
            dir_path: 目录路径。
            config_type: 配置类型，可选 ``pipeline``、``agent``、``auto``。

        Returns:
            字典，键为文件路径，值为该文件的错误列表。
        """
        dir_path = Path(dir_path)
        results: dict[str, list[str]] = {}

        if not dir_path.exists():
            return {str(dir_path): [f"目录不存在: {dir_path}"]}

        if not dir_path.is_dir():
            return {str(dir_path): [f"路径不是目录: {dir_path}"]}

        for yaml_file in sorted(dir_path.rglob("*.yaml")):
            file_errors = self.validate_yaml_file(yaml_file, config_type)
            if file_errors:
                results[str(yaml_file)] = file_errors

        for yaml_file in sorted(dir_path.rglob("*.yml")):
            if str(yaml_file) not in results:
                file_errors = self.validate_yaml_file(yaml_file, config_type)
                if file_errors:
                    results[str(yaml_file)] = file_errors

        return results

    @staticmethod
    def _detect_config_type(path: Path, data: dict[str, Any]) -> str:  # noqa: PLR0911
        """根据文件路径和内容自动检测配置类型。

        检测规则：
        - 路径包含 ``pipelines`` → pipeline
        - 路径包含 ``agents`` → agent
        - 路径包含 ``models`` → model
        - 数据含 ``input_routes`` + ``output_routes`` → pipeline
        - 数据含 ``config_id`` → agent
        - 数据含 ``models`` + ``providers`` → model
        - 其他 → unknown

        Args:
            path: 文件路径。
            data: 已解析的 YAML 数据。

        Returns:
            配置类型标识。
        """
        # 路径关键词优先
        path_str = str(path).lower()
        if "pipelines" in path_str:
            return "pipeline"
        if "agents" in path_str:
            return "agent"
        if "models" in path_str:
            return "model"

        # 内容特征判断
        if "input_routes" in data and "output_routes" in data:
            return "pipeline"
        if "config_id" in data:
            return "agent"
        if "models" in data and "providers" in data:
            return "model"

        return "unknown"
