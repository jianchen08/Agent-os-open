"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时委托给
MemoryContextService.compress_messages 进行预算驱动的分层压缩。

本插件只负责：检查阈值 → 注入依赖 → 调用压缩 → 更新 state。

State 命名空间:
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import contextlib
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

        配置优先级（高→低）：
          ① Agent YAML plugins.enabled.context_window_guard.trigger_ratio
             （由 plugin_resolver 合并进 config，或由 _apply_runtime_config 从 ctx.state 读）
          ② Pipeline YAML plugins.context_window_guard.config.trigger_ratio
             （即本 __init__ 收到的 config 参数）
          ③ System YAML config/system/context_window_config.yaml 的 compress_trigger_ratio
          ④ 代码硬编码默认 0.55

        Args:
            config: 插件配置字典（来自 pipeline yaml），支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（不配则继承 system yaml）
                - compression_model: 压缩专用模型 ID（如 minimax-m3），
                  为空时回退到 llm.yaml 的 defaults.compression，再为空则用主模型
        """
        self._config = config or {}
        self._trigger_ratio = self._resolve_trigger_ratio(self._config.get("trigger_ratio"))
        self._compression_model: str | None = self._resolve_compression_model(
            self._config.get("compression_model"),
        )
        # 实例级追踪：插件可能被重复实例化，state 不一定跨迭代持久化
        # 用实例变量做主存储，ctx.state 做辅助（重启恢复场景）
        self._tracked_msg_count: int = 0

    @staticmethod
    def _resolve_trigger_ratio(explicit: float | None) -> float:
        """解析 trigger_ratio：pipeline 显式值 → system yaml → 代码默认。

        三层覆盖链路中 ②→③ 的衔接：当 pipeline yaml 没配 trigger_ratio 时，
        从 system 的 context_window_config.yaml 继承 compress_trigger_ratio。

        Args:
            explicit: pipeline yaml 显式配置的 trigger_ratio（可能为 None）

        Returns:
            最终生效的 trigger_ratio
        """
        # ② Pipeline 显式配置优先
        if explicit is not None:
            return explicit

        # ③ System YAML fallback
        try:
            from memory.context_compressor import CompressionConfig  # noqa: PLC0415

            sys_config = CompressionConfig.from_yaml_config(context_window=128000)
            return sys_config.compress_trigger_ratio
        except Exception:
            pass

        # ④ 代码默认（见 config.defaults.COMPRESS_TRIGGER_RATIO）
        return 0.55

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
            from config.models import get_model_config_loader  # noqa: PLC0415

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
    # Agent 级运行时配置覆盖
    # ------------------------------------------------------------------

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数。

        三层覆盖链路（高优先级覆盖低优先级）：
          ① Agent YAML (plugins.enabled.context_window_guard.{key})
          ② Pipeline YAML (plugins.context_window_guard.config.{key})
          ③ 代码默认值

        Agent 覆盖通过两条路径生效：
        - 路径 A：plugin_resolver.apply_agent_plugin_configs() 已用合并后的
          config 重新构造本插件实例（_config 已含 agent override），构造时
          _trigger_ratio 已正确。此方法处理路径 B。
        - 路径 B：ctx.state 中可能携带 agent 注入的运行时覆盖（与 stop_check
          等插件从 ctx.state 读 max_iterations 同一机制）。

        本方法读 ctx.state 里的 context_guard.trigger_ratio（如有）覆盖 _trigger_ratio。

        Args:
            ctx: 插件执行上下文
        """
        state_ratio = ctx.state.get("context_guard.trigger_ratio")
        if state_ratio is not None:
            self._trigger_ratio = state_ratio

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

    async def _estimate_effective_tokens(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
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
                logger.debug(
                    "[%s] 估算: llm_usage 为空，从 track 回退: prev_input=%d",
                    self.name,
                    prev_input,
                )

        # 策略 1：prev_input + delta（仅同进程连续迭代有效）
        # 重启后 tracked 归零、messages 从存储全量恢复，prev_input（上一进程某轮
        # 的值）与当前 messages 不匹配，增量假设会双重计算 → 跳过让策略 2 接管
        tracked = max(self._tracked_msg_count, ctx.state.get("_tracked_msg_count", 0))
        current_non_sys = sum(1 for m in messages if m.get("role") != "system")
        restart_signature = tracked == 0 and current_non_sys > 50
        logger.debug(
            "[%s] 估算分叉: prev_input=%d, tracked=%d, current_non_sys=%d, restart_signature=%s, msg_total=%d",
            self.name,
            prev_input,
            tracked,
            current_non_sys,
            restart_signature,
            len(messages),
        )
        if prev_input > 0 and not restart_signature:
            if current_non_sys <= tracked:
                logger.debug(
                    "[%s] 估算(无增量): %d tokens (prev_input=%d, tracked=%d, current=%d)",
                    self.name,
                    prev_input,
                    prev_input,
                    tracked,
                    current_non_sys,
                )
                return prev_input

            non_sys_msgs = [m for m in messages if m.get("role") != "system"]
            delta_msgs = non_sys_msgs[tracked:]
            delta_tokens = sum(self._estimate_msg_tokens(m) for m in delta_msgs)

            effective = prev_input + delta_tokens
            logger.debug(
                "[%s] 估算(增量): %d tokens (prev_input=%d + delta=%d, tracked=%d, current=%d, delta_count=%d)",
                self.name,
                effective,
                prev_input,
                delta_tokens,
                tracked,
                current_non_sys,
                len(delta_msgs),
            )
            return effective

        # 策略 2：压缩块拼接估算
        assembled = await self._estimate_assembled_tokens(ctx, messages)
        logger.debug(
            "[%s] 估算(策略2/压缩块拼接): assembled=%d, msg_count=%d",
            self.name,
            assembled,
            len(messages),
        )
        if assembled >= 0:
            return assembled

        # 策略 3：全量字符估算（最后手段）
        estimated = sum(self._estimate_msg_tokens(m) for m in messages)
        logger.warning(  # 落到策略3说明前两个都失败了，值得告警
            "[%s] 估算(策略3/全量字符 兜底): estimated=%d, msg_count=%d, prev_input=%d, tracked=%d",
            self.name,
            estimated,
            len(messages),
            prev_input,
            tracked,
        )
        return estimated

    async def _estimate_assembled_tokens(
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
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
            l1_chunks = await chunk_service.find_by_pipeline(pipeline_id, "L1")
        except Exception:
            return -1

        if not l1_chunks:
            return -1

        # L1 压缩块 token 估算
        l1_tokens = sum(max(1, len(c.content) // 2) for c in l1_chunks)

        # STATE_SNAPSHOT token 估算
        snapshot_tokens = 0
        try:
            snapshots = await chunk_service.find_by_pipeline(pipeline_id, "STATE_SNAPSHOT")
            if snapshots:
                snapshot_tokens = max(1, len(snapshots[0].content) // 2)
        except Exception:
            pass

        # system 消息 + recent 消息（非压缩块的）
        system_tokens = sum(self._estimate_msg_tokens(m) for m in messages if m.get("role") == "system")

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
            self.name,
            l1_tokens,
            len(l1_chunks),
            snapshot_tokens,
            system_tokens,
            recent_tokens,
            max_end,
            total,
        )
        return total

    _warned_no_context_window = False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def execute(self, ctx: PluginContext) -> PluginResult:  # noqa: PLR0911
        """检查上下文大小并在超阈值时触发记忆系统压缩。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含压缩后 messages 的插件执行结果
        """
        # Agent 级覆盖：从 ctx.state 读 agent YAML 中 plugins.enabled.context_window_guard 的配置
        self._apply_runtime_config(ctx)

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

        # 重启场景裁剪：从存储全量恢复后，已被压缩块覆盖的旧消息需先剔除。
        # 必须在阈值估算之前做——否则策略 1/3 会把覆盖区的消息全算进去导致
        # 误触发压缩，且压缩分支拿到的 messages 还包含已被压缩过的旧消息。
        # 仅在重启特征（消息数远超上次追踪值）下裁剪，正常迭代不裁。
        trimmed = False
        if len(messages) > self._tracked_msg_count + 50:
            new_messages = await self._trim_covered_messages(ctx, messages)
            trimmed = new_messages is not messages
            if trimmed:
                messages = new_messages

        # 阈值检查
        estimated_tokens = await self._estimate_effective_tokens(messages, ctx)
        trigger_tokens = int(context_window * self._trigger_ratio)
        logger.debug(
            "[%s] 阈值检查: estimated=%d, trigger=%d, context_window=%d, ratio=%.2f, msg_count=%d, service=%s",
            self.name,
            estimated_tokens,
            trigger_tokens,
            context_window,
            self._trigger_ratio,
            len(messages),
            type(service).__name__,
        )
        if estimated_tokens < trigger_tokens:
            # 不压缩，仅更新追踪计数；messages 已在上方裁剪过
            current_non_sys = sum(1 for m in messages if m.get("role") != "system")
            self._tracked_msg_count = current_non_sys
            updates: dict[str, Any] = {"_tracked_msg_count": current_non_sys}
            if trimmed or cleaned is not None:
                updates["messages"] = messages
            return PluginResult(state_updates=updates)

        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f, msg_count=%d",
            self.name,
            estimated_tokens,
            trigger_tokens,
            context_window,
            self._trigger_ratio,
            len(messages),
        )

        # 前端压缩进度通知
        _on_chunk = ctx.state.get("on_chunk")
        if _on_chunk:
            with contextlib.suppress(Exception):
                _on_chunk(
                    {
                        "type": "compression_start",
                        "pipeline_id": ctx.state.get("pipeline_id", ""),
                    }
                )

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
                self.name,
                exc,
                type(service).__name__,
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
                self.name,
                len(messages),
                len(compressed),
            )
            # 压缩只搬运消息不格式化，会原样保留历史段里的 raw 格式 tool_calls
            # （缺 type / 扁平结构，来自执行记录/state 恢复的脏数据）。
            # 写回 state 前强制标准化，否则后续发上游会报"工具类型不能为空"
            # / "messages 参数非法"（实测 glm/zhipu 必 400）。
            # normalizer 的 _normalize_tool_calls_in_messages 是纯函数全量修复，
            # 同时同步配对的 tool result，不破坏配对。
            self._standardize_tool_calls(compressed)
            post_compress_count = sum(1 for m in compressed if m.get("role") != "system")
            self._tracked_msg_count = post_compress_count
            ctx.state["_tracked_msg_count"] = post_compress_count
            return PluginResult(
                state_updates={
                    "messages": compressed,
                    "_tracked_msg_count": post_compress_count,
                }
            )

        # 压缩返回 None（失败）或未减少消息数 → 终止管线
        logger.error(
            "[%s] 上下文压缩失败: estimated=%d 超过 trigger=%d 但压缩未能减少消息 (compressed=%s, original=%d)",
            self.name,
            estimated_tokens,
            trigger_tokens,
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

    def _standardize_tool_calls(self, messages: list[dict[str, Any]]) -> None:
        """压缩写回前把 tool_calls 标准化为 OpenAI API 格式。

        委托给 normalizer 的公共入口 standardize_tool_calls_in_messages
        （纯函数全量修复，同步配对的 tool result）。延迟 import 避免
        input 插件模块加载期耦合 core 插件模块。
        """
        try:
            from plugins.core.llm_core._message_normalizer import (  # noqa: PLC0415
                standardize_tool_calls_in_messages,
            )

            standardize_tool_calls_in_messages(messages)
        except Exception as exc:
            logger.warning(
                "[%s] tool_calls 标准化失败（不阻塞写回）: %s",
                self.name,
                exc,
            )

    async def _trim_covered_messages(  # noqa: PLR0911
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """裁剪被已有压缩块覆盖的旧消息（重启场景）。

        重启后从存储加载了全部历史消息，但压缩块已覆盖了前面的部分。
        如果不裁剪，prompt_build 会把压缩块 + 全部原始消息都发给 LLM，
        导致 tool_calls/tool_response 配对被破坏。

        裁剪逻辑：保留 system 消息 + 非系统消息中序号 > max_end 的部分。
        max_end 取自压缩块的 sequence_end（已修正为实际消息序号）。

        Args:
            ctx: 插件执行上下文
            messages: 当前消息列表

        Returns:
            裁剪后的消息列表（如果没有压缩块则原样返回）
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_id:
            return messages

        try:
            chunk_service = ctx.get_service("chunk_service")
        except (KeyError, AttributeError):
            return messages

        try:
            l1_chunks = await chunk_service.find_by_pipeline(pipeline_id, "L1")
        except Exception:
            return messages

        if not l1_chunks:
            return messages

        max_end = max((c.sequence_end for c in l1_chunks if c.sequence_end), default=0)
        if max_end <= 0:
            return messages

        # 裁剪：保留 system 消息 + 序号 > max_end 的非 system 消息
        non_sys_count = sum(1 for m in messages if m.get("role") != "system")
        if non_sys_count <= max_end:
            # 所有非 system 消息都被压缩块覆盖，只保留 system 消息
            trimmed = [m for m in messages if m.get("role") == "system"]
        else:
            # 保留 system 消息 + 最后 (non_sys_count - max_end) 条非 system 消息
            keep_recent = non_sys_count - max_end
            trimmed = []
            recent_seen = 0
            # 从尾部向前数，保留最后 keep_recent 条非 system 消息
            recent_part: list[dict[str, Any]] = []
            for m in reversed(messages):
                if m.get("role") != "system":
                    recent_seen += 1
                    if recent_seen <= keep_recent:
                        recent_part.append(m)
                else:
                    recent_part.append(m)
            # recent_part 是倒序的，需要再反转
            # 但 system 消息也混在里面了，需要重新分离
            trimmed_sys = [m for m in messages if m.get("role") == "system"]
            trimmed_recent = list(reversed([m for m in recent_part if m.get("role") != "system"]))
            trimmed = trimmed_sys + trimmed_recent

        if len(trimmed) < len(messages):
            logger.info(
                "[%s] 裁剪被压缩块覆盖的旧消息: %d -> %d (max_end=%d)",
                self.name,
                len(messages),
                len(trimmed),
                max_end,
            )
            return trimmed

        return messages

    @staticmethod
    def _get_memory_service(ctx: PluginContext):
        """获取 MemoryContextService 实例。"""
        try:
            return ctx.get_service("context_service")
        except (KeyError, AttributeError):
            pass

        from memory.memory_context_service import MemoryContextService  # noqa: PLC0415

        try:
            context_window = ctx.state.get("context_window", 128000)
            return MemoryContextService(
                config={
                    "context_window": context_window,
                    "compress_trigger_ratio": 0.55,
                },  # 见 config.defaults.COMPRESS_TRIGGER_RATIO
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
        with contextlib.suppress(KeyError, AttributeError):
            chunk_service = ctx.get_service("chunk_service")
        with contextlib.suppress(KeyError, AttributeError):
            memory_service = ctx.get_service("memory_service")
        with contextlib.suppress(KeyError, AttributeError):
            llm_core = ctx.get_service("llm_core")

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
            logger.debug(
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
