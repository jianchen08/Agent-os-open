"""RP /cheat 命令守卫纯逻辑（RP 批 3；落点与确认形态决策见
docs/decisions/2026-10-11-rp-batch3-cheat-interception.md）。

语义真值 = ADR 2026-10-06（作者权威 + /cheat 显式命令）：on 需显式同意（一期
降级为二次输入确认——`/cheat on` 置 pending，`/cheat on confirm` 才生效，全程
审计）；off 恢复；status 幂等零副作用；allow_cheat=false（作者声明）全形态拒绝。

两块：
- ``parse_cheat_command``：用户消息文本 → 命令（/cheat 起始 token 文法；非命令
  = None 零拦截；/cheat 起始但形态非法 = action="invalid"，fail-closed 出用法
  回执而不漏给 LLM）；大小写敏感（约定小写，/CHEAT 是普通消息）；
- ``decide``：确认状态机纯函数——输入当前标志（cheating/pending）与作者声明
  （allow_cheat），输出回执文案 + 待写标志 + 审计行（含信封外的行体；落盘由
  插件层经 rp_state_audit.append_audit_rows 完成）。审计行只覆盖状态翻转与
  拒绝尝试（on 确认生效 / off / denied）；pending 置位与 status 不落行。

纯函数、零 IO、零 state 访问：输入输出皆是值，宿主（mode_material_inject 守卫）
负责 state 装配与落盘。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REASON_CHEAT_ON = "cheat_on"
REASON_CHEAT_OFF = "cheat_off"
REASON_CHEAT_DENIED = "cheat_denied"

RECEIPT_PREFIX = "【系统】"

_USAGE_RECEIPT = (
    f"{RECEIPT_PREFIX}/cheat 用法：/cheat on（开启，需确认）| /cheat on confirm"
    "（确认生效）| /cheat off（关闭）| /cheat status（查看状态）。"
)

_TOKEN_RE = re.compile(r"^/cheat$")


@dataclass(frozen=True)
class CheatCommand:
    """解析产物：action ∈ {on, off, status, invalid}；confirm 仅 on 有意义。"""

    action: str
    confirm: bool = False


@dataclass(frozen=True)
class CheatOutcome:
    """decide 产物：回执文案 + 待写标志 + 审计行体（None = 本轮无审计行）。"""

    receipt: str
    write_cheating: bool = False
    cheating: bool = False
    write_pending: bool = False
    pending: bool = False
    audit_row: dict[str, Any] | None = None


def parse_cheat_command(text: Any) -> CheatCommand | None:
    """用户消息文本 → CheatCommand；非 /cheat 命令 = None（普通消息零拦截）。

    文法：strip 后按空白分词，首词必须是 ``/cheat`` token（``/cheatfoo`` 不是
    命令）；`/cheat` 等价 `/cheat on`；`on confirm` 第三词生效；其余组合 =
    invalid（用法回执，不透传 LLM）。非字符串（多模态内容等）= None。
    """
    if not isinstance(text, str):
        return None
    tokens = text.strip().split()
    if not tokens or not _TOKEN_RE.match(tokens[0]):
        return None
    if len(tokens) == 1:
        return CheatCommand(action="on")
    sub = tokens[1]
    if len(tokens) == 2:
        if sub in ("on", "off", "status"):
            return CheatCommand(action=sub)
    elif len(tokens) == 3 and sub == "on" and tokens[2] == "confirm":
        return CheatCommand(action="on", confirm=True)
    return CheatCommand(action="invalid")


def _audit_row(action: str, cheating: bool, reason: str) -> dict[str, Any]:
    """命令审计行体：key=cheating 翻转/拒绝尝试（信封 ts/source/idem 落盘步补）。

    from = 当前值；to = 目标值（拒绝行 = 被拒绝的目标：on→True / off→False）。
    """
    return {
        "key": "cheating",
        "op": "=",
        "from": cheating,
        "to": action != "off",
        "reason": reason,
    }


def _status_receipt(cheating: bool, pending: bool, keep_display: str) -> str:
    if cheating:
        return (
            f"{RECEIPT_PREFIX}作弊状态：已开启（作者保留执法键域：{keep_display}）。"
            "输入 /cheat off 关闭。"
        )
    if pending:
        return (
            f"{RECEIPT_PREFIX}作弊状态：未开启（有待确认的 /cheat on——"
            "输 /cheat on confirm 生效或 /cheat off 取消）。"
        )
    return f"{RECEIPT_PREFIX}作弊状态：未开启。"


def decide(
    cmd: CheatCommand,
    *,
    cheating: bool,
    pending: bool,
    allow_cheat: Any,
    keep_display: str,
) -> CheatOutcome:
    """确认状态机（纯函数）：命令 + 当前标志 → 回执/待写标志/审计行。

    allow_cheat = 卡 engine 声明原值：显式 ``False`` = 作者禁用，on/off 全形态
    拒绝（回执指向作者声明；status 一并拒绝——作者关死 /cheat 面时状态信息也
    不外泄）并落拒绝审计（status 除外——零副作用零审计）。其余值（缺省/True/
    非布尔）= 允许（禁用须作者显式声明；非布尔由 lint 面警告）。
    """
    if cmd.action == "invalid":
        return CheatOutcome(receipt=_USAGE_RECEIPT)
    if allow_cheat is False:
        receipt = (
            f"{RECEIPT_PREFIX}/cheat 已被本卡作者声明禁用（allow_cheat: false），"
            "无法执行。"
        )
        row = (
            _audit_row(cmd.action, cheating, REASON_CHEAT_DENIED)
            if cmd.action != "status"
            else None
        )
        return CheatOutcome(receipt=receipt, audit_row=row)

    if cmd.action == "status":
        return CheatOutcome(
            receipt=_status_receipt(cheating, pending, keep_display)
        )

    if cmd.action == "off":
        if cheating:
            return CheatOutcome(
                receipt=(
                    f"{RECEIPT_PREFIX}作弊模式已关闭，状态校验恢复（作者保留键域"
                    "执法照旧）。"
                ),
                write_cheating=True,
                write_pending=True,
                audit_row=_audit_row("off", True, REASON_CHEAT_OFF),
            )
        receipt = (
            f"{RECEIPT_PREFIX}待确认的开启请求已取消；作弊状态：未开启。"
            if pending
            else f"{RECEIPT_PREFIX}本会话未开启作弊模式。"
        )
        return CheatOutcome(
            receipt=receipt,
            write_pending=True,  # off 顺带清 pending（取消路径）
        )

    # action == "on"
    if cheating:
        return CheatOutcome(
            receipt=f"{RECEIPT_PREFIX}已处于作弊模式，无需重复开启。",
            write_pending=True,  # 清残留 pending（幂等）
        )
    if not cmd.confirm:
        return CheatOutcome(
            receipt=(
                f"{RECEIPT_PREFIX}收到 /cheat on。开启作弊需再次确认：请再输入一次"
                "「/cheat on confirm」生效（输入 /cheat off 取消）。"
            ),
            write_pending=True,
            pending=True,
        )
    if not pending:
        return CheatOutcome(
            receipt=(
                f"{RECEIPT_PREFIX}没有待确认的开启请求：请先输入 /cheat on。"
            ),
        )
    return CheatOutcome(
        receipt=(
            f"{RECEIPT_PREFIX}作弊模式已开启：本会话状态校验已旁路（作者保留键域"
            f"{keep_display}仍执法），状态卡将带「本会话已作弊」徽标，全程审计。"
            "输入 /cheat off 关闭。"
        ),
        write_cheating=True,
        cheating=True,
        write_pending=True,
        audit_row=_audit_row("on", False, REASON_CHEAT_ON),
    )
