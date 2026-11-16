"""LLM Core 插件 -- 基于 LLM Adapter 的大模型调用实现。

通过 LLM Adapter 中间层调用大模型，支持多模型 fallback 和流式回调。
重试/降级由 LLM Adapter（Router 层）负责；插件只上抛错误，错误处理语义见 ADR 2026-08-18。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由引擎/编排层按错误类型决定（瞬态 sidecar 崩溃→retry 一次；非瞬态→上抛）
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 共享上传目录解析（plugins/shared/uploads_path.py，ADR 2026-08-21）：
# 本文件位于 plugins/shared/llm_core/，上溯 1 级到 shared。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

# LLMResponse：聚合响应结构归属地（llm_core 不再直连 LLM API，调用走 llm_service）。
from _libs.pipeline_base.plugin import ICorePlugin, PluginContext  # noqa: E402
from _libs.pipeline_base.types import StateKeys  # noqa: E402
from _root_api import resolve_uploads_url  # noqa: E402
from adapter import LLMResponse  # noqa: E402
from track_stats import TrackStats  # noqa: E402

logger = logging.getLogger(__name__)

# ── 能力调用器（LLM 面唯一事实源 = llm_service）────────────────────────
# llm_core 的 LLM 调用经内核 tool-executor 能力跨进程调 llm.complete_stream
# （与 approval/hindsight 经 capability 的既有用法同构）。server.py 在 on_load
# 注入本函数；测试注入伪实现。未注入时调用抛明确错误（接线 bug 早暴露）。
# LLM 调用耗时可达 llm.yaml call_timeout（默认 600s），第三参 timeout 必须显式
# 传大值——SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 面向短调用，不传会先于 LLM
# 完成被掐断（与 human-interaction 同款长等待语义）。
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Any]

_capability_caller: CapabilityCaller | None = None


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入能力调用句柄（async fn `(method, params, timeout) -> Any`）。

    server.py 在 on_load 时调用：
    ``set_capability_caller(lambda m, p, t=None: plugin.get_capability("tool-executor").call(m, p, t))``
    LLM 调用（llm.complete_stream）据此经内核 tool-executor 转发到 llm_service。

    Args:
        caller: 能力调用 async 函数（method 传 capability 短名——SDK 句柄
            ``get_capability("tool-executor").call`` 内部组装
            ``<capability>.<method>`` 全名，传全名会双重前缀；timeout 透传
            SDK ``CapabilityHandle.call``，长等待语义必须传大值）；传 None 清空
    """
    global _capability_caller
    _capability_caller = caller


class PartialStreamOutcome:
    """流中断/取消的部分结果——``_call_llm`` 的半截返回形态。

    与成功路径（``LLMResponse``）区分：携带已组装好的 state_updates 结果字典
    （半截消息落库 ops + status/ended 标记），execute 直接返回，不再走
    ``LLMResponse`` 的组装逻辑。错误信息（``llm_error_info``）来自
    llm_service 返回值（已跨进程传输），不再从异常对象读取。
    """

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

# ── 思考参数覆盖（思考全链路）────────────────────────────────────────
# 前端 user_input 携带 thinking_strength = 参数组 JSON 串（选项即
# thinking_strength_params 配置的参数组本身，llm_service thinking-levels
# 端点下发、选中即透传；"" = 不覆盖），内核透传注入 state；llm_core 解析后
# 按**思考参数白名单**过滤，与 default_params 合并后进 kwargs。
# reasoning_effort 的 extra_body 透传（DeepSeek 等 OpenAI 兼容中转）由
# llm_service adapter 侧处理。
#
# 关闭思考 = 选中的参数组本身是关闭形态（如 thinking.type=disabled）——
# 与其他参数组同一条覆盖路径：模型默认参数常自带思考开启，不覆盖即照跑，
# 用户选关闭组就覆盖为关闭值。
#
# 白名单只含思考相关字段：temperature/max_tokens 等采样参数不随消息覆盖
# （参数组里写了也过滤），采样参数始终用模型 default_params。
_THINKING_STRENGTH_ALLOWED = {"reasoning_effort", "thinking"}

# 出站载荷内部字段黑名单：管道持久化态携带、但不属于任何 provider wire 契约
# 的消息字段。严格 provider（zhipu 错误码 1210/1214）对 messages 内未知字段
# 整请求拒绝；宽松 provider（minimax/deepseek）容忍但同样无消费方。
# reasoning_content 不入此名单——provider 适配层（adapt_messages_before_send）
# 的采样保留策略在下游消费它（DeepSeek rc 采样）。
_OUTBOUND_INTERNAL_FIELDS = frozenset(
    {
        "seq",
        "tool_result",
        "_context_form",
        "metadata",
        "status",
        "llm_error_info",
        # 引擎合成标记（内核 stage_finalize 失败终态直写）：带此标记的
        # assistant 消息在 _build_messages 整条剔除（不发给大模型），本名单
        # 兜底拦截漏网形态——字段本身不属于任何 wire 契约。
        "synthetic",
        # 消息执行身份戳记（气泡卡名/卡头像数据源）：持久化态与读侧透传用，
        # 不属于任何 provider wire 契约——不剥离会被严格 provider 整请求拒绝。
        "agent_id",
    }
)

# ── 系统通知衰减（编排上下文瘦身）────────────────────────────────────
# 缺陷实证（2026-09-29/30 夜）：triggers_ext 自动父通知经 chat.send_message
# 进入编排上下文后永不衰减，同一失败通知单请求内三连（MSG[212/214/216]），
# 一晚 payload_diag 38263 行，上下文膨胀 → 请求超时 → 429 加剧。
# 通知文本契约在 triggers_ext/triggers/manager.py："[系统通知] "前缀 +
# "(ID: <task_id>)" 锚。衰减只裁出站请求视图，state["messages"] 持久层不动。
_SYSTEM_NOTIFY_MARKER = "[系统通知]"
_SYSTEM_NOTIFY_ID_RE = re.compile(r"\(ID:\s*([^)\s]+)\s*\)")

# ── 注入文本工具引用对账（卡4②，出站构造期拦截）────────────────────
# 事故形态（2026-10-17 装机）：工具数组缩水后，注入的触发器指令仍在指挥
# 「先调 file_read」——模型照做时找不到工具，只能抓在场工具编造。对账在
# 出站请求构造期拦截：指令引用了声明但不在当前工具面的工具 → 出站副本
# 附加对账标注 + ERROR 留痕（不炸整轮）。
# 选「标注」不选「剔除」：自由文本里引用片段的边界不可确定解析，剔除有
# 截断用户原文/留下半截指令的风险；标注保留原文、每请求幂等（state 持久
# 层不动）、与 tool_surface.unavailable 全局通告互补（通告报面、标注点位）。
_INJECTED_MSG_MARKERS = ("[触发器通知]", _SYSTEM_NOTIFY_MARKER, "【系统纪律】")
_RECON_ANNOTATION_PREFIX = "[系统对账]"

# 内核错误链逐跳累积的传输包装（tool_executor「tool execution failed:」→
# invoker「MCP tool call failed:」→ mcp「tool call failed: <tool>:」「MCP
# protocol error: [N] 」），同一根因被多层前缀包裹。内核折叠文案 280 字
# 截断只保头部——不剥噪则用户只看到传输噪声、根因（含 LLM 供应商分类摘要）
# 被截掉。最后一跳「MCP protocol error: [N] 」之后即插件异常原文。
_MCP_PROTOCOL_MARKER = "MCP protocol error:"

# ── 模型绑定快照（run 启动时的模型解析结果）────────────────────────
# 裁定（2026-10-20 用户）：模型配置改动 = 下一管道生效——run 启动时快照
# 模型解析结果，热重载只影响新 run；在飞 run 沿用启动时快照（不随
# defaults.chat / model_tier 改指向漂移，模型被改名/移除也不当场杀死
# 在飞管道）。快照落 state（key=llm_model_binding）并带 run_id 归属：
# state 的非持久键不落库，新 run 重新解析（跨 run 不串快照）。
_MODEL_BINDING_KEY = "llm_model_binding"

# 空转熔断（2026-11-04 用户裁定「空转到时间应该掐掉」）：验收类 agent 实测
# 同一步骤连发 12 轮相同正文（每轮 2 秒即翻页燃烧迭代）——连续
# _LOOP_STREAK_LIMIT 轮「正文去空白后逐字节相同且无工具调用」即判无进展，
# execute 上抛 RuntimeError（任务失败面带明确原因）。计度键随 state 持久
# （跨轮/跨 run 归属同管道），有工具调用或正文变化即重置——工作中叙述与
# 正常推进不受影响。
_LOOP_STREAK_KEY = "llm_loop_streak"
_LOOP_HASH_KEY = "llm_loop_last_hash"
_LOOP_STREAK_LIMIT = 3


@dataclasses.dataclass(frozen=True)
class ModelIdentity:
    """单次调用的模型身份快照（execute 解析完绑定即原子捕获）。

    llm_core 单实例被多管道并发复用（主对话与派发子任务同进程交错执行）：
    模型身份不得落实例可变字段跨 await 读取——并发 execute 的
    ``_apply_binding`` 会覆写共享字段，把本管道的调用参数与结果归属踩成
    并发方的模型（装机 382ec1ee 实测：绑定 glm 的调用被贴/真发成子任务的
    MiniMax）。契约（2026-10-27 用户裁定）：模型身份随 state 走——每次
    execute 解析完绑定即捕获本快照，调用参数与结果组装全部读快照，实例
    字段仅保留作构造兜底（本轮无模型意图时复用 ``_boot_identity``）。
    """

    model_id: str
    model: str
    provider: str
    api_base: str | None
    api_key: str | None
    context_window: int | None
    supports_vision: bool
    default_params: dict[str, Any]
    # 流式超时配置（llm.yaml 键，随绑定快照；None=快照未携带，调用侧不透传）
    stream_idle_timeout: float | None = None
