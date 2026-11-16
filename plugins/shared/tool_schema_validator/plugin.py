"""工具 Schema 验证与自动修复 Input 插件。

负责在管道循环的输入阶段验证工具调用的参数是否符合工具定义的
input_schema。对不符合的调用先尝试自动修复类型不匹配的字段，
修复仍失败的调用才记录错误并标记跳过。

同时检测 LLM 生成的 tool_call arguments 是否被截断：
当 arguments JSON 字符串不完整时，repair_json_string 会丢弃尾部
不完整的字段（如 goal），导致工具收到残缺参数并返回模糊错误
（如 MISSING_GOAL）。本插件在输入阶段提前检测截断，返回精确
诊断信息指导 LLM 缩短参数后重试。

未声明参数按 schema 表态执法：对象声明 ``additionalProperties: false``
时，未声明键 fail-closed 拒绝并在错误里同时回显未知键与合法键（模型可
自纠）；未表态（缺省 / true / 子 schema）照旧放行，存量工具行为不变。

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
from collections import Counter
from collections.abc import Callable
from typing import Any

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _libs.pipeline_base.types import StateKeys

from agentos_plugin_sdk.tool_result_protocol import (
    merge_pre_decided,
    tool_result_entry,
)

logger = logging.getLogger(__name__)

# 参数削减效果观测（2026-10-19）：per-(tool, action, outcome) 进程级计数器。
# outcome ∈ pass / schema_fixed / prefill_rejected——「少填参数 → 调用更准」的
# 度量面；快照随每轮 state_updates["tool_action_stats"] 出口可查。
_ACTION_STATS: Counter[tuple[str, str, str]] = Counter()


def _reset_action_stats() -> None:
    """测试夹具：清零进程级计数器。"""
    _ACTION_STATS.clear()


def _action_of(args: Any) -> str:
    """动作选择器取值（action/mode/op 首个非空），无则空串。"""
    for key in ("action", "mode", "op"):
        value = args.get(key) if isinstance(args, dict) else None
        if value:
            return str(value)
    return ""


# 能力调用器（server.py on_load 经 wiring.make_capability_caller 注入）：
# 截断检测的 JSON 修复走 llm_service 的 llm.repair_json 能力——
# repair_json_string 单一真值源在 llm_service._message_normalizer，跨插件
# 共享经能力面收敛（2026-09-06 T7 裁定），不再 import 他插件模块。未注入
# （单元测试/能力缺失）→ 修复不可用，截断检测按"无法修复"返回 None。
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Any]

_capability_caller: CapabilityCaller | None = None


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入能力调用句柄（async fn `(method, params, timeout) -> Any`）。

    Args:
        caller: 能力调用 async 函数；传 None 清空
    """
    global _capability_caller
    _capability_caller = caller


async def _repair_json_string(text: str) -> str | None:
    """经 llm_service 的 llm.repair_json 能力修复残缺 JSON。

    返回修复后字符串；调用器未注入 / 能力调用失败 / 不可修复 → None
    （与修复函数返回 None 的既有语义一致）。
    """
    caller = _capability_caller
    if caller is None:
        return None
    params = {
        "tool_name": "llm.repair_json",
        "plugin_id": "llm_service",
        "args": {"text": text},
    }
    try:
        envelope = await caller("tool-executor.invoke", params, None)
    except Exception as exc:
        logger.warning("[tool_schema_validator] llm.repair_json 调用失败（按不可修复降级）: %s", exc)
        return None
    if not isinstance(envelope, dict) or not envelope.get("success"):
        logger.warning(
            "[tool_schema_validator] llm.repair_json 信封异常（按不可修复降级）: %r",
            envelope,
        )
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    repaired = data.get("repaired")
    return repaired if isinstance(repaired, str) else None


