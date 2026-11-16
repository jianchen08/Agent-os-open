"""ACP 派发引擎 —— 按 provider backend 派发并收回结果，统一归类记账。

两种后端（providers.toml `backend` 字段）：

- ``acp``：spawn provider CLI → ACP 握手 → 会话 → prompt → 收回合。客户端走
  官方 agent-client-protocol SDK（PyPI 发行名 ``agent-client-protocol``，导入名
  ``acp``），``spawn_agent_process`` 托管子进程 stdio JSON-RPC（ndjson）。许可
  策略 = 派发即委托——cwd 即授权边界（对齐任务隔离执行口径），对
  request_permission 选首个 allow_once 档，无则拒绝。
- ``oneshot``：一次性 CLI（如 zcode ``-p``）——spawn 命令并把派发目标全文替换进
  args 的 ``{goal}`` 占位符，cwd 即工作目录，stdout 收结果。无会话语义：结果按
  无 session_id 记账（账本 one-shot 口径），``--session-id`` 续做请求一律
  ValueError 拒绝（配置面，向上传播）。stdout 容错解析：优先 JSON ``response``
  字段，非 JSON 时回退原文——one-shot CLI 错误面常非 JSON。

失败语义：后端执行面错误在引擎内归类记账后转 ``status="failed"`` 出参（四键
provider/session_id/status/summary）；配置面错误（账本/注册表损坏、oneshot 上
请求会话续做）向上传播，由调用面如实报错。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from typing import Any

import acp.utils
from acp import PROTOCOL_VERSION, RequestError, RequestPermissionResponse, spawn_agent_process, text_block
from acp.interfaces import Client
from acp.schema import AllowedOutcome, DeniedOutcome
from acp_ledger import QuotaLedger
from acp_registry import BACKEND_ONESHOT, GOAL_PLACEHOLDER, ProviderSpec
from pydantic import BaseModel

__all__ = ["DispatchRequest", "dispatch", "classify_error"]

_SUMMARY_MAX_CHARS = 8000


# ── SDK 0.12.1 线缆违约补丁（显性技术债务）────────────────────────────
# acp.utils.serialize_params 用 exclude_defaults=True，把 ContentBlock.type 等
# 判别字段（Literal 默认值）剔出线缆——违反 ACP 线缆规范（判别字段必填），
# 宽容 peer（opencode/TS，zod 填默认）能过、严格 pydantic peer 必拒
# （fake agent 实证 Invalid params）。本补丁按线缆规范保留默认值字段。
# 升级触发：SDK 升级 ≥1.0 stable 时复验上游是否已修（grep exclude_defaults），
# 已修则删除本补丁。
def _wire_compliant_serialize(params: BaseModel) -> dict[str, Any]:
    return params.model_dump(mode="json", by_alias=True, exclude_none=True)


acp.utils.serialize_params = _wire_compliant_serialize


@dataclass(frozen=True)
class DispatchRequest:
    """一次派发：goal 必填；session_id 空 = 新会话，非空 = 续做既有会话。"""

    goal: str
    cwd: str
    session_id: str = ""


def classify_error(exc: BaseException) -> str:
    """把派发异常归类为账本错误类（额度墙判定词汇见 acp_ledger.WALL_CLASSES）。"""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, PermissionError)):
        return "spawn_failure"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    message = str(exc).lower()
    quota_markers = ("quota", "rate limit", "429", "usage limit", "credit", "billing")
    if any(marker in message for marker in quota_markers):
        return "quota_or_rate_limit"
    auth_markers = ("unauthorized", "401", "403", "api key", "credentials", "authentication")
    if any(marker in message for marker in auth_markers):
        return "auth"
    if isinstance(exc, RequestError):
        return "protocol"
    return "protocol_or_agent"


class _TurnCollector(Client):
    """收集本回合 agent 文本增量；许可请求按派发委托口径选 allow_once。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        text = getattr(getattr(update, "content", None), "text", None)
        if isinstance(text, str):
            self.chunks.append(text)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        for option in options or []:
            if getattr(option, "kind", None) == "allow_once" and getattr(option, "option_id", None):
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=option.option_id)
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _result(provider_id: str, session_id: str, status: str, summary: str) -> dict[str, str]:
    return {
        "provider": provider_id,
        "session_id": session_id,
        "status": status,
        "summary": summary[:_SUMMARY_MAX_CHARS],
    }


