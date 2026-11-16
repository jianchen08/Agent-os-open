"""todo_track 管道插件——单插件双步骤（capabilities.steps 注册，③级步骤服务）。

接线契约（ADR 2026-10-08-step-service-level3-wiring / 方案 r4）：

- ``todo.fold``（输入阶段，挂 prompt_build 之后）：说明常量（TODO_INSTRUCTIONS，
  模块级常量=会话内字节稳定）与 ``context.todo_text``（非空时）并入
  ``prompt.dynamic_vars`` 消息 content 尾部（prompt_build 每轮重建该消息，
  追加不累积；幂等：已在场不重复追加）；``prompt.dynamic_vars`` 缺失或形态
  不符 = 零动作直通。2026-10-23 缓存契约修复（用户裁定）：不再写
  ``context.system_prompt``——prompt_build 每轮重建系统消息，fold 追加在部分
  调用路径被重建覆盖，说明段在有/无之间横跳，系统消息头部失配打穿全量
  前缀缓存（装机 382ec1ee 管道实测 30 调用仅 3 次命中）；动态变量消息位于
  请求末尾（llm_core _build_messages 独立 user 消息），在此并入只影响尾部。
- ``todo.extract``（输出阶段）：读 messages 尾条 assistant 文本 →
  checkbox 行按增量协议（``[x]`` 勾选匹配 / ``[ ]`` 新增查重）并入
  ``context.todo_text``（下轮 fold 注入源）+ 内存 latest（pipeline_id →
  载荷，latest 端点拉取源——每轮整体覆写同键 = 同源替换）；无 ops = 状态
  停更（零写入直通，不回退不清空）。

数据面持久化（1.2.26 修）：``context.todo_text`` 必须经 manifest
``persistent_fields`` 落 ``pipeline_state``。内核每轮终态注销热缓存
（server.rs run 收尾 ``registry.remove``），下轮冷恢复只恢复
``pipeline_state`` 表里的声明键——未声明的键每轮丢一次，merge 基座恒空，
清单只剩最新一轮增量（用户实测「面板只显示最新的」根因）。1.2.26 起
``context.todo_state_v`` 标记状态版本：缺失/低于当前版本时从近轮 assistant
消息重放增量协议重建基座（一次性自愈，补 1.2.25 及以前每轮丢状态造成的
累积面缺口）。

降级契约：任何异常 warning 日志 + 空更新直通，不阻断管道。
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from todo_extract import (
    incremental_merge,
    parse_html_card_ops,
    parse_single_ops,
)

logger = logging.getLogger(__name__)

# static 贡献说明常量（2-3 行纪律，插件单源——任务提示/mode 材料/agent
# 骨架零承载）。模块级常量 = 会话内字节稳定（前缀缓存契约的执法点在
# "常量"这一事实，不建检测机制）。
TODO_INSTRUCTIONS = (
    "## 任务清单（todo）约定\n"
    "多步任务开始时用 GFM 任务清单（`- [ ] `）列出计划（聊天界面会渲染为"
    "可折叠清单卡，系统自动建待办）；**每完成一项，单行 `- [x] 条目内容`"
    " 即勾选**——只勾当前实际完成的，无需按顺序、无需重发全表；新增待办"
    "单行 `- [ ] 新条目`（系统自动查重并入）。保持勾选与实际进度一致。\n"
    "**清单只能写 GFM 任务行**：禁止把清单包进 ```html/```todo 等代码围栏、"
    "禁止输出 HTML 卡片或代码块形态（界面会按源码原样显示，状态也不会更新）。"
)

# 注入动态变量的清单块头（权威标注：与模型历史正文里的旧清单并存时以
# 本块为准）。
_TODO_HEADER = "当前任务清单（以此为准，变更时整表重发）："

# 注入面预算裁选（2026-10-27 用户裁定：超长裁选只作用于注入面，前端显示
# 面完整不截断）：未完成项超预算时保首尾、略中段——队首=计划顺序的下一步
# 动作（砍头丢计划入口），队尾=最近新增（当前焦点）；截断只在本注入视图，
# context.todo_text 状态面仍存全量，extract 合并对全量进行，不丢数据。
_TODO_PENDING_BUDGET = 30
_TODO_KEEP_HEAD = 10
_TODO_KEEP_TAIL = 10

# 清单状态版本（持久化声明键，见模块 docstring）：缺失/低于当前值 = 1.2.25
# 及以前的每轮状态丢失现场，extract 从近轮消息重放增量协议重建基座。
_TODO_STATE_VERSION = 1
_TODO_STATE_VERSION_KEY = "context.todo_state_v"

# 自愈重放窗口（assistant 消息条数）：窗口越大越接近历史全量真值，但会把
# 早已翻篇的旧条目一起捞回来。近轮窗口 = 模型最近几轮自述的计划面，正是
# 面板该显示的内容；一次性自愈后状态经 persistent_fields 落库，后续轮次
# 只走常规增量合并，不再重放。
_TODO_REPLAY_WINDOW = 10


class TodoTrackPlugin:
    """todo_track 双步骤插件（纯 state 进出，无跨插件调用）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        # 最近载荷内存态（pipeline_id → 载荷）：前端计划卡拉取源。重启丢
        # 内存态 = 卡消失（载荷本为本轮瞬态，可接受，同 state_marker_latest）。
        self._latest: dict[str, dict[str, Any]] = {}

    async def fold(self, state: dict[str, Any]) -> dict[str, Any]:
        """输入阶段：说明常量 + 当前清单并入动态变量消息，返回 state_updates。

        系统提示词零触碰（缓存契约，见模块 docstring）；fold 每轮只跑一次且
        prompt_build 每轮重建 dynamic_vars，追加不累积，幂等守卫只防同轮重复。
        """
        updates: dict[str, Any] = {}
        dvm = state.get("prompt.dynamic_vars")
        if not (isinstance(dvm, dict) and isinstance(dvm.get("content"), str)):
            return updates
        content = dvm["content"].rstrip()
        blocks = [content]
        if TODO_INSTRUCTIONS not in content:
            blocks.append(TODO_INSTRUCTIONS)
        todo_text = state.get("context.todo_text")
        if todo_text:
            pending = _pending_lines(str(todo_text))
            if pending:
                blocks.append(pending)
        merged = dict(dvm)
        merged["content"] = "\n\n".join(blocks)
        updates["prompt.dynamic_vars"] = merged
        return updates

    async def extract(self, state: dict[str, Any]) -> dict[str, Any]:
        """输出阶段：解析尾条 assistant 的清单操作，合并写注入源 + latest。

        统一增量协议（2026-10-24 用户裁定）：任何 checkbox 行都是操作——
        ``[x]`` 勾选匹配现有项（只勾实际完成的，不按顺序、不重发全表）、
        ``[ ]`` 新增查重追加（文本/哈希不同才加）；首批计划 = 空清单上全部
        追加；零有效变更 = 状态停更。

        合并基座 = state 里持久化的 ``context.todo_text``（manifest
        persistent_fields 声明，跨轮由 pipeline_state 冷恢复带回）。
        状态版本标记缺失/过期 = 旧版每轮丢状态的现场，先从近轮消息重放
        增量协议重建基座，再叠加本轮 ops（同批写版本标记，只跑一次）。
        """
        text = self._last_assistant_text(state)
        ops = parse_single_ops(text) if text else []
        if not ops and text:
            # HTML 任务卡兜底（2026-10-25 用户实测：模型仿历史折叠卡形态把
            # 清单包进 ```html 围栏——GFM 零 ops 时勾选/新增全丢且界面显示
            # 源码；卡形态特征守卫防普通 HTML 误解析）
            ops = parse_html_card_ops(text)
        updates: dict[str, Any] = {}
        base = _items_from_todo_text(state.get("context.todo_text"))
        changed = False
        if state.get(_TODO_STATE_VERSION_KEY) != _TODO_STATE_VERSION:
            replayed = self._replay_items(state.get("messages"))
            if replayed:
                base = incremental_merge(replayed, base) or replayed
                changed = True
            if base:
                updates[_TODO_STATE_VERSION_KEY] = _TODO_STATE_VERSION
        if ops:
            merged = incremental_merge(base, ops)
            if merged is not None:
                base = merged
                changed = True
        if changed and base:
            lines = [_render_item(item) for item in base]
            updates["context.todo_text"] = _TODO_HEADER + "\n" + "\n".join(lines)
            pipeline_id = str(state.get("pipeline_id", "") or "")
            if pipeline_id:
                self._latest[pipeline_id] = {
                    "items": base,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
        return updates

    def latest_payload(self, pipeline_id: str) -> dict[str, Any] | None:
        """前端计划卡拉取源（GET /ext/pipeline_todo_track/latest）。"""
        return self._latest.get(pipeline_id)

    def latest_any(self) -> dict[str, Any] | None:
        """无 pipeline_id 时的最近载荷（updated_at 最大者）——webview 计划页
        与角标免 session→pipeline 映射的取数形态（桌面单活跃任务流：最近
        更新者即当前活动管道）。"""
        best: dict[str, Any] | None = None
        for payload in self._latest.values():
            if best is None or str(payload.get("updated_at", "")) > str(
                best.get("updated_at", "")
            ):
                best = payload
        return best

    @staticmethod
    def _last_assistant_text(state: dict[str, Any]) -> str:
        """messages 尾条 assistant 文本（multimodal list 取 text 部件拼接）。"""
        messages = state.get("messages")
        if not isinstance(messages, list):
            return ""
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            return _message_text(msg)
        return ""

    def _replay_items(self, messages: Any) -> list[dict[str, Any]]:
        """近轮 assistant 消息重放增量协议（一次性自愈的基座重建）。

        窗口 = 最后 ``_TODO_REPLAY_WINDOW`` 条 assistant 消息（含本轮）：状态
        版本标记缺失/过期说明此前每轮 merge 基座都被冷恢复丢弃，累积面只剩
        最新一轮增量。重放按历史顺序把 checkbox 行当操作序列推进，等价于
        「状态一直在的话应该长成的样子」的有界近似——窗口外的旧条目属于已
        翻篇阶段，不捞回。messages 缺失/形态不符返回 []（零动作）。
        """
        if not isinstance(messages, list):
            return []
        texts = [
            _message_text(msg)
            for msg in messages
            if isinstance(msg, dict) and msg.get("role") == "assistant"
        ]
        items: list[dict[str, Any]] = []
        for text in [t for t in texts if t][-_TODO_REPLAY_WINDOW:]:
            ops = parse_single_ops(text) or parse_html_card_ops(text)
            if not ops:
                continue
            merged = incremental_merge(items, ops)
            if merged is not None:
                items = merged
        return items


def _pending_lines(todo_text: str) -> str:
    """注入面清单视图（2026-10-24 完成过滤 + 2026-10-27 预算裁选）。

    已完成行（`- [x] `）整行剔除、头部补一行已完成计数（进度可感、零内容
    回放——回放会把模型锚死在旧阶段，G2 循环事故实证）；未完成项超预算时
    保首尾、略中段，省略行不用 checkbox 语法（防模型整表重发时被当新增项
    合并进状态面）。全完成时返回空串，清单块整体不注入（TODO_INSTRUCTIONS
    约定仍在场，无清单即无剩余工作）。非任务行（header/空行）原位保留。
    """
    lines = todo_text.splitlines()
    is_done = [_is_done_item(line) for line in lines]
    done_n = sum(is_done)
    pending_idx = [
        i
        for i, line in enumerate(lines)
        if not is_done[i] and _ITEM_LINE_RE.match(line)
    ]
    keep = set(pending_idx)
    omitted = 0
    if len(pending_idx) > _TODO_PENDING_BUDGET:
        keep = set(pending_idx[:_TODO_KEEP_HEAD]) | set(
            pending_idx[-_TODO_KEEP_TAIL:]
        )
        omitted = len(pending_idx) - len(keep)
    out: list[str] = []
    done_count_written = False
    marker_pending = omitted > 0
    for i, line in enumerate(lines):
        if is_done[i]:
            if not done_count_written:
                out.append(f"（已完成 {done_n} 项，不再重列）")
                done_count_written = True
            continue
        if not _ITEM_LINE_RE.match(line):
            out.append(line)  # 非任务行（header/空行）原位保留
            continue
        if marker_pending and i not in keep:
            out.append(f"（已省略 {omitted} 项较早未完成项）")
            marker_pending = False
        if i in keep:
            out.append(line)
    text = "\n".join(out).rstrip()
    return text if "- [ ]" in text else ""


def _is_done_item(line: str) -> bool:
    """checkbox 行且为完成态（`- [x] `/`- [x]`，与提取面 x/X 宽容同口径）。"""
    stripped = line.lstrip()
    return stripped.startswith("- [x] ") or stripped == "- [x]"



def _render_item(item: dict[str, Any]) -> str:
    """单条清单项 → GFM 任务行（勾选态回写为小写 x 规范形）。"""
    mark = "x" if item.get("done") else " "
    return f"- [{mark}] {item.get('text', '')}"


def _items_from_todo_text(todo_text: Any) -> list[dict[str, Any]]:
    """从注入面清单文本还原 items（增量合并的现有态来源）。"""
    if not isinstance(todo_text, str) or not todo_text:
        return []
    items: list[dict[str, Any]] = []
    for line in todo_text.splitlines():
        m = _ITEM_LINE_RE.match(line)
        if m:
            items.append({"text": m.group(2).strip(), "done": m.group(1).lower() == "x"})
    return items


def _message_text(msg: dict[str, Any]) -> str:
    """单条消息的文本面（multimodal list 取 text 部件拼接；非文本返回空串）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(s for s in parts if s)
    return ""


# 与 todo_extract._ITEM_RE 同口径的行解析（注入面回读，避免跨模块私有正则导出）
_ITEM_LINE_RE = re.compile(r"^\s*-\s+\[([xX ])\]\s+(.*)$")
