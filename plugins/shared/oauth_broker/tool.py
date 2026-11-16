"""oauth_broker 工具实现（``oauth_authorize`` 跑授权流程 / ``oauth_status`` 查状态）。

错误是值：``OAuthError`` 在工具边界翻译成失败信封（``error_code`` 机器可读、
文本给人看）——不向内核抛未处理异常。凭据面：token / client_secret / code /
code_verifier 只出现在用户根 ``.env`` 与内存里，永不进返回值（只回键名与状态；
引导面只回 URL 与要手填的非敏感值）。
"""

from __future__ import annotations

from typing import Any

import flow
from oauth_protocol import OAuthError

from agentos_plugin_sdk import ToolExecutionResult, create_failure_result, create_success_result

__all__ = ["OAuthAuthorizeTool", "OAuthStatusTool"]


class OAuthAuthorizeTool:
    """``oauth_authorize``：确保目标 MCP 服务器已授权（有效/刷新/全量授权/引导注册）。"""

    def __init__(self, guidance: Any = None) -> None:
        #: 服务方引导页表（``plugin.get_config()["guidance"]``；装载期注入，缺省空表）
        self._guidance = guidance

    async def execute(self, args: dict[str, Any]) -> ToolExecutionResult:
        try:
            wait_seconds = _parse_wait(args.get("wait_seconds"))
            guidance_action = _parse_guidance_action(args.get("guidance_action"))
            result = await flow.authorize(
                server_url=str(args.get("server_url") or ""),
                client_id=_optional_text(args.get("client_id")),
                client_secret=_optional_text(args.get("client_secret")),
                scope=_optional_text(args.get("scope")),
                wait_seconds=wait_seconds,
                callback_base_url=_optional_text(args.get("callback_base_url")),
                resource_metadata_url=_optional_text(args.get("resource_metadata_url")),
                guidance=self._guidance,
                guidance_action=guidance_action,
            )
        except OAuthError as exc:
            return create_failure_result(str(exc), exc.code)
        return create_success_result(result)


class OAuthStatusTool:
    """``oauth_status``：列出已授权服务器状态（绝不回 token 值）。"""

    async def execute(self, args: dict[str, Any]) -> ToolExecutionResult:
        try:
            report = flow.status(server_url=_optional_text(args.get("server_url")))
        except OAuthError as exc:
            return create_failure_result(str(exc), exc.code)
        return create_success_result(report)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _parse_wait(value: Any) -> float:
    """``wait_seconds`` 归一边界：缺省用默认；非数字/负数/超上限 → OPTION_INVALID。"""
    if value is None:
        return flow.DEFAULT_WAIT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OAuthError("OPTION_INVALID", "wait_seconds 必须是数字（秒）")
    if value < 0:
        raise OAuthError("OPTION_INVALID", "wait_seconds 不能为负")
    if value > flow.MAX_WAIT_SECONDS:
        raise OAuthError("OPTION_INVALID", f"wait_seconds 超出上限 {int(flow.MAX_WAIT_SECONDS)} 秒")
    return float(value)


def _parse_guidance_action(value: Any) -> str:
    """``guidance_action`` 归一边界：缺省开注册页；白名单外 → OPTION_INVALID。"""
    text = _optional_text(value)
    if text is None:
        return flow.GUIDANCE_ACTION_REGISTER_APP
    if text not in flow.GUIDANCE_ACTIONS:
        raise OAuthError("OPTION_INVALID", f"guidance_action 只接受 {'/'.join(flow.GUIDANCE_ACTIONS)}")
    return text
