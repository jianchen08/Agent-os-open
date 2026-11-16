# @feature: FP-0.2.一 上下文窗口守卫 | @ci: pytest-plugins
"""压缩链支持函数——由 plugin.py 平移（文件长度机械闸触碰即拆，
ADR 2026-10-12-file-length-mechanical-gate 纪律：存量触线拆或缩，不放宽基线）。
plugin.py 经 re-import 保持原符号面（测试与生产引用不变）。"""

from __future__ import annotations

from typing import Any

from _root_api import now_iso_utc as _now_iso
from collections.abc import Awaitable, Callable

from _libs.pipeline_base.plugin import PluginContext
from _libs.pipeline_base.types import StateKeys

# 与 plugin.py 同款类型别名（本地定义防循环导入）
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Awaitable[Any]]
LLMCallFn = Callable[[str | list[dict[str, Any]]], Awaitable[str]]


def _make_minimal_ctx(
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
    pipeline_id: str = "",
) -> PluginContext:
    """构造测试用最小 PluginContext（不直接被生产代码调用）。

    Args:
        state: state 字典初值（与 pipeline_id 合并）
        config: 配置字典
        services: 服务注册表
        pipeline_id: 写入 state[StateKeys.PIPELINE_ID]

    Returns:
        PluginContext 实例
    """
    s: dict[str, Any] = dict(state or {})
    if pipeline_id:
        s.setdefault(StateKeys.PIPELINE_ID, pipeline_id)
    return PluginContext(state=s, config=config or {}, _services=services or {})


# ═══════════════════════════════════════════════════════════
# 压缩预算配置（注入命名空间 context_window 的单一实现；内联形态真值即
# manifest fields.default，引用/内联两形态统一经 build_injected_config 送达）
# ═══════════════════════════════════════════════════════════



