"""RP 状态审计流水 append 单源 + 事件语义面（RP 批 2-Ⅰ 引入机制，批 3 提取
共享防两处漂移；缺口批次二补事件粒度——§7.4 沉淀回路的输入维度）。

两个写方共享同一文件与幂等契约（文件 = 会话工作空间 ``<ws>/rp_state_audit.jsonl``，
accepted/rejected/cheat 同流水）：

- state_marker_parse（落账步）：idem = ``{pipeline_id}:{尾条 assistant 消息 seq}``，
  缺省 source=state_marker（作弊轮 meta.cheating 在场时 = cheat；回退开关
  allow_state_proposals 开启时 = llm——2026-10-13 动作驱动基线；behavior /
  action / state_field_ignored 行自带行级 source 覆盖缺省）；
- mode_material_inject 守卫（RP 批 3 /cheat 命令行）：idem =
  ``{pipeline_id}:cheat:{用户消息 seq}``，source=cheat——与落账步同一扫描幂等机制、
  不同 idem 空间（"cheat" 中缀不与数字 seq 相撞）。

追加幂等：写前扫描文件已含同 idem 行 = 整批跳过（同轮重放不双记；坏行跳过不影
响判定）。ws 不可得 / IO 失败 = warning + 返回 False，绝不抛出（审计降级不阻断
调用方——调用方自行决定是否把审计失败升级为业务失败，如 /cheat on 无审计不生效）。

事件语义（缺口批次二）：写面逐行补 ``actor/action/target/summary`` 四件套——
actor 从行来源推导（behavior 名 > 玩家动作 > 作弊 > ``narrator`` 缺省）、
action = 行内声明动作 id 保留或 op 动词映射、target = 变更键、summary 一行
可读（被拒行——layer 由 Validator 只盖在拒绝行上——带「（被拒 <reason>）」
后缀）。读面 ``recent_events`` 返回文件序最后 K 条完整事件行，供下轮动态
注入源（§7.5 #5 世界记忆事件粒度）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 事件 actor 缺省（叙述性变更——标记/llm/init/快照等无更具体行动者时归
# 叙述者；声明动作/行为在场时 actor 取玩家/行为名，见 _event_actor）。
EVENT_ACTOR_DEFAULT = "narrator"
EVENT_ACTOR_PLAYER = "player"
EVENT_ACTOR_CHEAT = "cheat"

# op 动词映射（事件 action 面）；op 不承载的记录行（state_field_ignored 等
# 未结算观察）= observe。
_EVENT_OP_ACTIONS = {"+": "increase", "-": "decrease", "=": "set"}
_EVENT_UNSETTLED_ACTION = "observe"
# 事件 summary 值段截断上限（行值假定可序列化，与信封写面同一前提）。
_EVENT_VALUE_MAX_CHARS = 40


def last_assistant_seq(state: dict[str, Any]) -> Any:
    """尾条 assistant 消息 seq（审计 idem 派生用；无消息/形态非法 = None）。"""
    messages = state.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg.get("seq")
    return None


def story_idem(pipeline_id: str, seq: Any) -> str:
    """story 域审计批基式幂等键 = ``{pipeline_id}:{seq}``（seq 缺席 = "-" 占位）。

    幂等键格式单源（评审修复刀自 state_marker_parse 两处派生点下沉）——
    story 批与 game 域批（``suffixed`` 追加域后缀）同调本基式，防两处漂移。
    """
    return f"{pipeline_id}:{seq if seq is not None else '-'}"


def suffixed(base: str, suffix: str) -> str:
    """基式 idem 追加域后缀（game 域批与 story 批分幂等空间，"cheat" 类中缀
    不与数字 seq 相撞）。"""
    return f"{base}{suffix}"


def has_idem(workspace: Any, idem: str) -> bool:
    """审计文件已含同 idem 行（写前探针；ws 无效/文件缺失/读取失败 = False）。

    与 append_audit_rows 共用同一扫描判定（_file_has_idem 单源）——幂等门与
    幂等写同空间（RP 面板 v3.1 刀1 init effects 会话首轮防重放闸的探针面）。
    """
    if not isinstance(workspace, str) or not workspace:
        return False
    return _file_has_idem(Path(workspace) / "rp_state_audit.jsonl", idem)


def _file_has_idem(audit_path: Path, idem: str) -> bool:
    """单文件 idem 扫描（追加幂等与探针共用；坏行跳过不影响判定）。"""
    if not audit_path.is_file():
        return False
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # 坏行跳过（不影响幂等判定）
            if isinstance(row, dict) and row.get("idem") == idem:
                return True
    except OSError:
        return False
    return False


def _event_actor(row: dict[str, Any]) -> str:
    """actor 推导：行为名 > 玩家动作（source=action/operation——自由操作命令是
    玩家 Command，与 NPC Decision 同一校验同一归属，世界内嵌批次二）> 作弊 >
    narrator 缺省。"""
    behavior = row.get("behavior")
    if isinstance(behavior, str) and behavior:
        return behavior
    source = row.get("source")
    if source in ("action", "operation"):
        return EVENT_ACTOR_PLAYER
    if source == "cheat":
        return EVENT_ACTOR_CHEAT
    return EVENT_ACTOR_DEFAULT


def _event_action(row: dict[str, Any]) -> str:
    """action 推导：动作通道行保留声明动作 id（行内 action 键即事件动作）；
    其余按 op 映射变更动词；op 不承载的记录行 = observe。"""
    action = row.get("action")
    if isinstance(action, str) and action:
        return action
    op = row.get("op")
    if not isinstance(op, str):
        return _EVENT_UNSETTLED_ACTION
    return _EVENT_OP_ACTIONS.get(op, _EVENT_UNSETTLED_ACTION)


def _event_value_text(value: Any) -> str:
    """事件值段文本（JSON 序列化 + 截断——摘要可读不吞真）。"""
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > _EVENT_VALUE_MAX_CHARS:
        return text[:_EVENT_VALUE_MAX_CHARS] + "…"
    return text


def _event_summary(row: dict[str, Any]) -> str:
    """一行可读摘要：``<actor> <action> <target>: <from>→<to>``（未结算行无
    变更链）；被拒行（layer 在场）追加「（被拒 <reason>）」。"""
    actor, action, target = row["actor"], row["action"], row.get("key")
    if row.get("op") is None:
        base = f"{actor} {action} {target}: {_event_value_text(row.get('to'))}"
    else:
        base = (
            f"{actor} {action} {target}: "
            f"{_event_value_text(row.get('from'))}→{_event_value_text(row.get('to'))}"
        )
    if row.get("layer") is not None:
        base += f"（被拒 {row.get('reason')}）"
    return base


def append_audit_rows(
    workspace: Any, rows: list[dict[str, Any]], idem: str, default_source: str
) -> bool:
    """审计行 append 到 ``<ws>/rp_state_audit.jsonl``；返回是否本批落盘。

    行信封 = ``{ts, source, idem, **row}``（行级 source/同名字段覆盖缺省）；
    信封合并后逐行补事件语义四件套（actor/action/target/summary，见模块
    docstring 事件语义节）。ws 非字符串或空 = False；文件已含同 idem = False
    （幂等跳过）；IO 失败 = warning + False。rows 为空表 = 不落盘返回 True
    （无事可记不算失败）。
    """
    if not isinstance(workspace, str) or not workspace:
        logger.warning(
            "[rp_state_audit] state.workspace 缺失，审计跳过（降级不阻断）"
        )
        return False
    try:
        audit_path = Path(workspace) / "rp_state_audit.jsonl"
        if _file_has_idem(audit_path, idem):
            logger.info(
                "[rp_state_audit] 审计幂等命中，同轮重放不双记 | idem=%s", idem
            )
            return False
        if not rows:
            return True
        ts = datetime.now(UTC).isoformat()
        with open(audit_path, "a", encoding="utf-8") as f:
            for row in rows:
                full_row = {"ts": ts, "source": default_source, "idem": idem, **row}
                full_row["actor"] = _event_actor(full_row)
                full_row["action"] = _event_action(full_row)
                full_row["target"] = full_row.get("key")
                full_row["summary"] = _event_summary(full_row)
                f.write(json.dumps(full_row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        logger.warning(
            "[rp_state_audit] 审计写入失败，跳过（降级不阻断）| ws=%s | err=%s",
            workspace,
            exc,
        )
        return False


def recent_events(workspace: Any, limit: int = 8) -> list[dict[str, Any]]:
    """近 K 条事件（审计文件尾部读取面，K 缺省 8——§7.5 #5 近 K=8 条事件）。

    返回文件序（发生序）最后 ``limit`` 条完整事件行（含信封与写面补齐的
    actor/action/target/summary）。ws 无效 / 文件缺失 / 读取失败 / limit<1 =
    空表（fail-soft，与写面降级同口径，不抛不猜）；坏行跳过不占窗口。
    """
    if not isinstance(workspace, str) or not workspace or limit < 1:
        return []
    audit_path = Path(workspace) / "rp_state_audit.jsonl"
    if not audit_path.is_file():
        return []
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # 坏行跳过（不占 K 窗口）
        if isinstance(row, dict):
            events.append(row)
    return events[-limit:]
