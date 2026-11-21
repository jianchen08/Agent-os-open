"""ecosystem_import 工具实现（``ecosystem_translate`` 只读翻译 / ``ecosystem_apply`` 落地）。

错误是值：翻译/落地异常在工具边界翻译成 ``create_failure_result`` 失败信封
（``error_code`` 机器可读、``error`` 给人看），不向内核抛未处理异常。

两个工具的能力面下沉既有通道：技能落用户根 ``skills/``（``skill_manager`` 注册
面可见）；MCP 服务器默认只报事实位，``apply_mcp=true`` 时由 ``ecosystem_apply``
把非 OAuth 条目合并进当前配置根（OAuth 条目需授权能力，暂不写）。

``kind`` 分派两家前端：``codex``（A 批）与 ``claude_code``（B 批，三形态自动识别，
``entry`` 选市场条目）。``allow_undeclared_license`` / ``apply_mcp`` 原值透传给落地层
校验（不在此做真值转换——``"false"`` 的布尔转换是 True，静默转换会破坏 fail-closed）。

落地段（``apply_plugin``：整树复制/覆盖换入/来源快照）是纯同步文件操作，必须
``asyncio.to_thread`` 下放——async handler 在合宿宿主事件循环里执行，同步阻塞
超 watchdog 阈值会拖整组宿主自杀。
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_translator import translate_claude_code
from codex_translator import TranslationError, translate_codex
from import_ir import PluginIR
from license_gate import check_license
from skill_landing import (
    ON_SKILL_REJECT_ABORT,
    ApplyReport,
    LandError,
    apply_plugin,
)

from agentos_plugin_sdk import ToolExecutionResult, create_failure_result, create_success_result
from agentos_plugin_sdk.diagnostics_settings import user_root

__all__ = ["EcosystemApplyTool", "EcosystemTranslateTool"]

SUPPORTED_KINDS: tuple[str, ...] = ("codex", "claude_code")
MCP_INSTALL_NOTE = "MCP 服务器需安装后才可用：ecosystem_apply 加 apply_mcp=true 合并进配置根（OAuth 条目本批不写）"
APPS_NOTE = "apps 不可移植（宿主托管应用，本仓无对应物），本批不导入"


class EcosystemTranslateTool:
    """``ecosystem_translate``：外部插件清单 → IR 摘要（只读、零写入、零子进程）。"""

    async def execute(self, args: dict[str, Any]) -> ToolExecutionResult:
        source_dir = str(args.get("source_dir") or "").strip()
        kind = str(args.get("kind") or "codex").strip()
        entry = str(args.get("entry") or "").strip() or None
        # 空串兜底：Path("") == "." 会把进程 cwd 当源目录，必须显式拒
        if not source_dir:
            return create_failure_result("source_dir 不能为空", "SOURCE_NOT_FOUND")
        if kind not in SUPPORTED_KINDS:
            return create_failure_result(
                f"未支持的来源类型：{kind}（本批支持：{'/'.join(SUPPORTED_KINDS)}）",
                "KIND_UNSUPPORTED",
            )
        try:
            ir = _translate(kind, source_dir, entry)
        except TranslationError as exc:
            return create_failure_result(str(exc), exc.code)
        return create_success_result(_translate_summary(ir))


class EcosystemApplyTool:
    """``ecosystem_apply``：翻译 + 治理闸门 + 技能落地用户根 + 溯源登记（显式调用才写）。"""

    async def execute(self, args: dict[str, Any]) -> ToolExecutionResult:
        source_dir = str(args.get("source_dir") or "").strip()
        kind = str(args.get("kind") or "codex").strip()
        entry = str(args.get("entry") or "").strip() or None
        force = bool(args.get("force") or False)
        on_skill_reject = str(args.get("on_skill_reject") or ON_SKILL_REJECT_ABORT).strip()
        allow_undeclared_license = args.get("allow_undeclared_license", False)
        apply_mcp = args.get("apply_mcp", False)
        if not source_dir:
            return create_failure_result("source_dir 不能为空", "SOURCE_NOT_FOUND")
        if kind not in SUPPORTED_KINDS:
            return create_failure_result(
                f"未支持的来源类型：{kind}（本批支持：{'/'.join(SUPPORTED_KINDS)}）",
                "KIND_UNSUPPORTED",
            )
        try:
            ir = _translate(kind, source_dir, entry)
            report = await asyncio.to_thread(
                apply_plugin,
                ir,
                user_root=user_root(),
                force=force,
                on_skill_reject=on_skill_reject,
                allow_undeclared_license=allow_undeclared_license,
                apply_mcp=apply_mcp,
            )
        except TranslationError as exc:
            return create_failure_result(str(exc), exc.code)
        except LandError as exc:
            return create_failure_result(str(exc), exc.code)
        return create_success_result(_apply_summary(ir, report))


def _translate(kind: str, source_dir: str, entry: str | None) -> PluginIR:
    """按来源类型分派前端（``entry`` 仅 Claude Code 市场条目形态使用）。"""
    if kind == "claude_code":
        return translate_claude_code(source_dir, entry=entry)
    return translate_codex(source_dir)


def _translate_summary(ir: PluginIR) -> dict[str, Any]:
    """IR 的可读摘要（对外契约；MCP 只报事实位，凭据/端点不入摘要）。"""
    gate = check_license(ir.license, license_file=ir.license_file)
    notes: list[str] = []
    if ir.mcp_servers:
        notes.append(MCP_INSTALL_NOTE)
    if ir.apps:
        notes.append(APPS_NOTE)
    return {
        "kind": ir.kind,
        "source_dir": str(ir.source_dir),
        "plugin": {
            "name": ir.name,
            "version": ir.version,
            "description": ir.description,
            "author": ir.author,
            "license": ir.license,
        },
        "skills": [
            {"skill_id": skill.skill_id, "name": skill.name, "description": skill.description} for skill in ir.skills
        ],
        "mcp_servers": [
            {"name": server.name, "transport": server.transport, "requires_oauth": server.requires_oauth}
            for server in ir.mcp_servers
        ],
        "apps": [{"name": app.name, "app_id": app.app_id, "portable": False} for app in ir.apps],
        "agents": [{"name": item.name, "model": item.model, "tools_count": item.tools_count} for item in ir.agents],
        "commands": [{"name": item.name} for item in ir.commands],
        "hooks": {"declared": ir.hooks_declared, "events": list(ir.hooks_events)},
        "license_file": ir.license_file,
        "license_gate": {"allowed": gate.allowed, "license": gate.license, "reason": gate.reason},
        "notes": notes,
    }


def _apply_summary(ir: PluginIR, report: ApplyReport) -> dict[str, Any]:
    """落地结果摘要：写入路径 + 被拒技能 + 登记册路径 + 豁免留痕 + MCP 写面（只报名与原因）。"""
    mcp = report.mcp
    written = mcp.written if mcp is not None else ()
    skipped = mcp.skipped if mcp is not None else ()
    return {
        "plugin": {"name": ir.name, "version": ir.version, "license": ir.license},
        "skills_written": [{"skill_id": item.skill_id, "path": str(item.path)} for item in report.skills],
        "skills_rejected": [
            {"skill_id": item.skill_id, "error_code": item.code, "reason": item.reason} for item in report.rejected
        ],
        "provenance_path": str(report.provenance_path),
        "license_override": report.license_override,
        "mcp": {
            "written": [{"name": item.name, "transport": item.transport} for item in written],
            "skipped": [{"name": item.name, "reason": item.reason} for item in skipped],
        },
    }
