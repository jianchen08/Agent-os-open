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
    "多步任务开始时用 `- [ ] ` 列计划（自动建待办）。每条待办只写两次："
    "建卡 `- [ ] 条目`，勾销 `- [x] 条目`，逐字复用原文、不改措辞不加 ID。\n"
    "**用户需求必须建待办**：提出即建，不得口头应承。\n"
    "**清单只能写 GFM 任务行**：禁止把清单包进 ```html/```todo 等代码围栏、"
    "禁止输出 HTML 卡片或代码块形态，也禁止把清单输出成文件。\n"
    "**勾销门槛**：原始需求须用户确认后勾；中间步骤证据到位即勾。\n"
    "**决策分流**：无决策→直接派；有问题要问、有决策要拍→第一优先级用 "
    "`human_interaction` 交互工具出卡；待确认项下一轮批量确认；等外部条件"
    "写明等什么。\n"
    "**失效条目**：被取代的标「已失效」关闭；同一件事多口径合并为一条。"
)

# 注入动态变量的清单块头（权威标注：与模型历史正文里的旧清单并存时以
# 本块为准）。1.2.28 修：「变更时整表重发」是 2026-10-24 增量协议裁定前
# 整表替换时代的遗留，与 TODO_INSTRUCTIONS「无需重发全表」直接冲突——
# 贴数据、每轮出现的指令面赢，模型逐轮重发整表，措辞漂移绕过精确查重，
# 近重复条目累积（用户实测「很多重复的文本又不一样」根因）。
# 约束：本行不得含字面 `- [ ]`/`- [x]`（_pending_lines 以 "- [ ]" in text
# 判定空清单不注入；含字面语法会破「全完成清单块整体不注入」契约）。
_TODO_HEADER = "当前任务清单（以此为准；已随每轮注入，无需重发整表——变更只写有变化的单行）："

# 注入面预算裁选（2026-10-27 用户裁定：超长裁选只作用于注入面，前端显示
# 面完整不截断）：未完成项超预算时保首尾、略中段——队首=计划顺序的下一步
# 动作（砍头丢计划入口），队尾=最近新增（当前焦点）；截断只在本注入视图，
# context.todo_text 状态面仍存全量，extract 合并对全量进行，不丢数据。
_TODO_PENDING_BUDGET = 30
_TODO_KEEP_HEAD = 10
_TODO_KEEP_TAIL = 10

