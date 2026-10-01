"""审批决策契约——语义封闭枚举与交互错误码的单一真值源。

ADR 2026-10-01（权限审批语义收敛）：
- 「决定」从透明字符串升级为类型化契约。发起交互的插件建卡时为每个选项声明
  ``semantics``（封闭枚举）；human 服务在 respond 入口把任何词形（选项 id/label/
  历史别名）按创建方声明归一为该枚举；消费方（security_check/approval_service）
  只读语义，不做词形反推。
- 交互通道错误分类统一 ``error_code``（INTERACTION_*），消费方读码不复原消息
  子串——``"denied" in err_msg`` 式字符串嗅探随本契约退役。

枚举扩号纪律：新增成员必须先修订 ADR 2026-10-01（防枚举变第二张别名表）。

共享模块沉 SDK 的原因：human（生产方）与 security_check/approval（消费方）分属
三个插件，插件自包含约束下禁止跨插件 import，公共契约统一沉本包（editable 安装
下各插件即时可见）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "InteractionErrorCode",
    "OptionSemantics",
    "is_valid_semantics",
    "semantics_values",
]


class OptionSemantics(StrEnum):
    """选项语义封闭枚举（ADR 2026-10-01 决策 2）。

    - approve_once            : 批准，仅本次生效（最小授权）
    - approve_and_remember    : 批准并记忆（同指纹后续免审）
    - deny                    : 显式拒绝（唯一武装拒绝记忆的语义）
    - grant_write             : 授权写入，仅本管道/会话级
    - grant_write_permanent   : 授权写入，永久（名单文件）
    - grant_read              : 授权读取，仅本管道/会话级
    - grant_read_permanent    : 授权读取，永久（名单文件）
    - cancel                  : 取消（含未知词形的 fail-closed 归一落点）
    """

    APPROVE_ONCE = "approve_once"
    APPROVE_AND_REMEMBER = "approve_and_remember"
    DENY = "deny"
    GRANT_WRITE = "grant_write"
    GRANT_WRITE_PERMANENT = "grant_write_permanent"
    GRANT_READ = "grant_read"
    GRANT_READ_PERMANENT = "grant_read_permanent"
    CANCEL = "cancel"


def semantics_values() -> frozenset[str]:
    """全部合法语义词形（供建卡侧校验声明值）。"""
    return frozenset(s.value for s in OptionSemantics)


def is_valid_semantics(raw: object) -> bool:
    """声明值是否为封闭枚举成员。

    非法词形由建卡点丢弃 semantics 键（选项退化为无语义上卡），审批便利
    路由对无语义选项 fail-closed 拒绝——合法枚举成员是唯一可结算形态。
    """
    return isinstance(raw, str) and raw in semantics_values()


class InteractionErrorCode(StrEnum):
    """交互通道错误码（ADR 2026-10-01 决策 8）。

    human 服务的 wait_for_choice 经 capability 返回 ``{"error": ..., "error_code":
    ...}`` 时使用；消费方按码分类（超时/拒绝/取消），禁止对 error 消息做子串嗅探。
    """

    INTERACTION_TIMEOUT = "INTERACTION_TIMEOUT"
    INTERACTION_DENIED = "INTERACTION_DENIED"
    INTERACTION_CANCELLED = "INTERACTION_CANCELLED"
