#!/usr/bin/env python3
"""plugin_verify 工具 MCP 服务端（接口适配层）。

工具只做一件事：把"验证某个工作空间内插件"的请求交给固定脚本
`skills/plugin-dev/scripts/verify_host.py`（安装位内、SHA-256 pin 锁定）。
参数面闭集：plugin_path / scope / timeout_s；workspace 由内核/管道注入。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from verifier import VerifierError, run_plugin_verification  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("plugin_verifier")

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plugin_path": {
            "type": "string",
            "description": "插件目录（当前工作空间相对路径，如 plugins/shared/my_plugin）；不接受绝对路径或 ..",
        },
        "scope": {
            "type": "string",
            "enum": ["backend", "frontend"],
            "default": "backend",
            "description": "backend=scratch 内核装载 + 工具面/schema 断言；frontend=再跑真前端 Playwright 截图（慢，UI 改动时用）",
        },
        "timeout_s": {
            "type": "integer",
            "default": 240,
            "minimum": 60,
            "maximum": 600,
            "description": "整体超时秒数（超时杀整棵验证进程树）；后端一般 60-120，前端建议 240+",
        },
    },
    "required": ["plugin_path"],
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "status": {"type": "string", "enum": ["completed", "failed", "timeout", "error"]},
        "plugin_id": {"type": "string"},
        "scope": {"type": "string"},
        "all_pass": {"type": "boolean"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
            },
        },
        "screenshots": {"type": "array", "items": {"type": "string"}},
        "evidence_path": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    },
    "required": ["success", "status", "plugin_id", "scope", "all_pass"],
}


@plugin.tool(
    name="plugin_verify",
    schema=_INPUT_SCHEMA,
    description="验证当前工作空间内的插件：起一个 scratch 内核实例（端口/DB/用户空间三隔离，"
    "不碰现役内核）装载插件，断言工具面与 schema，可选真前端 Playwright 截图。"
    "改动插件后调用；plugin_path 用工作空间相对路径。返回逐项 checks、证据 JSON 路径与截图路径；"
    "失败时 error 给出原因。",
    output_schema=_OUTPUT_SCHEMA,
    render={"card": "generic"},
)
async def plugin_verify(
    plugin_path: str = "",
    scope: str = "backend",
    timeout_s: int = 240,
    workspace: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """plugin_verify 入口：结构化错误在工具边界收口，不向 MCP 抛裸异常。"""
    del kwargs  # 内核注入的其余上下文（project_root/config 等）本工具不消费
    try:
        # 验证体是纯同步实现（子进程 communicate(timeout=timeout_s) + 清理退避
        # time.sleep），整体出事件循环——最长 600s 不得冻宿主循环。
        return await asyncio.to_thread(
            run_plugin_verification, workspace, plugin_path, scope, timeout_s
        )
    except VerifierError as exc:
        return {
            "success": False,
            "status": "error",
            "plugin_id": "",
            "scope": str(scope or ""),
            "all_pass": False,
            "checks": [],
            "screenshots": [],
            "evidence_path": None,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - 工具边界必须回结构化错误
        logger.exception("[plugin_verifier] 验证执行异常")
        return {
            "success": False,
            "status": "error",
            "plugin_id": "",
            "scope": str(scope or ""),
            "all_pass": False,
            "checks": [],
            "screenshots": [],
            "evidence_path": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


if __name__ == "__main__":
    plugin.run()
