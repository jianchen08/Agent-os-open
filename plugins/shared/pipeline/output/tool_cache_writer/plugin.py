"""工具缓存写入 Output 插件。

在管道 output 阶段（工具执行完成之后）读取 tool_core 的调用快照
（``_executed_tool_calls``）+ 执行结果（``tool_results``），
将成功的结果写入 tool_cache（input 阶段）共享的模块级单例缓存
（agentos_plugin_sdk.tool_result_cache，两端同源导入）。

接通 tool_cache 的写入断路：tool_cache.put() 原本无调用方，
本插件是它的生产调用方。下一轮 LLM 若再次产出相同工具调用，
tool_cache 命中缓存直接返回，跳过实际执行。

数据源说明：raw_tool_calls 由 llm_core 产出、tool_core 执行后**清空**
（对齐 tool_core Rust 实现 lib.rs:106）；post 链读 raw_tool_calls 恒为空。
tool_core 执行时把执行前的调用列表快照写入 ``_executed_tool_calls``，
本插件以它为写入源（与结果按下标配对）。

依赖关系：
    - 与 tool_cache（input）共享 SDK tool_result_cache 单例缓存字典
    - exclude_tools 中的有副作用工具（bash/file_write 等）不写缓存
    - 失败的工具调用（result 含 error）不写缓存

State 命名空间：
    - 只读 _executed_tool_calls / tool_results，不写 state（纯副作用：写缓存）
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

from agentos_plugin_sdk.tool_result_cache import ToolResultCache, namespace_from_state

logger = logging.getLogger(__name__)

# spill 定位符条目（spill_guard 2026-09-14 起：_full_tool_results 超阈值原文
# 落 spill 文件、state 留定位符）→ 本插件回读存档还原完整 ToolResult 后入缓存。
_SPILLED_MARKER = "__spilled__"
# spill 存储基准（与 Rust spill_guard resolve_base_path 同契约）：
# 环境变量显式锚点优先，缺省相对内核进程 cwd（sidecar 继承）。
_SPILL_BASE_ENV = "AGENTOS_SPILL_BASE"
# 单条缓存体积上限：超过不缓存（重复调用重新执行，spill 链路照常兜底）。
# 缓存是 sidecar 常驻内存，数百 MB 级工具全文入缓存等于把泄漏从 state 挪进缓存。
MAX_CACHE_ENTRY_BYTES = 1_000_000


def _sanitize_key(key: str) -> str:
    """对齐 Rust spill_store::sanitize_key（消毒规则两侧一致才能互读存档）。"""
    sanitized = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in key)
    while ".." in sanitized:
        sanitized = sanitized.replace("..", "_.")
    if not sanitized.strip("._"):
        return f"spill_{len(key)}"
    return sanitized


def _resolve_spilled(entry: dict[str, Any]) -> dict[str, Any] | None:
    """定位符 → 回读 spill 存档（gzip magic 自动识别）还原完整 ToolResult。

    任何失败（路径非法/文件缺失/解压/反序列化）返回 None，调用方跳过该条。
    """
    locator = entry.get("locator") or ""
    pipeline_id, _, key = locator.partition("/")
    if not pipeline_id or not key:
        return None
    base = os.environ.get(_SPILL_BASE_ENV) or os.path.join(os.getcwd(), "data", "spill")
    path = Path(base) / _sanitize_key(pipeline_id) / _sanitize_key(key)
    try:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        resolved = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        # OSError：文件缺失/无权限；ValueError：解压/解码/JSON 反序列化
        return None
    return resolved if isinstance(resolved, dict) else None


class ToolCacheWriter(IOutputPlugin):
    """工具缓存写入 Output 插件。

    读取工具执行结果，把成功的（tool_name, args)→result 写入 tool_cache
    共享的模块级单例缓存。exclude_tools 中的有副作用工具不写。

    配置项：
    - enabled: 是否启用缓存写入（默认 True）
    - default_ttl: 默认缓存过期时间，单位秒（默认 300）
    - max_size: 最大缓存条目数（默认 100）
    - exclude_tools: 不缓存的工具名列表（默认含 bash_execute 等有副作用的工具）

    优先级：25（尽早写缓存）
    缓存写入异常不阻塞管道。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具缓存写入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用缓存写入（默认 True）
                - default_ttl: 默认缓存过期时间，单位秒（默认 300）
                - max_size: 最大缓存条目数（默认 100）
                - exclude_tools: 不缓存的工具名列表
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_cache_writer"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 25)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行缓存写入。

        读取 _executed_tool_calls + tool_results，把成功的工具调用结果写入
        tool_cache 共享的模块级单例缓存。

        Args:
            ctx: 插件执行上下文

        Returns:
            空结果（纯副作用：写缓存，不修改 state）
        """
        if not self._enabled:
            return OutputResult()

        tool_results = ctx.state.get("_full_tool_results") or ctx.state.get(
            StateKeys.TOOL_RESULTS, []
        )
        pipeline_id = ctx.state.get("pipeline_id", "")
        # 全文优先：tool_core 留档 _full_tool_results 是缓存真值源——
        # state 的 TOOL_RESULTS 已是截断展示版（2026-09-09 用户裁定），
        # 缓存必须存全文，LLM 重发同一调用才能取回完整结果。
        executed_calls = ctx.state.get("_executed_tool_calls", [])

        if not tool_results or not executed_calls:
            return OutputResult()

        # 用本插件配置构造缓存核（SDK 单源），写端 put / 排除判定同 input 端
        cache = ToolResultCache(self._config)
        # 身份隔离维度与 input 查询端同源（namespace_from_state 读同一组
        # pipeline state 身份键），写入条目按 pipeline/user/session 隔离。
        namespace = namespace_from_state(ctx.state)

        written = 0
        skipped_exclude = 0
        skipped_error = 0
        skipped_spill_miss = 0
        skipped_oversize = 0
        call_count = len(executed_calls)
        result_count = len(tool_results)

        for i in range(min(call_count, result_count)):
            tool_call = executed_calls[i]
            result = tool_results[i]

            # spill 定位符 → 回读存档还原全文（回读失败跳过该条：缓存宁可缺
            # 不可存截断版——缓存的价值就在全文重发）
            if isinstance(result, dict) and result.get(_SPILLED_MARKER):
                resolved = _resolve_spilled(result)
                if resolved is None:
                    skipped_spill_miss += 1
                    continue
                result = resolved

            # 失败的工具调用不缓存——按 error 值判定（完整 ToolResult 成功时
            # 也带 "error": null 键，键存在≠失败）
            if isinstance(result, dict) and result.get("error"):
                skipped_error += 1
                continue

            tool_name = tool_call.get("name", "")
            if cache.is_excluded(tool_name):
                skipped_exclude += 1
                continue

            # 超大结果不缓存：全文已由 spill 链路存档，重复调用会重新执行并
            # 再次兜底；常驻缓存不承载 MB 级条目。
            if len(json.dumps(result, ensure_ascii=False)) > MAX_CACHE_ENTRY_BYTES:
                skipped_oversize += 1
                continue

            cache.put(tool_call, result, pipeline_id=pipeline_id, namespace=namespace)
            written += 1

        if written or skipped_exclude or skipped_error or skipped_spill_miss or skipped_oversize:
            logger.debug(
                "[%s] cache write | written=%d excluded=%d error=%d spill_miss=%d oversize=%d",
                self.name,
                written,
                skipped_exclude,
                skipped_error,
                skipped_spill_miss,
                skipped_oversize,
            )

        return OutputResult()