_MIGRATION_KEY = "llm_model_migration"

# 模型不可用错误标记（跨插件文本契约）：模型解析失败的错误文本含本标记，
# duplicate_check 据此识别「模型不可用」类失败并快速失败（不进重复输出
# 计数）。两侧各自硬编码同一字符串——跨插件不共享 import（播种副本对共享
# 根裸模块不可达）。
MODEL_UNAVAILABLE_MARKER = "MODEL_UNAVAILABLE"

# 显式改名映射的跟随上限（防映射环 a→b→a）
_MIGRATION_MAX_HOPS = 5


def _strip_transport_noise(err: Any) -> Any:
    """剥离 MCP 错误链逐跳累积的传输前缀，保留根因尾部。

    取字符串中最后一处「MCP protocol error: [N] 」之后的原文（插件异常的
    str()，含 llm_service 分类摘要「LLM 供应商调用失败[认证失败]…」）；
    标记缺失（非 MCP 通道/格式漂移）或剥离结果为空时原样返回，不做猜测。
    """
    if not isinstance(err, str):
        return err
    idx = err.rfind(_MCP_PROTOCOL_MARKER)
    if idx < 0:
        return err
    tail = err[idx + len(_MCP_PROTOCOL_MARKER):].lstrip()
    if tail.startswith("["):
        end = tail.find("]")
        if end >= 0:
            tail = tail[end + 1:].lstrip()
    return tail or err


def _system_notify_task_id(msg: dict[str, Any]) -> str | None:
    """系统通知识别：user 纯文本消息带 "[系统通知]" 前缀且可提取 task 锚。

    Returns:
        通知所属 task_id；非系统通知（含 list 形态 content / 无 ID 锚）返回 None。
    """
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if not isinstance(content, str) or not content.startswith(_SYSTEM_NOTIFY_MARKER):
        return None
    matched = _SYSTEM_NOTIFY_ID_RE.search(content)
    return matched.group(1) if matched else None