def _compression_status_payload(
    status: str,
    reason: str = "",
    original_tokens: int = 0,
    compressed_tokens: int = 0,
    message_count_before: int = 0,
    message_count_after: int = 0,
) -> dict[str, Any]:
    """构造压缩终态留痕载荷（写入 state ``context.compression``）。

    落管道 state 供面板/排查可见：压缩失败是「上下文持续膨胀」的静默劣化，
    只留 WARNING 日志等于用户侧零感知（2026-10-28 装机实证：十一轮压缩全
    败、上下文停在 65 万 token 而界面无任何提示）。
    """
    payload: dict[str, Any] = {
        "status": status,
        "ended_at": _now_iso(),
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "message_count_before": message_count_before,
        "message_count_after": message_count_after,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _resolve_compress_model(
    ctx: PluginContext, injected: dict[str, Any] | None = None
) -> str:
    """压缩用模型 id 解析（2026-10-24 用户裁定：默认跟随管道当前模型）。

    优先级（高 → 低）：
    1. manifest 内联 ``compression.model`` 显式非空——用户点名固定压缩
       模型时尊重之；
    2. ``state.llm_model``（llm_core 每轮成功调用后写入的管道当前模型）——
       缺省路径。fork 前缀逐字节复现执行请求（_render_fork_message 契约），
       同模型才能命中供应商前缀缓存；跨模型（如固定 minimax-m3 压
       MiniMax-M3.1-Flash 的 fork）缓存零复用，整段前缀全额重付；
    3. 都无（冷启动未调过 LLM 且未显式配置）→ 空串，调用方停用压缩。

    本 manifest 的 fields.default 即配置本体（单一真值裁定 2026-09-02 同款
    形态），不经 llm.yaml 解析——guard 所在 sidecar 未注入 llm.yaml 配置
    shim，任何经 loader 的兜底链在这里都是死路。

    Args:
        ctx: 插件执行上下文（读 state.llm_model）
        injected: 注入的 compression 配置 dict（manifest fields.default 内联）

    Returns:
        model_id；无可解析返回空串
    """
    explicit = injected.get("model") if injected else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    # 管道当前模型：优先 **model id**（llm.yaml models 段的 key，全系统唯一
    # 标识，查表/选路/凭证/超时全以它为基础）；回退 model_name（老 state 无
    # id 键时的兼容面，llm 侧入口另有归一兜底）。
    # 把 model_name 当 id 用会让 llm 侧查不到模型条目与 provider → 落 Router
    # 报「no healthy deployments」→ 压缩整链失效（2026-10-28 装机实证：
    # 主管道 382ec1ee 上下文 65 万 token，十一轮压缩全败、上下文零收缩）。
    pipeline_model_id = ctx.state.get("llm_model_id")
    if isinstance(pipeline_model_id, str) and pipeline_model_id.strip():
        return pipeline_model_id.strip()
    pipeline_model = ctx.state.get("llm_model")
    if isinstance(pipeline_model, str) and pipeline_model.strip():
        return pipeline_model.strip()
    return ""


def _compress_capability_timeout(model_id: str) -> float:
    """压缩 LLM 反向调用的等待上限：压缩模型 call_timeout + 余量。

    SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 会先于压缩完成掐断请求（压缩是
    LLM 级耗时，上界即 llm.yaml call_timeout）。+60s 余量让 llm_service 的
    结构化错误信封先于 SDK 超时返回（错误是值，不丢语义）。

    _config_models 不可达（llm.yaml 未注入本 sidecar）或模型无配置时，
    用 360s 口径（loader 内部默认 300s + 60s 余量）。
    """
    try:
        from _config_models import get_model_config_loader  # noqa: PLC0415
    except ImportError:
        return 360.0
    loader = get_model_config_loader()
    conf = loader.get_llm_core_config(model_id) if model_id else None
    call_timeout = float(conf["call_timeout"]) if conf else 300.0
    return call_timeout + 60.0


def _build_compress_llm_call_fn(
    caller: CapabilityCaller,
    model_id: str = "",
    extra_params: dict[str, Any] | None = None,
) -> LLMCallFn:
    """构建压缩用的 LLM 调用函数。

    经 capability_caller 调 llm.complete_stream（llm_service 服务轴，与
    llm_core 同款调用形态）：tool-executor.invoke 携带 plugin_id 显式点名
    llm_service，返回 {success, data, error} 信封；data 是聚合响应 dict
    （text/tool_calls/thinking_text/usage/finish_reason/partial），压缩取
    data.text 为摘要。

    model 参数 = _resolve_compress_model 解析结果（显式 compression.model
    或管道当前模型，由调用方保证非空——空模型名发往 llm_service 会被
    litellm 以 BadRequest 拒绝（You passed in model=）。

    采样参数（temperature/max_tokens/thinking 等）不在本函数硬编码：
    缺省由 llm.complete_stream 按 llm.yaml 模型条目 default_params 服务侧
    回填（单一真值在模型配置）；extra_params = compression 配置显式携带
    的参数（enabled/model 之外的键），作为调用方显式参数覆盖服务侧默认
    （2026-09-03 用户裁定：manifest 只给模型名也行，给了完整参数就覆盖）。

    Args:
        caller: 能力调用 async 函数 (method, params, timeout) -> Any
        model_id: 压缩用模型 id（manifest compression.model）
        extra_params: 调用方显式覆盖参数（原样透传进工具 args）

    Returns:
        async (messages) -> response_text 的 LLM 调用函数；
        llm.complete_stream 调用失败时抛 RuntimeError 并携带原因
        （禁止把调用错误伪装成空响应——上游会把空串诊断为"LLM 空响应"）
    """
    timeout = _compress_capability_timeout(model_id)

    async def _call(payload: str | list[dict[str, Any]]) -> str:
        # llm.complete_stream 收消息数组；字符串形态包成单条 user 消息
        if isinstance(payload, list):
            messages = payload
        else:
            messages = [{"role": "user", "content": payload}]
        params = {
            "tool_name": "llm.complete_stream",
            "plugin_id": "llm_service",
            "args": {"model": model_id, "messages": messages, **(extra_params or {})},
        }
        try:
            result = await caller("tool-executor.invoke", params, timeout)
        except Exception as e:
            raise RuntimeError(f"[compress_llm_call] llm.complete_stream 调用失败: {e}") from e
        if not isinstance(result, dict):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 信封形状异常: {type(result).__name__}"
            )
        # tool-executor.invoke 返回 {success, data, error} 信封（对齐 llm_core）：
        # success=false（服务未注册/执行失败）是错误不是空响应，fail-closed 抛出。
        if not result.get("success"):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 工具执行失败: {result.get('error') or result}"
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 返回形状异常: {type(data).__name__}"
            )
        # 流中断/取消：半截内容不可作压缩摘要（压缩须全文理解），显式放弃
        if data.get("partial") is not None:
            raise RuntimeError(
                "[compress_llm_call] llm.complete_stream 流中断（partial），放弃压缩"
            )
        return str(data.get("text") or "")

    return _call


