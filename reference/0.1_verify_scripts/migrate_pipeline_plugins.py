#!/usr/bin/env python3
"""批量迁移 47 个管道插件到 plugins/shared/pipeline/。

源：src/plugins/shared/{input|output|core}/{name}/plugin.py
目标：plugins/shared/pipeline/{input|output|core}/{name}/{plugin.py,server.py,plugin.json}
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# ── 插件清单： (name, class_name, category, display_name) ──────────────────

PLUGINS: list[tuple[str, str, str, str]] = [
    # ── Input (21) ──
    ("circuit_breaker",        "CircuitBreaker",           "input", "Circuit Breaker"),
    ("context_build",          "ContextBuildPlugin",       "input", "Context Build"),
    ("context_window_guard",   "ContextWindowGuardPlugin", "input", "Context Window Guard"),
    ("cost_control",           "CostControlPlugin",        "input", "Cost Control"),
    ("injected_param_validator","InjectedParamValidator",  "input", "Injected Param Validator"),
    ("isolation_guard",        "IsolationGuard",           "input", "Isolation Guard"),
    ("knowledge_inject",       "KnowledgeInjectPlugin",    "input", "Knowledge Inject"),
    ("level_guard",            "LevelGuardPlugin",         "input", "Level Guard"),
    ("memory_read",            "MemoryReadPlugin",         "input", "Memory Read"),
    ("multimodal_preprocessor","MultimodalPreprocessor",   "input", "Multimodal Preprocessor"),
    ("param_inject",           "ParamInjectPlugin",        "input", "Param Inject"),
    ("pause_guard",            "PauseGuardPlugin",         "input", "Pause Guard"),
    ("prompt_build",           "PromptBuildPlugin",        "input", "Prompt Build"),
    ("reasoning_check",        "ReasoningCheckPlugin",     "input", "Reasoning Check"),
    ("security_check",         "SecurityCheckPlugin",      "input", "Security Check"),
    ("task_event_receiver",    "TaskEventReceiverPlugin",  "input", "Task Event Receiver"),
    ("tool_cache",             "ToolCache",                "input", "Tool Cache"),
    ("tool_call_guard",        "ToolCallGuard",            "input", "Tool Call Guard"),
    ("tool_context",           "ToolContextPlugin",        "input", "Tool Context"),
    ("tool_schema",            "ToolSchemaPlugin",         "input", "Tool Schema"),
    ("tool_schema_validator",  "ToolSchemaValidator",      "input", "Tool Schema Validator"),
    # ── Output (20) ──
    ("approval_view_route",    "ApprovalViewRoutePlugin",  "output", "Approval View Route"),
    ("child_task_guard",       "ChildTaskGuard",           "output", "Child Task Guard"),
    ("conversation_mode",      "ConversationModeDetector", "output", "Conversation Mode"),
    ("delegate_depth_guard",   "DelegateDepthGuardPlugin", "output", "Delegate Depth Guard"),
    ("duplicate_check",        "DuplicateCheckPlugin",     "output", "Duplicate Check"),
    ("error_check",            "ErrorCheckPlugin",         "output", "Error Check"),
    ("event_callback",         "EventCallbackPlugin",      "output", "Event Callback"),
    ("experience_consolidator","ExperienceConsolidatorPlugin","output","Experience Consolidator"),
    ("fire_and_forget",        "FireAndForgetPlugin",      "output", "Fire And Forget"),
    ("llm_error_recovery",     "LLMErrorRecoveryPlugin",   "output", "LLM Error Recovery"),
    ("multimodal_postprocessor","MultimodalPostprocessor", "output", "Multimodal Postprocessor"),
    ("output_repetition_guard","OutputRepetitionGuard",    "output", "Output Repetition Guard"),
    ("pending_tools",          "PendingToolsOutput",       "output", "Pending Tools"),
    ("result_format",          "ResultFormatPlugin",       "output", "Result Format"),
    ("sensitive_checker",      "SensitiveChecker",         "output", "Sensitive Checker"),
    ("stop_check",             "StopCheckPlugin",          "output", "Stop Check"),
    ("stuck_detector",         "StuckDetector",            "output", "Stuck Detector"),
    ("task_reminder",          "TaskReminder",             "output", "Task Reminder"),
    ("tool_progress",          "ToolProgressReporter",     "output", "Tool Progress"),
    ("track",                  "TrackPlugin",              "output", "Track"),
    # ── Core (3) ──
    ("llm_core",               "LLMCore",                  "core",  "LLM Core"),
    ("stream_repeat_monitor",  "StreamRepetitionMonitor",   "core",  "Stream Repeat Monitor"),
    ("tool_core",              "ToolCore",                 "core",  "Tool Core"),
]

# ── 路径常量 ──────────────────────────────────────────────

SRC_BASE = Path("src/plugins/shared")
DST_BASE = Path("plugins/shared/pipeline")


# ── server.py 模板 ────────────────────────────────────────

SERVER_TEMPLATE_PIPELINE = '''#!/usr/bin/env python3
"""{name} {category} pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/{category}/{name}/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import {ClassName}  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("{name}_pipeline")

