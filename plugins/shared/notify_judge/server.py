#!/usr/bin/env python3
"""notify_judge pipeline plugin MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py；本文件只做接口适配：invoke_entry notify_judge.execute
注册 + capability caller 注入（LLM 面唯一通道 llm.complete_stream，与
llm_core server 同款接线）。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import NotifyJudge, set_capability_caller, set_view_checker  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("notify_judge_pipeline")


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize notify_judge plugin：接线 LLM 调用通道 + 查看态查询通道。"""

    async def _call(method: str, params_: dict, timeout: float | None = None) -> Any:
        return await plugin.get_capability("tool-executor").call(method, params_, timeout)

    set_capability_caller(_call)

    async def _view_call(method: str, params_: dict, timeout: float | None = None) -> Any:
        return await plugin.get_capability("pipeline-executor").call(method, params_, timeout)

    set_view_checker(_view_call)


@plugin.tool(
    name="notify_judge.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {
                "type": "object",
                "default": {},
                "description": "注入配置（config_files settings 命名空间嵌套）",
            },
        },
        "required": ["state"],
    },
    description="Execute notify_judge pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """收尾通知判定：worthy=true 返回 messages set-op，否则零写入。"""
    try:
        judge = NotifyJudge(config=config)
        return {"state_updates": await judge.judge(state or {})}
    except Exception as exc:  # 降级契约：失败不阻断管道
        logger.warning("[notify_judge] 执行失败，零写入直通 | err=%s", exc)
        return {"state_updates": {}}


if __name__ == "__main__":
    plugin.run()
