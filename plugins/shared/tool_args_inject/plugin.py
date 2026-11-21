"""工具参数注入 Input 插件。

仅 tool_execute 轮激活：把 LLM 产出的工具调用参数归一化为 dict（残缺 JSON
经 llm.repair_json 能力兜底修复）、剥离 ``_`` 前缀伪造键（安全边界）、注入
服务端权威参数（身份/工作空间/层级）、展开 {{project_root}} 模板、按 config
default_params 补缺，产出写回 state["raw_tool_calls"] 供 tool_core 执行。

首轮声明自检（原 injected_param_validator 职责并入，ADR 2026-10-02）：校验
全部工具的 injected_params 声明是否有注入来源，警告不阻断；注入知识由注入
方自持（_NATIVE_INJECT_KEYS 与注入方法同文件），不再维护跨插件副本。

State 出口口径：
    - tool.params_injected : 本轮是否发生注入
    - tool.injected_params_checked : 首轮声明自检完成标记
    - raw_tool_calls : 注入后的工具调用列表（写回）
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _libs.pipeline_base.types import StateKeys
from _root_api import (
    state_fields,  # noqa: E402 — plugins/shared 平铺模块（state 字段两形态容忍单点）
    zone_policy,  # noqa: E402 — plugins/shared 平铺模块（授权区 state 键单源）
)

logger = logging.getLogger(__name__)

# 能力调用器（server.py on_load 经 wiring.make_capability_caller 注入）：
# JSON 兜底修复走 llm_service 的 llm.repair_json 能力——repair_json_string
# 单一真值源在 llm_service._message_normalizer，跨插件共享经能力面收敛
# （2026-09-06 T7 裁定），不再 import 他插件模块。未注入（单元测试/能力
# 缺失）→ 修复不可用，走既有"修复失败"路径（arguments 置空 dict，不阻塞注入）。
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
    （与修复函数返回 None 的既有语义一致：调用方降级，不阻塞注入）。
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
        logger.warning("[tool_args_inject] llm.repair_json 调用失败（按不可修复降级）: %s", exc)
        return None
    if not isinstance(envelope, dict) or not envelope.get("success"):
        logger.warning(
            "[tool_args_inject] llm.repair_json 信封异常（按不可修复降级）: %r",
            envelope,
        )
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    repaired = data.get("repaired")
    return repaired if isinstance(repaired, str) else None


def _is_task_pipeline(state: dict[str, Any]) -> bool:
    """按血缘键在场判定任务管道。

    task_submit 出生协议给任务管道写全血缘扁平键（父形式
    ``lineage.parent_pipeline_id`` / 根形式 ``lineage.root`` 二选一，另带
    origin 键）；主会话管道无任何 lineage.* 键。前缀扫描覆盖两种出生形态
    （与 agent_config_load 同键位判据），防逐键枚举漏新形态。
    """
    return any(str(key).startswith("lineage.") for key in state)


def classify_args_parse_failure(raw: str) -> str:
    """对 json.loads 失败的原始串做诚实分类，避免一律归因为 max_tokens 截断。

    返回稳定标识符，供日志/诊断使用：
    - ``empty``: 空串或纯空白
    - ``markdown_wrapped``: 被 markdown 代码块（```...```）包裹
    - ``leading_noise``: 前导非 JSON 文本（不以 ``{`` 开头，且非 markdown）
    - ``truncated``: 结构性截断——出现 ``{`` 但无匹配的 ``}``（末尾残缺）
    - ``malformed``: 其它语法错误（如未转义字符、尾逗号等结构完整但非法）

    背景：原日志固定打印「疑似输出被 max_tokens 截断」，但生产误报案例中
    arguments 仅 283 字符，根本不可能触达 max_tokens。真实原因多为 markdown
    包裹或前导自然语言，应如实标注。
    """
    if not raw or not raw.strip():
        return "empty"

    s = raw.strip()
    if s.startswith("```"):
        return "markdown_wrapped"
    if not s.startswith("{"):
        return "leading_noise"

    # 判定结构性截断：花括号不配对（{ 比 } 多，且字符串外未闭合）。
    # 用与 repair_json_string 一致的字符串状态机，避免被字符串内的括号误导。
    depth = 0
    in_string = False
    escape_next = False
    for c in s:
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    # depth > 0 → 有未闭合的 { → 末尾残缺
    return "truncated" if depth > 0 else "malformed"


def _resolve_project_root() -> Path | None:
    """推导 Agent OS 项目根目录（含 ``config/kernel`` 的目录）。

    解析单源在 plugins/shared/project_root.py（env 优先→向上探测→缓存，
    F-2 收敛；探测锚定本文件位置，指纹与收敛前 repo_anchor 委托一致）；
    未命中返回 None 时 {{project_root}} 模板替换与 project_root 回退注入
    不生效（约定不变）。延迟导入与 user_space 先例同款（本模块可被仅挂
    插件目录的加载方导入）。
    """
    from _root_api import resolve_project_root  # noqa: PLC0415

    return resolve_project_root("config/kernel", start=Path(__file__).resolve().parent)


# 首轮声明自检完成标记（state 出口键，见模块 docstring）。
_CHECK_DONE_KEY = "tool.injected_params_checked"

# 注入知识单一真值源：本插件自有注入键全集，与下方 _inject_* 方法同文件
# 维护——增删注入键时同步本表（ADR 2026-10-02：自 injected_param_validator
# 的跨插件硬编码副本并入，复制必漂移故由注入方自持）。config default_params
# 的键与工具 schema 可见参数在自检时动态并入，不入本表。
_NATIVE_INJECT_KEYS = frozenset({
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
    "llm_vision_max_pixels",
})


class ToolArgsInjectPlugin(IInputPlugin):
    """工具参数注入 Input 插件（含首轮 injected_params 声明自检）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化参数注入插件。"""
        self._config = config or {}
        self._inject_session_id = self._config.get("inject_session_id", True)
        self._inject_user_id = self._config.get("inject_user_id", True)
        self._inject_timestamp = self._config.get("inject_timestamp", True)
        self._default_params = self._config.get("default_params", {})

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_args_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 20)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行参数注入。"""
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行参数注入逻辑。"""
        updates: dict[str, Any] = self._check_declared_injected_params(ctx)

        # 无待执行调用（纯文本轮）不注入；raw_tool_calls 非空 ⟺ 本轮有调用
        # 待执行（静态化管道下的调用轮判据）。
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            updates["tool.params_injected"] = False
            return updates

        # 注入上下文参数到每个工具调用
        injected_calls = []
        for tc in tool_calls:
            injected_tc = dict(tc)
            args = await self._normalize_args(injected_tc)
            self._strip_forged_keys(args)
            tool_name = injected_tc.get("name", "")
            self._inject_identity_params(ctx, args, tool_name)
            self._inject_workspace_ctx(ctx, args, tool_name)
            self._inject_vision_max_side(ctx, args, tool_name)
            self._inject_parent_agent_level(ctx, args)
            self._inject_agent_config_id(ctx, args)
            # 动态目标（call_tool）的嵌套参数：同一套注入作用到目标参数对象，
            # 壳退化为纯转发（2026-11-18 用户裁定：注入只留注入方一处）。
            self._inject_dynamic_target(ctx, args, tool_name)
            self._inject_dynamic_tools(ctx, args, tool_name)
            self._inject_tool_deny(ctx, args, tool_name)
            self._apply_default_params(args, injected_tc.get("name", ""))
            self._expand_project_root_template(args)

            injected_tc["args"] = args
            injected_calls.append(injected_tc)

        updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
        updates["tool.params_injected"] = True

        logger.debug(
            "[%s] Parameters injected | count=%d",
            self.name,
            len(injected_calls),
        )

        return updates

    def _check_declared_injected_params(self, ctx: PluginContext) -> dict[str, Any]:
        """首轮校验全部工具的 injected_params 声明是否有注入来源（警告不阻断）。

        原 pipeline_injected_param_validator 职责并入（ADR 2026-10-02）：注入
        方自持注入知识，替代跨插件硬编码副本。权威定义取 tool_registry 服务，
        取不到回退 state["_tool_definitions"]（测试夹具兼容，与
        tool_schema_validator 同款）；两者皆空不置完成标记，下轮重试。
        自有注入键、config default_params 键与工具 schema 可见参数视为有来源。

        Returns:
            state 更新（首次实际校验后含完成标记，否则空 dict）
        """
        if ctx.state.get(_CHECK_DONE_KEY):
            return {}
        tool_definitions = self._get_tool_definitions(ctx)
        if not tool_definitions:
            return {}
        default_keys = {k for params in self._default_params.values() for k in params}
        for tool_name, tool_def in tool_definitions.items():
            injected_params = self._get_injected_params(tool_def)
            if not injected_params:
                continue
            visible_props = self._get_visible_props(tool_def)
            for param_name in injected_params:
                if param_name in _NATIVE_INJECT_KEYS or param_name in default_keys:
                    continue
                if param_name in visible_props:
                    continue
                logger.warning(
                    "[%s] 工具 '%s' 的 injected_params 包含 '%s'，"
                    "但无已知注入来源且不在 input_schema.properties 中。"
                    "运行时该参数可能永远为空。",
                    self.name,
                    tool_name,
                    param_name,
                )
        return {_CHECK_DONE_KEY: True}

    @staticmethod
    def _get_tool_definitions(ctx: PluginContext) -> dict[str, Any]:
        """取 {工具名: 定义}：tool_registry 服务权威，state 键回退（测试夹具）。"""
        registry = None
        try:
            registry = ctx.get_service("tool_registry")
        except Exception:  # noqa: BLE001
            logger.debug("[tool_args_inject] tool_registry 服务不可用，回退 state[_tool_definitions]")

        defs: dict[str, Any] = {}
        if registry is not None:
            try:
                for tool in registry.list_all():
                    defs[tool.name] = tool
            except Exception:  # noqa: BLE001
                logger.debug("[tool_args_inject] registry.list_all() 失败，回退到 state[_tool_definitions]")
                defs = {}

        if not defs:
            defs = ctx.state.get("_tool_definitions", {}) or {}
        return defs

    @staticmethod
    def _get_injected_params(tool_def: Any) -> list[str]:
        """从工具定义（dict 或对象）提取 injected_params 列表。"""
        if isinstance(tool_def, dict):
            return tool_def.get("injected_params", []) or []
        return getattr(tool_def, "injected_params", []) or []

    @staticmethod
    def _get_visible_props(tool_def: Any) -> set[str]:
        """从工具定义（dict 或对象）提取 LLM 可见的属性名集合。"""
        if isinstance(tool_def, dict):
            input_schema = tool_def.get("input_schema")
        else:
            input_schema = getattr(tool_def, "input_schema", None)
        if not isinstance(input_schema, dict):
            return set()
        return set(input_schema.get("properties", {}).keys())

    async def _normalize_args(self, injected_tc: dict[str, Any]) -> dict[str, Any]:
        """把工具调用的原始 arguments 归一化为 dict。

        字符串 JSON 先解析；解析失败做诚实分类（classify_args_parse_failure）
        并经 llm.repair_json 能力兜底修复——修复成功且真实分类为结构性截断时
        打 ``_args_truncated`` 标记（供 tool_schema_validator 提示「请分块」）；
        修复不可用（能力未注入/调用失败）与无法修复同语义降级为空 dict。

        Args:
            injected_tc: 工具调用副本（读取 args/arguments）

        Returns:
            归一化后的参数 dict（独立副本，后续注入不污染原调用）
        """
        raw_args = injected_tc.get("args", injected_tc.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                tool_name = injected_tc.get("name", "?")
                reason = classify_args_parse_failure(raw_args)
                logger.warning(
                    "[%s] 工具 %s 的 arguments JSON 解析失败 | reason=%s | 长度=%d | 前200字符: %s",
                    self.name,
                    tool_name,
                    reason,
                    len(raw_args),
                    raw_args[:200],
                )
                # 兜底修复：经 llm.repair_json 能力尽量保住完整字段（含半截
                # content），避免直接 raw_args={} 把半截内容全部丢失，导致下游
                # 验证器/tool_core 拿不到任何内容，只能返回模糊的
                # "不支持的操作: None"。
                repaired = await _repair_json_string(raw_args)
                if repaired is not None:
                    try:
                        raw_args = json.loads(repaired)
                    except (json.JSONDecodeError, TypeError):
                        raw_args = {}
                    # 仅当真实分类为截断时才打结构性截断标记；其它原因
                    # （markdown 包裹/前导噪声）修复成功不算截断，无差别打标
                    # 会误导下游给出错误的「请分块」提示。
                    if reason == "truncated":
                        injected_tc["_args_truncated"] = True
                    logger.info(
                        "[%s] 工具 %s arguments 兜底修复成功 | reason=%s | 已保住可用字段 %s",
                        self.name,
                        tool_name,
                        reason,
                        list(raw_args.keys()) if isinstance(raw_args, dict) else [],
                    )
                else:
                    raw_args = {}
        if not isinstance(raw_args, dict):
            return {}
        return dict(raw_args)

    @staticmethod
    def _strip_forged_keys(args: dict[str, Any]) -> None:
        """剥离 LLM 夹带的 ``_`` 前缀内部键（安全边界）。

        `_owner/_call_context/_container_id/_isolation_provider` 等下划线键是
        内核（dispatch 期）与管道插件（isolation_guard，位于本插件之后的
        服务端注入通道——isolation_guard 无条件覆盖注入 _container_id，故
        此处先剥不影响合法注入；内核注入的 _owner/_log_ctx 发生在管道之后，
        亦不受影响）。任何工具 schema 均未声明下划线参数，出现即视为
        提示注入伪造（下划线参数可被用于绕过危险命令黑名单），先剥后
        注入，服务端值权威。
        """
        for forged_key in [k for k in args if k.startswith("_")]:
            del args[forged_key]

    def _inject_identity_params(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入会话/用户/时间戳/任务/管道身份参数（仅当参数不存在时）。

        task_id 例外：注入参数是系统权威值，args 中任何空值占位
        （None/""）都不算"已存在"——只要没有有效值就必须注入，否则
        下游 task_submit/task_evaluate 拿不到父任务身份无法定位任务。
        """
        if self._inject_session_id and "session_id" not in args:
            session_id = ctx.state.get(StateKeys.SESSION_ID, "")
            if session_id:
                args["session_id"] = session_id

        if self._inject_user_id and "user_id" not in args:
            user_id = ctx.state.get("user_id", "")
            if user_id:
                args["user_id"] = user_id

        if self._inject_timestamp and "timestamp" not in args:
            args["timestamp"] = datetime.now(UTC).isoformat()

        if not args.get("task_id"):
            # 任务身份权威键是 task.id（点号键，内核 chat_send_handler
            # 创建管道时注入，值 = pipeline_id，引擎注入不可伪造）。
            task_id = ctx.state.get(StateKeys.TASK_ID, "")
            if task_id:
                args["task_id"] = task_id
            # 诊断：任务管道（state 带血缘键——task_submit 出生协议写全，
            # 父形式 lineage.parent_pipeline_id / 根形式 lineage.root 二选一）
            # 缺 task.id = 引擎 state 未携带本任务 ID，task_submit/task_evaluate
            # 等依赖该注入的工具将无法确定父任务，真异常才告警。主会话不是
            # 任务（无血缘键，state 无 task.id 属设计常态），降 debug 防噪音。
            elif tool_name in ("task_submit", "task_evaluate", "task_manage"):
                detail = (
                    "[tool_args_inject] task_id 注入失败 | tool=%s | state[task.id]=%r | pipeline_id=%s"
                )
                log_args = (
                    tool_name,
                    ctx.state.get(StateKeys.TASK_ID),
                    str(ctx.state.get(StateKeys.PIPELINE_ID, ""))[:12],
                )
                if _is_task_pipeline(ctx.state):
                    logger.warning(detail, *log_args)
                else:
                    logger.debug(detail, *log_args)

        if "pipeline_id" not in args:
            pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
            if pipeline_id:
                args["pipeline_id"] = pipeline_id

    def _inject_workspace_ctx(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入工作空间坐标（服务端权威值，无条件覆盖）。

        仅 task_submit 例外——其 workspace/isolation 是 agent 显式选择项，
        覆盖会吞掉合法选择；task_submit 专属 parent_ws_meta（提交者工作空间
        坐标，覆盖式注入防 LLM 伪造，父无工作空间时以 None 覆盖）。
        """
        if tool_name != "task_submit":
            workspace = ctx.state.get("workspace", "")
            if workspace:
                args["workspace"] = workspace

            # 会话级隔离模式注入（task_submit 跳过：隔离由 agent 显式选择或
            # 任务类型默认，不再经 state 继承）
            isolation_level = ctx.state.get("isolation_level", "")
            if isolation_level:
                args["isolation_level"] = isolation_level

            # 注入 project_root：state 权威值（任务管道 = 工作空间，
            # 主会话 = 配置的工作空间根，均由 workspace_lifecycle 写入）。
            # 不做仓库根回退——state 缺锚点时文件工具 fail-closed 报错，
            # 不得把项目源码树变成 agent 读写面。
            project_root = ctx.state.get("project_root", "")
            if project_root:
                args["project_root"] = project_root

            # 管道级授权写区（zone_grant 卡批准写入，ADR 2026-09-24 决策3）：
            # JSON 数组串（前缀列表）。读键取 zone_policy.STATE_KEY 单源——
            # 与 security_check 落盘键同名同前缀（task.authorized_write_zones，
            # pipeline-state 仅认 task.* 前缀）；写面工具签名声明者收得到，
            # 其余工具由 SDK 按签名过滤静默丢弃（workspace/project_root 同款
            # 注入契约）。
            authorized_zones = ctx.state.get(zone_policy.STATE_KEY, "")
            if authorized_zones:
                args["authorized_zones"] = authorized_zones

            # 管道级授权读取（读授权卡「仅本管道」写入，用户裁定 2026-09-28）：
            # 读黑名单命中经用户批准的前缀，读面工具（file_read/list_directory/
            # enhanced_search）签名声明者消费。读键同取 zone_policy 常量单源。
            authorized_read = ctx.state.get(zone_policy.STATE_KEY_READ, "")
            if authorized_read:
                args["authorized_read_zones"] = authorized_read
        else:
            # parent_ws_meta：子任务出生契约经它携带父工作空间坐标，使
            # workspace_lifecycle 的共享决策不依赖发起瞬间的聚合读可见性
            # （父管道运行中 registry 行可能尚未建立）。坐标键域分派：
            # 任务管道父 = task.ws_meta（出生/创建时一次写全），主会话父 =
            # ws_meta（会话域坐标键）——两域互斥不并存，非回退链。
            # as_dict 两形态容忍走 state_fields 单点（state 字段读取契约）。
            parent_ws_meta = state_fields.as_dict(
                ctx.state.get("task.ws_meta") or ctx.state.get("ws_meta"),
                field="parent_ws_meta",
            )
            # as_dict 契约：缺失/非法（required=False）返回 None——无父坐标
            # 注入 None 覆盖入参，伪造坐标不放行。
            if not parent_ws_meta or not parent_ws_meta.get("path"):
                parent_ws_meta = None
            args["parent_ws_meta"] = parent_ws_meta

    def _inject_vision_max_side(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入视觉输入不缩放总像素上限（R21 总像素口径，仅 file_read，服务端权威覆盖）。

        来源 = state.llm_vision_max_pixels（llm_core 按 llm.yaml
        multimodal.max_pixels 解析，仅支持视觉的模型写值）；消费 =
        multimodal.split_image 原图+分块一次注入（大图直喂会被模型 API 端
        视觉编码器缩放丢细节）。覆盖式注入防 LLM 伪造规格绕过切分；非视觉
        模型 state 无此键 = 不注入 = file_read 行为与既往逐字节一致。
        """
        if tool_name != "file_read":
            return
        max_pixels = ctx.state.get("llm_vision_max_pixels")
        if isinstance(max_pixels, int) and not isinstance(max_pixels, bool) and max_pixels > 0:
            args["llm_vision_max_pixels"] = max_pixels

    def _inject_parent_agent_level(self, ctx: PluginContext, args: dict[str, Any]) -> None:
        """注入 parent_agent_level：当前 Agent 层级（task_submit/task_manage
        等工具判断权限和设置子任务层级用）。"""
        if "parent_agent_level" in args:
            return
        raw_level = ctx.state.get(StateKeys.AGENT_LEVEL, "")
        if raw_level:
            level_str = str(raw_level).upper().lstrip("L")
            with contextlib.suppress(ValueError, TypeError):
                args["parent_agent_level"] = int(level_str)

    def _inject_agent_config_id(self, ctx: PluginContext, args: dict[str, Any]) -> None:
        """注入 agent_config_id：当前 Agent 的 config_id（memory 等工具自动
        标记记忆来源用）。"""
        if "agent_config_id" in args:
            return
        agent_config_id = ctx.state.get("agent_config_id", "")
        if agent_config_id:
            args["agent_config_id"] = agent_config_id

    def _inject_dynamic_target(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """把原生注入语义前瞻到 ``call_tool`` 的嵌套目标参数（单一真源）。

        模型调 ``call_tool`` 时，本插件看到 ``args["tool"]``（目标名）与
        ``args["arguments"]``（目标参数对象）。此前动态目标由壳（call_tool）
        自行再注入一遍，键表三处手抄已漂移（2026-11-18 矩阵 R2 实证：strict
        schema 被壳误杀、external MCP 漏剥、工作区覆盖/默认参数语义缺失）。
        改为注入方直接作用到嵌套对象——身份/工作区/vision/父级/agent_config/
        默认参数/模板展开全部复用同一组方法，动态调用与原生调用共享一份注入
        代码，壳退化为纯转发。

        external MCP 目标（``dynamic_tools[target].source == "mcp"``）跳过：
        标准 MCP server 对未声明参数严格校验，剥离责任在内核
        ``sanitize_external_mcp_arguments``，注入方从源头不添乱。

        目标参数若是 JSON 字符串先解析为对象再注入（壳只做透传，不替模型
        解析嵌套参数）；解析失败保持原样，交壳的线形校验报错。
        """
        if tool_name != "call_tool":
            return
        target = str(args.get("tool") or "").strip()
        nested = args.get("arguments")
        if isinstance(nested, str):
            try:
                parsed = json.loads(nested)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                nested = parsed
                args["arguments"] = parsed
        if not target or not isinstance(nested, dict):
            return
        if self._dynamic_target_is_external_mcp(ctx, target):
            return
        self._strip_forged_keys(nested)
        self._inject_identity_params(ctx, nested, target)
        self._inject_workspace_ctx(ctx, nested, target)
        self._inject_vision_max_side(ctx, nested, target)
        self._inject_parent_agent_level(ctx, nested)
        self._inject_agent_config_id(ctx, nested)
        self._apply_default_params(nested, target)
        self._expand_project_root_template(nested)

    def _dynamic_target_is_external_mcp(self, ctx: PluginContext, target: str) -> bool:
        """动态目标是否 external MCP（``source`` 标记由 resource_search 定义面提供）。

        定义集缺席/未命中 = 不是外部 MCP（按内部工具注入；壳侧门禁会另行拒绝
        不在动态面的目标）。
        """
        defs = ctx.state.get("dynamic_tools")
        if not isinstance(defs, list):
            return False
        for item in defs:
            if isinstance(item, dict) and str(item.get("name") or "") == target:
                return str(item.get("source") or "") == "mcp"
        return False

    def _inject_dynamic_tools(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入 ``_dynamic_tools``：本次运行已引入的动态工具定义快照（仅 ``call_tool`` 需要）。

        快照内容是 ``tool_schema`` 按 name **累积保留**的整个定义集（引入一次后跨工具
        迭代持续有效，不做轮次回收），故 ``call_tool`` 侧对已引入工具无需重复搜索。

        为什么必须走入参而不是让 ``call_tool`` 自己读：工具插件经
        ``pipeline-state.list`` 拿到的是 registry 常驻状态，而 registry 只在
        **run 收尾**时由 ``stage_exec`` 刷新（``reg.update_state(persisted)``）——
        run 进行中写入的 ``dynamic_tools`` 不在其中，本轮调用必然读到空快照
        （批 1 活体验证实证：``introduced=[]`` 恒现）。本插件与 ``tool_schema``
        同轮执行、经 ``state.reads`` 声明可直接看到活值，故由注入方在**同轮**
        把快照放进入参，绕开跨轮读面。

        键名带 ``_`` 前缀是安全边界：``_strip_forged_keys`` 已先于本方法剥掉
        模型产出的全部下划线键，此处注入的是服务端权威值、模型无法伪造；内核
        ``tool-executor`` 的 ``internal_keys`` 剥离名单不含 ``_dynamic_tools``，
        故该键能原样到达工具 sidecar。

        **只在声明了该注入键需要时才注入**：仅 ``call_tool``（动态面唯一消费者）
        需要，其余工具不收——避免给全部工具调用无谓增重（每轮定义集可达数十条）。
        """
        if tool_name != "call_tool":
            return
        defs = ctx.state.get("dynamic_tools")
        if isinstance(defs, list):
            args["_dynamic_tools"] = defs

    def _inject_tool_deny(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入 ``_tool_deny``：本 agent 动态面否决集（ADR 2026-11-18）。

        消费者 = resource_search（搜索结果过滤）与 call_tool（门禁拒调）——
        其余工具不收。键名 ``_`` 前缀同 ``_dynamic_tools``：服务端权威值、
        模型下划线键已被剥离无法伪造。state 无键 = 缺席语义 = 无否决。
        """
        if tool_name not in ("resource_search", "call_tool"):
            return
        deny = ctx.state.get("tool_deny")
        if isinstance(deny, list):
            args["_tool_deny"] = deny

    def _apply_default_params(self, args: dict[str, Any], tool_name: str) -> None:
        """注入工具默认参数（仅补缺，不覆盖已有值）。"""
        if tool_name in self._default_params:
            for param, value in self._default_params[tool_name].items():
                if param not in args:
                    args[param] = value

    @staticmethod
    def _expand_project_root_template(args: dict[str, Any]) -> None:
        """把 args 字符串值中的 {{project_root}} 替换为实际项目根路径。"""
        project_root = _resolve_project_root()
        if project_root is None:
            return
        pr_str = str(project_root)
        for key, val in args.items():
            if isinstance(val, str) and "{{project_root}}" in val:
                args[key] = val.replace("{{project_root}}", pr_str)