_instance: {ClassName} | None = None


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize {name} plugin."""
    global _instance
    config = plugin.get_config()
    _instance = {ClassName}(config=config)


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup {name} plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="{name}.execute",
    schema={{
        "type": "object",
        "properties": {{
            "state": {{"type": "object", "description": "Pipeline state dict"}},
            "config": {{"type": "object", "default": {{}}, "description": "Plugin config overrides"}},
        }},
        "required": ["state"],
    }},
    description="Execute {display_name} pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the {name} pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from pipeline.plugin import PluginContext  # noqa: PLC0415
    from pipeline.types import create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {{}})
    result = await _instance.execute(ctx)

    # Core 插件返回 dict，Input/Output 返回 PluginResult/OutputResult
    if isinstance(result, dict):
        return result

    data: dict = {{"state_updates": result.state_updates}}
    route_sig = getattr(result, "route_signal", None)
    if route_sig:
        data["route_signal"] = {{
            "route_type": route_sig.route_type,
            "target": route_sig.target,
            "reason": route_sig.reason,
        }}
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()
'''

SERVER_TEMPLATE_UTILITY = '''#!/usr/bin/env python3
"""{name} {category} pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/{category}/{name}/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import {ClassName}  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("{name}_pipeline")

_instance: {ClassName} | None = None


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize {name} plugin."""
    global _instance
    _instance = {ClassName}()


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup {name} plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="{name}.check",
    schema={{
        "type": "object",
        "properties": {{
            "chunks": {{
                "type": "array",
                "items": {{"type": "string"}},
                "description": "Content chunks to check for repetition",
            }},
        }},
    }},
    description="Check content chunks for repetition using {display_name}",
)
async def check_repetition(chunks: list[str]) -> dict:
    """Check if content chunks show repetition patterns.

    Args:
        chunks: List of content chunks to analyze.

    Returns:
        Analysis result indicating whether repetition was detected.
    """
    result = _instance.check(chunks)
    if isinstance(result, dict):
        return result
    return {{"detected": bool(result)}}


if __name__ == "__main__":
    plugin.run()
'''


def make_plugin_json(name: str, display_name: str, category: str) -> dict:
    """生成 plugin.json 字典。"""
    tool_name = f"{name}.execute" if category != "core" or name != "stream_repeat_monitor" else f"{name}.check"
    return {
        "id": f"pipeline_{name}",
        "name": display_name,
        "version": "1.0.0",
        "plugin_type": "pipeline",
        "language": "python",
        "host_type": "sidecar",
        "entry": "python3 server.py",
        "capabilities": {
            "tools": [
                {"name": tool_name, "description": f"{display_name} pipeline plugin"}
            ],
            "resources": [],
            "route_signals": [],
            "lifecycle_hooks": ["on_load", "on_unload"],
        },
        "dependencies": [],
        "permissions": {},
        "error_policy": "skip",
        "priority": 50,
    }


def migrate_plugin(name: str, class_name: str, category: str, display_name: str) -> str:
    """迁移单个插件。返回状态字符串。"""
    src_dir = SRC_BASE / category / name
    dst_dir = DST_BASE / category / name

    if not src_dir.exists():
        return f"SKIP (source not found): {name}"

    # 确保目标目录存在
    dst_dir.mkdir(parents=True, exist_ok=True)

    # 1. 复制 plugin.py
    src_plugin = src_dir / "plugin.py"
    dst_plugin = dst_dir / "plugin.py"
    if src_plugin.exists():
        shutil.copy2(src_plugin, dst_plugin)
    else:
        return f"SKIP (no plugin.py): {name}"

    # 2. 生成 server.py
    is_utility = (name == "stream_repeat_monitor")
    template = SERVER_TEMPLATE_UTILITY if is_utility else SERVER_TEMPLATE_PIPELINE
    server_code = template.format(
        name=name,
        ClassName=class_name,
        category=category,
        display_name=display_name,
    )
    (dst_dir / "server.py").write_text(server_code, encoding="utf-8")

    # 3. 生成 plugin.json
    pj = make_plugin_json(name, display_name, category)
    (dst_dir / "plugin.json").write_text(
        json.dumps(pj, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return f"OK: {category}/{name} ({class_name})"


def main() -> None:
    """执行批量迁移。"""
    ok = 0
    skip = 0
    for name, cls, cat, disp in PLUGINS:
        status = migrate_plugin(name, cls, cat, disp)
        print(status)
        if status.startswith("OK"):
            ok += 1
        else:
            skip += 1

    print(f"\n=== Migration complete: {ok} OK, {skip} skipped, {ok + skip} total ===")


if __name__ == "__main__":
    main()
