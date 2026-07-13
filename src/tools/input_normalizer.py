"""工具输入规范化与修复。

从 ToolExecutor 中提取的输入验证/规范化逻辑，处理 LLM 返回的类型不一致问题。
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def normalize_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """规范化输入参数，移除无关字段，提高缓存命中率。"""
    normalized: dict[str, Any] = {}
    skip_keys = {
        "timestamp",
        "request_id",
        "session_id",
        "user_id",
        "tool_call_id",
        "execution_id",
    }
    for key, value in inputs.items():
        if key in skip_keys:
            continue
        if isinstance(value, dict):
            nested = normalize_inputs(value)
            if nested:
                normalized[key] = nested
        elif value is not None and value != "":
            normalized[key] = value
    return normalized


def normalize_input_types(  # noqa: PLR0912
    inputs: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """规范化输入参数类型，修复 LLM 返回的类型不一致问题。"""
    if not isinstance(inputs, dict) or not isinstance(schema, dict):
        return inputs

    properties = schema.get("properties", {})
    normalized = dict(inputs)

    for key, value in normalized.items():
        if key not in properties:
            continue

        prop_schema = properties[key]
        expected_type = prop_schema.get("type")

        if expected_type == "boolean" and isinstance(value, str):
            lower_value = value.lower().strip()
            if lower_value in ("true", "1", "yes"):
                normalized[key] = True
                logger.debug(f"[normalize_input_types] 自动转换: {key}='{value}' -> True")
            elif lower_value in ("false", "0", "no"):
                normalized[key] = False
                logger.debug(f"[normalize_input_types] 自动转换: {key}='{value}' -> False")
        elif expected_type == "integer" and isinstance(value, str):
            try:
                normalized[key] = int(value)
                logger.debug(f"[normalize_input_types] 自动转换: {key}='{value}' -> {normalized[key]}")
            except ValueError:
                pass
        elif expected_type == "number" and isinstance(value, str):
            try:
                normalized[key] = float(value)
                logger.debug(f"[normalize_input_types] 自动转换: {key}='{value}' -> {normalized[key]}")
            except ValueError:
                pass
        elif expected_type == "object" and isinstance(value, str):
            parsed = try_parse_json_string(value)
            if parsed is not None:
                normalized[key] = parsed
                logger.debug(f"[normalize_input_types] 自动转换: {key} 从字符串解析为对象")

        if isinstance(normalized.get(key), dict) and expected_type == "object":
            normalize_nested_object(normalized[key], prop_schema)

    return normalized


def try_parse_json_string(value: str) -> dict | None:
    """尝试将 JSON 字符串解析为字典。"""
    stripped = value.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def normalize_nested_object(  # noqa: PLR0912
    obj: dict[str, Any], schema: dict[str, Any]
) -> None:
    """递归规范化嵌套对象中的字符串类型字段。"""
    nested_props = schema.get("properties", {})
    additional = schema.get("additionalProperties")

    for key, value in obj.items():
        if isinstance(value, str):
            prop_schema = nested_props.get(key)
            if prop_schema is None and additional and isinstance(additional, dict):
                prop_schema = additional

            if prop_schema and isinstance(prop_schema, dict):
                expected = prop_schema.get("type")
                if expected == "object":
                    parsed = try_parse_json_string(value)
                    if parsed is not None:
                        obj[key] = parsed
                        logger.debug(f"[normalize_nested_object] {key} 从字符串解析为对象")
                        normalize_nested_object(obj[key], prop_schema)
                elif expected == "boolean":
                    lower = value.lower().strip()
                    if lower in ("true", "1", "yes"):
                        obj[key] = True
                    elif lower in ("false", "0", "no"):
                        obj[key] = False
                elif expected == "integer":
                    with contextlib.suppress(ValueError):
                        obj[key] = int(value)
                elif expected == "number":
                    with contextlib.suppress(ValueError):
                        obj[key] = float(value)

        elif isinstance(value, dict):
            prop_schema = nested_props.get(key)
            if prop_schema is None and additional and isinstance(additional, dict):
                prop_schema = additional
            if prop_schema and isinstance(prop_schema, dict):
                normalize_nested_object(value, prop_schema)


def fix_task_submit_inputs(inputs: dict[str, Any]) -> None:  # noqa: PLR0912
    """自动修复 task_submit 工具的常见 LLM 输入错误。"""

    fix_object_field(inputs, "acceptance_criteria")
    fix_object_field(inputs, "metadata")

    fix_acceptance_criteria_inputs(inputs)

    task_scope = inputs.get("task_scope", "non_container")
    if task_scope == "non_container" and "target_type" in inputs:
        ac = inputs.get("acceptance_criteria")
        if not ac or not isinstance(ac, dict) or len(ac) == 0:
            target_id = inputs.get("target_id", "unknown")
            inputs["acceptance_criteria"] = {
                "file_check": {
                    "input_params": {
                        "target_id": target_id,
                    }
                }
            }
            logger.info("[fix_task_submit_inputs] acceptance_criteria 缺失或无效，使用默认 file_check")

    if "goal" in inputs:
        goal = inputs["goal"]
        if isinstance(goal, str):
            try:
                parsed = json.loads(goal)
                if isinstance(parsed, dict):
                    inputs["goal"] = parsed
                    goal = parsed
                    logger.info("[fix_task_submit_inputs] goal 从字符串解析为对象")
            except (json.JSONDecodeError, TypeError):
                inputs["goal"] = {"title": goal[:50] if len(goal) > 50 else goal}
                logger.info("[fix_task_submit_inputs] goal 从字符串转为 {title: ...}")
                return

        if isinstance(goal, dict) and "title" not in goal:
            try:
                if "description" in goal:
                    desc = goal["description"]
                    title = desc.split("。")[0].split(".")[0].split("，")[0].split(",")[0].strip()
                    if len(title) > 50:
                        title = title[:47] + "..."
                    if not title:
                        title = "未命名任务"
                    goal["title"] = title
                    logger.info(f"[fix_task_submit_inputs] 自动为 goal 添加 title: {title}")
                else:
                    goal["title"] = "未命名任务"
                    logger.info("[fix_task_submit_inputs] goal 使用默认 title")
            except Exception as e:
                logger.error(f"[fix_task_submit_inputs] 修复 goal 时出错: {e}")

    if "goal" not in inputs and "title" in inputs:
        logger.info("[fix_task_submit_inputs] 检测到 LLM 将参数平铺在顶层，自动包装为 goal")
        goal_obj = {"title": inputs.pop("title")}
        if "description" in inputs:
            goal_obj["description"] = inputs.pop("description")
        if "requirements" in inputs:
            goal_obj["context"] = {"requirements": inputs.pop("requirements")}
        if "agent_config" in inputs:
            goal_obj.setdefault("context", {})["agent_config"] = inputs.pop("agent_config")
        inputs["goal"] = goal_obj
        logger.info(f"[fix_task_submit_inputs] 重组后的 goal: {goal_obj}")


def fix_acceptance_criteria_inputs(inputs: dict[str, Any]) -> None:
    """修复 acceptance_criteria 中 metric 对象缺少 input_params 的问题。

    LLM 经常将 metric 的参数直接平铺（如 {"criteria": "..."}），
    而不是按照 schema 要求包装在 input_params 中（如 {"input_params": {"criteria": "..."}}）。
    此方法检测并自动修复这种格式错误。
    """
    ac = inputs.get("acceptance_criteria")
    if not ac or not isinstance(ac, dict):
        return

    known_keys = {"input_params", "expected_output", "pass_threshold"}

    for metric_id, metric_config in ac.items():
        if not isinstance(metric_config, dict):
            continue

        if "input_params" in metric_config:
            continue

        other_keys = {k for k in metric_config if k not in known_keys}
        if other_keys:
            input_params = {k: metric_config.pop(k) for k in list(other_keys)}
            metric_config["input_params"] = input_params
            logger.info(
                f"[fix_acceptance_criteria_inputs] metric '{metric_id}' 缺少 input_params，"
                f"已将字段 {other_keys} 包装为 input_params"
            )
        else:
            metric_config["input_params"] = {}
            logger.info(
                f"[fix_acceptance_criteria_inputs] metric '{metric_id}' 缺少 input_params，已补充空 input_params"
            )


def fix_object_field(inputs: dict[str, Any], field_name: str) -> None:  # noqa: PLR0912
    """修复 LLM 将 object 类型字段传为 JSON 字符串的问题。"""
    if field_name not in inputs:
        return

    value = inputs[field_name]

    if isinstance(value, dict):
        return

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            inputs.pop(field_name, None)
            return

        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                inputs[field_name] = parsed
                logger.info(f"[fix_object_field] {field_name} 从字符串解析为对象")
                return
            if isinstance(parsed, list):
                logger.warning(f"[fix_object_field] {field_name} 解析为列表而非对象，移除该字段")
                inputs.pop(field_name, None)
                return
        except (json.JSONDecodeError, TypeError):
            pass

        if stripped.startswith("{"):
            fixed = try_fix_truncated_json(stripped)
            if fixed is not None:
                inputs[field_name] = fixed
                logger.info(f"[fix_object_field] {field_name} 截断 JSON 修复成功")
            else:
                logger.warning(f"[fix_object_field] {field_name} JSON 修复失败，使用空对象: {stripped[:100]}")
                inputs[field_name] = {}
        else:
            logger.warning(f"[fix_object_field] {field_name} 不是有效对象，移除该字段: {type(value)}")
            inputs.pop(field_name, None)

    elif isinstance(value, bool):
        logger.warning(f"[fix_object_field] {field_name} 收到布尔值 True（LLM 错误），移除该字段")
        inputs.pop(field_name, None)

    elif not isinstance(value, dict):
        logger.warning(f"[fix_object_field] {field_name} 类型异常({type(value).__name__})，移除该字段")
        inputs.pop(field_name, None)


def try_fix_truncated_json(json_str: str) -> dict | None:
    """尝试修复被截断的 JSON 字符串。"""
    open_braces = json_str.count("{") - json_str.count("}")
    open_brackets = json_str.count("[") - json_str.count("]")

    in_string = False
    escape_next = False
    for ch in json_str:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue

    if not in_string and open_braces >= 0 and open_brackets >= 0:
        fixed = json_str + "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        try:
            parsed = json.loads(fixed)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    return None