# 清单状态版本（持久化声明键，见模块 docstring）：缺失/低于当前值 = 提取面
# 行为变更现场，extract 从近轮消息重放增量协议重建基座。
# v2（2026-11-10 用户提出）：提取面只扫正文最终输出（跳过 thinking/reasoning
# 段）——v1 状态面可能含思考段清单草稿的幽灵条目（同一事项多变体措辞堆积、
# 正文按原文精确勾选永远匹配不上），迁移 = 旧基座作废、按修复后口径从近轮
# 正文重放重建（幽灵清理的一次性自愈通道）。
# v3（2026-11-16 用户实测 7fb38fe89e82：同条待办存 2~3 份副本）：新增查重面
# 加匹配分兜底 + 归一化剥「— `任务ID`（状态）」尾注（todo_extract 1.2.32）——
# v2 基座里已固化的重述副本按旧口径合并不掉，迁移 = 旧基座作废、按修复后
# 口径从近轮正文重放重建（同 v2 一次性自愈通道）。
_TODO_STATE_VERSION = 3
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
            # 版本迁移（一次性自愈）：旧基座作废——可能含提取面行为变更前的
            # 污染条目（v1 时代思考段清单草稿被捕获成幽灵待办），从近轮
            # assistant 消息按当前口径重放重建；近轮正文无清单 = 空基座，
            # 注入源显式清空（旧污染不固化、不再随 fold 注入）。
            base = self._replay_items(state.get("messages"))
            updates[_TODO_STATE_VERSION_KEY] = _TODO_STATE_VERSION
            changed = True
        if ops:
            merged = incremental_merge(base, ops)
            if merged is not None:
                base = merged
                changed = True
        if changed:
            if base:
                lines = [_render_item(item) for item in base]
                updates["context.todo_text"] = _TODO_HEADER + "\n" + "\n".join(lines)
                pipeline_id = str(state.get("pipeline_id", "") or "")
                if pipeline_id:
                    self._latest[pipeline_id] = {
                        "items": base,
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
            else:
                # 空基座显式清空注入源（仅版本迁移路径可达：常规增量路径
                # merged 非 None 才置 changed，base 必非空）；空串 falsy，
                # 下轮 fold 不再注入清单块。面板载荷同步置空清单（状态面=
                # 面板不变量：前端计划卡取数源 self._latest，状态已清而面板
                # 残留旧清单即两面对用户不一致，残留项还可能经 latest_any
                # 串桶；updated_at 保持推进 = latest_any 取到本管道空载荷
                # 而非其他管道残留）。不变量以 pipeline_id 在场为前提——
                # 缺失/空串时无键可写，面板残留属异常态残留边（上游同一
                # pipeline 的 pipeline_id 恒定，正常流不可达）。
                updates["context.todo_text"] = ""
                pipeline_id = str(state.get("pipeline_id", "") or "")
                if pipeline_id:
                    self._latest[pipeline_id] = {
                        "items": [],
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
    """单条消息的文本面（multimodal list 取 text 部件拼接；非文本返回空串）。

    提取面只扫正文最终输出（2026-11-10 用户提出）：剥 ``<think>`` 段（闭合
    多段 + 流中断未闭合尾段）与纯思考兜底轮（llm/adapter 非流式把
    reasoning_content 回填正文时 content==reasoning_content，无正文可扫）；
    reasoning_content 独立字段本就不读（隔离通道）。
    """
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = "\n".join(s for s in parts if s)
    else:
        return ""
    reasoning = msg.get("reasoning_content")
    if (
        isinstance(reasoning, str)
        and reasoning.strip()
        and isinstance(content, str)
        and content == reasoning
    ):
        # 纯思考兜底轮（raw 对 raw，剥离前）：llm/adapter 非流式把
        # reasoning_content 回填正文时 content==reasoning_content，同文 =
        # 无正文可扫。归一化基准必须一致——「剥离后正文对 raw reasoning」
        # 的比对双向皆错（同文含 think 段的轮次漏判、reasoning 恰等于剥离
        # 后正文的轮次误判丢正文），故以原始值同文判定；multimodal list
        # 无 raw 同文语义，不参与兜底轮判定（该分支当前不可达：上游
        # assistant content 恒为 str，llm_core 三处构造点均为 str；上游若
        # 引入 multimodal assistant content 需重新评估）。
        return ""
    # 剥离后另行判定空正文（纯 think 块/未闭合尾段占满 = 无正文可扫）
    return _strip_thinking(text)


# think 标签族剥离正则——canonical 口径与 plugins/shared/llm/adapter_minimax.py
# 的 _THINK_PATTERN / _THINK_PATTERN_NO_GT 同族实现（防漂移）：族一 = 开标签带
# 属性 + 闭合变体（<think type="x">…</think...>），族二 = 开始标签无 > 形态
# （<think␣…</think/>）。字面 <think> 单正则覆盖不了变体族，MiniMax 系恰是
# 变体标签的主要产出方，漏变体 = 整段草稿漏进提取面。
_THINK_BLOCK_RE = re.compile(
    r"<think[^>]*>(.*?)</think[^>]*>",
    re.DOTALL,
)
_THINK_BLOCK_NO_GT_RE = re.compile(
    r"<think\s(.*?)</think[^>]*>",
    re.DOTALL,
)
# 剥闭合块后残留开标签（流中断未闭合尾段）检测——同族宽口径：字面 <think> /
# 带属性 <think …> / 无 > 的 <think␣ 三形态全命中。注：闭合 <thinking>…</thinking>
# 会被 _THINK_BLOCK_RE 整块剥除（[^>]* 吸收 "ing"），但未闭合 <thinking> 残留
# 不被本正则截断——thinking 非思考标签族（canonical 同款正则亦然），整段
# 保留与误剥代价同口径（该轮漏提取，停更方向，无错误动作）。
_THINK_OPEN_RE = re.compile(r"<think(?:\s[^>]*)?>|<think(?=\s)")


def _strip_thinking(text: str) -> str:
    """剥 thinking/reasoning 段——提取面只扫正文最终输出（任务 AC）。

    think 标签族按 canonical 口径剥除（与 llm/adapter_minimax 同族实现）：
    闭合块多段剥除（含带属性开标签、``</think...>`` 闭合变体、开始标签无
    ``>`` 形态）；剥后残留的未闭合开标签（流中断半截）起全段视同思考剥至
    末尾。正文含字面 ``<think>`` 示例文本的误剥代价 = 该轮漏提取（停更方向，
    无错误动作），安全。
    """
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_BLOCK_NO_GT_RE.sub("", text)
    open_m = _THINK_OPEN_RE.search(text)
    if open_m:
        text = text[: open_m.start()]
    return text.strip()


# 与 todo_extract._ITEM_RE 同口径的行解析（注入面回读，避免跨模块私有正则导出）
_ITEM_LINE_RE = re.compile(r"^\s*-\s+\[([xX ])\]\s+(.*)$")