def _decay_system_notifications(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """裁剪已取代/已消费的历史系统通知（保留每 task_id 最后一条未消费通知）。

    通知 i（task T）出站裁剪当且仅当：
    - 被取代：其后存在同 task_id 的更新通知（每 task_id 至多存活末条），或
    - 已消费：其后编排器已产出过工具调用（assistant.tool_calls 非空——通知
      的唤醒目的已达成）。

    只移除 user 角色纯文本通知，assistant/tool 消息一概不动，工具对完整性
    不受影响。入参列表与其中的 dict 均不被修改。
    """
    last_idx: dict[str, int] = {}
    for i, m in enumerate(messages):
        tid = _system_notify_task_id(m)
        if tid is not None:
            last_idx[tid] = i
    consumed_after = [False] * (len(messages) + 1)
    for i in range(len(messages) - 1, -1, -1):
        acted = messages[i].get("role") == "assistant" and bool(messages[i].get("tool_calls"))
        consumed_after[i] = consumed_after[i + 1] or acted
    return [
        m
        for i, m in enumerate(messages)
        if (tid := _system_notify_task_id(m)) is None
        or (last_idx[tid] == i and not consumed_after[i + 1])
    ]


def _dangling_tool_names(state: dict[str, Any]) -> set[str]:
    """声明工具集 ∖ 当前工具面 = 悬空引用域（tool_ids/tool_schemas 任一缺失返回空集）。

    tool_ids 是 agent 声明（agent_config_load 每 run 注入），tool_schemas 是
    tool_schema 本轮查活注册表的返回——两者的差即「指令里出现就算悬空」的
    名字域。配置断链轮（任一键缺失/非列表）不判定，宁漏不误。
    """
    declared = state.get("tool_ids")
    schemas = state.get("tool_schemas")
    if not isinstance(declared, list) or not isinstance(schemas, list):
        return set()
    available = {
        (s.get("function") or {}).get("name")
        for s in schemas
        if isinstance(s, dict)
    }
    return {t for t in declared if isinstance(t, str) and t not in available}


def _annotate_dangling_tool_refs(
    messages: list[dict[str, Any]], state: dict[str, Any]
) -> None:
    """注入指令引用悬空工具 → 出站副本附加对账标注 + ERROR 留痕（原地更新列表）。

    只扫 user 角色纯文本且带注入标记（触发器通知/系统通知/系统纪律）的消息
    ——对账面 = 系统注入的指令文本，普通用户消息不受影响。标注追加在消息
    末尾，原文不动；入参列表中的 dict 不被共享引用改写（新建副本换入）。
    """
    dangling = _dangling_tool_names(state)
    if not dangling:
        return
    hits: list[tuple[int, list[str]]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or _RECON_ANNOTATION_PREFIX in content:
            continue
        if not any(marker in content for marker in _INJECTED_MSG_MARKERS):
            continue
        referenced = sorted(t for t in dangling if t in content)
        if referenced:
            hits.append((i, referenced))
            names = "、".join(referenced)
            messages[i] = {
                **msg,
                "content": (
                    f"{content}\n\n{_RECON_ANNOTATION_PREFIX} 本消息引用的工具 "
                    f"{names} 不在可用工具面，相关指令本轮无法执行："
                    "如实向用户说明受阻情况，禁止编造这些工具的调用结果。"
                ),
            }
    if hits:
        logger.error(
            "[llm_core] 工具引用对账：注入指令引用不在可用工具面的工具，"
            "出站已标注 | 消息数=%d | 缺席工具=%s",
            len(hits),
            sorted(dangling),
        )


def resolve_thinking_params(raw: str) -> dict[str, Any] | None:
    """消息携带的思考参数覆盖集（JSON 串）→ 白名单过滤后的 dict；空/非法 → None。

    ``thinking_strength`` 线上形态 = 参数组 JSON 串（llm_service
    thinking-levels 端点下发的选项 value，选中即透传；"" = 不覆盖）：本侧
    只做白名单过滤（只允许思考相关键），无档位查表——选项即参数组，映射
    语义在配置侧（thinking_strength_params），执行端零推断。
    """
    if not raw:
        return None
    try:
        params = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("thinking_strength 非 JSON 参数组，忽略: %.80s", raw)
        return None
    if not isinstance(params, dict):
        return None
    filtered = {k: v for k, v in params.items() if k in _THINKING_STRENGTH_ALLOWED}
    return filtered or None


class LLMCore(ICorePlugin):
    """LLM Core -- 经 llm_service 调用大模型，流式回调。

    通过 LLM Adapter 中间层调用大模型，支持多模型 fallback。
    成功时输出 raw_result 和 raw_tool_calls，并将 assistant 回复写入 messages。
    失败时直接抛出异常，由引擎/编排层按错误类型处理：瞬态 sidecar 崩溃由
    invoker with_transparent_recovery 重试一次；非瞬态错误上抛（ADR 2026-08-18）。
    LLM 面唯一事实源 = llm_service（经 tool-executor 能力调用
    llm.complete_stream，见 ``_call_llm``）。

    Attributes:
        _config: 插件配置字典，包含 provider/model/api_base/api_key 等
        _provider: 模型提供商（如 openai、minimax）
        _model: 模型标识（如 gpt-4、MiniMax-M2.7）
        _api_base: API 端点 URL
        _api_key: API 密钥
        _default_params: 默认调用参数（temperature、max_tokens 等）
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        router: Any | None = None,
    ) -> None:
        """初始化 LLM Core 插件。

        Args:
            config: 插件配置字典，支持以下键：
                - provider: 模型提供商（如 openai、minimax）
                - model_name: 模型标识（如 gpt-4、MiniMax-M2.7）
                - api_base: API 端点 URL
                - api_key: API 密钥
                - default_params: 默认调用参数（temperature、max_tokens 等）
        """
        self._config = config or {}
        self._provider: str = self._config.get("provider", "openai")
        # model_id（yaml key，如 deepseek-v4-pro-apigo）：路由标识，
        # 传给 llm_service 做 deployment 匹配，保证不同 provider 的同名模型隔离。
        # model_name（yaml 的 model_name，如 deepseek-v4-pro）：发给上游的真实模型名。
        # 两者必须分开：model_name 重名时（官方与 apigo 同底模），靠 model_id 区分路由。
        self._model_id: str = self._config.get("model_id", "")
        self._model: str = self._config.get("model_name", "gpt-4")
        self._api_base: str | None = self._config.get("api_base")
        self._api_key: str | None = self._config.get("api_key")
        # 视觉能力缺省 False（fail-closed）：_apply_model_from_state 解析模型
        # 时按 llm.yaml multimodal 声明覆盖。
        self._supports_vision: bool = bool(self._config.get("supports_vision", False))
        self._context_window: int | None = self._config.get("context_window")
        if not self._context_window:
            logger.warning(
                "[%s] context_window 未配置！上下文守卫将无法工作。"
                " 请在模型配置（llm.yaml）或 core_plugins 中设置 context_window。"
                " model=%s, provider=%s",
                self.name,
                self._model,
                self._provider,
            )
        # default_params 兜底：空 dict = 不设采样参数，全走上游默认（=不设限），
        # 避免 reasoning model 被人为限 token。本字段是全链路唯一兜底点——
        # 配置层(models.py)与 resolver 均不覆盖空 dict，漏配模型最终落此。
        self._default_params: dict[str, Any] = self._config.get("default_params", {})
        # 构造期身份快照：本轮 state 未表达模型意图（三级来源全空）时的调用
        # 身份——不读实例字段，避免并发 execute 重绑踩踏（见 ModelIdentity）。
        self._boot_identity = ModelIdentity(
            model_id=self._model_id,
            model=self._model,
            provider=self._provider,
            api_base=self._api_base,
            api_key=self._api_key,
            context_window=self._context_window,
            supports_vision=self._supports_vision,
            default_params=dict(self._default_params),
        )

        # 空回复重试上限（用户裁定 2026-08-30）：LLM 返回空文本且无工具调用
        # 时跨轮重试，超过上限标记耗尽交 output 步骤裁决任务失败。
        # 每次 execute 由 _apply_runtime_config 按 plugin_configs 覆盖。
        self._max_empty_retries: int = int(self._config.get("max_empty_retries", 2))

        # 每轮追踪统计（原 pipeline/output/track 插件职责，2026-09-15 并入：
        # 本插件产出的 llm_usage 即其输入，同步骤累加免跨插件边界重读 state）。
        # 实例级：_start_time 是耗时回退锚点，sidecar 实例跨管道复用。
        self._track = TrackStats(self._config.get("track"))

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数（每次 execute 复位后应用）。

        同 sidecar 实例被多个 agent 管道连续复用：先回构造默认值再应用本
        管道 state.plugin_configs 注入的 max_empty_retries——前一 agent 的
        配置不得残留到下一 agent（与 task_reminder 同范式）。
        """
        self._max_empty_retries = int(self._config.get("max_empty_retries", 2))
        plugin_configs = ctx.state.get("plugin_configs", {})
        if isinstance(plugin_configs, dict):
            llm_core_cfg = plugin_configs.get("llm_core", {})
            if isinstance(llm_core_cfg, dict):
                override = llm_core_cfg.get("max_empty_retries")
                if isinstance(override, int) and override >= 0:
                    self._max_empty_retries = override

    def _apply_model_from_state(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, ModelIdentity]:
        """从管道 state 解析本次调用的模型并更新 self，返回调用身份快照。

        优先级：``state.model_id`` > ``state.model_tier``(→ defaults.tiers)
        > ``llm.yaml defaults.chat``。任一命中即按其 model_id 查
        ``get_llm_core_config``；三级来源全部为空（本轮未表达模型意图）才
        保持构造时的默认配置不变（不阻断）。

        在飞保护（2026-10-20 裁定「模型配置改动 = 下一管道生效」；2026-10-23
        用户裁定升格为**管线级绑定**——绑定快照跨 run 复用，显式选择才换绑）：

        - 管线首次解析 → **快照模型解析结果**落 state（``llm_model_binding``），
          后续 run（同管线）沿用快照——tier/defaults 热重载漂移不再在会话中途
          换模型（装机 382ec1ee 管道实测：glm↔MiniMax 中途翻转 3 次，缓存按
          模型隔离全灭，是命中率 8.97% 的三大根因之一）；
        - 唯一换绑条件 = 本 run 携带**显式 ``model_id`` 且与绑定不同**（前端
          模型选择器最高优先级覆盖管道配置）；空值/同值沿用绑定；
        - 快照模型仍在可用列表 → 沿用快照（同模型改参亦以快照为准，改动留给
          下一管道）；快照模型已改名/移除 → **显式映射**（llm.yaml 顶层
          ``aliases`` / ``models.<id>.superseded_by``）命中则按映射**显式切换
          并留痕**；未命中则沿用快照（配置改动不杀在飞管道），新管线
          fail-closed。

        换模型即整套配置切换，禁止用上一模型的 api_base 兜底：跨 provider
        复用端点会把请求发给错误的供应商（静默串味），缺配必须显式报错终止
        本次调用。解析出的模型名未命中 models 映射且无显式映射 → 本轮显式
        失败（不以其他模型静默回落继续对话——失败直写语义 R16⑧：引擎据
        plugin_errors 把错误文本直写回复，用户可见）。配置注入是单次整体快照
        （``set_config`` 原子换 dict），models 与 defaults 无撕读窗口，无需
        时间维暂态豁免。

        Args:
            state: 管道状态字典，读 ``model_id`` / ``model_tier`` 与模型绑定
                快照（``llm_model_binding``）。

        Returns:
            (待写回 state 的绑定快照, 显式改名映射留痕, 本次调用身份快照)；
            前二者皆 None 表示绑定未变化、无迁移发生。身份快照是调用面唯一
            模型事实源（实例字段不读——并发 execute 会重绑共享字段）。

        Raises:
            RuntimeError: 新 run 解析出的模型名不在 llm.yaml models 映射且
                无显式映射（不在可用列表，点名报错），或模型配置缺 api_base
                （换模型不得带病切换）。
        """
        binding = self._read_model_binding(state)
        if binding is None:
            return self._resolve_and_snapshot_model(state)
        # 换绑唯一入口：本 run 显式 model_id 且与绑定不同（选择器覆盖，最高
        # 优先级）。model_tier/defaults 非显式意图——漂移不换道（管线粘性）。
        explicit_id = str(state.get("model_id") or "")
        if explicit_id and explicit_id != str(binding.get("model_id") or ""):
            return self._resolve_and_snapshot_model(state)
        return self._apply_existing_binding(binding)
    def _resolve_and_snapshot_model(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, ModelIdentity]:
        """本 run 首次解析模型 → 快照落 state（返回绑定、迁移留痕与调用身份）。"""
        # 解析链单源 `_config_models.resolve_model_id`（与 model.resolve 服务共用，
        # 落定键 2026-10-18）：显式 model_id > tier→defaults.tiers > defaults.chat。
        from _config_models import resolve_model_id  # noqa: PLC0415

        resolved_id = resolve_model_id(
            state.get("model_id", ""), state.get("model_tier", "")
        )
        if not resolved_id:
            # 本轮未表达模型意图 → 保持构造期默认配置（不阻断）；身份用构造
            # 快照，不读实例字段（并发 execute 可能已重绑）。
            return None, None, self._boot_identity

        llm_conf: dict[str, Any] | None
        effective_id, llm_conf, source = self._resolve_model_entry(resolved_id)
        migration: dict[str, Any] | None = None
        if not llm_conf:
            raise RuntimeError(
                f"{MODEL_UNAVAILABLE_MARKER}: [{self.name}] 模型 {resolved_id} "
                f"不在可用列表（llm.yaml models 未命中，且无显式 "
                f"alias/superseded_by 映射），拒绝以其他模型静默回落继续对话；"
                f"请核对 defaults.chat / model_tier / model_id 指向的模型配置"
            )
        if source:
            migration = {
                "from": resolved_id,
                "to": effective_id,
                "source": source,
                "at": datetime.now(UTC).isoformat(),
            }
        resolved_id = effective_id

        # 先整体验证再落字段：任何缺失配置都在触碰实例状态前显式失败，
        # 不留半套改动（部分旧值 + 部分新值是更难排查的串味形态）。
        api_base = llm_conf.get("api_base")
        if not api_base:
            raise RuntimeError(
                f"[{self.name}] 模型 {resolved_id} 缺少 api_base 配置，"
                f"拒绝以模型 {self._model_id} 的端点兜底（跨 provider 串味）；"
                f"请在 llm.yaml models.{resolved_id} 或其 provider 段补齐"
            )

        binding = self._build_model_binding(
            resolved_id,
            llm_conf,
            state,
            migrated_from=migration["from"] if migration else None,
            migration_source=migration["source"] if migration else None,
        )
        identity = self._apply_binding(binding, llm_conf)
        logger.info(
            "[%s] model bound: model_id=%s provider=%s model=%s",
            self.name,
            identity.model_id,
            identity.provider,
            identity.model,
        )
        return binding, migration, identity

    def _apply_existing_binding(
        self, binding: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, ModelIdentity]:
        """沿用既有绑定快照（管线级粘性：跨 run、热重载只影响新管线）。

        快照模型仍在可用列表 → 沿用快照；快照模型已改名/移除 → 显式映射
        命中则按映射显式切换并留痕；未命中则沿用快照（配置改动不杀在飞
        管道——既不静默换模型，也不当场判死）。
        """
        model_id = str(binding.get("model_id") or "")
        effective_id, live_conf, source = self._resolve_model_entry(model_id)
        if live_conf is not None and not source:
            # 快照模型仍可用且无后继声明 → 沿用启动时快照
            return None, None, self._apply_binding(binding, live_conf)

        if live_conf is None:
            logger.warning(
                "[%s] 绑定模型 %s 已不在 llm.yaml models（热重载移除/改名）且无显式 "
                "alias/superseded_by 映射；在飞 run 沿用启动时快照（不静默换模型，"
                "也不因配置改动当场判死）",
                self.name,
                model_id,
            )
            return None, None, self._apply_binding(binding, None)

        new_binding = self._build_model_binding(
            effective_id,
            live_conf,
            {"run_id": binding.get("run_id", "")},
            migrated_from=model_id,
            migration_source=source,
        )
        migration = {
            "from": model_id,
            "to": effective_id,
            "source": source,
            "at": datetime.now(UTC).isoformat(),
        }
        logger.warning(
            "[%s] 模型改名显式切换（映射来源=%s）| from=%s to=%s",
            self.name,
            source,
            model_id,
            effective_id,
        )
        return new_binding, migration, self._apply_binding(new_binding, live_conf)

    def _read_model_binding(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """读管线模型绑定快照（跨 run 复用；run_id 仅为落定来源留痕）。"""
        binding = state.get(_MODEL_BINDING_KEY)
        if not isinstance(binding, dict):
            return None
        model_id = binding.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            return None
        return binding

    @staticmethod
    def _loop_fingerprint(result_text: str, tool_calls: Any) -> str:
        """空转指纹：无工具调用且正文非空 → 去空白正文 sha256 前 16 位；
        有工具调用或空正文 → 空串（不计度——工作中叙述/空轮属正常形态）。"""
        if tool_calls or not (result_text or "").strip():
            return ""
        normalized = "".join((result_text or "").split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _check_loop_guard(
        self, ctx: Any, result_text: str, tool_calls: Any
    ) -> tuple[int, str]:
        """空转计度：与上一轮指纹相同则 streak+1，否则重置为 0。

        读 ctx.state 既有计度键（上一轮经 result 字段落 state），返回
        (新计度, 本轮指纹)——写回由调用方并入 result（state 归管道合并，本侧
        不直接改 ctx.state）。
        """
        fp = self._loop_fingerprint(result_text, tool_calls)
        if not fp:
            return 0, ""
        prev_fp = str(ctx.state.get(_LOOP_HASH_KEY) or "")
        if fp != prev_fp:
            return 0, fp
        try:
            streak = int(ctx.state.get(_LOOP_STREAK_KEY) or 0)
        except (TypeError, ValueError):
            streak = 0
        return streak + 1, fp

    def _build_model_binding(
        self,
        model_id: str,
        conf: dict[str, Any],
        state: dict[str, Any],
        *,
        migrated_from: str | None = None,
        migration_source: str | None = None,
    ) -> dict[str, Any]:
        """构造模型绑定快照（解析结果全量；凭证不落快照——state 会进轨迹/投影面）。"""
        binding: dict[str, Any] = {
            "run_id": str(state.get("run_id") or ""),
            "model_id": model_id,
            "provider": str(conf.get("provider") or ""),
            "model_name": str(conf.get("model_name") or ""),
            "api_base": str(conf.get("api_base") or ""),
            "default_params": conf.get("default_params") or {},
            "context_window": conf.get("context_window"),
            "call_timeout": conf.get("call_timeout"),
            # 流式超时配置（llm.yaml defaults/models.<id> 键，配置桥已解析）：
            # 随快照走才能在 _call_llm 透传给 llm_service——2026-11-04 装机事故
            # （e4bfd6538ed0）：此前 binding 丢弃两键，adapter 恒用 600s 兜底，
            # 流中途冻结 10min28s 才判错。
            "stream_idle_timeout": conf.get("stream_idle_timeout"),
            "resolved_at": datetime.now(UTC).isoformat(),
        }
        if migrated_from:
            binding["migrated_from"] = migrated_from
            binding["migration_source"] = migration_source or ""
        return binding

    def _apply_binding(
        self, binding: dict[str, Any], live_conf: dict[str, Any] | None
    ) -> ModelIdentity:
        """把绑定快照落实例字段并返回本次调用身份快照（凭证取当前配置视图，不落快照）。

        实例字段仍同步写入（构造兜底语义保留），但调用面身份一律用返回的
        ``ModelIdentity``——原子构造自本地值，不受并发 execute 后续重绑影响。
        """
        model_id = str(binding.get("model_id") or "")
        provider = str(binding.get("provider") or "")
        model = str(binding.get("model_name") or "")
        api_base = str(binding.get("api_base") or "")
        api_key = str((live_conf or {}).get("api_key") or "")
        context_window = binding.get("context_window")
        params = binding.get("default_params")
        default_params = dict(params) if isinstance(params, dict) else {}
        self._model_id = model_id
        self._provider = provider
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._context_window = context_window
        self._default_params = default_params
        # 模型视觉能力（llm.yaml models.<id>.multimodal.supports_image）：写入
        # state 供 tool_core inject_multimodal 作注图闸门——模型不支持时不注图，
        # 工具截图走文本引导路径（消费键 state.llm_supports_vision）。
        supports_vision = self._read_supports_vision(model_id)
        self._supports_vision = supports_vision
        return ModelIdentity(
            model_id=model_id,
            model=model,
            provider=provider,
            api_base=api_base,
            api_key=api_key,
            context_window=context_window,
            supports_vision=supports_vision,
            default_params=default_params,
            # 旧绑定快照（字段缺省）→ None → 调用侧不携带，adapter 落自身默认
            stream_idle_timeout=binding.get("stream_idle_timeout"),
        )

    def _resolve_model_entry(
        self, model_id: str
    ) -> tuple[str, dict[str, Any] | None, str]:
        """model_id → (有效 model_id, 配置或 None, 显式映射来源标签)。

        显式映射来源（llm.yaml）：

        - ``models.<id>.superseded_by: "<new_id>"``：旧条目保留为墓碑并声明
          后继（改名前后引用都可用）；
        - 顶层 ``aliases: {"<old_id>": "<new_id>"}``：别名表——旧条目已移除时
          唯一可用形态（事故形态：改名即删旧键）。

        解析序：条目自带后继声明（墓碑）→ 顶层别名表 → 条目本身。映射链最多
        跟 ``_MIGRATION_MAX_HOPS`` 跳（防映射环）。配置为 None 且无映射 =
        该模型不在可用列表（调用方据裁定 fail-closed 或沿用快照）。
        """
        from _config_models import get_model_config_loader  # noqa: PLC0415

        loader = get_model_config_loader()
        seen: set[str] = set()
        current = model_id
        source = ""
        for _ in range(_MIGRATION_MAX_HOPS):
            seen.add(current)
            conf = loader.get_llm_core_config(current)
            target, target_source = self._lookup_migration_target(loader, current)
            if target and target not in seen:
                if not source:
                    source = target_source
                current = target
                continue
            return current, conf, source
        return current, None, source

    @staticmethod
    def _lookup_migration_target(loader: Any, model_id: str) -> tuple[str, str]:
        """查显式映射目标 → (目标 model_id, 来源标签)；未命中返回 ("", "")。"""
        data = loader.load_llm_data()
        entry = LLMCore._lookup_ci(data.get("models"), model_id)
        if isinstance(entry, dict):
            target = entry.get("superseded_by")
            if isinstance(target, str) and target:
                return target, "superseded_by"
        target = LLMCore._lookup_ci(data.get("aliases"), model_id)
        if isinstance(target, str) and target:
            return target, "aliases"
        return "", ""

    @staticmethod
    def _lookup_ci(mapping: Any, key: str) -> Any:
        """大小写不敏感查表（与配置桥 models 查表口径一致）。未命中返回 None。"""
        if not isinstance(mapping, dict) or not key:
            return None
        if key in mapping:
            return mapping[key]
        lowered = key.lower()
        for k, v in mapping.items():
            if isinstance(k, str) and k.lower() == lowered:
                return v
        return None

    def _read_supports_vision(self, model_id: str) -> bool:
        """读模型声明的视觉能力（models.<id>.multimodal.supports_image）。

        条目缺失/字段未声明 → False（fail-closed：不注图，走文本引导路径）；
        配置桥查询失败只降级为不支持，不阻断模型解析。
        """
        try:
            from _config_models import get_model_config_loader  # noqa: PLC0415

            entry = get_model_config_loader().get_model_config(model_id) or {}
        except Exception:
            logger.debug(
                "[%s] multimodal 能力查询失败，按不支持处理 (model_id=%s)",
                self.name,
                model_id,
                exc_info=True,
            )
            return False
        mm = entry.get("multimodal") or {}
        return bool(mm.get("supports_image", False))

    def _get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """通过 sidecar 注入的 config 桥取模型配置。

        从 ``_config_models.get_model_config_loader()`` 拿 loader，调
        ``get_llm_core_config``。模型未配置时 loader 返回 None（合法降级）；
        import/loader 自身故障是 sidecar 接线 bug，直接抛出可见。
        """
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().get_llm_core_config(model_id)

    def _capability_timeout(self, model: str) -> float:
        """反向能力调用的等待上限：模型 call_timeout + 余量。

        SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 会先于长 LLM 调用完成掐断请求
        （llm.complete_stream 耗时可到 llm.yaml call_timeout）。+60s 余量让
        llm_service 的结构化错误信封先于 SDK 超时返回（错误是值，不丢语义）。
        模型不在 llm.yaml 时用 loader 内部默认 300s 口径（与 get_llm_core_config
        的 defaults 兜底一致）。

        入参是 **model id**（llm.yaml models 段的 key）——查表面以 id 为
        基础，传 model_name 会查不到条目而静默回落 300s 口径（2026-10-28
        装机实证：压缩/能力超时因 name 查表失配而错配）。
        """
        conf = self._get_llm_core_config(model)
        call_timeout = float(conf["call_timeout"]) if conf else 300.0
        return call_timeout + 60.0

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # type: ignore[override]
        """执行 LLM 调用，返回原始结果。

        调用 LLM 后，将 assistant 回复 append 到 messages 中。
        谁生产数据谁负责写入：LLMCore 生产的 assistant 回复，由 LLMCore 写入。

        失败时直接抛出异常，由引擎/编排层按错误类型处理（ADR 2026-08-18）。

        Args:
            ctx: 插件执行上下文

        Returns:
            核心执行结果字典，将合并到管道状态中

        Raises:
            Exception: LLM 调用失败时抛出异常
        """
        # 动态模型解析：每次 execute 从 state 读模型绑定快照（run 启动时
        # 解析结果）/ model_id / model_tier，并整体切换实例配置（优先级与
        # 在飞保护语义见 _apply_model_from_state）。
        self._apply_runtime_config(ctx)
        pending_binding, migration, identity = self._apply_model_from_state(ctx.state)

        messages = self._build_messages(ctx.state)
        streaming = ctx.state.get("streaming", True)

        # W2a 上下文粗门锚：本轮收到的字符账本快照（引擎记的数字，只透传不在
        # 本侧重算字符；缺失/非数字写 0）。与 usage 同点位写回（见 result 组装），
        # 保证锚 = 最近一次真实调用时刻的字符值；压缩 fork（context_window_guard
        # 内部经 memory.compress 调用）不经本点位，不会覆盖锚。
        chars_snapshot = ctx.state.get("track.messages_chars")
        messages_chars_at_llm = (
            chars_snapshot
            if isinstance(chars_snapshot, (int, float)) and not isinstance(chars_snapshot, bool)
            else 0
        )

        try:
            response: LLMResponse | PartialStreamOutcome = await self._call_llm(
                messages, ctx, stream=streaming, identity=identity
            )

            # 流中断/取消（llm.complete_stream 返回 partial）：半截消息
            # 已组装好落库 ops + status/ended 标记，直接返回（不再走
            # LLMResponse 成功组装路径）。
            if isinstance(response, PartialStreamOutcome):
                if pending_binding is not None:
                    response.result[_MODEL_BINDING_KEY] = pending_binding
                if migration is not None:
                    response.result[_MIGRATION_KEY] = migration
                return response.result

            result_text = response.text
            tool_calls = response.tool_calls
            thinking_text = response.thinking_text

            # 空转熔断（无进展自动掐停）：连续 _LOOP_STREAK_LIMIT 轮相同正文
            # 且无工具调用 → 上抛（任务失败面带明确原因）。校验须在组装/落库
            # 之前——熔断轮不必留痕（正文与上一轮逐字节相同）。
            loop_streak, loop_fp = self._check_loop_guard(ctx, result_text or "", tool_calls)
            if loop_streak >= _LOOP_STREAK_LIMIT:
                raise RuntimeError(
                    f"LLM 空转熔断：连续 {loop_streak + 1} 轮输出相同正文且无工具调用"
                    f"（无进展自动停止，阈值 {_LOOP_STREAK_LIMIT} 轮）；"
                    f"如需继续请调整任务指令后重新提交"
                )

            output_truncated = response.finish_reason == "length"

            llm_usage = self._build_usage(response, identity)
            self._log_success(ctx, response, streaming=streaming, identity=identity)

            # 执行身份戳记：state 持久键 agent.id（出生方经 state 透传，
            # context_build 同键消费）——空会话/未透传时为空串，消息不戳记。
            agent_id_stamp = str(ctx.state.get("agent.id", "") or "")
            appended_msg = self._build_assistant_message(
                result_text, tool_calls, thinking_text, agent_id=agent_id_stamp or None
            )

            # 仅当产出了 assistant 消息才 emit append op（无文本/无工具调用时不追加）。
            messages_update = (
                {"_ops": [{"op": "set", "msg": appended_msg}]}
                if appended_msg is not None
                else None
            )
            result: dict[str, Any] = {
                StateKeys.RAW_RESULT: result_text,
                StateKeys.RAW_ERROR: None,
                StateKeys.RAW_TOOL_CALLS: tool_calls,
                StateKeys.RAW_THINKING: thinking_text,
                "llm_usage": llm_usage or {},
                # W2a 粗门供给：字符锚 + 本轮解析到的模型窗口（未配置写 0，缺省
                # 告警见 __init__/_apply_model_from_state）——窗口事实由本插件按
                # 实际模型写入 state，管道 when 只做算术，引擎零领域知识。
                # 身份键一律读本次调用快照（identity）——实例字段会被并发
                # execute 的重绑踩踏，await 之后读必然串道（装机 382ec1ee 实测）。
                "track.messages_chars_at_llm": messages_chars_at_llm,
                "track.model_context_window": int(identity.context_window or 0),
                "context_window": identity.context_window,
                "llm_model": identity.model,
                # 模型 id（llm.yaml models 段的 key）是全系统唯一标识：
                # 路由/查表/凭证/超时一律以它为基础，model_name 只作展示与
                # 上游请求串。单独暴露 llm_model_id 让下游（压缩链路等）不必
                # 拿 model_name 反查 id（反查在同名多 id 时歧义，猜错会打到
                # 另一个 provider）。
                "llm_model_id": identity.model_id,
                "llm_provider": identity.provider,
                "llm_api_base": identity.api_base,
                "llm_supports_vision": identity.supports_vision,
                "output_truncated": output_truncated,
                # 空转计度持久键：随 result 落 state，下一轮 execute 读回续计
                _LOOP_STREAK_KEY: loop_streak,
                _LOOP_HASH_KEY: loop_fp,
            }
            if messages_update is not None:
                result["messages"] = messages_update
            # 模型绑定快照（run 启动时的解析结果）：仅在绑定新建/显式改名切换
            # 时写回——state 非持久键不落库，新 run 重新解析（跨 run 不串）。
            if pending_binding is not None:
                result[_MODEL_BINDING_KEY] = pending_binding
            if migration is not None:
                result[_MIGRATION_KEY] = migration
            self._apply_empty_retry_policy(ctx, result, result_text, tool_calls)
            # 每轮 token / 耗时统计（原 pipeline/output/track 插件，2026-09-15
            # 并入——本插件产出的 llm_usage 就是该统计的输入，同一步骤内累加
            # 免去跨插件边界重读 state；见 ADR 2026-09-15-track-merged-into-llm-core）。
            # 本轮用量显式传入：core 阶段 state["llm_usage"] 还是上一轮的值。
            # 统计异常按观测面降级隔离：原 track 是独立 post 插件，出错时引擎
            # warn+继续（ADR 2026-08-18）；并入后若放任其上抛会被下方 except
            # 当作「LLM 调用失败」丢弃本轮成功的 assistant 消息。
            try:
                result.update(await self._track.run(ctx, llm_usage))
            except Exception:
                logger.exception("[%s] 追踪统计失败（观测面降级，本轮结果照常返回）", self.name)
            return result

        except Exception as exc:
            logger.error(
                "[%s] LLM call failed: %s — %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            # 流中断/取消的半截落库已由 _call_llm 在返回值路径处理
            # （llm_service partial → PartialStreamOutcome → execute 直接返回）；
            # 此处仅传播未预期异常（建连失败/零累积内容/通道故障）。
            # 消息面修复由 llm_service 标准化唯一关卡每轮全量幂等执行，
            # 无调用方缓存需重置。
            raise

    def _build_usage(
        self, response: LLMResponse, identity: ModelIdentity
    ) -> dict[str, Any] | None:
        """从响应构造用量轨迹字典（无用量时返回 None）。

        model/provider 随用量同帧落轨迹（traces 按模型聚合 token 的归属依据；
        state.llm_model 是可变当前值，diff 轨迹在模型未变的轮次不落盘）。
        归属读本次调用快照 identity——用量数字来自响应，身份不得来自实例
        字段（await 期间会被并发 execute 的重绑踩踏）。
        """
        if not response.usage:
            return None
        return {
            "input_tokens": response.usage.get("prompt_tokens", 0),
            "output_tokens": response.usage.get("completion_tokens", 0),
            "total_tokens": response.usage.get("total_tokens", 0),
            "cached_tokens": response.usage.get("cached_tokens", 0),
            "model": identity.model,
            "provider": identity.provider,
        }

    def _log_success(
        self,
        ctx: PluginContext,
        response: LLMResponse,
        streaming: bool,
        identity: ModelIdentity,
    ) -> None:
        """落成功调用日志（INFO 摘要 + DEBUG 全文与 tool_call 诊断）。"""
        result_text = response.text
        tool_calls = response.tool_calls
        thinking_text = response.thinking_text
        logger.info(
            "[%s] LLM call succeeded (streaming=%s, thinking=%s, text=%s, tool_calls=%d, finish_reason=%s)",
            self.name,
            streaming,
            bool(thinking_text),
            (result_text or "")[:200],
            len(tool_calls or []),
            response.finish_reason,
        )
        # 完整响应记录到管道日志（DEBUG 级别）
        logger.debug(
            "[%s] LLM full response: text=%d chars, thinking=%d chars, usage=%s",
            self.name,
            len(result_text or ""),
            len(thinking_text or ""),
            self._build_usage(response, identity),
        )
        for tc in tool_calls or []:
            logger.debug(
                "[%s] tool_call: %s(%s)",
                self.name,
                tc.get("name", "?"),
                str(tc.get("args", tc.get("arguments", "")))[:200],
            )
            # 诊断：arguments repr，定位转义层级（adapter 返回时是否已双重转义）
            _tc_args_raw = tc.get("args", tc.get("arguments", ""))
            if isinstance(_tc_args_raw, str) and len(_tc_args_raw) > 100:
                logger.debug(
                    "[%s] tool_call arguments repr前80: %s",
                    self.name,
                    repr(_tc_args_raw[:80]),
                )
        logger.debug(
            "[%s] pipeline=%s iter=%d LLM returned: text=%d chars, tool_calls=%d, thinking=%d chars",
            self.name,
            ctx.state.get("pipeline_id", "?"),
            ctx.state.get("iteration", -1),
            len(response.text) if response.text else 0,
            len(response.tool_calls) if response.tool_calls else 0,
            len(response.thinking_text) if response.thinking_text else 0,
        )

    def _build_assistant_message(
        self,
        result_text: str | None,
        tool_calls: list[dict[str, Any]],
        thinking_text: str | None,
        agent_id: str | None = None,
    ) -> dict[str, Any] | None:
        """构造本轮 assistant 回复消息（append 到 messages）。

        LLMCore 生产的 assistant 回复。新模型(op-based):只 emit 一个 append set op,
        由引擎 apply 到 state["messages"] + message_slots(不再返回全量 history)。
        统一保留 reasoning_content 到内存（不管 provider）：发送给 API 时由
        ProviderAdapter 按 provider 决定是否剥离。

        执行身份戳记契约：``agent_id`` = 消息生成时的管道执行身份（state 持久键
        ``agent.id``），随消息 blob 持久化并透传到前端——气泡卡名/卡头像的数据源。
        空则不写字段（缺省不污染；出站请求经 _OUTBOUND_INTERNAL_FIELDS 剥离）。

        Returns:
            assistant 消息 dict；无文本且无工具调用时返回 None
        """
        if tool_calls:
            # 将解析后的 id 回写到 raw_tool_calls，供后续 tool_core 使用
            resolved_ids = self._resolve_tool_call_ids(tool_calls)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result_text or "",
                "tool_calls": [
                    {
                        "id": resolved_ids[i],
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("args", tc.get("arguments", "")),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }
            if thinking_text:
                assistant_msg["reasoning_content"] = thinking_text
            if agent_id:
                assistant_msg["agent_id"] = agent_id
            return assistant_msg
        if result_text:
            plain_msg: dict[str, Any] = {"role": "assistant", "content": result_text}
            if thinking_text:
                plain_msg["reasoning_content"] = thinking_text
            if agent_id:
                plain_msg["agent_id"] = agent_id
            return plain_msg
        return None

    def _apply_empty_retry_policy(
        self,
        ctx: PluginContext,
        result: dict[str, Any],
        result_text: str | None,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """按空回复重试策略回填计数与续跑标志（就地修改 result）。

        空回复重试（用户裁定 2026-08-30）：LLM 偶发"只思考不回答"
        （adaptive thinking 下 content 空且无工具调用，实测 MiniMax-M3）。
        跨轮计数：未超限置 _has_new_llm_input 触发 post 路由回 LLM 重试
        （输入不变再给一次机会，模型输出有随机性）；超限置
        llm_empty_exhausted 交 output 步骤裁决任务终态（直接失败）。
        有产出即复位计数并清续跑标志（防残留标志把后续纯文本轮误路由回
        LLM 造成死循环，与 task_reminder 次级证据放行同键语义）。
        """
        is_empty = not (result_text or "").strip() and not tool_calls
        if not is_empty:
            result["llm_empty_streak"] = 0
            result["_has_new_llm_input"] = False
            return

        streak = int(ctx.state.get("llm_empty_streak", 0) or 0) + 1
        result["llm_empty_streak"] = streak
        pipeline_id = ctx.state.get("pipeline_id", "?")
        if streak > self._max_empty_retries:
            result["llm_empty_exhausted"] = True
            result["_has_new_llm_input"] = False
            logger.warning(
                "[%s] 空回复重试耗尽（已重试 %d 次）pipeline=%s，交任务域裁决失败",
                self.name,
                streak - 1,
                pipeline_id,
            )
        else:
            result["_has_new_llm_input"] = True
            logger.warning(
                "[%s] 空回复（无文本无工具调用），第 %d/%d 次重试 pipeline=%s",
                self.name,
                streak,
                self._max_empty_retries,
                pipeline_id,
            )

    def _resolve_tool_call_ids(self, tool_calls: list[dict[str, Any]]) -> list[str]:
        """解析 tool_call id，回写到入参列表并返回 id 序列。

        provider 返回的 id 原样透传（单一关联键，ADR 2026-09-28-tool-call-live-card）：
        流式增量事件（llm_service 逐 chunk 发原始 id）、tool_start/tool_result 事件、
        assistant 消息、raw_tool_calls、持久化与前端渲染全程使用同一个 id。仅
        缺失/空 id 时兜底生成 ``call_<hex>``。禁止按内部格式白名单重造非空
        id——重造会让流事件（原始 id）与最终结果（重造 id）分叉，前端同一
        工具调用渲染两张卡（MiniMax call-<uuid> 连字符形态实测复发）。

        Args:
            tool_calls: 工具调用列表（原地回写 ``id`` 字段）

        Returns:
            与入参顺序一致的 id 列表
        """
        resolved_ids: list[str] = []
        for tc in tool_calls:
            raw_id = tc.get("id")
            if raw_id:
                resolved_ids.append(raw_id)
            else:
                resolved_ids.append(f"call_{uuid.uuid4().hex[:24]}")
        for i, tc in enumerate(tool_calls):
            tc["id"] = resolved_ids[i]
        return resolved_ids

    def _build_partial_failure_result(
        self,
        partial: dict[str, Any],
        *,
        status: str,
        error_info: dict[str, Any] | None = None,
        identity: ModelIdentity,
    ) -> dict[str, Any]:
        """流中断/取消 → 半截 assistant 消息落库（``status: error|interrupted``）。

        落库判据：文本/思考/保留 tool_calls 任一非空即落——思考（reasoning）
        非空亦属可显示内容（用户裁定 2026-11-04「所有消息都要有出口」：纯思考
        中断轮不落库则内核事件桥对 round 无 assistant 发 null 占位，前端流式
        占位失去权威坐标成为幽灵气泡，装机 2026-11-03 实证钉死消息流底部）。
        真零产出（text/thinking/保留 tool_calls 全空）不落痕，防空气泡回潮
        （装机库实测 374 条存量教训）。

        tool_calls 处理（硬约束：tool_calls/tool 配对完整性）：
        - arguments JSON 未闭合（解析失败/空串）→ 从半截消息剥离该 tool_call；
        - 已闭合 → 保留，并同步追加占位 tool 结果消息（工具未执行，
          ``status:"interrupted"``），保证 history 永远配对完整。
        ``raw_tool_calls`` 恒为空——中断的工具调用绝不交由 tool_core 执行；
        ``raw_error`` 为 None——半截返回是正常落库（非错误轮次），错误信息
        经 ``llm_error_info`` 随消息 blob 持久化。

        中断（interrupted）：置 ``ended=true`` + ``router.stop_reason=user_requested``
        ——引擎既有 ended 边界检查让 run 优雅收尾（post 链跳过、exit 体照跑、
        persist_run_end 正常），persist_run_end 据 stop_reason 落
        Cancelled（不覆写为 Completed）+ 消息带 interrupted 状态（方案 §四.1）。

        Args:
            partial: llm_service 返回的半截内容快照
                （text / thinking_text / tool_calls / usage）
            status: "error"（流中途异常）/ "interrupted"（调用方停止）
            error_info: llm_service 随返回交付的 llm_error_info
                （error_type/error_message；中断路径为 None）

        Returns:
            合并到管道状态的部分结果字典
        """
        text = partial.get("text")
        thinking_text = partial.get("thinking_text")
        is_interrupted = status == "interrupted"

        # 未闭合 tool_call 剥离：arguments 必须是完整 JSON 才保留
        kept_calls: list[dict[str, Any]] = []
        stripped = 0
        for tc in partial.get("tool_calls") or []:
            try:
                json.loads(tc.get("arguments", ""))
            except (ValueError, TypeError):
                stripped += 1
                continue
            kept_calls.append(tc)

        ops: list[dict[str, Any]] = []
        partial_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text or "",
            "status": status,
        }
        if error_info:
            partial_msg["llm_error_info"] = error_info
        if thinking_text:
            partial_msg["reasoning_content"] = thinking_text
        if kept_calls:
            resolved_ids = self._resolve_tool_call_ids(kept_calls)
            partial_msg["tool_calls"] = [
                {
                    "id": resolved_ids[i],
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", ""),
                    },
                }
                for i, tc in enumerate(kept_calls)
            ]
        # 可显示内容判据（聊天消息流权威化设计真值 docs/working/聊天消息流权威化重设计_20261027.md
        # 不变量 4 现行版）：文本/思考/保留 tool_calls 任一非空即落 assistant 消息
        # （用户裁定 2026-11-04 所有消息都要有出口：纯思考中断轮不落库则事件桥
        # 发 null 占位，前端流式占位失坐标成幽灵气泡——装机 2026-11-03 实证）。
        # 真零产出（三者全空）不落痕防空气泡回潮（装机库实测 374 条存量教训）。
        # ended/stop_reason 照常置位，run 收尾语义不受影响。
        if (text or "").strip() or (thinking_text or "").strip() or kept_calls:
            ops.append({"op": "set", "msg": partial_msg})
        # 占位 tool 结果：闭合的 tool_call 已保留在 assistant 消息里，必须补
        # 配对结果消息（assistant 之后的槽位），否则下一轮请求 400
        for tc in kept_calls:
            ops.append(
                {
                    "op": "set",
                    "msg": {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "（生成中断，工具未执行）",
                        "status": "interrupted",
                    },
                }
            )

        usage = partial.get("usage")
        llm_usage: dict[str, Any] = {}
        if usage:
            # model/provider 归属依据，同成功路径（监控按模型聚合 token）；
            # 归属读本次调用快照——实例字段在 await 期间会被并发重绑踩踏。
            llm_usage = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cached_tokens": usage.get("cached_tokens", 0),
                "model": identity.model,
                "provider": identity.provider,
            }

        logger.warning(
            "[%s] 流式中断，半截内容落库 status=%s text=%d chars "
            "thinking=%d chars tool_calls=%d 保留/%d 剥离",
            self.name,
            status,
            len(text or ""),
            len(thinking_text or ""),
            len(kept_calls),
            stripped,
        )
        result: dict[str, Any] = {
            StateKeys.RAW_RESULT: text,
            StateKeys.RAW_ERROR: None,
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.RAW_THINKING: thinking_text,
            "llm_usage": llm_usage,
            "context_window": identity.context_window,
            "llm_model": identity.model,
            "llm_model_id": identity.model_id,
            "llm_provider": identity.provider,
            "llm_api_base": identity.api_base,
        }
        # 与成功路径同构：无消息可落就不带 messages 键（引擎零 op 语义同成功路径
        # appended_msg=None 分支）。
        if ops:
            result["messages"] = {"_ops": ops}
        if is_interrupted:
            result[StateKeys.ENDED] = True
            # 用户停止信号：引擎收尾据此落 run=Cancelled（不覆写为 Completed）
            result["router.stop_reason"] = "user_requested"
        return result

    # 多模态引用→二进制的解析上限（与 preprocessor max_file_size 默认对齐，20MB）
    _MAX_IMAGE_BYTES = 20 * 1024 * 1024

    @classmethod
    def _resolve_multimodal_blocks(
        cls, blocks: Any
    ) -> list[dict[str, Any]]:
        """把 multimodal_content 里的本地引用解析成 base64 data URL（发送前转换）。

        分工（ADR 2026-08-21）：preprocessor 只输出引用（/uploads/... 或绝对
        路径——state/trace 恒小）；本方法在 LLM 请求装配时读文件转
        ``data:{mime};base64,...``，二进制不进任何持久层。

        - http(s) URL：原样透传（API 直连拉取）；
        - /uploads/ 引用 / 已存在文件的绝对路径：读文件 → base64 data URL；
        - 非 image_url 块（text 等）：原样透传；
        - 解析失败（文件丢失/过大/读错）：替换为 `[附件 … 解析失败：原因]`
          占位块，LLM 可见附件缺失说明（2026-09-08 用户裁定：附件解析失败
          不得静默跳过——后端要报错），不阻断其余内容。

        Args:
            blocks: state["multimodal_content"]（None/非列表 → 空结果）

        Returns:
            解析后的内容块列表
        """
        if not isinstance(blocks, list):
            return []
        resolved_blocks: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                if isinstance(block, dict):
                    resolved_blocks.append(block)
                continue
            url = (block.get("image_url") or {}).get("url", "")
            data_url, reason = cls._resolve_image_ref(url)
            if data_url:
                resolved_blocks.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                )
            elif url.startswith(("http://", "https://", "data:")):
                resolved_blocks.append(block)
            else:
                logger.warning(
                    "[LLMCore] 多模态图片引用解析失败: %s (%s)", url, reason
                )
                resolved_blocks.append(
                    {
                        "type": "text",
                        "text": f"[附件 {url} 解析失败：{reason}]",
                    }
                )
        return resolved_blocks

    @classmethod
    def _resolve_image_ref(cls, url: str) -> tuple[str, str]:
        """单条图片引用 → (base64 data URL, 失败原因)。

        Args:
            url: /uploads/ 引用或本地绝对路径

        Returns:
            成功：(data URL, 空串)。失败：(空串, 非空原因——文件不存在/
            过大/读取失败)。http/data 引用返回 (空串, 空串)——非本地解析
            失败，调用方原样透传。
        """
        if not url or url.startswith(("http://", "https://", "data:")):
            return "", ""
        path = resolve_uploads_url(url)
        if path is None:
            # 绝对路径引用（preprocessor 透传的用户本地路径）
            if not os.path.isabs(url) or not os.path.isfile(url):  # noqa: PTH117
                return "", "文件不存在"
            path = Path(url)
        try:
            if not path.is_file():
                return "", "文件不存在"
            if path.stat().st_size > cls._MAX_IMAGE_BYTES:
                logger.warning(
                    "[LLMCore] 图片超过 %d 字节上限: %s", cls._MAX_IMAGE_BYTES, url
                )
                return "", f"文件过大（超过 {cls._MAX_IMAGE_BYTES} 字节上限）"
            b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
            ext = path.suffix.lower()
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }.get(ext, "image/png")
            return f"data:{mime};base64,{b64}", ""
        except OSError as exc:
            logger.warning("[LLMCore] 读取图片失败: %s, %s", url, exc)
            return "", "文件读取失败"

    def _build_messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """从管道状态构建 LLM messages 列表。

        拼接顺序：
        1. state["system_message"] -- prompt_build 产出的纯 SystemMessage
        2. state["compression_messages"] -- 压缩块独立消息（L2→L1→state_snapshot）
        3. state["messages"] -- 管道维护的对话历史（最近消息）
        4. state["multimodal_content"] -- 多模态内容（图片/文件等，合并到最后一条用户消息）
        5. state["prompt.dynamic_vars"] -- 动态变量（追加在最后）

        Args:
            state: 管道状态字典

        Returns:
            符合 OpenAI Chat API 格式的 messages 列表
        """
        messages: list[dict[str, Any]] = []

        # 1. SystemMessage（纯 prompt，永不变化 → cache hit）
        system_msg = state.get("system_message")
        if system_msg:
            messages.append(system_msg)

        # 2. 压缩消息（每个块独立消息，老→新。前缀匹配 → cache hit）
        #    块消息持久化态携带 seq/_context_form/metadata.compression_ref 等
        #    管道内部字段——出站剥离（与 history 段同一黑名单）。
        for cm in state.get("compression_messages", []):
            if any(k in cm for k in _OUTBOUND_INTERNAL_FIELDS):
                cm = {k: v for k, v in cm.items() if k not in _OUTBOUND_INTERNAL_FIELDS}  # noqa: PLW2901
            messages.append(cm)

        # 3. 历史消息（管道维护的对话历史——压缩后只含最近消息）。
        #    出站前衰减系统通知（已取代/已消费的自动父通知不再全量携带，
        #    state 持久层不动——见 _decay_system_notifications）。
        history = _decay_system_notifications(state.get("messages", []))
        for m in history:
            # 引擎合成的失败终态消息（stage_finalize 失败直写，synthetic 标记）
            # 整条剔除：失败原因是给人看的不是给模型看的——LLM 供应商层错误
            # （认证/限流/网络）模型无任何处置能力，回灌历史只会污染上下文
            # 并诱导模型对基础设施错误做无意义解释（2026-10-16 用户报告）。
            # 合成消息恒无 tool_calls，剔除不产生配对孤儿。
            if m.get("role") == "assistant" and m.get("synthetic"):
                continue
            # 清理管道内部字段，不发给 LLM（_OUTBOUND_INTERNAL_FIELDS，严格
            # provider 如 zhipu 对 messages 内未知字段整请求拒绝——1210 实锤
            # 2026-09-03）：seq 槽位号；tool_result 持久化信封；_context_form
            # 语义标记；metadata（内核 client_message_id 幂等键等 UI 对账数据，
            # router.rs ADR-2026-08-21）；status/llm_error_info（轮次终态与
            # 错误信息，前端消费）；synthetic（引擎合成标记）；agent_id（消息
            # 执行身份戳记，前端气泡卡名/卡头像数据源）。reasoning_content 不
            # 剥——provider 适配层的采样保留策略在下游消费它。
            if any(k in m for k in _OUTBOUND_INTERNAL_FIELDS):
                m = {k: v for k, v in m.items() if k not in _OUTBOUND_INTERNAL_FIELDS}  # noqa: PLW2901
            messages.append(m)

        # 4. 多模态内容（合并到最后一条用户消息）
        #    引用块在此处解析成 base64 data URL（发送前转换，ADR 2026-08-21）：
        #    preprocessor 输出的 /uploads/ 引用与绝对路径引用保持 state/trace 恒小，
        #    二进制只活在本次请求装配的局部变量里。
        multimodal_content = self._resolve_multimodal_blocks(state.get("multimodal_content"))
        if multimodal_content and messages:
            # 找到最后一条用户消息
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    # 将纯文本内容转换为 content blocks 格式
                    existing_content = messages[i].get("content", "")
                    if isinstance(existing_content, str):
                        # 转换为 content blocks 数组
                        messages[i]["content"] = [{"type": "text", "text": existing_content}] + multimodal_content
                    elif isinstance(existing_content, list):
                        # 已经是 content blocks，直接追加
                        messages[i]["content"].extend(multimodal_content)
                    break

        # 5. 动态变量（每轮变化的上下文：时间戳、session_id 等）
        #    作为独立 user 消息追加在末尾，绝不合并进 messages[0]（system_message）。
        #    system_message 必须保持纯 prompt、永不变化（prompt cache 命中依赖此不变性），
        #    而 dynamic_vars 含时间戳等每轮变化的内容，合并进去会破坏 cache 并污染系统提示词。
        #    role 必须用 user：末尾的 role=system 且每轮变化的消息会让 DeepSeek prompt
        #    cache 缓存单元边界错位（命中率从 ~97% 崩到 ~5%），role=user 则正常（~99%）。
        #    内容用 <dynamic_vars> XML 包裹并标注"系统注入"，模型仍能识别为背景信息。
        dynamic_vars_msg = state.get("prompt.dynamic_vars")
        if dynamic_vars_msg:
            if isinstance(dynamic_vars_msg, dict):
                content = dynamic_vars_msg.get("content", "")
            else:
                content = str(dynamic_vars_msg)
            if content:
                messages.append(
                    {
                        "role": "user",
                        "content": content,
                    }
                )

        # 注入文本工具引用对账（卡4②）：指令引用声明但不在当前数组的工具
        # → 出站副本追加对账标注 + ERROR 留痕（构造期拦截，不炸整轮）。
        _annotate_dangling_tool_refs(messages, state)

        return messages

    async def _call_llm(  # noqa: PLR0912
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
        identity: ModelIdentity,
    ) -> LLMResponse | PartialStreamOutcome:
        """经 llm_service（llm.complete_stream）调用 LLM。

        LLM 面唯一事实源 = llm_service：经内核 tool-executor 能力跨进程调用
        （调用形态与 approval/hindsight 一致）。返回值是聚合响应 dict（与
        adapter 的 LLMResponse 同构）；``partial`` 字段非 None（流中断/取消）
        时组装半截结果（``PartialStreamOutcome``），由 execute 直接落库。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            stream: 是否使用流式模式（llm.complete_stream 恒流式，此参数
                保留调用面不变，透传场景由 llm_service 统一处理）

        Returns:
            成功：LLMResponse；流中断/取消：PartialStreamOutcome
        """
        logger.info(
            "[%s] Sending %d messages to LLM",
            self.name,
            len(messages),
        )

        # 每消息仅输出结构化摘要（角色/长度/预览）到 DEBUG——完整内容已在
        # 管道 state/trace 落库，INFO 全量打印属日志噪音且放大隐私面。
        for idx, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            name = msg.get("name", "")
            tc_list = msg.get("tool_calls", [])
            prefix = f"[{self.name}] MSG-{idx} role={role}"
            if name:
                prefix += f" name={name}"
            if tc_list:
                try:
                    tc_str = json.dumps(tc_list, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    tc_str = str(tc_list)
                logger.debug("%s tool_calls=%d chars=%s", prefix, len(tc_list), len(tc_str))
            else:
                preview = repr(str(content)[:80]) if content else ""
                logger.debug("%s content_len=%d preview=%s", prefix, len(str(content)), preview)

        # 服务调用参数：model 用 yaml key（model_id）做 deployment 匹配，
        # 保证 model_name 重名时（官方与 apigo 同底模）能路由到正确 provider。
        # _model_id 为空（旧 config）时回退到 _model，保持兼容。
        # 消息标准化（结构/配对/MiniMax）在 llm_service 侧统一执行
        # （llm.complete_stream 入口唯一关卡），本插件不再做调用方预处理。
        # 身份一律读本次调用快照：实例字段会被并发 execute 的重绑在 await
        # 前后踩踏（线程交错窗口），读了就把请求发给并发方的模型。
        kwargs: dict[str, Any] = {
            "model": identity.model_id or identity.model,
            "messages": messages,
            **identity.default_params,
        }

        # agent 层级优先级透传：本进程（llm_core）的 contextvar 不跨进程共享，
        # llm_service 的 KeyPool 优先级排队读的是它自己的 contextvar——经
        # kwargs 显式接收落位（llm_service complete_stream 侧配套）。
        kwargs["agent_level"] = ctx.state.get("agent_level", "L3")

        # 流式超时配置透传（llm.yaml defaults/models.<id> 键，键名与配置一致，
        # llm_service 侧 _normalize_timeout_keys 归一为 adapter 内部键）。
        # first_token_timeout 退役不上送（2026-11-04 用户裁定：吐字间隔超时
        # 含首 token，首块预算与吐字间隔同源，独立的更短首 token 预算失真）。
        # stream_idle_timeout：None（旧绑定快照/配置缺省）不携带——adapter 落
        # 自身默认。
        if identity.stream_idle_timeout is not None:
            kwargs["stream_idle_timeout"] = identity.stream_idle_timeout

        # 思考参数覆盖：state.thinking_strength = 参数组 JSON 串（选项即配置
        # 参数组，选中即透传）；解析+白名单过滤后与 default_params 合并，
        # 空/非法不覆盖。
        thinking_strength = str(ctx.state.get("thinking_strength") or "")
        strength_params = resolve_thinking_params(thinking_strength)
        if strength_params:
            kwargs.update(strength_params)
            logger.info(
                "[%s] thinking_strength=%s → params=%s",
                self.name,
                thinking_strength,
                strength_params,
            )

        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            logger.info(
                "[%s] tool_schemas count=%d | %s",
                self.name,
                len(tool_schemas),
                ", ".join(t.get("function", {}).get("name", "?") for t in tool_schemas),
            )
            # tools 透传：llm.complete_stream 的 tools 形参接收工具 schema，litellm
            # 据此发 function calling 请求（内核 inject_tool_schemas 已按 agent
            # tool_ids 注入 state.tool_schemas）。空面不携带该键——形参默认 None，
            # 也不触发 capability input_schema 对 tools 的 array 校验。
            kwargs["tools"] = tool_schemas
        else:
            # 空面出站观测（BUG-17）：工具面注入缺失（tool_schema 步骤断链/
            # 漂移清零）时本轮请求无 tools，模型会凭历史降级为正文输出原生
            # 工具标记且永不执行——负例必须留痕，否则主路径断裂不可见。
            logger.warning(
                "[%s] tool_schemas 空——本轮请求不带 tools"
                "（工具面注入断链或 agent 显式声明零工具）",
                self.name,
            )

        # 调用前记录模型/API 信息
        model_str = identity.model
        logger.info(
            "[%s] Calling LLM: model=%s, provider=%s, streaming=%s",
            self.name,
            model_str,
            identity.provider,
            stream,
        )

        caller = _capability_caller
        if caller is None:
            raise RuntimeError(
                "[llm_core] capability caller 未注入：llm_service 调用通道未接线"
                "（server.py on_load 应调用 set_capability_caller）"
            )

        # run_id 取消感知锚：内核注入的 state.run_id 透传给 llm_service 启用
        # 停止感知——契约全管道同一份（主会话/任务/子任务管道无域别，差别只在
        # state 内容），用户停止（dispatch_stop → run Suspended）对所有管道
        # 同等生效：流式协作中断 → partial 落库 → run 终态 Cancelled。
        run_id = ctx.state.get("run_id", "") or ""
        if run_id:
            kwargs["run_id"] = run_id

        # 调用 llm.complete_stream：流式事件经 llm_service 内部 event-bus
        # 推送，返回 dict 携带完整聚合响应；partial 非 None 表示流中断/取消。
        # plugin_id 显式点名 llm_service（capability 声明在 services 轴，不依赖
        # LLM 工具注册表反查）。
        # 流式事件路由键（_call_context）：llm_service 侧 _resolve_envelope 只读
        # arguments["_call_context"]；内核 tool-executor 把 params 级 _call_context
        # 合入 args（args 级同名字段按内建键剥离——防伪造），故必须放 params 顶层。
        # 键与 tool_core 的读取面一致（state["session_id"]/["thread_id"]/
        # ["pipeline_id"]/["message_id"]——引擎每轮注入本轮 a_ 消息 id）。
        params = {
            "tool_name": "llm.complete_stream",
            "plugin_id": "llm_service",
            "args": kwargs,
            "_call_context": {
                "thread_id": ctx.state.get("session_id")
                or ctx.state.get("thread_id", "")
                or "",
                "pipeline_id": ctx.state.get("pipeline_id", "") or "",
                "message_id": ctx.state.get("message_id", "") or "",
            },
        }
        envelope = await caller(
            "invoke", params, self._capability_timeout(identity.model_id or identity.model)
        )
        # tool-executor.invoke 返回 {success, data, error} 信封（内核 ToolResult
        # 序列化）：success=false（工具未注册/执行失败）是错误不是空响应，
        # fail-closed 抛出携带 error——否则盲取字段把失败伪装成"text 空的成功"。
        if not isinstance(envelope, dict):
            raise RuntimeError(
                f"llm.complete_stream 信封形状异常: {type(envelope).__name__}"
            )
        if not envelope.get("success"):
            raise RuntimeError(
                f"llm.complete_stream 工具执行失败: "
                f"{_strip_transport_noise(envelope.get('error') or envelope)}"
            )
        result = envelope.get("data")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"llm.complete_stream 返回形状异常: {type(result).__name__}"
            )

        partial = result.get("partial")
        if partial is not None:
            return PartialStreamOutcome(
                self._build_partial_failure_result(
                    partial,
                    status=result.get("status", "error"),
                    error_info=result.get("llm_error_info"),
                    identity=identity,
                )
            )

        return LLMResponse(
            text=result.get("text"),
            tool_calls=result.get("tool_calls") or [],
            thinking_text=result.get("thinking_text"),
            usage=result.get("usage") or None,
            finish_reason=result.get("finish_reason"),
        )
