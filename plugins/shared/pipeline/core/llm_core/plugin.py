"""LLM Core 插件 -- 基于 LLM Adapter 的大模型调用实现。

通过 LLM Adapter 中间层调用大模型，支持多模型 fallback 和流式回调。
重试/降级由 LLM Adapter（Router 层）负责；插件只上抛错误，错误处理语义见 ADR 2026-08-18。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由引擎/编排层按错误类型决定（瞬态 sidecar 崩溃→retry 一次；非瞬态→上抛）
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 共享上传目录解析（plugins/shared/uploads_path.py，ADR 2026-08-21）：
# 本文件位于 plugins/shared/pipeline/core/llm_core/，上溯 3 级到 shared。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from _message_normalizer import (  # noqa: E402
    _is_valid_tool_call_id,
    normalize_messages_for_provider,
)

# LLMResponse/LLMAdapter：响应类型与测试注入适配器协议定义（adapter 模块保留
# 为类型/响应结构归属地；llm_core 不再直连 LLM API，调用走 llm_service）。
from adapter import LLMAdapter, LLMResponse  # noqa: E402
from pipeline.plugin import ICorePlugin, PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402
from uploads_path import resolve_uploads_url  # noqa: E402

logger = logging.getLogger(__name__)

# ── 能力调用器（LLM 面唯一事实源 = llm_service）────────────────────────
# llm_core 的 LLM 调用经内核 tool-executor 能力跨进程调 llm.complete_stream
# （与 approval/hindsight 经 capability 的既有用法同构）。server.py 在 on_load
# 注入本函数；测试注入伪实现。未注入时调用抛明确错误（接线 bug 早暴露）。
CapabilityCaller = Callable[[str, dict[str, Any]], Any]

_capability_caller: CapabilityCaller | None = None


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入能力调用句柄（async fn `(method, params) -> Any`）。

    server.py 在 on_load 时调用：
    ``set_capability_caller(lambda m, p: plugin.get_capability("tool-executor").call(m, p))``
    LLM 调用（llm.complete_stream）据此经内核 tool-executor 转发到 llm_service。

    Args:
        caller: 能力调用 async 函数（method 传 capability 短名——SDK 句柄
            ``get_capability("tool-executor").call`` 内部组装
            ``<capability>.<method>`` 全名，传全名会双重前缀）；传 None 清空
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

# ── 思考强度 → 模型参数（思考强度全链路）────────────────────────────
# 前端 user_input 携带 thinking_strength（off/low/medium/high），内核透传注入
# state；llm_core 在请求构造时按档位覆盖**思考相关参数**（reasoning_effort /
# thinking），与 default_params 合并后进 kwargs。reasoning_effort 经
# _provider_registry.apply_pre_send 对 openai/ 前缀 provider 自动挪进 extra_body
# 透传（DeepSeek 等）。off/缺失 → 不覆盖（保持 llm.yaml default_params 现状）。
#
# 决策（用户确认）：temperature / max_tokens 等采样参数**不随强度覆盖**——
# 强度只路由思考参数，采样参数始终用模型 default_params。
#
# 路由规则（模型配置优先）：模型可在 llm.yaml 的 models.<id>.thinking_strength_params
# 定义自己的强度→参数映射（不同模型思考参数不一致：DeepSeek reasoning_effort /
# MiniMax adaptive thinking / 无 reasoning 的普通模型）。模型配置了某档位 →
# 用模型配置；未配置的档位 → 回退内置默认表。内置表是各模型的兜底基线。
_THINKING_STRENGTH_PARAMS: dict[str, dict[str, Any]] = {
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "high"},
}

# 思考强度允许覆盖的参数白名单：只含思考相关字段；
# temperature/max_tokens 等采样参数不随强度覆盖（即使用户配置里写了也过滤）。
_THINKING_STRENGTH_ALLOWED = {"reasoning_effort", "thinking"}


def resolve_thinking_strength_params(
    strength: str,
    model_params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """思考强度 → 思考参数覆盖集；off/未知/空 → None（不覆盖）。

    Args:
        strength: 思考强度档位（off/low/medium/high）
        model_params: 模型级路由规则（llm.yaml models.<id>.thinking_strength_params），
            配置了该档位则优先使用（缺失字段用内置表补全）；None 或未配置该档位
            时回退内置默认表。仅白名单内思考参数生效（temperature/max_tokens 忽略）。
    """
    if not strength or strength == "off":
        return None
    if model_params and isinstance(model_params, dict):
        override = model_params.get(strength)
        if isinstance(override, dict):
            base = _THINKING_STRENGTH_PARAMS.get(strength, {})
            merged = {**base, **override}
        else:
            merged = _THINKING_STRENGTH_PARAMS.get(strength, {})
    else:
        merged = _THINKING_STRENGTH_PARAMS.get(strength, {})
    if not merged:
        return None
    return {k: v for k, v in merged.items() if k in _THINKING_STRENGTH_ALLOWED}


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
        _adapter: 测试注入的进程内适配器（None = 走 llm_service 通道）
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        adapter: LLMAdapter | None = None,
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
            adapter: 测试注入的进程内适配器实例；None = 生产走 llm_service
                能力通道（server.py on_load 注入 capability caller）
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
        # 模型级思考强度路由规则（llm.yaml models.<id>.thinking_strength_params）：
        # 由 _apply_model_from_state 解析模型时更新；None = 未配置 → 用内置默认表。
        self._thinking_strength_params: dict[str, dict[str, Any]] | None = (
            self._config.get("thinking_strength_params")
        )
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

        # 构建适配器：llm_core 的 LLM 面唯一事实源 = llm_service 的
        # llm.complete_stream（经 tool-executor 跨进程调用）；adapter 参数仅
        # 测试注入用（进程内假实现），None = 走 llm_service 通道。
        if adapter is not None:
            self._adapter = adapter
        else:
            self._adapter = None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    def _apply_model_from_state(self, state: dict[str, Any]) -> None:
        """从管道 state 动态解析本次调用的模型并更新 self（0.2 适配）。

        0.1 由 ``plugin_resolver.apply_agent_model_override`` 在管道启动前
        一次性覆盖 llm_call 实例；0.2 sidecar 无 plugin_resolver，改为
        execute 时从 state 读 model_id/model_tier 动态解析。

        优先级：``state.model_id`` > ``state.model_tier``(→ defaults.tiers)
        > ``llm.yaml defaults.chat``。任一命中即用其 model_id 查
        ``get_llm_core_config`` 更新 provider/model_name/api_base/api_key 等；
        全部缺失则保持构造时的默认配置不变（不阻断，由调用方降级）。

        Args:
            state: 管道状态字典，读 ``model_id`` / ``model_tier``。
        """
        # 已锁定同一 model_id 则跳过（避免每轮重复解析）
        resolved_id = state.get("model_id", "")
        if not resolved_id:
            tier = state.get("model_tier", "")
            if tier:
                resolved_id = self._resolve_tier(tier)
        if not resolved_id:
            resolved_id = self._default_chat_model()
        if not resolved_id or resolved_id == self._model_id:
            return

        llm_conf = self._get_llm_core_config(resolved_id)
        if not llm_conf:
            logger.warning(
                "[%s] model_id=%s 在 llm.yaml 未找到配置，保持当前 model=%s",
                self.name,
                resolved_id,
                self._model,
            )
            return

        self._model_id = resolved_id
        self._provider = llm_conf.get("provider", self._provider)
        self._model = llm_conf.get("model_name", self._model)
        self._api_base = llm_conf.get("api_base") or self._api_base
        self._api_key = llm_conf.get("api_key") or self._api_key
        self._context_window = llm_conf.get("context_window") or self._context_window
        if llm_conf.get("default_params"):
            self._default_params = llm_conf["default_params"]
        # 模型级思考强度路由规则（llm.yaml models.<id>.thinking_strength_params）：
        # 随模型解析更新；未配置 → None（_call_llm 回退内置默认表）。
        if "thinking_strength_params" in llm_conf:
            self._thinking_strength_params = llm_conf["thinking_strength_params"]
        logger.info(
            "[%s] model resolved: model_id=%s provider=%s model=%s",
            self.name,
            self._model_id,
            self._provider,
            self._model,
        )

    def _get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """通过 sidecar 注入的 config 桥取模型配置（与 0.1 对齐）。

        从 ``_config_models.get_model_config_loader()`` 拿 loader，调
        ``get_llm_core_config``。模型未配置时 loader 返回 None（合法降级）；
        import/loader 自身故障是 sidecar 接线 bug，直接抛出可见。
        """
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().get_llm_core_config(model_id)

    def _resolve_tier(self, tier: str) -> str:
        """tier → model_id（defaults.tiers 解析）。"""
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().resolve_tier(tier)

    def _default_chat_model(self) -> str:
        """defaults.chat 默认对话模型 id。"""
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().get_default_chat_model()

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
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
        # 动态模型选择（0.2 适配）：每次 execute 从 state 读 model_id/model_tier，
        # 从注入的 llm.yaml 解析完整配置并更新 self。0.1 由 plugin_resolver 在
        # 管道启动前一次性覆盖 llm_call 实例；0.2 sidecar 无 plugin_resolver，
        # 改为 execute 时动态解析（支持多 agent/多模型切换）。
        # 优先级：state.model_id > state.model_tier(→ defaults.tiers) > defaults.chat
        self._apply_model_from_state(ctx.state)

        messages = self._build_messages(ctx.state)
        streaming = ctx.state.get("streaming", True)

        try:
            response: LLMResponse | PartialStreamOutcome = await self._call_llm(
                messages, ctx, stream=streaming
            )

            # 流中断/取消（llm.complete_stream 返回 partial）：半截消息
            # 已组装好落库 ops + status/ended 标记，直接返回（不再走
            # LLMResponse 成功组装路径）。
            if isinstance(response, PartialStreamOutcome):
                return response.result

            result_text = response.text
            tool_calls = response.tool_calls
            thinking_text = response.thinking_text
            # 输出截断信号：finish_reason=="length" 表示命中 max_tokens，
            # tool_call 的 arguments JSON 可能不完整。供下游识别截断、
            # 在写入结果中提示模型续写，避免留下半截文件。
            output_truncated = response.finish_reason == "length"

            llm_usage = None
            if response.usage:
                llm_usage = {
                    "input_tokens": response.usage.get("prompt_tokens", 0),
                    "output_tokens": response.usage.get("completion_tokens", 0),
                    "total_tokens": response.usage.get("total_tokens", 0),
                    "cached_tokens": response.usage.get("cached_tokens", 0),
                }

            logger.info(
                "[%s] LLM call succeeded (streaming=%s, thinking=%s, text=%s, tool_calls=%d)",
                self.name,
                streaming,
                bool(thinking_text),
                (result_text or "")[:200],
                len(tool_calls or []),
            )
            # 完整响应记录到管道日志（DEBUG 级别）
            logger.debug(
                "[%s] LLM full response: text=%d chars, thinking=%d chars, usage=%s",
                self.name,
                len(result_text or ""),
                len(thinking_text or ""),
                llm_usage,
            )
            if tool_calls:
                for tc in tool_calls:
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

            # LLMCore 生产的 assistant 回复。新模型(op-based):只 emit 一个 append set op,
            # 由引擎 apply 到 state["messages"] + message_slots(不再返回全量 history)。
            appended_msg: dict[str, Any] | None = None
            if tool_calls:
                # 将解析后的 id 回写到 raw_tool_calls，供后续 tool_core 使用
                resolved_ids = self._resolve_tool_call_ids(tool_calls)

                # LLM 返回工具调用 -> append assistant 消息（含 tool_calls）
                # 统一保留 reasoning_content 到内存（不管 provider）：
                # 发送给 API 时由 ProviderAdapter 按 provider 决定是否剥离
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
                appended_msg = assistant_msg
            elif result_text:
                # LLM 普通文本回复 -> append assistant 消息
                _plain_msg: dict[str, Any] = {"role": "assistant", "content": result_text}
                if thinking_text:
                    _plain_msg["reasoning_content"] = thinking_text
                appended_msg = _plain_msg

            _pipeline_id = ctx.state.get("pipeline_id", "?")
            _iteration = ctx.state.get("iteration", -1)
            logger.debug(
                "[%s] pipeline=%s iter=%d LLM returned: text=%d chars, tool_calls=%d, thinking=%d chars",
                self.name,
                _pipeline_id,
                _iteration,
                len(result_text) if result_text else 0,
                len(tool_calls) if tool_calls else 0,
                len(thinking_text) if thinking_text else 0,
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
                "context_window": self._context_window,
                "llm_model": self._model,
                "llm_provider": self._provider,
                "llm_api_base": self._api_base,
                "output_truncated": output_truncated,
            }
            if messages_update is not None:
                result["messages"] = messages_update
            return result

        except Exception as exc:
            logger.error(
                "[%s] LLM call failed: %s — %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            # 工具调用错误后重置消息配对缓存，确保下次全量扫描
            exc_msg = str(exc)
            if "tool_call" in exc_msg.lower() or "tool call" in exc_msg.lower():
                # 同目录平铺 import（与本文件 line 27 一致）。原 ``plugins.core...``
                # 路径不存在（plugins.core 包未定义），ImportError 会顶替原始异常上抛。
                from _message_normalizer import reset_pairing_cache  # noqa: PLC0415

                # 精确重置当前管道的缓存（pipeline_id 维度隔离后必须带 ID）
                _pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
                reset_pairing_cache(
                    self._provider,
                    self.name,
                    pipeline_id=_pipeline_id,
                )
                logger.info(
                    "[%s] 检测到 tool_call 相关错误，已重置配对缓存 (pipeline=%s)",
                    self.name,
                    _pipeline_id or "?",
                )
            # 流中断/取消的半截落库已由 _call_llm 在返回值路径处理
            # （llm_service partial → PartialStreamOutcome → execute 直接返回）；
            # 此处仅传播未预期异常（建连失败/零累积内容/通道故障）。
            raise

    def _resolve_tool_call_ids(self, tool_calls: list[dict[str, Any]]) -> list[str]:
        """解析并标准化 tool_call id，回写到入参列表并返回 id 序列。

        部分模型返回非标准格式（如 call_function_xxx_1），统一替换为
        ``call_<hex>`` 格式，确保系统内一致且 API 兼容；assistant 消息与
        state 中的 raw_tool_calls 使用同一份 id。

        Args:
            tool_calls: 工具调用列表（原地回写 ``id`` 字段）

        Returns:
            与入参顺序一致的标准化 id 列表
        """
        resolved_ids: list[str] = []
        for tc in tool_calls:
            raw_id = tc.get("id")
            if raw_id and _is_valid_tool_call_id(raw_id):
                resolved_ids.append(raw_id)
            else:
                std_id = f"call_{uuid.uuid4().hex[:24]}"
                resolved_ids.append(std_id)
                if raw_id:
                    logger.info(
                        "[%s] LLM 返回非标准 tool_call_id，已修正: %s → %s",
                        self.name,
                        raw_id,
                        std_id,
                    )
        for i, tc in enumerate(tool_calls):
            tc["id"] = resolved_ids[i]
        return resolved_ids

    def _build_partial_failure_result(
        self,
        partial: dict[str, Any],
        *,
        status: str,
        error_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """流中断/取消 → 半截 assistant 消息落库（``status: error|interrupted``）。

        tool_calls 处理（硬约束：tool_calls/tool 配对完整性）：
        - arguments JSON 未闭合（解析失败/空串）→ 从半截消息剥离该 tool_call；
        - 已闭合 → 保留，并同步追加占位 tool 结果消息（工具未执行，
          ``status:"interrupted"``），保证 history 永远配对完整。
        ``raw_tool_calls`` 恒为空——中断的工具调用绝不交由 tool_core 执行；
        ``raw_error`` 为 None——半截返回是正常落库（非错误轮次），错误信息
        经 ``llm_error_info`` 随消息 blob 持久化。

        中断（interrupted）：置 ``ended=true``——引擎既有 ended 边界检查让
        run 优雅收尾（post 链跳过、exit 体照跑、persist_run_end 正常），
        dispatch_stop 已把 run 置 suspended 的信号由 persist_run_end 覆写为
        Completed + 消息带 interrupted 状态（方案 §四.1）。

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
            llm_usage = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cached_tokens": usage.get("cached_tokens", 0),
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
            "messages": {"_ops": ops},
            "llm_usage": llm_usage,
            "context_window": self._context_window,
            "llm_model": self._model,
            "llm_provider": self._provider,
            "llm_api_base": self._api_base,
        }
        if is_interrupted:
            result[StateKeys.ENDED] = True
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
        - 解析失败（文件丢失/过大/读错）：warning + 丢弃该块，不阻断请求
          （模型收到剩余内容，损失可见）。

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
            data_url = cls._resolve_image_ref(url)
            if data_url:
                resolved_blocks.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                )
            elif url.startswith(("http://", "https://", "data:")):
                resolved_blocks.append(block)
            else:
                logger.warning(
                    "[LLMCore] 多模态图片引用解析失败，已丢弃该块: %s", url
                )
        return resolved_blocks

    @classmethod
    def _resolve_image_ref(cls, url: str) -> str:
        """单条图片引用 → base64 data URL（仅本地引用；http/data 返回空串）。

        Args:
            url: /uploads/ 引用或本地绝对路径

        Returns:
            data URL；非本地引用、文件不存在、过大或读取失败返回空串
        """
        if not url or url.startswith(("http://", "https://", "data:")):
            return ""
        path = resolve_uploads_url(url)
        if path is None:
            # 绝对路径引用（preprocessor 透传的用户本地路径）
            if not os.path.isabs(url) or not os.path.isfile(url):  # noqa: PTH117
                return ""
            path = Path(url)
        try:
            if not path.is_file():
                return ""
            if path.stat().st_size > cls._MAX_IMAGE_BYTES:
                logger.warning(
                    "[LLMCore] 图片超过 %d 字节上限，跳过: %s", cls._MAX_IMAGE_BYTES, url
                )
                return ""
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
            return f"data:{mime};base64,{b64}"
        except OSError as exc:
            logger.warning("[LLMCore] 读取图片失败: %s, %s", url, exc)
            return ""

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
        #    _context_form 是语义标记内部字段（prompt_build 打标），发送前剥离，
        #    不进 API 载荷（同下方 history 段的 seq/tool_result 处理）。
        for cm in state.get("compression_messages", []):
            if "_context_form" in cm:
                cm = {k: v for k, v in cm.items() if k != "_context_form"}  # noqa: PLW2901
            messages.append(cm)

        # 3. 历史消息（管道维护的对话历史——压缩后只含最近消息）
        history = state.get("messages", [])
        for m in history:
            # 清理内部标记字段，不发给 LLM：
            # seq 槽位号（内核 apply 时带上，LLM 不需要）；
            # tool_result envelope 字段（tool 消息持久形态的一部分，发送前剥离，
            # 是 Rust 侧后续 envelope 搬家改动的前置配合）；
            # _context_form 语义标记（压缩优化任务 1：产出方打的内部字段，
            # 只供压缩链路消费，最终 LLM 载荷必须剥离以保持 cache 不变）。
            if "seq" in m or "tool_result" in m or "_context_form" in m:
                m = {k: v for k, v in m.items() if k not in ("seq", "tool_result", "_context_form")}  # noqa: PLW2901
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

        return messages

    def _writeback_cleaned_history(
        self,
        state: dict[str, Any],
        raw_messages: list[dict[str, Any]],
        cleaned_messages: list[dict[str, Any]],
    ) -> None:
        """把 normalize 清理后的历史段写回 state["messages"]。

        _build_messages 拼接顺序为 [system?] + compression* + history* + [dynamic_vars?]。
        normalize 的配对清理只发生在 history 段（移除孤儿 tool result /
        未配对 assistant(tool_calls)），不会删除 system/compression/dynamic_vars，
        因此前缀计数与后缀计数不变，可用偏移量定位历史段。

        Args:
            state: 管道状态字典
            raw_messages: normalize 前的完整消息列表
            cleaned_messages: normalize 后的完整消息列表
        """
        prefix_len = 0
        if state.get("system_message"):
            prefix_len += 1
        prefix_len += len(state.get("compression_messages", []))

        suffix_len = 1 if state.get("prompt.dynamic_vars") else 0

        raw_history_len = len(raw_messages) - prefix_len - suffix_len
        cleaned_history_len = len(cleaned_messages) - prefix_len - suffix_len
        if cleaned_history_len <= 0 or raw_history_len <= 0:
            return

        cleaned_history = cleaned_messages[prefix_len : prefix_len + cleaned_history_len]
        state["messages"] = list(cleaned_history)
        logger.info(
            "[%s] normalize 清理写回 state: history %d → %d 条（移除孤儿/未配对消息）",
            self.name,
            raw_history_len,
            cleaned_history_len,
        )

    async def _call_llm(  # noqa: PLR0912
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
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
        normalized_messages = normalize_messages_for_provider(
            messages,
            provider=self._provider,
            name=self.name,
            pipeline_id=ctx.state.get(StateKeys.PIPELINE_ID, ""),
        )

        if len(normalized_messages) < len(messages):
            self._writeback_cleaned_history(ctx.state, messages, normalized_messages)

        # 主动修复：Phase 1-4 转换后仍可能存在遗漏（极端边界情况），
        # 此处主动修复而非仅做诊断日志。
        if self._provider == "minimax":
            fix_count = 0
            for _i, _m in enumerate(normalized_messages):
                if _i > 0 and _m.get("role") == "system":
                    logger.warning(
                        "[%s] MiniMax 主动修复: 非首位 system→user idx=%d, content=%s",
                        self.name,
                        _i,
                        str(_m.get("content", ""))[:200],
                    )
                    _m["role"] = "user"
                    _m.pop("name", None)
                    fix_count += 1
            if fix_count:
                logger.warning(
                    "[%s] MiniMax 主动修复了 %d 条遗漏的 system 消息",
                    self.name,
                    fix_count,
                )

        logger.info(
            "[%s] Sending %d messages to LLM",
            self.name,
            len(normalized_messages),
        )

        for idx, msg in enumerate(normalized_messages):
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
                logger.info(
                    "%s tool_calls=%s",
                    prefix,
                    tc_str if tc_list else "[]",
                )
            else:
                logger.info(
                    "%s content=%s",
                    prefix,
                    str(content) or "",
                )

        # 服务调用参数：model 用 yaml key（model_id）做 deployment 匹配，
        # 保证 model_name 重名时（官方与 apigo 同底模）能路由到正确 provider。
        # _model_id 为空（旧 config）时回退到 _model，保持兼容。
        kwargs: dict[str, Any] = {
            "model": self._model_id or self._model,
            "messages": normalized_messages,
            **self._default_params,
        }

        # agent 层级优先级透传：本进程（llm_core）的 contextvar 不跨进程共享，
        # llm_service 的 KeyPool 优先级排队读的是它自己的 contextvar——经
        # kwargs 显式接收落位（llm_service complete_stream 侧配套）。
        kwargs["agent_level"] = ctx.state.get("agent_level", "L3")

        # 思考强度 → 模型参数覆盖：state.thinking_strength 非空（low/medium/high）
        # 时覆盖采样参数（与 default_params 合并）；off/缺失不覆盖（现状不变）。
        # 路由规则：模型级 thinking_strength_params 优先，未配置回退内置默认表。
        thinking_strength = str(ctx.state.get("thinking_strength") or "")
        strength_params = resolve_thinking_strength_params(
            thinking_strength, self._thinking_strength_params
        )
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

        # 调用前记录模型/API 信息
        model_str = self._model
        logger.info(
            "[%s] Calling LLM: model=%s, provider=%s, streaming=%s",
            self.name,
            model_str,
            self._provider,
            stream,
        )

        caller = _capability_caller
        if caller is None:
            raise RuntimeError(
                "[llm_core] capability caller 未注入：llm_service 调用通道未接线"
                "（server.py on_load 应调用 set_capability_caller）"
            )

        # run_id 取消轮询锚：会话轮次（state.run_id 由内核注入 initial_state）
        # 透传给 llm_service 启用取消轮询；任务管道（task_id 非空）不传——
        # 任务域暂停走任务既有机制（pause_guard/suspend_pipeline），不被
        # 聊天停止误伤。
        run_id = ctx.state.get("run_id", "") or ""
        if run_id and not ctx.state.get(StateKeys.TASK_ID):
            kwargs["run_id"] = run_id

        # 调用 llm.complete_stream：流式事件经 llm_service 内部 event-bus
        # 推送，返回 dict 携带完整聚合响应；partial 非 None 表示流中断/取消。
        # plugin_id 显式点名 llm_service（capability 声明在 services 轴，不依赖
        # LLM 工具注册表反查）。
        params = {
            "tool_name": "llm.complete_stream",
            "plugin_id": "llm_service",
            "args": kwargs,
        }
        envelope = await caller("invoke", params)
        # tool-executor.invoke 返回 {success, data, error} 信封（内核 ToolResult
        # 序列化）：success=false（工具未注册/执行失败）是错误不是空响应，
        # fail-closed 抛出携带 error——否则盲取字段把失败伪装成"text 空的成功"。
        if not isinstance(envelope, dict):
            raise RuntimeError(
                f"llm.complete_stream 信封形状异常: {type(envelope).__name__}"
            )
        if not envelope.get("success"):
            raise RuntimeError(
                f"llm.complete_stream 工具执行失败: {envelope.get('error') or envelope}"
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
                )
            )

        return LLMResponse(
            text=result.get("text"),
            tool_calls=result.get("tool_calls") or [],
            thinking_text=result.get("thinking_text"),
            usage=result.get("usage") or None,
            finish_reason=result.get("finish_reason"),
        )