def _extract_json_top_keys(s: str) -> list[str]:
    """从不完整/截断的 JSON 字符串中提取顶层 key 名称列表。

    使用简单正则扫描，不依赖完整 JSON 解析，适用于截断场景。

    Args:
        s: 可能被截断的 JSON 字符串

    Returns:
        提取到的顶层 key 名称列表
    """
    return re.findall(r'[{,]\s*"([^"]+)"\s*:', s)


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*(.*?)\s*```\s*$", re.DOTALL)


def _json_parse_candidates(text: str) -> list[str]:
    """字符串形态 JSON 的解析候选序列：原文 → 剥 markdown 围栏 → 最外层
    括号截取（prose 夹带兜底）。

    对应 LLM 常见走样形态（业界实证）：裸 JSON / ```` ```json ```` 围栏
    包裹 / "Here is the JSON: {...}" 式散文夹带。调用方依序 json.loads，
    首个成功者胜；全部失败返回原值交类型校验拦截（P9：不伪造空集合）。
    """
    candidates = [text]
    fenced = _FENCE_RE.match(text)
    if fenced:
        candidates.append(fenced.group(1))
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


_PAIR_NAME_KEYS = ("name", "key", "header", "field", "param", "k")
_PAIR_VALUE_KEYS = ("value", "val", "v")

# 系统注入键（注入面真值在 tool_args_inject._NATIVE_INJECT_KEYS）：管道序
# tool_args_inject → 本插件，校验看到的是「注入后 args」——这些键非 LLM 发出，
# 对 additionalProperties:false 的严格工具不得据此判为未知键（otherwise 生产
# 全轮必死：timestamp/pipeline_id 无条件注入）。注入方增键未同步由测试机械
# 守卫（tests/test_tool_schema_validator.py::TestUnknownKeyPolicy::
# test_system_key_set_covers_injector_declaration，两边相碰）。
_SYSTEM_INJECT_KEYS = frozenset({
    "session_id",
    "user_id",
    "timestamp",
    "task_id",
    "pipeline_id",
    "workspace",
    "isolation_level",
    "project_root",
    "authorized_zones",
    "authorized_read_zones",
    "parent_ws_meta",
    "parent_agent_level",
    "agent_config_id",
})


def _pairs_list_to_dict(value: list[Any]) -> dict[str, Any] | None:
    """name/value 键值对列表 → 映射（map 形 object 字段的既定替身形态）。

    每个元素须恰好两键且各命中名称侧/取值侧约定之一，名称侧须非空字符串；
    任一元素不合形 → None（不猜测，交类型校验拦截）。
    """
    as_map: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict) or len(item) != 2:
            return None
        name_key = next((k for k in item if k in _PAIR_NAME_KEYS), None)
        value_key = next((k for k in item if k in _PAIR_VALUE_KEYS), None)
        if name_key is None or value_key is None:
            return None
        name = item[name_key]
        if not isinstance(name, str) or not name.strip():
            return None
        as_map[name] = item[value_key]
    return as_map or None


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
    - additionalProperties:false 对象的未声明键检查（fail-closed，
      错误回显未知键 + 该层合法键；未表态的对象不查）

    支持的自动修复类型转换（形态走样目录，无歧义才收编，歧义交校验拦截）：
    - string→object/array: 候选序列解析（原文 → 剥 markdown 围栏 → 最外层
      括号截取），全部失败保持原值（P9：不伪造空集合）
    - dict→array: 单键包装对象（{"item": v}）或数字键顺序对象（{"0": a,...}）
      解包；多键/空洞键不猜测
    - 标量(int/float/bool)→array: 包单元素数组（字符串除外，内容承载不猜）
    - 单元素数组→object: [{"..."}] 取该元素；多元素不猜测
    - int/float/bool→string: str(value)（布尔输出 "true"/"false" 小写）
    - string→integer: int()，失败经 float 桥接（"3.0"→3），仍失败保持原值
    - float→integer: 整数值（3.0）直转，非整数值（3.5）保持原值
    - string→number: 尝试 float()，失败保持原值
    - string→boolean: "true"/"1"→True, "false"/"0"→False

    优先级：30（校验级，在参数注入之后）
    验证失败不终止管道。

    Attributes:
        _config: 插件配置字典
    """

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
        pre_entries: list[dict[str, Any]] = []
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
            truncation_result = await self._check_args_truncation(args, tool_name)
            if truncation_result:
                # 截断诊断经结果预填直出（ADR 2026-09-28）：调用保留在
                # raw_tool_calls，tool_core 命中预定结果跳过执行并统一整形
                # （配对消息/事件），LLM 据诊断重试——不再自建 role=tool 消息。
                lost = truncation_result["lost_keys"]
                pre_entries.append(
                    tool_result_entry(
                        tool_name,
                        call_id=tc_call_id or None,
                        success=False,
                        error=(
                            f"工具 {tool_name} 参数 JSON 截断（ARGS_TRUNCATED）: "
                            f"{truncation_result['error']}; 丢失字段: {', '.join(lost)}。"
                            "请补齐后重新调用。"
                        ),
                        metadata={
                            "decided_by": "tool_schema_validator",
                            "error_code": "ARGS_TRUNCATED",
                            "lost_keys": lost,
                        },
                    )
                )
                validated_calls.append(tc)
                _ACTION_STATS[(tool_name, _action_of(args), "prefill_rejected")] += 1
                logger.warning(
                    "[%s] 截断 tool_call %s 已预填拒绝结果，丢失字段: %s",
                    self.name,
                    tool_name,
                    lost,
                )
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
                    # 严格模式未注册工具：预填拒绝（调用保留配对，LLM 收到
                    # 反馈——旧路径静默丢弃调用会留孤儿 assistant tool_call）
                    pre_entries.append(
                        tool_result_entry(
                            tool_name,
                            call_id=tc_call_id or None,
                            success=False,
                            error=f"工具 {tool_name} 未注册（TOOL_NOT_FOUND），无法执行。请改用已注册工具。",
                            metadata={
                                "decided_by": "tool_schema_validator",
                                "error_code": "TOOL_NOT_FOUND",
                            },
                        )
                    )
                    validated_calls.append(tc)
                    _ACTION_STATS[(tool_name, _action_of(args), "prefill_rejected")] += 1
                else:
                    validated_calls.append(tc)
                    _ACTION_STATS[(tool_name, _action_of(args), "prefill_rejected")] += 1
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

                # 未声明键收集出口：定稿入参（修复后）的未知键带限定路径入
                # 拒绝结果 metadata，供"哪些工具被投喂额外键"可观测（静默
                # 失效转显式拒绝的度量面）。不得据此剥键凑绿——原始 args 一字不动。
                unknown_keys: list[str] = []
                re_errors = self._validate_args(
                    fixed_args, input_schema, unknown_sink=unknown_keys
                )
                if re_errors:
                    # 参数校验失败（缺 required / 类型不匹配且无法修复）：
                    # 经结果预填直出拒绝结果（ADR 2026-09-28）——调用保留在
                    # raw_tool_calls，tool_core 命中预定结果跳过执行并统一
                    # 整形配对消息，缺失明细反馈 LLM 自我修正。
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
                    reject_metadata: dict[str, Any] = {
                        "decided_by": "tool_schema_validator",
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                        "validation_errors": re_errors,
                        "output_truncated": tc_truncated,
                    }
                    if unknown_keys:
                        reject_metadata["unknown_keys"] = unknown_keys
                    pre_entries.append(
                        tool_result_entry(
                            tool_name,
                            call_id=tc_call_id or None,
                            success=False,
                            error=(
                                f"工具 {tool_name} 参数校验失败（SCHEMA_VALIDATION_FAILED）："
                                + "; ".join(re_errors)
                                + "。请补齐/修正对应参数后重新调用。"
                                + truncated_hint
                            ),
                            metadata=reject_metadata,
                        )
                    )
                    validated_calls.append(tc)
                    _ACTION_STATS[(tool_name, _action_of(args), "prefill_rejected")] += 1
                    logger.warning(
                        "[%s] Schema validation failed, blocked call | tool=%s | errors=%s | truncated=%s",
                        self.name,
                        tool_name,
                        re_errors,
                        tc_truncated,
                    )
                    continue
                fixed_tc = dict(tc)
                fixed_tc["args"] = fixed_args
                validated_calls.append(fixed_tc)
                _ACTION_STATS[(tool_name, _action_of(fixed_args), "schema_fixed")] += 1
            else:
                validated_calls.append(tc)
                _ACTION_STATS[(tool_name, _action_of(args), "pass")] += 1

        if schema_errors:
            state_updates["schema_errors"] = schema_errors
        state_updates["schema_validated"] = validated_calls
        if all_fix_messages:
            state_updates["schema_fixes"] = all_fix_messages
        if pre_entries:
            state_updates["pre_decided_results"] = merge_pre_decided(
                ctx.state.get("pre_decided_results"), pre_entries
            )
        # 用验证后的调用列表更新 RAW_TOOL_CALLS（被拒调用保留在内——tool_core
        # 按 pre_decided_results 命中跳过执行并统一整形配对消息），
        # 确保下游插件拿到的是经过验证和修复的数据
        state_updates[StateKeys.RAW_TOOL_CALLS] = validated_calls
        # 观测快照：进程级聚合计数（单调），供「削减前后调用形变率」对比。
        # 键字符串化（"tool|action|outcome"，工具名是标识符不含 |，无碰撞）：
        # tuple 键过不了 MCP/JSON 边界，会把整个 step 的 state_updates 拖炸
        # （2026-10-19 幽灵续跑改判事故 §4.4）。仅观测用途，无下游消费方。
        state_updates["tool_action_stats"] = {
            "|".join(k): v for k, v in _ACTION_STATS.items()
        }

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

            # object 型 property 带 properties 声明 → 下钻修复（嵌套字段
            # 类型错顶层看不见）；无 properties 的自由 object 不碰。
            if expected_type == "object" and isinstance(value, dict) and field_schema.get("properties"):
                nested_fixed, nested_msgs = self._auto_fix_args(value, field_schema)
                fixed_args[field_name] = nested_fixed
                fix_messages.extend(f"{field_name}.{msg}" for msg in nested_msgs)
                continue

            # map 形 object（声明 additionalProperties、无 properties，如
            # headers/query/params）：name/value 键值对列表是映射的既定
            # 替身形态，合形才收编；其余列表形态不落泛化单元素解包——
            # 那会把 [{"name": X, "value": Y}] 误修成 {"name": ..., "value": ...}
            # 当映射用（与工具侧 dict 契约错位的静默语义污染）。
            if (
                expected_type == "object"
                and not field_schema.get("properties")
                and field_schema.get("additionalProperties") is not None
            ):
                if isinstance(value, list):
                    # 列表形态只吃 pairs 收编，不落泛化单元素解包。
                    as_map = _pairs_list_to_dict(value)
                    if as_map is not None:
                        fixed_args[field_name] = as_map
                        fix_messages.append(f"{field_name}: pairs-list({len(value)})→map")
                    continue
                # 非列表值（str 等）落泛化字符串解析路径。

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
            if target_type in ("object", "array") and isinstance(value, str):
                # 字符串形态 JSON 的常见走样（裸 JSON/围栏包裹/prose 夹带）
                # 依候选序列尝试；全部失败返回原值，交类型校验报错——
                # 不伪装成合法空对象/空数组通过校验（P9）。
                for candidate in _json_parse_candidates(value):
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        continue
                return value

            if target_type == "string" and isinstance(value, bool):
                return "true" if value else "false"

            if target_type == "string" and isinstance(value, (int, float)):
                return str(value)

            if target_type == "integer" and isinstance(value, str):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    # "3.0" 类十进制串经 float 桥接；非整数值保持原值
                    try:
                        bridged = float(value)
                    except (ValueError, TypeError):
                        return value
                    return int(bridged) if bridged.is_integer() else value

            if target_type == "integer" and isinstance(value, float):
                return int(value) if value.is_integer() else value

            if target_type == "array" and isinstance(value, dict):
                # 包装 dict 走样：单键通用键对象（{"item": v}）或数字键
                # 顺序对象（{"0": a, "1": b}，业界实证高频）→ 解包收编；
                # 多键/空洞键/dict 值意图不明不猜测，返回原值交类型校验报错。
                if len(value) == 1:
                    only = next(iter(value.values()))
                    if isinstance(only, list):
                        return only
                    if isinstance(only, (str, int, float, bool)):
                        return [only]
                elif value and all(k.isdigit() for k in value):
                    keys = sorted(int(k) for k in value)
                    if keys == list(range(len(value))):
                        return [value[str(k)] for k in keys]
                return value

            if target_type == "array" and isinstance(value, (int, float, bool)):
                # 裸标量（数字/布尔，无隐藏结构可言）→ 单元素数组；
                # 字符串除外（内容承载，P9 不猜测）。
                return [value]

            if target_type == "object" and isinstance(value, list):
                # 单元素数组包对象（[{...}]）→ 取该元素；多元素取哪个
                # 不明，不猜测。
                if len(value) == 1 and isinstance(value[0], dict):
                    return value[0]
                return value

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

    async def _check_args_truncation(  # noqa: PLR0911
        self,
        args: Any,
        tool_name: str,
    ) -> dict[str, Any] | None:
        """检测 arguments JSON 是否被截断。

        当 LLM 生成的 tool_call arguments 字符串不完整时，
        llm.repair_json 能力会补全括号并丢弃尾部不完整的字段。
        本方法在输入阶段检测这种情况，返回精确诊断错误。

        检测逻辑：
        1. args 是字符串且无法直接 json.loads → 可能被截断
        2. 经 llm.repair_json 能力尝试修复
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

        # 尝试修复（经 llm_service 的 llm.repair_json 能力；能力不可用 /
        # 无法修复均返回 None → 不是截断场景，交给 tool_core 处理）
        repaired = await _repair_json_string(args)
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
        _prefix: str = "",
        *,
        unknown_sink: list[str] | None = None,
    ) -> list[str]:
        """根据 input_schema 验证参数。

        检查 required 字段、属性类型与未声明键，返回错误列表。
        空列表表示验证通过。object 型 property 带 properties 声明时
        下钻校验，错误字段名带限定路径（``acceptance_criteria.files``），
        让 LLM 能定位内层字段；无 properties 的自由 object 不下钻。

        未声明键策略（JSON Schema 语义）：schema 明示
        ``additionalProperties: false`` 时未声明键 fail-closed 拒绝，错误
        同时回显未知键与该层合法键（模型可自纠）；未表态（缺省 / true /
        子 schema）照旧放行，存量工具行为不变。

        Args:
            args: 工具调用参数
            schema: 工具的 input_schema 定义
            _prefix: 递归用限定路径前缀（外层调用不传）
            unknown_sink: 未声明键收集出口（带限定路径，供拒绝结果 metadata）

        Returns:
            验证错误字符串列表
        """
        errors: list[str] = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for field_name in required:
            if field_name not in args:
                errors.append(f"Missing required field: {_prefix}{field_name}")

        errors.extend(
            self._check_unknown_keys(args, schema, _prefix, properties, unknown_sink)
        )

        for field_name, value in args.items():
            if field_name not in properties:
                continue
            field_schema = properties[field_name]
            expected_type = field_schema.get("type")
            if (
                expected_type == "object"
                and isinstance(value, dict)
                and (
                    field_schema.get("properties")
                    or field_schema.get("additionalProperties") is False
                )
            ):
                errors.extend(
                    self._validate_args(
                        value,
                        field_schema,
                        f"{_prefix}{field_name}.",
                        unknown_sink=unknown_sink,
                    )
                )
                continue
            if expected_type and not self._check_type(value, expected_type):
                errors.append(
                    f"Type mismatch for '{_prefix}{field_name}': expected {expected_type}, got {type(value).__name__}"
                )

        return errors

    @staticmethod
    def _check_unknown_keys(
        args: dict[str, Any],
        schema: dict[str, Any],
        prefix: str,
        properties: dict[str, Any],
        unknown_sink: list[str] | None,
    ) -> list[str]:
        """未声明键检查——只在 schema 明示 ``additionalProperties: false`` 时执法。

        拒绝面语义严格按 JSON Schema：只有字面 ``false`` 是拒绝面，
        缺省 / true / 子 schema 一律放行。注入键豁免只在根对象生效
        （注入只写根级 args，嵌套层的同名键是模型笔误，照拒）。

        Args:
            args: 当前层入参
            schema: 当前层 schema
            prefix: 当前层限定路径前缀（根层为空串）
            properties: 当前层声明的合法键
            unknown_sink: 命中未声明键时追加限定路径（可空）

        Returns:
            错误列表（无未声明键时为空；未知键与合法键同文案回显）
        """
        if schema.get("additionalProperties") is not False:
            return []

        unknown = sorted(
            key
            for key in args
            if key not in properties
            and not (prefix == "" and key in _SYSTEM_INJECT_KEYS)
        )
        if not unknown:
            return []

        scope = prefix.rstrip(".")
        qualified = [f"{scope}.{key}" if scope else key for key in unknown]
        allowed = (
            ", ".join(sorted(f"{scope}.{p}" if scope else p for p in properties))
            if properties
            else "none"
        )
        if unknown_sink is not None:
            unknown_sink.extend(qualified)
        return [
            f"Unknown field(s): {', '.join(qualified)} (allowed fields: {allowed})"
        ]

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
        # union 型（如 ["object", "array"]）按未知类型放行——类型联合
        # 本身即声明「多型皆合法」，不做单型检查也不修复。
        expected_python_types = type_map.get(expected_type) if isinstance(expected_type, str) else None
        if expected_python_types is None:
            return True
        return isinstance(value, expected_python_types)
