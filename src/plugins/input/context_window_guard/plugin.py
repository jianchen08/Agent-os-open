"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时委托给
MemoryContextService.compress_messages 进行预算驱动的分层压缩。

本插件只负责：检查阈值 → 注入依赖 → 调用压缩 → 更新 state。

State 命名空间:
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ContextWindowGuardPlugin(IInputPlugin):
    """上下文窗口守卫 Input 插件。

    检查 messages 的估算 token 数，超阈值时委托 MemoryContextService 压缩。

    优先级：5（在 prompt_build 的 10 之前执行）
    错误策略：SKIP（压缩失败不阻塞管线）

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.5）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（默认 0.5）
                - compression_model: 压缩专用模型 ID（如 minimax-m3），
                  为空时回退到 llm.yaml 的 defaults.compression，再为空则用主模型
        """
        self._config = config or {}
        self._trigger_ratio = self._config.get("trigger_ratio", 0.5)
        self._compression_model: str | None = self._resolve_compression_model(
            self._config.get("compression_model"),
        )

    @staticmethod
    def _resolve_compression_model(explicit: str | None) -> str | None:
        """解析压缩模型：插件配置优先，回退到 llm.yaml defaults.compression。

        Args:
            explicit: 插件配置中显式指定的 compression_model（可能为空）

        Returns:
            最终使用的模型 ID；若都为空则返回 None（运行时用主模型）
        """
        if explicit:
            return explicit
        try:
            from config.models import get_model_config_loader

            loader = get_model_config_loader()
            defaults = loader._load_llm_data().get("defaults", {})
            default_id = defaults.get("compression", "")
            if default_id:
                return default_id
        except Exception:
            pass
        return None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_window_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 prompt_build 之前执行。"""
        return self._config.get("priority", 5)

    # ------------------------------------------------------------------
    # Token 估算（统一算法：len//2）
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_msg_tokens(msg: dict[str, Any]) -> int:
        """估算单条消息的 token 数（简化版：字符数 // 2）。"""
        content = str(msg.get("content", ""))
        tokens = max(1, len(content) // 2) if content else 0
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            if args:
                tokens += max(1, len(args) // 2)
        return tokens

    def _estimate_effective_tokens(
        self, messages: list[dict[str, Any]], ctx: PluginContext,
    ) -> int:
        """估算有效上下文大小。

        三级估算策略：
        1. prev_input + delta：用上一轮 LLM 真实 input_tokens + 新增消息增量
        2. 压缩块拼接估算：L1 块 tokens + recent 消息 tokens（重启/llm_usage 丢失时）
        3. 全量字符估算：最后手段
        """
        llm_usage = ctx.state.get("llm_usage", {})
        prev_input = llm_usage.get("input_tokens", 0)

        # llm_usage 可能为空（空响应/截断），从历史累计回退
        if prev_input == 0:
            track_usage = ctx.state.get("track.llm_usage", {})
            prev_input = track_usage.get("input_tokens", 0)
            if prev_input > 0:
                logger.info(
                    "[%s] 估算: llm_usage 为空，从 track 回退: prev_input=%d",
                    self.name, prev_input,
                )

        # 策略 1：prev_input + delta
        if prev_input > 0:
            tracked = ctx.state.get("_tracked_msg_count", 0)
            current_non_sys = sum(1 for m in messages if m.get("role") != "system")

            if current_non_sys <= tracked:
                logger.info(
                    "[%s] 估算(无增量): %d tokens (prev_input=%d, tracked=%d, current=%d)",
                    self.name, prev_input, prev_input, tracked, current_non_sys,
                )
                return prev_input

            non_sys_msgs = [m for m in messages if m.get("role") != "system"]
            delta_msgs = non_sys_msgs[tracked:]
            delta_tokens = sum(self._estimate_msg_tokens(m) for m in delta_msgs)

            effective = prev_input + delta_tokens
            logger.info(
                "[%s] 估算(增量): %d tokens (prev_input=%d + delta=%d, tracked=%d, current=%d)",
                self.name, effective, prev_input, delta_tokens, tracked, current_non_sys,
            )
            return effective

        # 策略 2：压缩块拼接估算
        assembled = self._estimate_assembled_tokens(ctx, messages)
        if assembled >= 0:
            logger.info(
                "[%s] 估算(压缩块拼接): %d tokens, msg_count=%d",
                self.name, assembled, len(messages),
            )
            return assembled

        # 策略 3：全量字符估算（最后手段）
        estimated = sum(self._estimate_msg_tokens(m) for m in messages)
        logger.info(
            "[%s] 估算(全量字符): %d tokens, msg_count=%d",
            self.name, estimated, len(messages),
        )
        return estimated

    def _estimate_assembled_tokens(
        self, ctx: PluginContext, messages: list[dict[str, Any]],
    ) -> int:
        """用已有的压缩块 + recent 消息估算实际发送给 LLM 的 token 数。

        模拟 prompt_build 的拼接逻辑：
        system 消息 + L1 压缩块 + STATE_SNAPSHOT + recent 消息

        Returns:
            估算 token 数，无法估算时返回 -1
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_id:
            return -1

        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return -1

        try:
            l1_chunks = chunk_service.find_by_pipeline_sync(pipeline_id, "L1")
        except Exception:
            return -1

        if not l1_chunks:
            return -1

        # L1 压缩块 token 估算
        l1_tokens = sum(max(1, len(c.content) // 2) for c in l1_chunks)

        # STATE_SNAPSHOT token 估算
        snapshot_tokens = 0
        try:
            snapshots = chunk_service.find_by_pipeline_sync(pipeline_id, "STATE_SNAPSHOT")
            if snapshots:
                snapshot_tokens = max(1, len(snapshots[0].content) // 2)
        except Exception:
            pass

        # system 消息 + recent 消息（非压缩块的）
        system_tokens = sum(
            self._estimate_msg_tokens(m) for m in messages
            if m.get("role") == "system"
        )

        # recent 消息：从最大 L1 块的 sequence_end 之后开始
        max_end = max((c.sequence_end for c in l1_chunks if c.sequence_end), default=0)
        recent_tokens = 0
        non_sys_count = 0
        for m in messages:
            if m.get("role") != "system":
                non_sys_count += 1
                if non_sys_count > max_end:
                    recent_tokens += self._estimate_msg_tokens(m)

        total = l1_tokens + snapshot_tokens + system_tokens + recent_tokens
        logger.debug(
            "[%s] 压缩块拼接估算: l1=%d (blocks=%d), snapshot=%d, system=%d, recent=%d (after=%d), total=%d",
            self.name, l1_tokens, len(l1_chunks), snapshot_tokens, system_tokens, recent_tokens, max_end, total,
        )
        return total

    _warned_no_context_window = False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """检查上下文大小并在超阈值时触发记忆系统压缩。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含压缩后 messages 的插件执行结果
        """
        context_window = ctx.state.get("context_window")
        if not context_window:
            if not self._warned_no_context_window:
                self._warned_no_context_window = True
                logger.error(
                    "[%s] context_window 未设置，上下文守卫无法工作！"
                    " 请检查模型配置（llm.yaml）是否包含 context_window，"
                    "以及 core_plugins 是否正确合并了模型配置。",
                    self.name,
                )
            return PluginResult()

        messages = ctx.state.get("messages", [])
        if not messages:
            return PluginResult()

        # 获取 service
        service = self._get_memory_service(ctx)
        if not service:
            return PluginResult()

        # 注入外部依赖到 service
        self._setup_service(ctx, service, context_window)

        # 窗口变更检测
        cleaned = await service.clean_if_window_changed(messages, context_window)
        if cleaned is not None:
            messages = cleaned

        # 阈值检查
        estimated_tokens = self._estimate_effective_tokens(messages, ctx)
        trigger_tokens = int(context_window * self._trigger_ratio)
        logger.info(
            "[%s] 阈值检查: estimated=%d, trigger=%d, context_window=%d, "
            "ratio=%.2f, msg_count=%d, service=%s",
            self.name, estimated_tokens, trigger_tokens, context_window,
            self._trigger_ratio, len(messages), type(service).__name__,
        )
        if estimated_tokens < trigger_tokens:
            if cleaned is not None:
                return PluginResult(state_updates={"messages": messages})
            return PluginResult()

        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f, msg_count=%d",
            self.name, estimated_tokens, trigger_tokens,
            context_window, self._trigger_ratio, len(messages),
        )

        # 前端压缩进度通知
        _on_chunk = ctx.state.get("on_chunk")
        if _on_chunk:
            try:
                _on_chunk({
                    "type": "compression_start",
                    "pipeline_id": ctx.state.get("pipeline_id", ""),
                })
            except Exception:
                pass

        # 调用压缩
        logger.info("[%s] 开始调用 compress_messages ...", self.name)
        try:
            compressed = await service.compress_messages(
                messages=messages,
                context_window=context_window,
                trigger_ratio=self._trigger_ratio,
            )
        except Exception as exc:
            logger.error(
                "[%s] compress_messages 异常: %s | service=%s",
                self.name, exc, type(service).__name__,
                exc_info=True,
            )
            # 压缩异常 → 终止管线
            ctx.state[StateKeys.ENDED] = True
            return PluginResult(
                state_updates={StateKeys.ENDED: True, "input_route_target": "end"},
                skip_remaining=True,
            )

        if compressed and len(compressed) < len(messages):
            logger.info(
                "[%s] 压缩完成: %d -> %d 条消息",
                self.name, len(messages), len(compressed),
            )
            ctx.state["_tracked_msg_count"] = sum(
                1 for m in compressed if m.get("role") != "system"
            )
            return PluginResult(state_updates={"messages": compressed})

        # 压缩返回 None（失败）或未减少消息数 → 终止管线
        logger.error(
            "[%s] 上下文压缩失败: estimated=%d 超过 trigger=%d 但压缩未能减少消息"
            " (compressed=%s, original=%d)",
            self.name, estimated_tokens, trigger_tokens,
            f"{len(compressed)}条" if compressed else "None",
            len(messages),
        )
        ctx.state[StateKeys.ENDED] = True
        return PluginResult(
            state_updates={StateKeys.ENDED: True, "input_route_target": "end"},
            skip_remaining=True,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _get_memory_service(ctx: PluginContext):
        """获取 MemoryContextService 实例。"""
        try:
            return ctx.get_service("context_service")
        except (KeyError, AttributeError):
            pass

        from memory.memory_context_service import MemoryContextService

        try:
            context_window = ctx.state.get("context_window", 128000)
            return MemoryContextService(
                config={"context_window": context_window, "compress_trigger_ratio": 0.5},
            )
        except Exception:
            return None

    def _setup_service(self, ctx: PluginContext, service, context_window: int) -> None:
        """将外部依赖注入到 service。"""
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        session_id = ctx.state.get("context.session_id", "")
        user_id = ctx.state.get("user_id", "")
        model_name = ctx.state.get("model_name", "")

        # 获取可选服务
        chunk_service = None
        memory_service = None
        llm_core = None
        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            pass
        try:
            memory_service = ctx.get_service("memory_service")
        except (KeyError, AttributeError):
            pass
        try:
            llm_core = ctx.get_service("llm_core")
        except (KeyError, AttributeError):
            pass

        try:
            service.setup(
                chunk_service=chunk_service,
                memory_service=memory_service,
                llm_core=llm_core,
                pipeline_id=pipeline_id,
                session_id=session_id,
                context_window=context_window,
                user_id=user_id,
                compression_model_id=self._compression_model,
                model_name=model_name,
            )
            logger.info(
                "[%s] setup 完成: chunk_service=%s, memory_service=%s, llm_core=%s, "
                "compression_model=%s, pipeline_id=%s",
                self.name,
                type(chunk_service).__name__ if chunk_service else "无",
                type(memory_service).__name__ if memory_service else "无",
                type(llm_core).__name__ if llm_core else "无",
                self._compression_model,
                pipeline_id[:8] if pipeline_id else "无",
            )
        except Exception as exc:
            logger.error("[%s] setup 异常: %s", self.name, exc, exc_info=True)