# ═══════════════════════════════════════════════════════════
# 语义标记词汇表（对齐 DSH ContextForm，docs/tasks/task_compression_optimization.md 任务 1）
# ═══════════════════════════════════════════════════════════
# 消息的内部字段 _context_form 声明"这条内容是什么"（语义形态），与 role（谁说的）
# 正交。产出方（prompt_build 压缩块 / memory_read 检索条目等）打标，消费者：
# 1. 压缩 fork——与执行路径（llm_core._build_messages）同集剥离，内容原样，
#    不渲染标签（2026-09-03 用户裁定：渲染会在首个标记消息处分叉 token
#    前缀，击穿与执行请求的 prefix cache 对齐）；
# 2. capability 回退序列化（_format_messages）——渲染为 [form] 可见前缀；
# 3. 最终 LLM——llm_core._build_messages 发送前清理该字段（同 seq/tool_result），
#    不进 API 载荷，不影响 prompt cache。
CONTEXT_FORM_INSTRUCTIONS = "instructions"  # 文件规则/项目约定（须遵守的指令性内容）
CONTEXT_FORM_NOTICE = "notice"  # 一次性通知（发生了什么，无需展开）
CONTEXT_FORM_RECALL = "recall"  # 从记忆库检索的内容
CONTEXT_FORM_RELAY = "relay"  # 其他 agent 传来的消息
CONTEXT_FORM_SNAPSHOT = "snapshot"  # 状态快照（后一份覆盖前一份）

CONTEXT_FORM_LABELS = {
    CONTEXT_FORM_INSTRUCTIONS: "[instructions]",
    CONTEXT_FORM_NOTICE: "[notice]",
    CONTEXT_FORM_RECALL: "[recall]",
    CONTEXT_FORM_RELAY: "[relay]",
    CONTEXT_FORM_SNAPSHOT: "[snapshot]",
}


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """估算单条消息的 token 数（统一口径：content/reasoning_content/tool_calls 字符数 // 2）。

    reasoning_content（思考全文）随 assistant 消息驻留并整段回放（MiniMax
    适配器 replay_reasoning 契约），实测可占消息本体 85%+（2026-10-24 G2
    三连败：42.7 万字符正文 + 286.6 万字符思考滚到 input 101.5%）。切分线/
    批次预算/压缩验收全部经本函数——漏算思考会让"压无可压"假象与真实
    input 失配，压缩空转。content 维持 str() 化口径（多模态 list 等非字符串
    形态照旧计长）；reasoning_content 只认 str。
    """
    content = msg.get("content", "")
    tokens = max(1, len(str(content)) // 2) if content else 0
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        tokens += max(1, len(reasoning) // 2)
    for tc in msg.get("tool_calls", []) or []:
        args = tc.get("function", {}).get("arguments", "")
        if args:
            tokens += max(1, len(args) // 2)
    return tokens
