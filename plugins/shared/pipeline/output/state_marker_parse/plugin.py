"""状态统一标记解析步（通用输出管道步骤，2026-09-28 设计）。

设计真值 docs/working/角色状态统一标记机制设计_20260928.md §2/§3：
- 提取本轮 assistant 回复（state["messages"] 尾条 role=assistant）的
  `<state>` 标记（语法/区间单源在共享平铺模块 state_marker.py）；
- **账本白名单分发**（键 = 目标账本，未知键丢弃 + warning——fail-closed 不
  透传）：`state` → character_state.update（card_id 取自 state["agent.id"]
  卡键尾段；非卡键 = 状态账本不落账，warning 降级）；`memory` → memory 工具
  store（action=store）。跨插件调用经 tool-executor.invoke 显式 plugin_id
  （mode_evolution 先例同通道）；调用失败 = 该键留痕跳过，其余键继续；
- **render 回写**：每轮调 character_state.render(card_id) 写
  `context.character_state_text`——供下轮动态变量消息注入（消息尾部形态，
  system_prompt 前缀不动，缓存零损失）；无标记也回写（首轮初始状态）；
- **写 `context.state_updates`**：{entries（落账成功的账本键载荷）, span,
  ts}——前端 pull-from-state 渲染状态小卡并按 span 隐藏标记原文；无标记 =
  不写该键（零渲染语义）。

降级契约：任何失败（无卡键/服务失败/坏 JSON）→ warning + 不阻断管道；
解析只读 messages 尾条，不改消息原文（展示剥离归前端渲染层）。
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any, Callable

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext

from mode_keys import parse_mode_agent_key as _parse_mode_agent_key
from state_marker import parse_state_marker

logger = logging.getLogger(__name__)

# 账本白名单（键 → 处理器名）；未知键 = 丢弃 + warning（fail-closed 不透传）。
KNOWN_LEDGER_KEYS = frozenset({"state", "memory"})


async def _noop_caller(_tool_name: str, _plugin_id: str, _args: dict) -> dict[str, Any]:
    raise RuntimeError("tool-executor caller 未注入")


class StateMarkerParsePlugin(IOutputPlugin):
    """状态标记解析输出插件（通用步骤，零模式特判）。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        invoke_caller: Callable[[str, str, dict], Any] | None = None,
    ) -> None:
        self._config = config or {}
        # tool-executor 调用句柄（async (tool_name, plugin_id, args) -> dict）；
        # 生产侧由 server.py 经 plugin.get_capability 注入，测试侧注入 fake。
        self._invoke = invoke_caller or _noop_caller
        # 最近载荷内存态（pipeline_id → state_updates）：前端状态小卡的拉取源
        # （GET /ext/state_marker_parse/latest）。pipelineStates 是内核白名单
        # 裁剪视图不透出该键，零内核边界下端点拉取是唯一通道；重启丢内存态 =
        # 卡消失（载荷本为本轮瞬态，可接受）。
        self._latest: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "state_marker_parse"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        try:
            return OutputResult(state_updates=await self._do_work(ctx.state))
        except Exception as exc:  # noqa: BLE001 — 降级语义：解析失败不阻断管道
            logger.warning("[state_marker_parse] 执行失败，零写入直通 | err=%s", exc)
            return OutputResult(state_updates={})

    async def _do_work(self, state: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        text = self._last_assistant_text(state)
        marker = parse_state_marker(text) if text else None
        card_id = self._resolve_card_id(state)

        entries: dict[str, Any] = {}
        span: list[int] | None = None
        if marker is not None:
            span = marker["span"]
            for key, payload in marker["entries"].items():
                if key not in KNOWN_LEDGER_KEYS:
                    logger.warning(
                        "[state_marker_parse] 未知账本键已丢弃（白名单外）| key=%s", key
                    )
                    continue
                try:
                    entries[key] = await self._dispatch(key, payload, card_id)
                except Exception as exc:  # noqa: BLE001 — 单键失败不拖垮其余键
                    logger.warning(
                        "[state_marker_parse] 账本落账失败，跳过 | key=%s | err=%s",
                        key,
                        exc,
                    )

        # render 回写（每轮，无标记也回写——首轮初始状态注入；非卡键零动作）
        if card_id:
            try:
                rendered = await self._invoke(
                    "character_state.render",
                    "character_state",
                    {"card_id": card_id},
                )
                state_text = self._service_text(rendered)
                if state_text:
                    updates["context.character_state_text"] = state_text
            except Exception as exc:  # noqa: BLE001 — 降级语义：渲染失败不阻断
                logger.warning(
                    "[state_marker_parse] 状态渲染回写失败 | card_id=%s | err=%s",
                    card_id,
                    exc,
                )

        if marker is not None and entries:
            # 全部键落账失败（entries 空）= 无可渲染内容 → 不写载荷（前端零渲染语义）
            payload = {
                "entries": entries,
                "span": span,
                "ts": datetime.now(UTC).isoformat(),
            }
            updates["context.state_updates"] = payload
            pipeline_id = str(state.get("pipeline_id", "") or "")
            if pipeline_id:
                self._latest[pipeline_id] = payload
        return updates

    def latest_payload(self, pipeline_id: str) -> dict[str, Any] | None:
        """端点读面：该管道最近一轮 state_updates 载荷（无 = None）。"""
        return self._latest.get(pipeline_id)

    async def _dispatch(self, key: str, payload: Any, card_id: str | None) -> Any:
        """单账本落账；返回载荷回执（进 state_updates.entries）。"""
        if key == "state":
            if not isinstance(payload, dict):
                raise ValueError("state 账本载荷须为 JSON 对象（key → 新值）")
            if not card_id:
                raise ValueError("非卡键会话状态账本不落账（归属卡未定）")
            res = await self._invoke(
                "character_state.update",
                "character_state",
                {"card_id": card_id, "changes": payload},
            )
            return payload
        if key == "memory":
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError("memory 账本载荷须为非空字符串")
            res = await self._invoke(
                "memory",
                "memory",
                {"action": "store", "content": payload.strip()},
            )
            return {"stored": True}
        raise ValueError(f"未知账本键: {key}")

    @staticmethod
    def _last_assistant_text(state: dict[str, Any]) -> str:
        messages = state.get("messages")
        if not isinstance(messages, list):
            return ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content")
                return content if isinstance(content, str) else ""
        return ""

    @staticmethod
    def _resolve_card_id(state: dict[str, Any]) -> str | None:
        agent_id = str(state.get("agent.id", "") or "")
        parsed = _parse_mode_agent_key(agent_id)
        if parsed is None:
            return None
        return parsed[1]

    @staticmethod
    def _service_text(res: Any) -> str:
        """服务回执 → 文本（容忍 {data: ...} 信封或文本本体）。"""
        data = res.get("data") if isinstance(res, dict) else None
        payload = data if data is not None else res
        if isinstance(payload, dict):
            payload = payload.get("text") or payload.get("content") or ""
        return payload.strip() if isinstance(payload, str) else ""
