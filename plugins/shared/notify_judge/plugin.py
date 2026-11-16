"""notify_judge 管道插件——消息收尾判定「该不该给用户弹系统通知」。

判定瀑布（v4：终局门 + LLM 主判 + 快切省钱）。适用面由管道 when 门控：只有
主 agent（agent_level == 'L1'）会话进本步，任务/子 agent 轮次不判定零打标
（agent_config_load 每 run 无条件写 state.agent_level 单一真值；用户裁定
2026-10-17：通知面单点归主会话）：

00. 查看态门（2026-10-21 用户裁定）：用户正在前端查看本会话 → 零 LLM 零
    打标整步跳过（人在场通知无意义；查询失败按不在场回退后续瀑布）；
0. 终局门（v4 新增）：尾条 assistant 消息带 tool_calls → 零判定零打标
   （带调用=中间轮/过程轮，本轮尚未交付；装机库实测 120 条打标里 43 条
   带 tool_calls —— 每条中间轮都弹一次，正是「每个 AI 回复都弹」的成因）；
1. 硬规则（防双响）：尾部窗口出现 human_interaction 调用 → 不弹（交互卡
   路径已通知，双重通知属噪音）；
2. 快切：最终文本命中强承诺/意图词（config settings.keywords 统一词表）
   → 弹（跳过 LLM）；
3. 其余 → LLM 主判：尾部窗口序列化（用户消息 + assistant/tool 调用摘要，
   总长截 max_prompt_chars，便宜模型档）+ 一句话判据「这条输出是否值得
   打断用户？」（报错/最终交付/向用户提问=是；过程碎语/中间进度=否）；
4. LLM 失败 → 不弹（v4：fail-closed 减噪）+ warning。判定器的职责是减噪，
   失败即全量放行等于减噪器失效（装机实测 2026-10-20 单日 17 次超时/空
   输出降级，每次降级=一次无谓打扰）；快切词表已在前置覆盖「明确承诺/
   提醒」场景，漏报风险由词表与交互卡路径承担。

产出：worthy=true → 经 messages set-op 在尾条 assistant 消息 metadata 写
``notify_worthy=true``（true-only 契约键，与前端 systemNotifier 对账；
false 不写，前端按真值判断）；无 seq 可定位时零写入。任何异常 warning
直通，不阻断消息流。

LLM 通道：经内核 tool-executor 能力调 llm.complete_stream（LLM 面唯一
事实源，与 llm_core 同款调用形态）；不携带 _call_context——流式事件路由
键为空，内核按 thread_id 空值丢弃，判定调用不污染前端消息流。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# 能力调用函数类型：(method, params, timeout秒) -> Any（调 llm.complete_stream）。
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Awaitable[Any]]

_capability_caller: CapabilityCaller | None = None

# 查看态查询通道（pipeline-executor.is_pipeline_viewed，server.py on_load 注入）。
_view_checker: CapabilityCaller | None = None

# 查看态查询超时（秒）：内核内存查询，秒级余量足够；失败按不在场回退判定链。
_VIEW_CHECK_TIMEOUT_SECONDS = 5.0


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入 capability caller（server.py on_load；测试注入 fake 同一口）。"""
    global _capability_caller  # noqa: PLW0603 — llm_core/wiring 同款模块级单例
    _capability_caller = caller


def set_view_checker(caller: CapabilityCaller | None) -> None:
    """注入查看态查询 caller（server.py on_load；测试注入 fake 同一口）。"""
    global _view_checker  # noqa: PLW0603 — 同上模块级单例
    _view_checker = caller


# 消息 metadata 契约键（前端 systemNotifier：metadata.notify_worthy===true 必弹）。
METADATA_KEY = "notify_worthy"

# 快切缺省词表（config settings.keywords 覆盖）：强承诺 + 通知/提醒意图统一词表。
_DEFAULT_KEYWORDS: tuple[str, ...] = (
    "已设置提醒",
    "会通知你",
    "到时候叫你",
    "安排上了",
    "记好了",
    "搞定了",
    "完成✅",
    "提醒我",
    "通知我",
    "别忘了",
)