def _resolve_command(command: str) -> str:
    """PATH 解析（Windows 下 npm shim/无扩展名件不一定是可 CreateProcess 的真身）。"""
    return shutil.which(command) or command


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _oneshot_summary(stdout: str) -> str:
    """one-shot stdout 容错解析：JSON 时取 ``response`` 字段，否则回退原文。"""
    stripped = stdout.strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(data, dict) and isinstance(data.get("response"), str):
        return data["response"].strip()
    return stripped


async def _dispatch_oneshot(
    request: DispatchRequest, spec: ProviderSpec, ledger: QuotaLedger
) -> dict[str, str]:
    """执行一次 one-shot CLI 派发；无会话语义，结果按无 session_id 记账。"""
    argv = [arg.replace(GOAL_PLACEHOLDER, request.goal) for arg in spec.args]
    proc = await asyncio.create_subprocess_exec(
        _resolve_command(spec.command),
        *argv,
        cwd=request.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_seconds)
    except TimeoutError:  # asyncio.TimeoutError 自 3.11 起为内建别名
        proc.kill()
        await proc.wait()
        raise
    stdout = _decode(stdout_raw)
    stderr = _decode(stderr_raw)
    summary = _oneshot_summary(stdout)
    if proc.returncode != 0 or not summary:
        detail = (stderr.strip() or stdout.strip() or "(无输出)")[:2000]
        raise RuntimeError(f"one-shot exit={proc.returncode}: {detail}")
    # one-shot 口径：无会话可续，账本 session_id 恒空（与 ACP 家区分）。
    ledger.record_success(spec.id, session_id="", summary=summary)
    return _result(spec.id, "", "ok", summary)


async def _dispatch_acp(
    request: DispatchRequest, spec: ProviderSpec, ledger: QuotaLedger
) -> dict[str, str]:
    """执行一次 ACP 派发回合（握手 → 会话 → prompt → 收回合）。"""
    # SDK Client 基类回调全为空体（@compatible_class 运行时语义），mypy 判抽象
    # 误报——只覆写 session_update/request_permission 即为完整客户端。
    collector = _TurnCollector()  # type: ignore[abstract]
    async with spawn_agent_process(
        collector, _resolve_command(spec.command), *spec.args
    ) as (conn, _proc):
        await conn.initialize(protocol_version=PROTOCOL_VERSION)
        if request.session_id:
            # 0.12.1 LoadSessionResponse 不携带 session_id（后续协议版本才加）：
            # 续做会话 id = 请求值，prompt 直接复用。
            await conn.load_session(cwd=request.cwd, session_id=request.session_id)
            session_id = request.session_id
        else:
            created = await conn.new_session(cwd=request.cwd, mcp_servers=[])
            session_id = created.session_id
        response = await asyncio.wait_for(
            conn.prompt(session_id=session_id, prompt=[text_block(request.goal)]),
            timeout=spec.timeout_seconds,
        )
        text = "".join(collector.chunks)
        stop_reason = getattr(response, "stop_reason", "")
        if stop_reason == "end_turn":
            ledger.record_success(spec.id, session_id=session_id, summary=text)
            return _result(spec.id, session_id, "ok", text)
        summary = f"(stop_reason={stop_reason}) {text}"
        ledger.record_failure(spec.id, error_class="agent_stop", message=summary)
        return _result(spec.id, session_id, "failed", summary)


async def dispatch(
    request: DispatchRequest, spec: ProviderSpec, ledger: QuotaLedger
) -> dict[str, str]:
    """按 provider backend 派发一次；结果恒为四键（provider/session_id/status/summary）。"""
    if spec.backend == BACKEND_ONESHOT and request.session_id:
        # 配置面错误：one-shot 后端无会话语义，续做请求如实拒绝（不记账不静默忽略）。
        raise ValueError(f"provider '{spec.id}' 为 oneshot 后端（无会话），不支持 session_id 续做")
    try:
        if spec.backend == BACKEND_ONESHOT:
            return await _dispatch_oneshot(request, spec, ledger)
        return await _dispatch_acp(request, spec, ledger)
    except Exception as exc:  # noqa: BLE001 —— 归类记账后转 failed 出参（错误是值）
        error_class = classify_error(exc)
        message = f"{type(exc).__name__}: {exc}"
        ledger.record_failure(spec.id, error_class=error_class, message=message)
        return _result(spec.id, "", "failed", message)
