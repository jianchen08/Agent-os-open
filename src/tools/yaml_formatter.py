"""
YAML 格式化工具

为 LLM 提供 YAML 格式的输入输出支持，节省 token 使用量。

功能：
- 工具描述的 YAML 格式化
- LLM 输出的 YAML 解析
- 格式验证和错误处理
"""

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class YAMLFormatter:
    """
    YAML 格式化器

    提供 LLM 工具调用的 YAML 格式支持
    """

    @staticmethod
    def format_tools_for_llm(tools: list[dict[str, Any]]) -> str:
        """
        将工具列表格式化为 LLM 友好的 YAML 格式

        Args:
            tools: 工具定义列表

        Returns:
            YAML 格式的工具描述字符串
        """
        simplified_tools = []

        for tool in tools:
            # 提取 function 信息
            if "function" in tool:
                func_info = tool["function"]
                simplified = {
                    "name": func_info["name"],
                    "desc": func_info["description"],
                }

                # 简化参数描述
                if "parameters" in func_info:
                    simplified["params"] = YAMLFormatter._simplify_parameters(
                        func_info["parameters"]
                    )

                simplified_tools.append(simplified)
            else:
                # 直接使用工具信息
                simplified_tools.append(
                    {
                        "name": tool.get("name", "unknown"),
                        "desc": tool.get("description", ""),
                        "params": YAMLFormatter._simplify_parameters(
                            tool.get("input_schema", {})
                        ),
                    }
                )

        return yaml.dump(
            {"tools": simplified_tools},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    @staticmethod
    def _simplify_parameters(params: dict[str, Any]) -> dict[str, Any]:
        """
        简化参数定义为紧凑格式

        Args:
            params: 原始参数定义

        Returns:
            简化后的参数定义
        """
        if not isinstance(params, dict):
            return {}

        simplified = {}

        # 处理 properties
        if "properties" in params:
            simplified["props"] = {}
            for prop_name, prop_def in params["properties"].items():
                prop_simple = {}

                # 基本类型
                if "type" in prop_def:
                    prop_simple["type"] = prop_def["type"]

                # 描述（简化）
                if "description" in prop_def:
                    desc = prop_def["description"]
                    # 截断过长的描述
                    if len(desc) > 50:
                        desc = desc[:47] + "..."
                    prop_simple["desc"] = desc

                # 默认值
                if "default" in prop_def:
                    prop_simple["default"] = prop_def["default"]

                # 枚举值
                if "enum" in prop_def:
                    prop_simple["enum"] = prop_def["enum"]

                # 是否必需
                if prop_name in params.get("required", []):
                    prop_simple["required"] = True

                simplified["props"][prop_name] = prop_simple

        return simplified

    @staticmethod
    def parse_llm_yaml_call(yaml_content: str) -> dict[str, Any]:
        """
        解析 LLM 输出的 YAML 格式工具调用

        Args:
            yaml_content: YAML 格式的工具调用内容

        Returns:
            解析后的工具调用字典

        Raises:
            ValueError: YAML 解析失败或格式不正确
        """
        try:
            # 解析 YAML
            parsed = yaml.safe_load(yaml_content)

            if not isinstance(parsed, dict):
                raise ValueError(f"YAML 内容必须是字典格式，得到: {type(parsed)}")

            # 验证必需字段
            if "tool" not in parsed:
                raise ValueError("缺少 'tool' 字段")

            tool_name = parsed["tool"]
            if not isinstance(tool_name, str):
                raise ValueError(f"工具名称必须是字符串，得到: {type(tool_name)}")

            # 提取参数
            params = parsed.get("params", {})
            if not isinstance(params, dict):
                raise ValueError(f"参数必须是字典格式，得到: {type(params)}")

            return {
                "tool_name": tool_name,
                "parameters": params,
                "metadata": {"format": "yaml", "original_content": yaml_content},
            }

        except yaml.YAMLError as e:
            raise ValueError(f"YAML 解析失败: {e}")
        except Exception as e:
            raise ValueError(f"工具调用解析失败: {e}")

    @staticmethod
    def format_tool_result_yaml(result: dict[str, Any]) -> str:
        """
        将工具执行结果格式化为 YAML 输出

        Args:
            result: 工具执行结果

        Returns:
            YAML 格式的结果字符串
        """
        # 简化结果结构
        simplified_result = {
            "success": result.get("success", False),
        }

        if result.get("success"):
            simplified_result["data"] = result.get("data")
        else:
            simplified_result["error"] = result.get("error", "未知错误")
            if "error_code" in result:
                simplified_result["error_code"] = result["error_code"]

        # 添加元数据（如果有）
        if "metadata" in result:
            metadata = result["metadata"]
            if metadata:
                simplified_result["meta"] = metadata

        return yaml.dump(
            simplified_result, default_flow_style=False, allow_unicode=True
        )

    @staticmethod
    def create_yaml_prompt_template() -> str:
        """
        创建 YAML 格式的提示模板

        Returns:
            YAML 格式提示模板
        """
        return """请使用以下 YAML 格式进行工具调用（节省 token）：

```yaml
tool: 工具名称
params:
  参数1: 值1
  参数2: 值2
```

示例：
```yaml
tool: file_read
params:
  path: /path/to/file.txt
  encoding: utf-8
```

可用工具：
{tools_yaml}

请严格按照 YAML 格式输出，不要添加额外的解释。"""

    @staticmethod
    def validate_yaml_format(content: str) -> bool:
        """
        验证内容是否为有效的 YAML 格式

        Args:
            content: 要验证的内容

        Returns:
            是否为有效 YAML
        """
        try:
            yaml.safe_load(content)
            return True
        except yaml.YAMLError:
            return False

    @staticmethod
    def estimate_token_savings(json_content: str, yaml_content: str) -> dict[str, Any]:
        """
        估算 YAML 相对于 JSON 的 token 节省量

        Args:
            json_content: JSON 格式内容
            yaml_content: YAML 格式内容

        Returns:
            节省统计信息
        """
        # 简单的字符数统计（近似 token 数）
        json_chars = len(json_content)
        yaml_chars = len(yaml_content)

        savings = json_chars - yaml_chars
        savings_percent = (savings / json_chars * 100) if json_chars > 0 else 0

        return {
            "json_chars": json_chars,
            "yaml_chars": yaml_chars,
            "savings_chars": savings,
            "savings_percent": round(savings_percent, 2),
            "estimated_token_savings": round(savings * 0.75, 0),  # 估算 token 数
        }


# 便利函数
def format_tools_yaml(tools: list[dict[str, Any]]) -> str:
    """
    格式化工具列表为 YAML（便利函数）

    Args:
        tools: 工具列表

    Returns:
        YAML 格式字符串
    """
    return YAMLFormatter.format_tools_for_llm(tools)


def parse_yaml_call(yaml_content: str) -> dict[str, Any]:
    """
    解析 YAML 工具调用（便利函数）

    Args:
        yaml_content: YAML 内容

    Returns:
        解析结果
    """
    return YAMLFormatter.parse_llm_yaml_call(yaml_content)


def create_yaml_prompt(tools: list[dict[str, Any]]) -> str:
    """
    创建包含工具列表的 YAML 提示（便利函数）

    Args:
        tools: 工具列表

    Returns:
        完整的 YAML 提示
    """
    tools_yaml = YAMLFormatter.format_tools_for_llm(tools)
    template = YAMLFormatter.create_yaml_prompt_template()
    return template.format(tools_yaml=tools_yaml)