# 防双响缺省工具面（config settings.silent_tools 覆盖）：交互卡路径已通知。
_DEFAULT_SILENT_TOOLS: tuple[str, ...] = ("human_interaction",)

# 工具调用参数摘录上限（字符）——LLM 判定只需名称+参数形态，不传全文。
_TOOL_ARGS_PREVIEW_CHARS = 120

# 模板不含占位符（判据里的 JSON 字面量与 str.format 字段语法冲突，拼接组装）。
# 判据给出成对正反例：正例=真正值得打断用户的终局交付；反例=过程轮常见形态
# （系统注入的执行回顾/无新指令的确认/调工具前奏），防「长过程消息看着像报告
# 就判值得」导致的误弹。
_JUDGE_PROMPT_HEAD = (
    "这条输出是否值得打断用户？报错/最终交付/向用户提问=是；"
    "过程碎语/中间进度/系统注入的执行回顾/无新指令的确认/调工具前的开场白=否。"
    "只输出 JSON："
    '{"worthy": bool, "reason": "一短语"}\n\n【对话尾部】\n'
)


class NotifyJudge:
    """通知判定器：纯 state 进出，LLM 经注入的 capability caller。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """config = 内核注入配置（{"settings": {...}} 命名空间嵌套形态）。"""
        cfg = config or {}
        settings = cfg.get("settings")
        s = settings if isinstance(settings, dict) else {}
        self._enabled = bool(s.get("enabled", True))
        # 空 = 不钉具体模型，判定模型走 llm.yaml small 档（经 model.resolve 单源解析）。
        self._model = str(s.get("model") or "")
        keywords = s.get("keywords")
        self._keywords: list[str] = (
            [str(k) for k in keywords if str(k)] if isinstance(keywords, list) and keywords else list(_DEFAULT_KEYWORDS)
        )
        self._tail_rounds = max(1, int(s.get("tail_rounds") or 2))
        self._max_prompt_chars = max(200, int(s.get("max_prompt_chars") or 4000))
        self._timeout_seconds = max(1.0, float(s.get("timeout_seconds") or 20.0))
        # max_tokens 缺省 2000：minimax-m3 推理档 thinking 吃预算，200 写死
        # 致正文空（「LLM 判定输出非 JSON:(空)」降级，装机 2026-10-15 实证）。
        self._max_tokens = max(1, int(s.get("max_tokens") or 2000))
        silent = s.get("silent_tools")
        self._silent_tools: list[str] = (
            [str(t) for t in silent if str(t)] if isinstance(silent, list) and silent else list(_DEFAULT_SILENT_TOOLS)
        )

    async def judge(self, state: dict[str, Any]) -> dict[str, Any]:
        """收尾判定：返回 state_updates（worthy=true 时携 messages set-op）。"""
        if not self._enabled:
            return {}
        # 查看态门（用户裁定 2026-10-21）：用户正在前端查看本会话时通知无意义
        # （消息他正看着）——零 LLM 零打标整步跳过。这也是 post 阶段生成态
        # 收尾延迟的最大单项：主会话每轮纯文本一次判定 LLM 往返，在场场景
        # 全免。查询失败按不在场回退既有判定链（跳过是增强优化，不减通知面）。
        if await self._viewer_watching(state):
            logger.info("[notify_judge] 用户正在查看本会话，跳过通知判定（零 LLM 零打标）")
            return {}
        # 触发来源轮次：是否通知用户由 agent 自主决定（走 human_interaction），
        # 收尾文本不再自动打标——否则「LLM 判断不打扰」仍会被通知面弹一次。
        if str(state.get("trigger.origin") or "") == "trigger":
            return {}
        messages = state.get("messages")
        if not isinstance(messages, list):
            return {}
        final = _last_assistant(messages)
        if final is None:
            return {}
        if _is_tool_call_round(final):
            return {}  # 终局门：带调用的中间轮非本轮交付，无通知面
        text = message_text(final)
        if not text.strip():
            return {}  # 无正文产出的轮（纯工具中转），无通知面
        window = self._tail_window(messages)
        if self._window_has_silent_call(window):
            return {}  # 交互卡路径已通知，防双响
        if self._fast_cut(text):
            return self._worthy_update(final)
        try:
            worthy = await self._llm_judge(window)
        except Exception as exc:  # 降级契约：fail-closed 减噪，失败不弹
            logger.warning("[notify_judge] LLM 判定失败，降级为静默不弹 | err=%s", exc)
            return {}
        if not worthy:
            return {}
        return self._worthy_update(final)

    # ── 规则面 ──

    async def _viewer_watching(self, state: dict[str, Any]) -> bool:
        """用户是否正在前端查看本管道所属会话（查看态门数据面）。

        真值源 = 内核 `pipeline-executor.is_pipeline_viewed`（RunChainRegistry
        活跃管道映射，active_thread_changed 权威 + 最近派发兜底）。checker 未
        注入 / state 无 pipeline_id / 查询异常 → False（按不在场继续判定）。
        """
        checker = _view_checker
        if checker is None:
            return False
        pipeline_id = str(state.get("pipeline_id") or "")
        if not pipeline_id:
            return False
        try:
            resp = await checker(
                "is_pipeline_viewed",
                {"pipeline_id": pipeline_id},
                _VIEW_CHECK_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 —— 查询失败按不在场（回退判定链）
            logger.debug("[notify_judge] 查看态查询失败，按不在场继续判定 | err=%s", exc)
            return False
        return isinstance(resp, dict) and resp.get("viewed") is True

    def _tail_window(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """尾部窗口：最近 N 个 assistant 轮 + 其前置 user 消息（含中间 tool 序列）。"""
        assistant_idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "assistant"]
        if not assistant_idx:
            return []
        start = assistant_idx[-self._tail_rounds :][0]
        # 回溯到当前输入的 user 消息（窗口必须含用户视角的当前输入）。
        while start > 0 and not (isinstance(messages[start - 1], dict) and messages[start - 1].get("role") == "user"):
            start -= 1
        if start > 0:
            start -= 1
        return [m for m in messages[start:] if isinstance(m, dict)]

    def _window_has_silent_call(self, window: list[dict[str, Any]]) -> bool:
        """尾部窗口出现过防双响工具（human_interaction 等）调用。"""
        for msg in window:
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and fn.get("name") in self._silent_tools:
                    return True
        return False

    def _fast_cut(self, text: str) -> bool:
        """快切：最终文本命中强承诺/意图词（大小写折叠匹配）。"""
        folded = text.casefold()
        return any(kw.casefold() in folded for kw in self._keywords)

    # ── LLM 主判 ──

    async def _resolve_model_id(self, caller: Any) -> str:
        """判定模型 id：显式 settings.model 优先；空经 model.resolve 走 small 档。

        档位解析单源在 llm_service（resolve_model_id 链），本插件只表达
        「要 small 档」的意图，不复制解析链、不钉具体模型名——换供应商
        只动 llm.yaml，本插件零改动。
        """
        if self._model:
            return self._model
        envelope = await caller(
            "invoke",
            {
                "tool_name": "model.resolve",
                "plugin_id": "llm_service",
                "args": {"model_tier": "small"},
            },
            self._timeout_seconds,
        )
        if not isinstance(envelope, dict) or not envelope.get("success"):
            raise RuntimeError(f"model.resolve 调用失败: {envelope}")
        data = envelope.get("data")
        resolved = str(data.get("model_id") or "") if isinstance(data, dict) else ""
        if not resolved:
            raise RuntimeError(
                "model.resolve 未解析到 small 档模型（llm.yaml defaults.tiers.small 与 defaults.chat 均空）"
            )
        return resolved

    async def _llm_judge(self, window: list[dict[str, Any]]) -> bool:
        """经 llm.complete_stream 分类尾部窗口；输出非 JSON/worthy 缺失一律抛错。"""
        caller = _capability_caller
        if caller is None:
            raise RuntimeError("capability caller 未注入（llm_service 通道未接线）")
        model_id = await self._resolve_model_id(caller)
        prompt = _JUDGE_PROMPT_HEAD + self._serialize_window(window)
        # 不携带 _call_context：流式事件路由键为空，内核按 thread_id 空值丢弃，
        # 判定调用不向前端消息流推 chunk。
        params = {
            "tool_name": "llm.complete_stream",
            "plugin_id": "llm_service",
            "args": {
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self._max_tokens,
            },
        }
        envelope = await caller("invoke", params, self._timeout_seconds)
        if not isinstance(envelope, dict) or not envelope.get("success"):
            raise RuntimeError(f"llm.complete_stream 调用失败: {envelope}")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"llm.complete_stream 返回形状异常: {type(data).__name__}")
        return self._parse_verdict(str(data.get("text") or ""))

    def _serialize_window(self, window: list[dict[str, Any]]) -> str:
        """尾部窗口 → 判定转录（user 全文；assistant 正文 + 工具调用摘要；
        tool 结果消息只承载执行细节，判定不需要，跳过）。保尾截断。"""
        lines: list[str] = []
        for msg in window:
            role = msg.get("role")
            if role == "user":
                text = message_text(msg).strip()
                if text:
                    lines.append(f"[用户] {text}")
            elif role == "assistant":
                for summary in _tool_call_summaries(msg):
                    lines.append(f"[工具调用] {summary}")
                text = message_text(msg).strip()
                if text:
                    lines.append(f"[助手] {text}")
        transcript = "\n".join(lines)
        if len(transcript) > self._max_prompt_chars:
            transcript = transcript[-self._max_prompt_chars :]
        return transcript

    @staticmethod
    def _parse_verdict(text: str) -> bool:
        """LLM 输出 → worthy 布尔（fail-closed：非 JSON/缺 worthy 布尔即抛错）。"""
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"LLM 判定输出非 JSON: {text[:200]}")
        verdict = json.loads(text[start : end + 1])
        if not isinstance(verdict, dict) or not isinstance(verdict.get("worthy"), bool):
            raise ValueError(f"LLM 判定缺少 worthy 布尔字段: {text[:200]}")
        return verdict["worthy"]

    # ── 产出 ──

    def _worthy_update(self, final: dict[str, Any]) -> dict[str, Any]:
        """尾条 assistant 消息 metadata 写 notify_worthy=true（messages set-op）。"""
        seq = final.get("seq")
        if seq is None:
            logger.warning("[notify_judge] 尾条 assistant 消息无 seq，无法定位槽位，零写入")
            return {}
        msg = dict(final)
        meta = msg.get("metadata")
        merged = dict(meta) if isinstance(meta, dict) else {}
        merged[METADATA_KEY] = True
        msg["metadata"] = merged
        return {"messages": {"_ops": [{"op": "set", "seq": seq, "msg": msg}]}}


def _last_assistant(messages: list[Any]) -> dict[str, Any] | None:
    """messages 尾条 assistant 消息（判定与产出同一目标）。"""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg
    return None


def _is_tool_call_round(msg: dict[str, Any]) -> bool:
    """尾条 assistant 是否带工具调用（=中间轮/过程轮，非本轮终局交付）。

    终局交付轮 = 无调用的纯文本回复；带 tool_calls 的 assistant 消息意味着
    本轮还要继续调工具，尚未交付——对它弹通知等于「每个 AI 回复都弹」。
    """
    tool_calls = msg.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def message_text(msg: dict[str, Any]) -> str:
    """消息文本（str 直返；multimodal list 拼 text 部件）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(s for s in parts if s)
    return ""


def _tool_call_summaries(msg: dict[str, Any]) -> list[str]:
    """assistant 消息的工具调用摘要（名称 + 参数形态预览）。"""
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    summaries: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        args = fn.get("arguments", "")
        args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        if len(args_text) > _TOOL_ARGS_PREVIEW_CHARS:
            args_text = args_text[:_TOOL_ARGS_PREVIEW_CHARS]
        summaries.append(f"{fn.get('name', '?')}({args_text})")
    return summaries
