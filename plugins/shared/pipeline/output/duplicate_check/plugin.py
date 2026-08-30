"""重复检查 Output 插件 — 合并 duplicate_call + repetitive_output。

负责在管道循环的输出阶段检测工具调用重复和输出内容重复，
采用三级渐进策略：软提示 → 拦截重路由 → 终止管道。

合并收益：共享重复计数状态（router.duplicate_count / router.repetitive_count）+ 低维护成本。

策略说明（终止/回路由经控制状态键表达，引擎与 DSL 消费）：
    - 第一级（count < max）：注入软提示，工具调用仍执行
    - 第二级（count >= max）：移除重复调用 + 注入强警告 + 写
      router.duplicate_back_llm=true（DSL 路由回 LLM）
    - 第三级（拦截次数 >= hard_limit）：should_stop=true 终止管道
      - 主 agent：注入用户通知消息后终止
      - 子 agent：直接终止（署名 duplicate_loop，终态映射 Failed）

State 命名空间：
    - router.duplicate_count : 工具调用重复计数（窗口内同签名的多余出现次数最大值）
    - router.recent_tool_sigs : 最近 signature_window_size 条单调用签名（跨迭代滑动窗口）
    - router.repetitive_count : 输出内容重复计数（跨迭代）
    - router.duplicate_intercepts : 拦截总次数
    - router.last_response : 上一次 LLM 响应摘要

消息修改一律经 state_updates["messages"]={"_ops":[...]} 回传——引擎 merge 只认
slot ops（set 缺 seq=append / set(seq,msg)=modify / set(seq,null)=delete），
直接改 ctx.state 的修改过不了 server 适配层的新 dict，真实链路上必丢。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

_HINT_TEMPLATES = {
    1: "你已经连续 {count} 次使用 {tool} 执行相同操作，结果不会有变化。请考虑换一种方式完成任务。",
    2: "你仍然在重复调用 {tool}，这已经是第 {count} 次了。请立即停止使用该工具和参数，尝试完全不同的方法。",
}

_MAIN_AGENT_TERMINATE_MSG = (
    "抱歉，我在执行过程中陷入了重复调用同一工具的死循环，无法继续完成当前任务。"
    "请提供更多指示或调整任务要求，我将重新尝试。"
)


class DuplicateCheckPlugin(IOutputPlugin):
    """重复检查 Output 插件。

    合并了旧代码中 duplicate_call 和 repetitive_output 两个策略。
    两者都维护重复计数器，合并后共享 router.duplicate_count 命名空间。

    检查维度：
    1. 工具调用重复：单调用签名（工具+参数）在滑动窗口内的多余出现——
       整组只比上一轮的旧算法对多工具场景恒不计数（LLM 每轮微调一个调用
       或交替两个调用即可绕过），已退役
    2. 输出内容重复：LLM 连续返回相同或高度相似的内容

    三级渐进策略：
    - 第一级：注入软提示（工具调用仍执行）
    - 第二级：移除重复调用 + 强警告 + 路由回 LLM
    - 第三级：终止管道（主 agent 通知用户，子 agent 直接终止）

    优先级：4（系统级）
    重复检测异常必须终止管道。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化重复检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_duplicate_calls: 工具调用重复拦截阈值（默认 3）
                - max_repetitive_output: 输出内容重复拦截阈值（默认 3）
                - hard_limit_intercepts: 拦截次数硬上限，达到后终止管道（默认 4）
                - similarity_threshold: 输出相似度阈值（默认 0.9）
                - signature_window_size: 单调用签名滑动窗口条数（默认 16，
                  覆盖 4 工具轮转时第 4 次出现的拦截）
        """
        self._config = config or {}
        self._max_duplicate_calls = self._config.get("max_duplicate_calls", 3)
        self._max_repetitive_output = self._config.get("max_repetitive_output", 3)
        self._hard_limit_intercepts = self._config.get("hard_limit_intercepts", 4)
        self._similarity_threshold = self._config.get("similarity_threshold", 0.9)
        self._signature_window_size = self._config.get("signature_window_size", 16)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "duplicate_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 4)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["next_llm", "end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行重复检查。

        采用三级渐进策略处理重复。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含重复检查结果和路由信号的输出结果
        """
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行重复检查逻辑。

        豁免门：
        - 管道已结束（ENDED）不判定——post-end 阶段不应判重复；
        - 仅 llm_call 轮次判定输出重复——工具结果文本不是 LLM 输出，误判会
          在 tool 消息后追加提示打断 assistant(tool_calls)→tool 序列；
        - 含评估结论 JSON 的输出不判重复（即使文本相似）。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典
        """
        # 管道已结束时跳过：post-end 阶段不应判定重复
        if ctx.state.get(StateKeys.ENDED, False):
            return {}

        # 仅在 llm_call 阶段判定输出重复：工具结果不是 LLM 输出，不参与重复判定。
        # 若在 tool_execute 阶段触发，工具结果文本会被误判为"输出重复"，
        # 并在 tool 消息后追加提示，打断 assistant(tool_calls)→tool 序列。
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        if core_type != "llm_call":
            return {}

        updates: dict[str, Any] = {}

        # 1. 工具调用重复检查
        dup_result = self._check_duplicate_calls(ctx)
        updates.update(dup_result)

        # 2. 输出内容重复检查
        rep_result = self._check_repetitive_output(ctx)
        updates.update(rep_result)

        # 3. 综合判断——两个检查对本轮计数恒有产出（无输入轮显式清零），
        #    绝不回退 ctx.state 的历史值：level-1 软提示不清零，陈旧计数会让
        #    后续无调用轮次误判重复（提示文本工具名还取空串）
        duplicate_count = updates.get("router.duplicate_count", 0)
        repetitive_count = updates.get("router.repetitive_count", 0)

        # 3a. 工具调用重复处理
        if duplicate_count > 0:
            return self._handle_duplicate_tool_calls(ctx, updates, duplicate_count)

        # 3b. 输出内容重复处理
        if repetitive_count > 0:
            return self._handle_repetitive_output(ctx, updates, repetitive_count)

        return updates

    def _handle_duplicate_tool_calls(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        count: int,
    ) -> dict[str, Any]:
        """处理工具调用重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        tool_desc = self._build_tool_call_description(ctx)
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, tool_desc, intercepts)

        # 第二级：重复达到阈值 → 拦截 + 路由回 LLM
        if count >= self._max_duplicate_calls:
            warning = f"检测到重复工具调用{tool_desc}，已跳过执行。请不要再次使用相同的工具和参数，请尝试其他方法。"
            messages = list(ctx.state.get("messages", []))
            strip_ops, stripped = self._build_strip_ops(messages)
            self._apply_strip_locally(messages, strip_ops)
            merge_ops = self._build_merge_ops(messages, f"[DuplicateCheck] {warning}")
            updates[StateKeys.RAW_TOOL_CALLS] = []
            updates["router.duplicate_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Duplicate tool calls intercepted | count=%d intercepts=%d tool=%s stripped_assistants=%d",
                self.name,
                count,
                intercepts + 1,
                tool_desc,
                stripped,
            )
            # 二级路由回 LLM 经状态键表达（DSL 路由 router.duplicate_back_llm 消费）
            updates["router.duplicate_back_llm"] = True
            updates["messages"] = {"_ops": strip_ops + merge_ops}
            return updates

        # 第一级：早期重复 → 注入软提示，工具调用仍执行
        hint = self._build_hint(count, tool_desc)
        self._inject_hint(ctx, updates, hint)
        logger.info(
            "[%s] Duplicate tool call soft hint | count=%d tool=%s",
            self.name,
            count,
            tool_desc,
        )
        return updates

    def _handle_repetitive_output(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        count: int,
    ) -> dict[str, Any]:
        """处理输出内容重复，三级渐进策略。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            count: 当前重复计数

        Returns:
            更新后的状态字典
        """
        intercepts = ctx.state.get("router.duplicate_intercepts", 0)

        # 第三级：拦截次数达到硬上限 → 终止管道
        if intercepts >= self._hard_limit_intercepts:
            return self._terminate_pipeline(ctx, updates, "重复输出", intercepts)

        # 第二级：重复达到阈值 → 清空输出 + 路由回 LLM
        if count >= self._max_repetitive_output:
            warning = "检测到重复输出相似内容，请尝试其他方法或给出不同的回复。"
            self._inject_warning(ctx, updates, warning)
            updates[StateKeys.RAW_RESULT] = ""
            updates["router.repetitive_count"] = 0
            updates["router.duplicate_intercepts"] = intercepts + 1
            logger.info(
                "[%s] Repetitive output intercepted | count=%d intercepts=%d",
                self.name,
                count,
                intercepts + 1,
            )
            updates["router.duplicate_back_llm"] = True
            return updates

        # 第一级：早期重复 → 注入软提示
        hint = f"你已经连续 {count} 次输出相似内容，请尝试换一种方式回复。"
        self._inject_hint(ctx, updates, hint)
        logger.info(
            "[%s] Repetitive output soft hint | count=%d",
            self.name,
            count,
        )
        return updates

    def _terminate_pipeline(
        self,
        ctx: PluginContext,
        updates: dict[str, Any],
        desc: str,
        intercepts: int,
    ) -> dict[str, Any]:
        """终止管道，主 agent 注入用户通知，子 agent 直接终止。

        Args:
            ctx: 插件执行上下文
            updates: 已有的状态更新字典
            desc: 重复描述
            intercepts: 当前拦截次数

        Returns:
            包含终止路由信号的更新字典
        """
        agent_level = ctx.state.get(StateKeys.AGENT_LEVEL, "L1")
        is_main = agent_level in ("L1", "L1_MAIN") or ctx.state.get("delegate_depth", 0) == 0

        if is_main:
            # 终止通知是给用户的自然语言收尾，append assistant 消息（不并入末尾消息）
            updates["messages"] = {
                "_ops": [{"op": "set", "msg": {"role": "assistant", "content": _MAIN_AGENT_TERMINATE_MSG}}]
            }
            logger.warning(
                "[%s] Pipeline terminating (main agent) | intercepts=%d desc=%s",
                self.name,
                intercepts,
                desc,
            )
        else:
            logger.warning(
                "[%s] Pipeline terminating (sub agent) | intercepts=%d desc=%s",
                self.name,
                intercepts,
                desc,
            )

        updates[StateKeys.SHOULD_STOP] = True
        updates["router.stop_reason"] = "duplicate_loop"
        return updates

    def _build_hint(self, count: int, tool_desc: str) -> str:
        """构建早期软提示消息。

        Args:
            count: 当前重复计数
            tool_desc: 工具调用描述

        Returns:
            软提示消息字符串
        """
        template = _HINT_TEMPLATES.get(count, _HINT_TEMPLATES[max(_HINT_TEMPLATES)])
        # 模板来自 _HINT_TEMPLATES 配置常量的运行时渲染，非静态拼接（f-string 不适用）
        return template.format(count=count, tool=tool_desc)

    def _build_tool_call_description(self, ctx: PluginContext) -> str:
        """构建工具调用的可读描述。

        Args:
            ctx: 插件执行上下文

        Returns:
            工具调用描述字符串，如 "file_read(path=xxx)"
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return ""
        parts = []
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            # raw_tool_calls 生产方（llm adapter _parse/_normalize_tool_calls）固定
            # 输出 {"id","name","arguments"}。
            args = tc.get("arguments", {})
            if isinstance(args, str):
                parts.append(f"{name}({args})")
            else:
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                parts.append(f"{name}({args_str})")
        return "、".join(parts)

    def _inject_warning(self, ctx: PluginContext, updates: dict[str, Any], message: str) -> None:
        """注入强警告（输出重复二级拦截时使用），merge ops 写入 updates。"""
        updates["messages"] = {
            "_ops": self._build_merge_ops(list(ctx.state.get("messages", [])), f"[DuplicateCheck] {message}")
        }

    def _inject_hint(self, ctx: PluginContext, updates: dict[str, Any], message: str) -> None:
        """注入软提示（第一级早期提示时使用），merge ops 写入 updates。"""
        updates["messages"] = {
            "_ops": self._build_merge_ops(list(ctx.state.get("messages", [])), f"[DuplicateCheck] {message}")
        }

    def _build_strip_ops(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """构造移除末尾连续 assistant(tool_calls) 的 delete ops。

        Level-2 拦截会清空 RAW_TOOL_CALLS，但 llm_core 已 append 的
        assistant(tool_calls) 仍残留 → 永远等不到 tool result → 未配对消息。
        因此拦截时同步产出删除 ops 撤销本次工具调用意图。

        Args:
            messages: 当前 messages 数组（元素带引擎分配的 seq）

        Returns:
            (delete ops 列表, 被剥离的 assistant 消息数量)
        """
        ops: list[dict[str, Any]] = []
        stripped = 0
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                seq = msg.get("seq")
                if seq is None:
                    break  # 无 seq 不可定位（真实链路消息恒带引擎 seq）
                ops.append({"op": "set", "seq": seq, "msg": None})
                stripped += 1
                continue
            break
        ops.reverse()
        return ops, stripped

    @staticmethod
    def _apply_strip_locally(messages: list[dict[str, Any]], strip_ops: list[dict[str, Any]]) -> None:
        """把 delete ops 应用到本地 messages 副本（后续 merge op 的定位基准）。"""
        removed = {op["seq"] for op in strip_ops}
        messages[:] = [m for m in messages if m.get("seq") not in removed]

    def _build_merge_ops(self, messages: list[dict[str, Any]], content: str) -> list[dict[str, Any]]:
        """构造把提醒合并进末尾消息的 slot ops（引擎三落点唯一通道）。

        合并规则：
        - 末尾为 tool/assistant/system 且带 seq → set modify（同 seq 替换，
          content 合并，保持 assistant(tool_calls)→tool 序列完整）
        - 末尾为 user、空数组或无 seq 可定位 → set 缺 seq = append 一条 user
          （append 无配对约束；无 seq 的受保护角色退化为 append 而非静默丢弃）

        Args:
            messages: 当前 messages 数组（元素带引擎分配的 seq）
            content: 要合并/追加的提醒文本

        Returns:
            slot ops 列表
        """
        if not messages:
            return [{"op": "set", "msg": {"role": "user", "content": content}}]

        last = messages[-1]
        seq = last.get("seq")
        if last.get("role") in ("tool", "assistant", "system") and seq is not None:
            merged = dict(last)
            original = merged.get("content") or ""
            merged["content"] = f"{original}\n\n{content}" if original else content
            return [{"op": "set", "seq": seq, "msg": merged}]

        return [{"op": "set", "msg": {"role": "user", "content": content}}]

    def _check_duplicate_calls(self, ctx: PluginContext) -> dict[str, Any]:
        """检查工具调用重复（单调用粒度滑动窗口）。

        对每个调用生成 工具名+参数 签名，与最近 signature_window_size 条
        签名窗口累计比对：重复计数 = 窗口 + 本轮中同一签名的"多余出现次数"
        （相对首次出现）最大值。整组只比上一轮的旧算法对多工具场景恒不
        计数——部分重复（组内一个调用变化即整组重置）与交替循环永不触发。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典；无工具调用轮返回显式清零（计数只表本轮，
            滑动窗口保留——签名未过窗，后续轮重复仍可由窗口重数检出）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"router.duplicate_count": 0}

        current_signatures = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            sig = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()[:8]  # noqa: S324
            current_signatures.append(sig)

        window = list(ctx.state.get("router.recent_tool_sigs", []))
        round_counts: dict[str, int] = {}
        for sig in current_signatures:
            round_counts[sig] = round_counts.get(sig, 0) + 1
        duplicate_count = max(
            (window.count(sig) + n - 1 for sig, n in round_counts.items()),
            default=0,
        )
        if duplicate_count > 0:
            logger.debug(
                "[%s] Duplicate tool call detected | count=%d",
                self.name,
                duplicate_count,
            )

        updated_window = (window + current_signatures)[-(self._signature_window_size) :]
        return {
            "router.duplicate_count": duplicate_count,
            "router.recent_tool_sigs": updated_window,
        }

    def _check_repetitive_output(self, ctx: PluginContext) -> dict[str, Any]:
        """检查输出内容重复。

        通过对 LLM 输出文本的前 N 个字符生成签名，
        与上一次输出对比，高度相似则增加重复计数。

        Args:
            ctx: 插件执行上下文

        Returns:
            重复检查结果字典；无输出轮返回显式清零（上次输出签名保留，
            供下一个有输出轮次对比）
        """
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        if raw_result is None:
            return {"router.repetitive_count": 0}

        # 包含评估结论 JSON 的输出不应被判定为重复（即使文本相似）
        raw_text = str(raw_result)
        if "evaluation_result" in raw_text and '"passed"' in raw_text:
            return {
                "router.last_response": hashlib.md5(raw_text[:500].encode()).hexdigest()[:8],
                "router.last_response_text": raw_text[:500],
                "router.repetitive_count": 0,
            }

        # 生成当前输出签名（取前 500 字符）
        current_text = str(raw_result)[:500]
        current_hash = hashlib.md5(current_text.encode()).hexdigest()[:8]  # noqa: S324

        last_hash = ctx.state.get("router.last_response", "")

        # 对比
        repetitive_count = ctx.state.get("router.repetitive_count", 0)
        if current_hash and current_hash == last_hash:
            repetitive_count += 1
            logger.debug(
                "[%s] Repetitive output detected | count=%d",
                self.name,
                repetitive_count,
            )
        else:
            # 相似度检查（简单字符级对比）
            last_text = ctx.state.get("router.last_response_text", "")
            if last_text and self._compute_similarity(current_text, last_text) > self._similarity_threshold:
                repetitive_count += 1
                logger.debug(
                    "[%s] Similar output detected | similarity>%.2f | count=%d",
                    self.name,
                    self._similarity_threshold,
                    repetitive_count,
                )
            else:
                repetitive_count = 0  # 不同则重置

        return {
            "router.repetitive_count": repetitive_count,
            "router.last_response": current_hash,
            "router.last_response_text": current_text,
        }

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度。

        使用简单的 Jaccard 相似度（基于字符 n-gram）。

        Args:
            text1: 第一段文本
            text2: 第二段文本

        Returns:
            相似度值 [0, 1]
        """
        if not text1 or not text2:
            return 0.0

        # 简单 word-level Jaccard
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)
