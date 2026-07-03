"""工具 Schema 验证与自动修复 Input 插件。

负责在管道循环的输入阶段验证工具调用的参数是否符合工具定义的
input_schema。对不符合的调用先尝试自动修复类型不匹配的字段，
修复仍失败的调用才记录错误并标记跳过。

同时检测 LLM 生成的 tool_call arguments 是否被截断：
当 arguments JSON 字符串不完整时，repair_json_string 会丢弃尾部
不完整的字段（如 goal），导致工具收到残缺参数并返回模糊错误
（如 MISSING_GOAL）。本插件在输入阶段提前检测截断，返回精确
诊断信息指导 LLM 缩短参数后重试。

使用简单类型检查实现，不依赖 jsonschema 第三方库。

State 命名空间：
    - schema_errors : 验证失败的工具调用错误列表
    - schema_validated : 已通过验证的工具调用列表
    - schema_fixes : 自动修复日志列表
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


def _extract_json_top_keys(s: str) -> list[str]:
    """从不完整/截断的 JSON 字符串中提取顶层 key 名称列表。

    使用简单正则扫描，不依赖完整 JSON 解析，适用于截断场景。

    Args:
        s: 可能被截断的 JSON 字符串

    Returns:
        提取到的顶层 key 名称列表
    """
    return re.findall(r'[{,]\s*"([^"]+)"\s*:', s)


class ToolSchemaValidator(IInputPlugin):
    """工具 Schema 验证与自动修复 Input 插件。

    对每个工具调用，从 state 中获取对应工具的 input_schema 定义，
    使用简单类型检查验证参数是否匹配。验证失败时先尝试自动修复
    类型不匹配的字段（如 string→object、string→array 等），
    修复仍失败的工具调用才被记录到 schema_errors 并标记跳过。

    支持的 schema 类型检查：
    - string, number, integer, boolean, array, object
    - required 字段检查
    - 嵌套属性的类型检查

    支持的自动修复类型转换：
    - string→object: 尝试 json.loads()，失败则设为 {}
    - string→array:  尝试 json.loads()，失败则设为 []
    - int/float→string: str(value)
    - string→integer: 尝试 int()，失败保持原值
    - string→number:  尝试 float()，失败保持原值
    - string→boolean: "true"/"1"→True, "false"/"0"→False

    优先级：30（校验级，在参数注入之后）
    错误策略：SKIP（验证失败不终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Schema 验证插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用验证（默认 True）
                - strict: 是否严格模式，未知工具也报错（默认 False）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._strict = self._config.get("strict", False)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_schema_validator"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 30)

    @staticmethod
    def _get_tool_definitions(ctx: PluginContext) -> dict[str, Any]:
        """获取 {工具名: 定义} 映射。

        优先从 tool_registry 服务取权威定义（与 tool_schema 插件同一来源），
        registry 取不到时回退 state["_tool_definitions"]（兼容测试夹具）。
        """
        registry = None
        try:
            registry = ctx.get_service("tool_registry")
        except Exception:  # noqa: BLE001
            registry = None

        defs: dict[str, Any] = {}
        if registry is not None:
            try:
                for tool in registry.list_all():
                    defs[tool.name] = {"input_schema": tool.input_schema}
            except Exception:  # noqa: BLE001
                logger.debug("[tool_schema_validator] registry.list_all() 失败，回退到 state[_tool_definitions]")
                defs = {}

        if not defs:
            defs = ctx.state.get("_tool_definitions", {}) or {}
        return defs

    async def execute(self, ctx: PluginContext) -> PluginResult:  # noqa: PLR0912,PLR0915
        """执行 Schema 验证与自动修复。

        读取 raw_tool_calls，对每个调用验证参数是否符合工具定义的
        input_schema。验证失败时尝试自动修复类型不匹配的字段，
        修复后再次验证，修复成功的调用放入 validated_calls。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含验证结果状态更新的插件执行结果。
        """
        if not self._enabled:
            return PluginResult()

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        tool_definitions = self._get_tool_definitions(ctx)
        schema_errors: list[dict[str, Any]] = []
        validated_calls: list[dict[str, Any]] = []
        all_fix_messages: list[dict[str, Any]] = []
        state_updates: dict[str, Any] = {}

        # 本轮输出是否被 max_tokens 截断（finish_reason=length，由 llm_core 写入）。
        # 截断时 tool_call 的 arguments 可能不完整 → 校验缺失字段时给出「文件太大」
        # 语义的精准提示，引导模型分块/重试，而非放任残缺参数漏进工具。
        output_truncated = bool(ctx.state.get("output_truncated", False))

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})
            tc_call_id = tc.get("id", "")
            # 本调用是否被截断：结构性标记（param_inject 修复时打，可靠）
            # 或本轮 finish_reason=length（依赖代理返回，辅助）。任一命中即判截断。
            tc_truncated = bool(tc.get("_args_truncated", False) or output_truncated)

            # ── 阶段 0: arguments JSON 截断检测 ──
            # LLM 生成 tool_call 时可能截断 arguments JSON 字符串，
            # 导致下游 repair_json_string 丢弃尾部不完整的字段。
            # 在此处提前检测，将诊断信息作为 tool result 注入 messages，
            # 让 LLM 立即知道哪些字段丢失并重试。
            truncation_result = self._check_args_truncation(args, tool_name)
            if truncation_result:
                # 将截断诊断作为 tool result 消息注入对话历史，
                # 模拟工具已执行并返回截断错误，LLM 可据此重试
                messages = list(ctx.state.get("messages", []))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_call_id,
                        "content": json.dumps(
                            {
                                "success": False,
                                "error": truncation_result["error"],
                                "error_code": "ARGS_TRUNCATED",
                                "lost_keys": truncation_result["lost_keys"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                state_updates["messages"] = messages
                logger.warning(
                    "[%s] 截断 tool_call %s 已注入诊断结果，丢失字段: %s",
                    self.name,
                    tool_name,
                    truncation_result["lost_keys"],
                )
                # 不加入 validated_calls → tool_core 不会重复执行此调用
                continue

            tool_def = tool_definitions.get(tool_name)
            if tool_def is None:
                if self._strict:
                    schema_errors.append(
                        {
                            "tool": tool_name,
                            "error": f"Tool definition not found: {tool_name}",
                        }
                    )
                    logger.warning(
                        "[%s] Unknown tool in strict mode | tool=%s",
                        self.name,
                        tool_name,
                    )
                else:
                    validated_calls.append(tc)
                continue

            input_schema = (
                tool_def.get("input_schema") if isinstance(tool_def, dict) else getattr(tool_def, "input_schema", None)
            )
            if input_schema is None:
                validated_calls.append(tc)
                continue

            # 首次验证
            errors = self._validate_args(args, input_schema)
            if errors:
                # 尝试自动修复
                fixed_args, fix_messages = self._auto_fix_args(args, input_schema)

                if fix_messages:
                    all_fix_messages.append(
                        {
                            "tool": tool_name,
                            "fixes": fix_messages,
                        }
                    )
                    logger.info(
                        "[%s] Auto-fixed args | tool=%s | fixes=%s",
                        self.name,
                        tool_name,
                        fix_messages,
                    )

                # 修复后再次验证
                re_errors = self._validate_args(fixed_args, input_schema)
                if re_errors:
                    # 参数校验失败（缺 required / 类型不匹配且无法修复）：
                    # 拦截该调用并注入 role=tool 诊断消息——保持 assistant(tool_calls)
                    # →tool 消息序列完整，同时把缺失明细反馈给 LLM。
                    # 截断场景额外提示「文件太大请分块」，让模型改用 append 续写。
                    schema_errors.append(
                        {
                            "tool": tool_name,
                            "errors": re_errors,
                            "attempted_fixes": fix_messages,
                        }
                    )
                    missing = [e.split(":", 1)[1].strip() for e in re_errors if e.startswith("Missing required field:")]
                    truncated_hint = ""
                    if tc_truncated and missing:
                        truncated_hint = (
                            " 本次输出因达到 max_tokens 被截断，疑似上述必填字段在"
                            "截断中丢失（文件/参数过大）。建议拆分为多次小批量调用："
                            "如 file_write 先写入前半部分，再用 action=append 续写后续内容。"
                        )
                    messages = list(ctx.state.get("messages", []))
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_call_id,
                            "content": json.dumps(
                                {
                                    "success": False,
                                    "error": (
                                        f"工具 {tool_name} 参数校验失败："
                                        + "; ".join(re_errors)
                                        + "。请补齐/修正对应参数后重新调用。"
                                        + truncated_hint
                                    ),
                                    "error_code": "SCHEMA_VALIDATION_FAILED",
                                    "validation_errors": re_errors,
                                    "output_truncated": tc_truncated,
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    state_updates["messages"] = messages
                    logger.warning(
                        "[%s] Schema validation failed, blocked call | tool=%s | errors=%s | truncated=%s",
                        self.name,
                        tool_name,
                        re_errors,
                        tc_truncated,
                    )
                    continue
                # 修复成功，用修复后的参数替换原始参数
                fixed_tc = dict(tc)
                fixed_tc["args"] = fixed_args
                validated_calls.append(fixed_tc)
            else:
                validated_calls.append(tc)

        if schema_errors:
            state_updates["schema_errors"] = schema_errors
        state_updates["schema_validated"] = validated_calls
        if all_fix_messages:
            state_updates["schema_fixes"] = all_fix_messages
        # 始终用验证后的调用列表更新 RAW_TOOL_CALLS，
        # 确保下游插件拿到的是经过验证和修复的数据
        state_updates[StateKeys.RAW_TOOL_CALLS] = validated_calls

        return PluginResult(state_updates=state_updates)

    def _auto_fix_args(
        self,
        args: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """尝试自动修复参数类型不匹配。

        遍历 schema["properties"] 中的每个字段，对比声明类型与
        实际值类型，尝试进行自动类型转换。

        Args:
            args: 工具调用参数
            schema: 工具的 input_schema 定义

        Returns:
            (fixed_args, fix_messages) 修复后的参数和修复日志列表
        """
        fixed_args = dict(args)
        fix_messages: list[str] = []
        properties = schema.get("properties", {})

        for field_name, field_schema in properties.items():
            if field_name not in fixed_args:
                continue

            value = fixed_args[field_name]
            expected_type = field_schema.get("type")
            if not expected_type:
                continue

            # 类型已经匹配则跳过
            if self._check_type(value, expected_type):
                continue

            new_value = self._try_convert(value, expected_type)
            if new_value is not value:
                fixed_args[field_name] = new_value
                fix_messages.append(f"{field_name}: {type(value).__name__}→{expected_type}")

        return fixed_args, fix_messages

    def _try_convert(self, value: Any, target_type: str) -> Any:  # noqa: PLR0911,PLR0912
        """尝试将值转换为目标类型。

        Args:
            value: 原始值
            target_type: 目标 JSON Schema 类型

        Returns:
            转换后的值，转换失败返回原值
        """
        try:
            if target_type == "object" and isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return {}

            if target_type == "string" and isinstance(value, (int, float)):
                return str(value)

            if target_type == "integer" and isinstance(value, str):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return value

            if target_type == "array" and isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return []

            if target_type == "number" and isinstance(value, str):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return value

            if target_type == "boolean" and isinstance(value, str):
                if value.lower() in ("true", "1"):
                    return True
                if value.lower() in ("false", "0"):
                    return False
        except Exception:
            logger.debug(
                "[%s] Type conversion failed | value=%r | target=%s",
                self.name,
                value,
                target_type,
            )

        return value

    def _check_args_truncation(  # noqa: PLR0911
        self,
        args: Any,
        tool_name: str,
    ) -> dict[str, Any] | None:
        """检测 arguments JSON 是否被截断。

        当 LLM 生成的 tool_call arguments 字符串不完整时，
        repair_json_string 会补全括号并丢弃尾部不完整的字段。
        本方法在输入阶段检测这种情况，返回精确诊断错误。

        检测逻辑：
        1. args 是字符串且无法直接 json.loads → 可能被截断
        2. 尝试 repair_json_string 修复
        3. 比较修复前后的顶层 key，找出丢失的字段
        4. 有字段丢失 → 返回截断错误

        Args:
            args: 工具调用参数（可能是 dict 或未解析的 JSON 字符串）
            tool_name: 工具名称

        Returns:
            截断错误字典，或 None（未检测到截断）
        """
        if not isinstance(args, str):
            return None

        # 能直接解析说明不是截断
        try:
            json.loads(args)
            return None
        except (json.JSONDecodeError, TypeError):
            pass

        # 尝试修复
        from plugins.core.llm_core._message_normalizer import (  # noqa: PLC0415
            repair_json_string,
        )

        repaired = repair_json_string(args)
        if repaired is None:
            # 完全无法修复 → 不是截断场景，交给 tool_core 处理
            return None

        # 比较修复前后的顶层 key
        original_keys = _extract_json_top_keys(args)
        try:
            repaired_dict = json.loads(repaired)
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(repaired_dict, dict):
            return None

        repaired_keys = set(repaired_dict.keys())
        lost_keys = [k for k in original_keys if k not in repaired_keys]

        if not lost_keys:
            return None

        logger.warning(
            "[%s] 工具 %s 的 arguments JSON 被截断修复，丢失字段: %s，原始长度=%d，修复后长度=%d",
            self.name,
            tool_name,
            lost_keys,
            len(args),
            len(repaired),
        )

        return {
            "tool": tool_name,
            "error": (
                f"工具 {tool_name} 的调用参数 JSON 在生成时被截断，"
                f"系统尝试修复但部分字段丢失（丢失字段: {', '.join(lost_keys)}），"
                f"当前保留的字段为: {', '.join(repaired_keys)}。\n"
                f"请缩短参数内容（尤其是 description 等长文本字段）后重新调用。"
                f"建议：\n"
                f"1. 缩短 description 文本，去掉不必要的换行和转义字符\n"
                f"2. 只保留关键字段，次要字段不要传入\n"
                f"3. 避免在参数值中使用大量嵌套引号和换行符"
            ),
            "lost_keys": lost_keys,
            "repaired_keys": sorted(repaired_keys),
            "truncated": True,
        }

    def _validate_args(
        self,
        args: dict[str, Any],
        schema: dict[str, Any],
    ) -> list[str]:
        """根据 input_schema 验证参数。

        检查 required 字段和属性类型，返回错误列表。
        空列表表示验证通过。

        Args:
            args: 工具调用参数
            schema: 工具的 input_schema 定义

        Returns:
            验证错误字符串列表
        """
        errors: list[str] = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for field_name in required:
            if field_name not in args:
                errors.append(f"Missing required field: {field_name}")

        for field_name, value in args.items():
            if field_name not in properties:
                continue
            field_schema = properties[field_name]
            expected_type = field_schema.get("type")
            if expected_type and not self._check_type(value, expected_type):
                errors.append(f"Type mismatch for '{field_name}': expected {expected_type}, got {type(value).__name__}")

        return errors

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """检查值是否匹配预期的 JSON Schema 类型。

        Args:
            value: 待检查的值
            expected_type: 预期的 JSON Schema 类型字符串

        Returns:
            是否类型匹配
        """
        type_map = {
            "string": (str,),
            "number": (int, float),
            "integer": (int,),
            "boolean": (bool,),
            "array": (list,),
            "object": (dict,),
        }
        expected_python_types = type_map.get(expected_type)
        if expected_python_types is None:
            return True
        return isinstance(value, expected_python_types)
